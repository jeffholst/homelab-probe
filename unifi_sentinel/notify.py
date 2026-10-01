"""Notifications: tell the owner when `diagnose` finds something new, worse or fixed.

This is the one place the tool sends data *off the machine* (never to the controller), so it is
deliberately small and opt-in:

* nothing is sent unless ``diagnose --notify`` is given and a destination is configured;
* the destination URLs and tokens are secrets, read from the environment, and never appear in
  any message, error or log line (errors say only which destination and a fixed reason);
* only the finding identity, severity and text are sent, never the API key, the controller
  address or a site id, and ``--notify-redact`` replaces names, addresses and MACs with the
  generic description of the check;
* a message goes out only when something changed since the last notified run, remembered in an
  owner-only state file, so a persistent problem is not re-sent every run.

The planning (`plan`) and the wording (`render_text`, `render_payload`) are pure functions; the
single network call is in `send`.
"""

import json
import os
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from .config import Config
from .diagnose import CODES, CRITICAL, INFO, SEVERITY_ORDER, WARNING, Finding
from .util import printable

STATE_VERSION = 1
DEFAULT_STATE_FILE = "snapshots/notify-state.json"     # the git-ignored directory that holds saved inventories
MAX_LINES = 20                                          # events spelled out in one message
KIND_ORDER = {"new": 0, "worsened": 1, "reminder": 2, "recovered": 3}
KIND_WORD = {"new": "NEW", "worsened": "WORSE", "reminder": "STILL", "recovered": "RECOVERED"}


@dataclass(frozen=True)
class Destination:
    kind: str                                  # "ntfy" or "webhook"
    url: str = field(repr=False)               # a secret: a topic name or a token is part of it
    token: str = field(default="", repr=False)


@dataclass(frozen=True)
class Event:
    kind: str                                  # new, worsened, reminder or recovered
    severity: str
    code: str
    subject: str
    message: str = ""


def destinations_from_config(config: Config) -> List[Destination]:
    found = []
    if config.notify_ntfy_url:
        found.append(Destination("ntfy", config.notify_ntfy_url, config.notify_ntfy_token))
    if config.notify_webhook_url:
        found.append(Destination("webhook", config.notify_webhook_url, config.notify_webhook_token))
    return found


# -- planning ------------------------------------------------------------------------------

def identity(code: str, subject: str) -> str:
    """The stable key of a finding: its check and its subject, never its wording (counts change)."""
    return f"{code or 'unknown'}|{subject}"


def split_identity(key: str) -> Tuple[str, str]:
    code, _, subject = key.partition("|")
    return code, subject


def empty_state() -> Dict[str, Any]:
    return {"version": STATE_VERSION, "active": {}}


def plan(findings: List[Finding], state: Dict[str, Any], now: float, min_severity: str = WARNING,
         repeat_hours: float = 24) -> Tuple[List[Event], Dict[str, Any]]:
    """What to tell the owner, and the state to save once it was delivered.

    A finding at or above ``min_severity`` that is not in the state is **new**; one that got worse
    than when it was last notified is **worsened**; a critical one still unresolved
    ``repeat_hours`` after it was last notified is a **reminder** (0 turns reminders off); and a
    notified one that is gone, or fell below ``min_severity``, is **recovered**. Everything else
    is quiet, so an unchanged situation sends nothing.
    """
    floor = SEVERITY_ORDER[min_severity]
    current: Dict[str, Finding] = {}
    for f in findings:
        if f.severity not in SEVERITY_ORDER or SEVERITY_ORDER[f.severity] > floor:
            continue
        key = identity(f.code, f.subject)
        if key not in current or SEVERITY_ORDER[f.severity] < SEVERITY_ORDER[current[key].severity]:
            current[key] = f                                   # the worst one speaks for the identity

    active = {}
    for key, entry in (state.get("active") or {}).items():
        if not isinstance(entry, dict) or entry.get("severity") not in SEVERITY_ORDER:
            continue
        try:
            normalized = {**entry, "last_notified": float(entry.get("last_notified") or 0)}
        except (TypeError, ValueError, OverflowError):
            continue
        active[key] = normalized
    events: List[Event] = []
    new_active: Dict[str, Any] = {}
    for key, f in current.items():
        previous = active.get(key)
        notified = {"severity": f.severity, "first_notified": now, "last_notified": now}
        if previous is None:
            events.append(Event("new", f.severity, f.code, f.subject, f.message))
            new_active[key] = notified
            continue
        last = float(previous.get("last_notified") or 0)
        entry = {"severity": f.severity, "first_notified": previous.get("first_notified", now), "last_notified": last}
        if SEVERITY_ORDER[f.severity] < SEVERITY_ORDER[previous["severity"]]:
            events.append(Event("worsened", f.severity, f.code, f.subject, f.message))
            entry["last_notified"] = now
        elif f.severity == CRITICAL and repeat_hours > 0 and now - last >= repeat_hours * 3600:
            events.append(Event("reminder", f.severity, f.code, f.subject, f.message))
            entry["last_notified"] = now
        new_active[key] = entry
    for key, previous in active.items():
        if key not in current:
            code, subject = split_identity(key)
            events.append(Event("recovered", previous["severity"], code, subject))
    events.sort(key=lambda e: (KIND_ORDER[e.kind], SEVERITY_ORDER[e.severity], e.subject, e.code))
    return events, {"version": STATE_VERSION, "active": new_active}


def baseline(findings: List[Finding], now: float, min_severity: str = WARNING) -> Dict[str, Any]:
    """The state in which every current finding counts as already reported (nothing is sent)."""
    return plan(findings, empty_state(), now, min_severity)[1]


# -- wording -----------------------------------------------------------------------------------

def _describe(e: Event) -> str:
    return CODES.get(e.code, e.code or "a problem")


def render_text(events: List[Event], redact: bool = False) -> Tuple[str, str]:
    """(title, body) for a plain-text channel. The title is ASCII (it travels as an HTTP header);
    the body has every name cleaned with ``printable``. With ``redact`` the body holds only the
    generic description of each check and a count, never a name, an address or a MAC."""
    problems = sum(e.kind != "recovered" for e in events)
    recovered = len(events) - problems
    bits = ([f"{problems} problem(s)"] if problems else []) + ([f"{recovered} recovered"] if recovered else [])
    title = "unifi-sentinel: " + ", ".join(bits)

    lines: List[str] = []
    if redact:
        counts = Counter((e.kind, e.severity, e.code) for e in events)
        for (kind, severity, code), n in sorted(counts.items(), key=lambda i: (
                KIND_ORDER[i[0][0]], SEVERITY_ORDER[i[0][1]], i[0][2])):
            what = CODES.get(code, code or "a problem")
            lines.append(f"[{severity.upper()}] {KIND_WORD[kind]}  {what}" + (f" (x{n})" if n > 1 else ""))
    else:
        for e in events:
            subject = printable(e.subject)
            if e.kind == "recovered":
                lines.append(f"[OK] {KIND_WORD[e.kind]}  {subject} ({e.code})")
            else:
                tail = " (reminder: still unresolved)" if e.kind == "reminder" else ""
                lines.append(f"[{e.severity.upper()}] {KIND_WORD[e.kind]}  {subject}: {printable(e.message)}{tail}")
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES] + [f"... and {len(lines) - MAX_LINES} more"]
    return title, "\n".join(lines)


def render_payload(events: List[Event], redact: bool = False) -> Dict[str, Any]:
    """The JSON a generic webhook receives. ``text`` is the same message as the plain-text channels."""
    title, body = render_text(events, redact)
    items = []
    for e in events:
        item: Dict[str, Any] = {"event": e.kind, "severity": e.severity, "code": e.code,
                                "description": _describe(e)}
        if not redact:
            item.update(subject=e.subject, message=e.message)
        items.append(item)
    return {"source": "unifi-sentinel", "version": 1, "redacted": redact, "title": title,
            "text": f"{title}\n{body}", "events": items}


def _priority(events: List[Event]) -> Tuple[str, str]:
    """(ntfy priority, ntfy tag) for the worst thing in the message."""
    worst = min((SEVERITY_ORDER[e.severity] for e in events if e.kind != "recovered"), default=None)
    if worst == SEVERITY_ORDER[CRITICAL]:
        return "5", "rotating_light"
    if worst == SEVERITY_ORDER[WARNING]:
        return "4", "warning"
    if worst == SEVERITY_ORDER[INFO]:
        return "3", "information_source"
    return "3", "white_check_mark"                      # only recoveries


# -- sending -----------------------------------------------------------------------------------

def send(destinations: List[Destination], events: List[Event], redact: bool, timeout: float,
         post: Optional[Callable[..., Any]] = None,
         trace: Optional[Callable[[str], None]] = None) -> List[Tuple[str, bool, str]]:
    """Send the message to every destination: ``[(kind, delivered, reason)]``.

    ``reason`` is a fixed phrase (``HTTP 403``, ``timed out``, ...), never the library's text,
    because that text contains the URL, which is a secret. Redirects are not followed (a
    redirect could carry the token to another host) and TLS is always verified.
    """
    post = post or requests.post
    title, body = render_text(events, redact)
    priority, tag = _priority(events)
    results = []
    for dest in destinations:
        headers: Dict[str, str] = {}
        if dest.token:
            headers["Authorization"] = f"Bearer {dest.token}"
        if dest.kind == "ntfy":
            headers.update({"Title": title.encode("ascii", "replace").decode("ascii"),
                            "Priority": priority, "Tags": tag})
            kwargs: Dict[str, Any] = {"data": body.encode("utf-8")}
        else:
            kwargs = {"json": render_payload(events, redact)}
        started = time.perf_counter()
        try:
            response = post(dest.url, headers=headers, timeout=timeout, allow_redirects=False, verify=True, **kwargs)
        except requests.exceptions.SSLError:
            outcome = (False, "TLS certificate verification failed")
        except requests.exceptions.Timeout:
            outcome = (False, "timed out")
        except requests.exceptions.ConnectionError:
            outcome = (False, "connection error")
        except requests.exceptions.RequestException:
            outcome = (False, "request error")
        else:
            status = getattr(response, "status_code", 0)
            outcome = (200 <= status < 300, f"HTTP {status}")
        results.append((dest.kind, *outcome))
        if trace is not None:
            trace(f"notify {dest.kind} -> {outcome[1]} ({(time.perf_counter() - started) * 1000:.0f} ms)")
    return results


# -- the state file ------------------------------------------------------------------------------

def load_state(path: Path) -> Tuple[Dict[str, Any], str]:
    """(state, warning). A missing file is an empty state; an unreadable or damaged one is too, with a
    warning (the next save replaces it), so a broken file never stops the checks."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return empty_state(), ""
    except OSError as e:
        return empty_state(), f"cannot read the notification state {path}: {e.strerror or e}; starting empty"
    try:
        state = json.loads(text)
    except ValueError:
        return empty_state(), f"{path} is not valid JSON; starting with an empty notification state"
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION or not isinstance(
            state.get("active", {}), dict):
        return empty_state(), f"{path} is not a notification state this version understands; starting empty"
    return state, ""


def save_state(path: Path, state: Dict[str, Any]) -> None:
    """Write the state owner-only, replacing the old file in one step."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        os.fchmod(f.fileno(), 0o600)
        json.dump(state, f, indent=2)
        f.write("\n")
    os.replace(temporary, path)
