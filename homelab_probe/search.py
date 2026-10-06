"""Global search over what one read of the controller shows: clients, devices, Wi-Fi networks, networks, findings and
the subjects of notes. Pure functions over a ``Snapshot`` (and the findings of that same snapshot): nothing here reads.

A query matches **text** (a case-insensitive substring; never a pattern, so ``.*`` is just two characters), a **MAC
address in any spelling** (``aa:bb:cc:dd:ee:ff``, ``AA-BB-CC-DD-EE-FF``, ``aabb.ccdd.eeff``, ``AABBCCDDEEFF`` or a
fragment of at least four hex digits, through ``util.hex_digits`` and ``util.normalize_mac``) and an **IP address in any
spelling** (a whole address is compared in its canonical form, so ``FE80:0:0:0:0:0:0:1`` finds ``fe80::1``; a fragment
matches as text). A hit says which fields matched (``matched``) and carries the controller's strings **verbatim**:
names are untrusted, so whoever shows them renders them as text.

Each kind is listed best match first (an exact value, then one that starts with the query, then one that contains it),
then by label and key, so the same snapshot always gives the same answer; ``limit`` bounds every kind separately and
``kinds`` says how many matched in all. A source that could not be read is named in ``unavailable`` and the answer says
it may be incomplete, never that there is nothing.
"""

import dataclasses
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .client_view import known_clients
from .diagnose import SEVERITY_ORDER, Finding, needs_for
from .diagnose.addresses import normalize_ip
from .events import DEFAULT_SINCE, parse_duration
from .query import network_rows, wlan_rows
from .snapshot import Needs, Snapshot
from .triage import finding_id, iso
from .util import hex_digits, normalize_mac

KINDS = ("client", "device", "ssid", "network", "finding", "note")        # the order of the kinds in a response
MIN_QUERY = 2
MAX_QUERY = 64
DEFAULT_LIMIT = 10
MAX_LIMIT = 50
MIN_MAC_DIGITS = 4                      # a MAC fragment shorter than this is matched as text only

# Everything the findings need (what ``diagnose`` reads), plus the history of clients (offline ones are searched), the
# Wi-Fi networks and the networks. It is the read of ``GET .../findings`` and of the client, network and Wi-Fi lists,
# so the cache answers it.
SEARCH_NEEDS: Needs = dataclasses.replace(needs_for(None, parse_duration(DEFAULT_SINCE)),
                                          offline=True, wlans=True, networks=True)

_MAC_FRAGMENT = re.compile(r"(?:[0-9a-f]+|[0-9a-f]{1,2}(?:[:-][0-9a-f]{1,2})+|[0-9a-f]{1,4}(?:\.[0-9a-f]{4}){1,2})",
                           re.IGNORECASE)
_NOT_COMPLETE = ("Some data could not be read (see the warnings), so something that exists may not be found.")
_UNAVAILABLE = {
    "ssid": "The Wi-Fi networks could not be read, so none were searched.",
    "network": "The networks could not be read, so none were searched.",
    "note": "The notes could not be read, so none were searched.",
}

Hit = Dict[str, Any]


@dataclasses.dataclass(frozen=True)
class Query:
    """A search text as it is matched: ``folded`` for text, ``digits`` for a MAC (empty unless the text looks like part
    of one and has at least ``MIN_MAC_DIGITS`` hex digits), ``ip`` for a whole IP address (canonical form)."""

    text: str
    folded: str
    digits: str
    ip: str


def parse_query(text: str) -> Query:
    """The ``Query`` of ``text``. ``ValueError`` with a fixed sentence when it has fewer than ``MIN_QUERY`` or more than
    ``MAX_QUERY`` characters once trimmed."""
    trimmed = text.strip()
    if not MIN_QUERY <= len(trimmed) <= MAX_QUERY:
        raise ValueError(f"The search text has {MIN_QUERY} to {MAX_QUERY} characters.")
    digits = hex_digits(trimmed) if _MAC_FRAGMENT.fullmatch(trimmed) else ""
    return Query(trimmed, trimmed.casefold(), digits if len(digits) >= MIN_MAC_DIGITS else "", normalize_ip(trimmed))


# -- matching: each returns 0 (the whole value), 1 (starts with it), 2 (contains it) or None --------------------------

def _text(query: Query, value: Any) -> Optional[int]:
    if not isinstance(value, str) or not value:
        return None
    folded = value.casefold()
    if folded == query.folded:
        return 0
    if folded.startswith(query.folded):
        return 1
    return 2 if query.folded in folded else None


def _mac(query: Query, value: Any) -> Optional[int]:
    mac = normalize_mac(value)
    if not mac:
        return None
    shown = _text(query, mac)
    if shown is not None or not query.digits:
        return shown
    have = hex_digits(mac)
    if have == query.digits:
        return 0
    if have.startswith(query.digits):
        return 1
    return 2 if query.digits in have else None


def _ip(query: Query, value: Any) -> Optional[int]:
    if isinstance(value, str) and query.ip and normalize_ip(value) == query.ip:
        return 0
    return _text(query, value.strip() if isinstance(value, str) else value)


def _best(*ranks: Optional[int]) -> Optional[int]:
    found = [r for r in ranks if r is not None]
    return min(found) if found else None


def _hit(kind: str, key: str, label: str, matches: Sequence[Tuple[str, Optional[int]]], **extra: Any) -> Optional[Hit]:
    """A hit, or None when no field matched. ``matched`` lists the fields that did, in the order given; ``_rank`` is
    private to the sort and is removed before the answer leaves."""
    matched = [(name, rank) for name, rank in matches if rank is not None]
    if not matched:
        return None
    return {"kind": kind, "key": key, "label": label, "matched": [name for name, _ in matched], **extra,
            "_rank": min(rank for _, rank in matched)}


def _order(hits: List[Optional[Hit]], secondary: Callable[[Hit], int] = lambda hit: 0) -> List[Hit]:
    """The hits that exist, best match first, then by ``secondary`` (severity for a finding), label and key. The sort is
    stable, so records that tie on all of it stay in the order the controller gave them."""
    return sorted((h for h in hits if h is not None),
                  key=lambda h: (h["_rank"], secondary(h), h["label"].casefold(), h["label"], h["key"]))


# -- the kinds -------------------------------------------------------------------------------------------------------

def _clients(snap: Snapshot, query: Query) -> List[Hit]:
    hits = []
    for record in known_clients(snap):
        mac = record["mac"]
        names = [n for n in record["names"] if isinstance(n, str)]
        hits.append(_hit("client", mac, names[0] if names else mac, [
            ("name", _best(*(_text(query, n) for n in names))),
            ("mac", _mac(query, mac)),
            ("ip", _best(*(_ip(query, ip) for ip in sorted(i for i in record["ips"] if isinstance(i, str)))))],
            mac=mac, ip=str(record["ip"]), status="online" if record["online"] else "offline"))
    return _order(hits)


def _devices(snap: Snapshot, query: Query) -> List[Hit]:
    legacy = {normalize_mac(d.get("mac")): d.get("name") for d in snap.legacy_devices}
    hits, seen = [], set()
    for device in snap.devices:
        mac = normalize_mac(device.get("macAddress"))
        if not mac or mac in seen:
            continue
        seen.add(mac)
        names = [n for n in (device.get("name"), legacy.get(mac)) if isinstance(n, str) and n]
        hits.append(_hit("device", mac, names[0] if names else mac, [
            ("name", _best(*(_text(query, n) for n in names))),
            ("mac", _mac(query, mac)),
            ("ip", _ip(query, device.get("ipAddress"))),
            ("model", _text(query, device.get("model")))],
            mac=mac, ip=str(device.get("ipAddress") or ""), model=str(device.get("model") or ""),
            status="online" if device.get("state") == "ONLINE" else "offline"))
    return _order(hits)


def _ssids(snap: Snapshot, query: Query) -> List[Hit]:
    hits = []
    for wlan, row in zip(snap.wlans or [], wlan_rows(snap), strict=True):
        name = str(row["Name"])
        hits.append(_hit("ssid", str(wlan.get("_id") or name), name, [("name", _text(query, name))],
                         security=row["Security"], enabled=row["Enabled"] == "Yes", network=str(row["Network"])))
    return _order(hits)


def _networks(snap: Snapshot, query: Query) -> List[Hit]:
    hits = []
    for network, row in zip(snap.networks, network_rows(snap), strict=True):
        name = str(row["Name"])
        hits.append(_hit("network", str(network.get("_id") or name), name, [
            ("name", _text(query, name)), ("subnet", _text(query, row["Subnet"])),
            ("vlan", _text(query, str(row["VLAN"])))],
            purpose=str(row["Purpose"]), vlan=row["VLAN"], subnet=row["Subnet"]))
    return _order(hits)


def _findings(findings: Sequence[Finding], query: Query) -> List[Hit]:
    hits = []
    for finding in findings:
        ident = finding_id(finding.code, finding.subject, finding.target_mac)
        hits.append(_hit("finding", ident, finding.subject, [
            ("code", _text(query, finding.code)), ("subject", _text(query, finding.subject)),
            ("message", _text(query, finding.message)), ("mac", _mac(query, finding.target_mac)),
            ("id", 0 if query.folded == ident else None)],
            severity=finding.severity, code=finding.code, message=finding.message))
    return _order(hits, lambda hit: SEVERITY_ORDER[hit["severity"]])


def _notes(subjects: Sequence[Dict[str, Any]], query: Query) -> List[Hit]:
    hits = []
    for item in subjects:
        subject, last = item["subject"], item["last_known"]
        kind, _, reference = subject.partition(":")
        name = last["name"] if last else ""
        hits.append(_hit("note", subject, name or subject, [
            ("reference", _best(_text(query, subject), _mac(query, reference) if kind != "finding" else None)),
            ("name", _text(query, name))],
            subject_kind=kind, note_count=item["note_count"], last_note_at=iso(item["last_note_at"])))
    return _order(hits)


def build_search(snap: Snapshot, findings: Sequence[Finding], query: Query, limit: int = DEFAULT_LIMIT,
                 subjects: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The hits for ``query`` in ``snap`` and its ``findings`` (the ones ``diagnose`` reports after the ignore list),
    and in ``subjects`` (``NotesStore.subjects()``; ``None`` when the notes could not be read), at most ``limit`` of
    each kind. ``items`` lists the kinds in the order of ``KINDS``; ``kinds`` has, for each, how many matched in all
    (``total``) and how many are in ``items`` (``shown``); ``truncated`` is true when any kind was cut."""
    found: Dict[str, List[Hit]] = {
        "client": _clients(snap, query), "device": _devices(snap, query),
        "ssid": _ssids(snap, query) if snap.wlans is not None else [],
        "network": _networks(snap, query) if snap.networks_available else [],
        "finding": _findings(findings, query), "note": _notes(subjects, query) if subjects is not None else []}
    unavailable = [kind for kind, missing in (("ssid", snap.wlans is None), ("network", not snap.networks_available),
                                              ("note", subjects is None)) if missing]
    kinds = {kind: {"total": len(found[kind]), "shown": min(len(found[kind]), limit)} for kind in KINDS}
    items = [{k: v for k, v in hit.items() if k != "_rank"} for kind in KINDS for hit in found[kind][:limit]]
    degraded = bool(snap.degraded)
    return {"query": query.text, "limit": limit, "items": items, "kinds": kinds,
            "truncated": any(v["total"] > v["shown"] for v in kinds.values()),
            "complete": not degraded and not unavailable, "unavailable": unavailable,
            "limitations": ([_NOT_COMPLETE] if degraded else []) + [_UNAVAILABLE[kind] for kind in unavailable]}
