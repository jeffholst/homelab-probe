"""Event history from the controller's system log: `unifi-sentinel events`.

The log is collected into a ``Snapshot``; this module only filters and renders it.
"""

import json
import re
from collections import Counter
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .query import format_table
from .snapshot import Snapshot
from .util import hex_digits, printable

DEFAULT_SINCE = "24h"
DEFAULT_LIMIT = 100
SEVERITIES = ["low", "medium", "high"]
EVENT_COLUMNS = ["Time", "Severity", "Category", "Event", "Message"]

_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
_PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_]+)\}")


def describe_duration(seconds: int) -> str:
    """A window as short text: 86400 -> '24h', 7 days -> '7d', 90 minutes -> '90m'."""
    if seconds >= 2 * 86400 and seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{max(1, seconds // 60)}m"


def parse_duration(text: str) -> int:
    """Seconds in a duration like ``90m``, ``24h``, ``7d`` or ``2w``."""
    match = re.fullmatch(r"\s*(\d+)\s*([mhdw])\s*", str(text).lower())
    if not match or int(match.group(1)) == 0:
        raise ValueError(f"invalid duration {text!r}: use a number and a unit, e.g. 90m, 24h, 7d, 2w")
    return int(match.group(1)) * _UNITS[match.group(2)]


# -- reading ---------------------------------------------------------------

def render_message(event: Dict[str, Any]) -> str:
    """The event's message with ``{PLACEHOLDER}`` filled from its parameters.

    A placeholder the controller gave no value for (some audit events) is shown as
    ``<setting name>`` instead of raw braces.
    """
    params = event.get("parameters") or {}

    def fill(match: "re.Match[str]") -> str:
        value = params.get(match.group(1))
        name = value.get("name") if isinstance(value, dict) else value
        if name not in (None, "") and not isinstance(name, (dict, list)):
            return str(name)
        return "<" + match.group(1).lower().replace("_", " ") + ">"

    return _PLACEHOLDER.sub(fill, event.get("message_raw") or event.get("title_raw") or "")


def subjects(event: Dict[str, Any], prefix: str) -> List[Dict[str, Any]]:
    """Parameter objects whose name starts with ``prefix`` (CLIENT, DEVICE, DEVICE_FROM...)."""
    params = event.get("parameters") or {}
    return [v for k, v in params.items() if k.startswith(prefix) and isinstance(v, dict)]


def _matches(objects: List[Dict[str, Any]], query: str, fields: Sequence[str]) -> bool:
    needle = query.strip().lower()
    hexq = hex_digits(query)
    for obj in objects:
        for field in fields:
            value = obj.get(field)
            if value in (None, ""):
                continue
            if needle in str(value).lower():
                return True
            if len(hexq) >= 6 and re.fullmatch(r"[0-9a-f]+", hexq) and hexq in hex_digits(value):
                return True
    return False


def make_filter(client: str = "", device: str = "", event: str = "") -> Optional[Callable[[Dict[str, Any]], bool]]:
    """A predicate for the filters the server cannot apply (None when there are none)."""
    if not (client or device or event):
        return None
    wanted = re.sub(r"[\s\-]+", "_", event.strip().lower())

    def predicate(e: Dict[str, Any]) -> bool:
        if client and not _matches(subjects(e, "CLIENT"), client, ("name", "hostname", "ip", "id")):
            return False
        if device and not _matches(subjects(e, "DEVICE"), device, ("name", "ip", "id", "model_name")):
            return False
        if wanted and wanted not in f"{e.get('event', '')}_{e.get('key', '')}".lower():
            return False
        return True

    return predicate


def fetch_events(
    snapshot: Snapshot,
    predicate: Optional[Callable[[Dict[str, Any]], bool]] = None,
    limit: int = DEFAULT_LIMIT,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Filter newest-first events already collected in a Snapshot."""
    found: List[Dict[str, Any]] = []
    for event in snapshot.events:
        if predicate is None or predicate(event):
            if limit and len(found) >= limit:
                return found, True
            found.append(event)
    return found, snapshot.events_truncated


# -- presenting ------------------------------------------------------------

def first_name(event: Dict[str, Any], prefix: str) -> str:
    objs = subjects(event, prefix)
    return str(objs[0].get("name") or "") if objs else ""


def local_time(timestamp_ms: Any) -> str:
    try:
        return datetime.fromtimestamp(timestamp_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def event_row(event: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "Time": local_time(event.get("timestamp")),
        "Severity": str(event.get("severity") or "").title(),
        "Category": event.get("category") or "",
        "Event": event.get("event") or event.get("key") or "",
        "Message": render_message(event),
    }


def event_json(event: Dict[str, Any]) -> Dict[str, Any]:
    def ident(prefix: str) -> Optional[Dict[str, Any]]:
        objs = subjects(event, prefix)
        return {k: objs[0].get(k) for k in ("name", "ip", "id") if objs[0].get(k)} if objs else None

    return {**event_row(event), "timestamp": event.get("timestamp"),
            "subcategory": event.get("subcategory") or "", "key": event.get("key") or "",
            "client": ident("CLIENT"), "device": ident("DEVICE")}


def summarize(events: List[Dict[str, Any]], top: int = 10) -> Dict[str, Any]:
    """Counts by event type and severity, and the noisiest (event, client/device) pairs,
    which is where a flapping device or client shows up."""
    pairs = Counter()
    for e in events:
        subject = first_name(e, "CLIENT") or first_name(e, "DEVICE")
        pairs[(e.get("event") or e.get("key") or "", subject)] += 1
    return {
        "total": len(events),
        "by_event": Counter(e.get("event") or e.get("key") or "" for e in events).most_common(),
        "by_severity": Counter(str(e.get("severity") or "").title() for e in events).most_common(),
        "noisiest": [{"Event": ev, "Subject": subj, "Count": n}
                     for (ev, subj), n in pairs.most_common(top)],
    }


def render_events(events: List[Dict[str, Any]], more: bool, as_json: bool = False,
                  summary: bool = False, cap_truncated: bool = False) -> str:
    if summary:
        s = summarize(events)
        if as_json:
            return json.dumps({
                "total": s["total"], "by_severity": dict(s["by_severity"]),
                "by_event": dict(s["by_event"]), "noisiest": s["noisiest"], "truncated": more}, indent=2)
        if not events:
            return "No events in this window."
        out = [f"{s['total']} event(s)" + (
                   " (more events omitted; the 20,000-event read cap was reached)"
                   if cap_truncated else " (limit reached; use --limit 0 for all)" if more else ""), "",
               "By severity: " + ", ".join(f"{printable(name) or '?'} {n}" for name, n in s["by_severity"]), "",
               "By event:",
               format_table([{"Event": ev or "?", "Count": n} for ev, n in s["by_event"]],
                            ["Event", "Count"]),
               "", "Noisiest (event and client/device):",
               format_table(s["noisiest"], ["Event", "Subject", "Count"])]
        return "\n".join(out)

    if as_json:
        return json.dumps([event_json(e) for e in events], indent=2)
    if not events:
        return "No events match."
    rows = [event_row(e) for e in events]
    note = ("\n(more events omitted; the 20,000-event read cap was reached)"
            if cap_truncated else
            "\n(showing the newest events only; use --limit 0 for all)" if more else "")
    return format_table(rows, EVENT_COLUMNS) + f"\n\n{len(rows)} event(s)" + note
