"""Local triage of findings: what was acknowledged or snoozed, and how findings are ordered and explained.

This module is pure standard library and holds no network detail: **the file keeps an opaque id, the finding code, a
state, who and when, and when the finding was first and last seen** (never a name, a MAC or an address), one file per
site next to that site's snapshots and notification state: ``snapshots/<site id>/triage.json``. Writes are atomic and
owner-only, under a lock (``triage.json.lock``), and a file that names another site is refused.

**Identity.** A finding is its check and what it is about: ``code`` plus the device's MAC when the finding has one (so
a renamed device keeps its state), else the subject text. The id is a hash of that, so it is the same on every read
and in every process, and says nothing about the network. A finding about something with no MAC loses its state when
the thing is renamed: that is a different finding by this definition.

**States.** ``open`` (nobody looked), ``acknowledged`` (an administrator saw it), ``snoozed`` (until a date). Both stay
visible: a state never hides or resolves a finding. **Acknowledged is not resolved**: an entry is dropped only by
``reconcile`` when a *complete* read of every check no longer shows the finding. A failed or partial read changes
nothing, so it can never imply that something cleared. A snooze whose date has passed is an open finding again.

**Priority** is explained, never a bare score: ordered by whether somebody has looked (open first), then severity,
then the scope of the check (the whole network, a device, a link, wireless, a client), then how long the finding has
been known **where there is a record** (it is left out, and said so, when there is none). The same input gives the
same order.
"""

import contextlib
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from .diagnose import CRITICAL, INFO, WARNING
from .diagnose.areas import area_of
from .util import LockTimeout, file_lock, normalize_mac, printable

FILE_NAME = "triage.json"
FORMAT_VERSION = 1
STATES = ("open", "acknowledged", "snoozed")
MAX_ENTRIES = 5000                       # findings tracked per site: a bound on the file, not one to meet
MAX_NOTE = 200
MAX_SNOOZE_DAYS = 365
LOCK_WAIT_SECONDS = 20.0
DAY = 86400.0

SEVERITY_WEIGHT = {CRITICAL: 3, WARNING: 2, INFO: 1}
# What a check is about, by area. A fixed table: where the finding is, not a guess about its consequences.
SCOPE_OF_AREA = {"devices": "device", "health": "network", "wan": "network", "clients": "client",
                 "reservations": "client", "ports": "link", "wifi": "wireless", "events": "events"}
SCOPE_WEIGHT = {"network": 3, "device": 2, "link": 2, "wireless": 1, "client": 1, "events": 1}
SCOPE_TEXT = {"network": "affects the network as a whole", "device": "is about one device",
              "link": "is about one link",
              "wireless": "is about wireless clients near one access point", "client": "is about one client",
              "events": "comes from the event history"}
# General guidance per area (not specific to a code): what to look at next. Labelled as general by the API.
NEXT_CHECKS = {
    "devices": ["Open the device in the controller and check its power, cabling and uplink.",
                "Compare with `hlp topology` to see what is connected through it."],
    "health": ["Open the controller's dashboard and look at the subsystem the finding names.",
               "Run `hlp doctor` to check the connection from this tool."],
    "wan": ["Run `hlp wan` for the monitors, the latency and the last speed tests.",
            "Check the modem or the provider's status page before the gateway."],
    "clients": ["Run `hlp client NAME` for where the client attaches and its link quality."],
    "reservations": ["Run `hlp query reservations` to compare the reservation with the DHCP pool and the client."],
    "ports": ["Run `hlp query ports` for the port's speed, errors and drops; check the cable and the other end."],
    "wifi": ["Run `hlp wifi` for the channels, the neighbouring networks and the clients' signal."],
    "events": ["Run `hlp events --since 7d` for the history around the time the finding appeared."],
}
DOCS = "diagnose.md#json-output-and-finding-codes"


def finding_id(code: str, subject: str, mac: Optional[str] = None) -> str:
    """The stable id of a finding: a short hash of its check and what it is about (the MAC when there is one, else the
    subject text), the same on every read."""
    about = normalize_mac(mac) if mac else ""
    identity = f"{code}|{about or subject.strip().lower()}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def scope_of(code: str) -> str:
    """What the check is about: ``network``, ``device``, ``link``, ``wireless``, ``client`` or ``events``."""
    return SCOPE_OF_AREA.get(area_of(code) or "", "device")


def iso(moment: Optional[float]) -> Optional[str]:
    return None if moment is None else datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TriageError(Exception):
    """A triage change that cannot be made; ``code`` is a fixed word (``invalid``, ``full``, ``unsafe``, ``unreadable``,
    ``locked``, ``another_site``) and the message never holds a name, a path or a secret."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TriageStore:
    """The triage file of one site. ``directory`` is the site's directory (``snapshots/<site id>``)."""

    def __init__(self, directory: Path, site_id: str) -> None:
        self.directory, self.site_id = Path(directory), site_id
        self.path = self.directory / FILE_NAME

    # -- reading and writing the file -------------------------------------------------------------------------

    def _refuse_links(self) -> None:
        if any(path.is_symlink() for path in (self.directory.parent, self.directory, self.path)):
            raise TriageError("unsafe", "The triage file or a directory above it is a symbolic link, which is left "
                                        "alone.")

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        self._refuse_links()
        try:
            with file_lock(self.directory / (FILE_NAME + ".lock"), LOCK_WAIT_SECONDS):
                yield
        except LockTimeout as error:
            raise TriageError("locked", "Another change to the triage was in progress too long.") from error
        except OSError as error:
            raise TriageError("unreadable", "The triage file cannot be used.") from error

    def load(self) -> Dict[str, Dict[str, Any]]:
        """The entries by id (empty when there is no file). ``TriageError`` for a file that is damaged or is another
        site's."""
        self._refuse_links()
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError) as error:
            raise TriageError("unreadable", "The triage file cannot be read.") from error
        try:
            document = json.loads(text)
            entries = document["entries"]
            ok = document["version"] == FORMAT_VERSION and isinstance(entries, dict) and all(
                isinstance(key, str) and isinstance(value, dict) and value.get("state") in STATES
                for key, value in entries.items())
            site = document.get("site")
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise TriageError("unreadable", "The triage file is damaged.") from error
        if not ok:
            raise TriageError("unreadable", "The triage file is damaged.")
        if site not in (None, self.site_id):
            raise TriageError("another_site", "The triage file belongs to another site.")
        loaded: Dict[str, Dict[str, Any]] = entries
        return loaded

    def _save(self, entries: Mapping[str, Mapping[str, Any]]) -> None:
        document = {"version": FORMAT_VERSION, "site": self.site_id, "entries": entries}
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(dir=self.directory, prefix=FILE_NAME + ".", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except OSError as error:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise TriageError("unreadable", "The triage file cannot be written.") from error

    # -- changes ---------------------------------------------------------------------------------------------

    def set_state(self, ident: str, code: str, state: str, by: str, now: float, until: Optional[float] = None,
                  note: str = "") -> Dict[str, Any]:
        """Acknowledge, snooze or reopen the finding ``ident``. A snooze needs an end in the future, at most a year
        away; the note is plain text of at most ``MAX_NOTE`` characters. The first and last seen times are kept."""
        if state not in STATES:
            raise TriageError("invalid", "The state must be open, acknowledged or snoozed.")
        if state == "snoozed" and (until is None or until <= now or until > now + MAX_SNOOZE_DAYS * DAY):
            raise TriageError("invalid", f"A snooze needs an end in the future, at most {MAX_SNOOZE_DAYS} days away.")
        text = printable(note).strip()
        if len(text) > MAX_NOTE:
            raise TriageError("invalid", f"The note is limited to {MAX_NOTE} characters.")
        with self.locked():
            entries = dict(self.load())
            old = entries.get(ident)
            if old is None and len(entries) >= MAX_ENTRIES:
                raise TriageError("full", "Too many findings are tracked; reopen or clear some first.")
            entry = {"code": code, "state": state, "by": by if state != "open" else "", "at": now,
                     "until": until if state == "snoozed" else None, "note": text if state != "open" else "",
                     "first_seen_at": (old or {}).get("first_seen_at", now), "last_seen_at": (old or {}).get(
                         "last_seen_at", now)}
            entries[ident] = entry
            self._save(entries)
            return entry

    def reconcile(self, present: Mapping[str, str], complete: bool, now: float) -> Dict[str, int]:
        """Bring the file in line with what a check just found (``present``: id to code). Findings that are there are
        recorded (first and last seen) and a snooze that has ended becomes open. A finding that is **not** there ends
        its entry **only when the read was complete** (``complete``) of every check: a failed or partial read
        changes nothing about what is absent. Returns the counts of what was added, kept and dropped."""
        with self.locked():
            entries = dict(self.load())
            added = dropped = 0
            for ident, code in present.items():
                entry = entries.get(ident)
                if entry is None:
                    if len(entries) < MAX_ENTRIES:
                        entries[ident] = {"code": code, "state": "open", "by": "", "at": now, "until": None,
                                          "note": "", "first_seen_at": now, "last_seen_at": now}
                        added += 1
                    continue
                entry["last_seen_at"] = now
                if entry["state"] == "snoozed" and (entry.get("until") or 0) <= now:
                    entry.update(state="open", until=None, by="", note="", at=now)
            if complete:
                for ident in [ident for ident in entries if ident not in present]:
                    del entries[ident]
                    dropped += 1
            self._save(entries)
            return {"added": added, "kept": len(entries) - added, "dropped": dropped}


# -- the view -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Ranked:
    """A finding with its place in the order and what put it there."""

    finding: Dict[str, Any]
    id: str
    rank: int
    score: int
    scope: str
    reasons: List[str]
    triage: Dict[str, Any]
    first_seen_at: Optional[float]
    limitations: List[str]


def effective_state(entry: Optional[Mapping[str, Any]], now: float) -> str:
    """The state that applies now: a snooze that has ended is open."""
    if entry is None:
        return "open"
    if entry["state"] == "snoozed" and (entry.get("until") or 0) <= now:
        return "open"
    return str(entry["state"])


def rank_findings(findings: List[Dict[str, Any]], entries: Mapping[str, Mapping[str, Any]], now: float,
                  complete: bool = True) -> List[Ranked]:
    """The findings (``Finding.to_dict`` forms) in the order to look at them, each with the reasons. Open findings come
    before acknowledged ones before snoozed ones; within a state by severity, scope and persistence; then by code and
    subject, so the order is always the same."""
    staged = []
    for finding in findings:
        ident = finding_id(finding["code"], finding["subject"], finding.get("mac") or None)
        entry = entries.get(ident)
        state = effective_state(entry, now)
        severity = finding["severity"]
        scope = scope_of(finding["code"])
        first = (entry or {}).get("first_seen_at")
        age_days = None if first is None else max(0.0, (now - first) / DAY)
        persistence = 0
        if age_days is not None:
            persistence = 3 if age_days >= 7 else 2 if age_days >= 1 else 1 if age_days >= 1 / 24 else 0
        reasons = [f"{severity} severity", f"the check {SCOPE_TEXT[scope]}"]
        limitations: List[str] = []
        if age_days is None:
            limitations.append("How long this has been happening is unknown: it has not been recorded before.")
        else:
            reasons.append("first recorded " + (f"{age_days:.0f} days ago" if age_days >= 1
                                                else "less than a day ago"))
        if state != "open":
            reasons.append(f"{state} by an administrator")
        if not complete:
            limitations.append("This read was partial: some checks did not run, so other findings may be missing.")
        score = SEVERITY_WEIGHT.get(severity, 0) * 100 + SCOPE_WEIGHT[scope] * 10 + persistence
        shown = entry if (entry is not None and state != "open") else {}
        triage = {"state": state, "by": shown.get("by") or None, "at": iso(shown.get("at")),
                  "until": iso(shown.get("until")) if state == "snoozed" else None, "note": shown.get("note") or ""}
        key = (STATES.index(state), -score, finding["code"], finding["subject"].lower(), ident)
        staged.append((key, finding, ident, score, scope, reasons, triage, first, limitations))
    staged.sort(key=lambda item: item[0])
    return [Ranked(finding, ident, number, score, scope, reasons, triage, first, limitations)
            for number, (_, finding, ident, score, scope, reasons, triage, first, limitations) in enumerate(staged, 1)]


def guidance(code: str) -> Tuple[List[str], str]:
    """(general next checks for the area of ``code``, the documentation link): general, not specific to the code."""
    return NEXT_CHECKS.get(area_of(code) or "", []), DOCS
