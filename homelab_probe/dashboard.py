"""The dashboard summary: one small document that says how the network is, and how much of that is known.

It adds nothing the other documents do not know: the findings of ``diagnose`` (counted by severity and, when the
server has a triage file, by triage state), the headline of ``wan`` and ``wifi``, the device and client counts and a few
notable events, all from one ``Snapshot`` that ``diagnose`` reads anyway. What it adds is the **honesty about
completeness**, because a page that shows green when a read failed is worse than no page:

* ``status`` is ``critical`` or ``warning`` when a check found that (whatever its triage state: a finding that is
  acknowledged or snoozed still exists), ``ok`` only when the read was complete, not served from an older cache
  and the controller answered, and ``unknown`` otherwise. It is never ``ok`` on a partial read.
* ``complete`` is False when an optional read failed (the same flag ``diagnose`` has), ``stale`` is True when a
  failed read was answered from an older cached answer, and ``controller.state`` says whether the controller
  answered (``ok``), could not be reached (``unreachable``), failed its certificate check (``certificate``) or
  refused the API key (``key_rejected``).
* Each section has ``available``: a False means "not read", never "none" or "zero". A counter whose source could
  not be read is ``null``.
* When the controller cannot be read at all and no cached answer exists, the document is still built (every section
  unavailable, ``status`` ``unknown``): a dashboard shows that, an error page would not.

Pure standard library and this package; the pieces are the builders the commands use (``diagnose``, ``wan``,
``wifi``, ``triage``).
"""

import datetime
import time
from typing import Any, Dict, Mapping, Optional, Sequence

from .client_view import DeviceIndex
from .diagnose import CRITICAL, INFO, WARNING, apply_ignores, diagnose
from .events import render_message
from .export import build_offline_clients
from .settings import DiagnoseSettings
from .snapshot import Snapshot
from .triage import STATES, rank_findings
from .wan import DEFAULT_DAYS, build_wan
from .wifi import radios

JSON_VERSION = 1             # the format of the dashboard document; it changes only when a field is removed or renamed
ATTENTION = 5                # findings listed (the open ones, most urgent first)
NOTABLE_EVENTS = 5           # events listed (newest first, severity above low)
SEVERITIES = (CRITICAL, WARNING, INFO)
STATUSES = ("ok", "warning", "critical", "unknown")
CONTROLLER_STATES = ("ok", "unreachable", "certificate", "key_rejected")
# The kinds of UniFiAPIError that mean the controller did not give a usable answer to the connection itself; every
# other kind (an HTTP error status, a body that is not JSON) is an answer, so the controller was reachable.
_STATE_OF_KIND = {"connection": "unreachable", "timeout": "unreachable", "tls": "certificate",
                  "unauthorized": "key_rejected", "forbidden": "key_rejected"}


def connection_state(kind: Optional[str]) -> str:
    """What a failed read says about the connection: one of ``CONTROLLER_STATES`` (``ok``: the controller answered,
    the failure is about this request)."""
    return _STATE_OF_KIND.get(kind or "", "ok")


# When failed reads of one build say different things (the reads run in parallel and report in the order they
# finish), the most specific and most actionable one wins, whatever the order: a refused key, then a failed
# certificate, then no answer at all.
_PRECEDENCE = ("key_rejected", "certificate", "unreachable")


def stale_state(kinds: Sequence[str]) -> str:
    """The connection state behind the failures that were answered from an older cache, independent of the order of
    ``kinds``: the highest of ``key_rejected``, ``certificate`` and ``unreachable`` that any kind says, else ``ok``."""
    states = {connection_state(kind) for kind in kinds}
    return next((state for state in _PRECEDENCE if state in states), "ok")


def overall_status(by_severity: Optional[Mapping[str, int]], complete: bool, stale: bool, controller: str) -> str:
    """``critical`` or ``warning`` when a check found it, else ``ok`` only for a complete, fresh read of a controller
    that answered, else ``unknown``. A read that is partial, old or failed is never ``ok``."""
    if by_severity is not None:
        if by_severity.get(CRITICAL):
            return "critical"
        if by_severity.get(WARNING):
            return "warning"
    return "ok" if by_severity is not None and complete and not stale and controller == "ok" else "unknown"


def _unavailable() -> Dict[str, Any]:
    return {"available": False}


def unreachable_dashboard(state: str) -> Dict[str, Any]:
    """The document for a controller that could not be read and has no cached answer: nothing is known, and it says
    so (``state`` is the connection state, ``unreachable``, ``certificate`` or ``key_rejected``)."""
    return {"version": JSON_VERSION, "status": "unknown", "complete": False, "stale": False,
            "controller": {"state": state}, "site": None, "findings": _unavailable(), "devices": _unavailable(),
            "clients": _unavailable(), "wan": _unavailable(), "wifi": _unavailable(), "events": _unavailable()}


def _findings_section(snap: Snapshot, settings: DiagnoseSettings, entries: Optional[Mapping[str, Mapping[str, Any]]],
                      now: float, today: Optional[datetime.date], complete: bool) -> Dict[str, Any]:
    """Counts by severity and by triage state (null when the triage file is not available) and the open findings
    that most need a look, in the order of the findings page."""
    kept, ignored = apply_ignores(diagnose(snap, settings), settings.ignore, today)
    ranked = rank_findings([f.to_dict() for f in kept], entries or {}, now, complete)
    by_state = {state: sum(r.triage["state"] == state for r in ranked) for state in STATES}
    return {
        "available": True, "total": len(kept), "ignored": len(ignored),
        "by_severity": {severity: sum(f.severity == severity for f in kept) for severity in SEVERITIES},
        "by_state": by_state if entries is not None else None,
        "triage_available": entries is not None,
        "attention": [{"id": r.id, "rank": r.rank, "severity": r.finding["severity"], "code": r.finding["code"],
                       "subject": r.finding["subject"], "message": r.finding["message"]}
                      for r in ranked if r.triage["state"] == "open"][:ATTENTION],
    }

def _devices_section(snap: Snapshot) -> Dict[str, Any]:
    states = [d.get("state") for d in snap.devices]
    online, offline = states.count("ONLINE"), states.count("OFFLINE")
    return {"available": True, "total": len(states), "online": online, "offline": offline,
            "other": len(states) - online - offline}          # adopting, updating, pending...: neither up nor down


def _clients_section(snap: Snapshot) -> Dict[str, Any]:
    wired = sum(c.get("type") == "WIRED" for c in snap.clients)
    wireless = sum(c.get("type") == "WIRELESS" for c in snap.clients)
    # "offline" is not known when the client history could not be read, which is not the same as none (an empty
    # history that was read is an answer).
    offline = (len(build_offline_clients(snap.clients, snap.devices, snap.all_users))
               if snap.all_users_available else None)
    return {"available": True, "connected": len(snap.clients), "wired": wired, "wireless": wireless,
            "offline": offline}


def _wan_section(snap: Snapshot, settings: DiagnoseSettings, now_ms: Optional[int]) -> Dict[str, Any]:
    if not any(entry.get("subsystem") == "wan" for entry in snap.health):
        return _unavailable()
    report = build_wan(snap, DEFAULT_DAYS, settings, now_ms)
    availability = [m["availability"] for m in report["monitoring"] if m["availability"] is not None]
    last = report["speedtests"]["last"]
    return {"available": True, "status": report["now"]["status"], "internet_status": report["now"]["internet_status"],
            "latency_ms": report["now"]["latency_ms"], "drops": report["now"]["drops"],
            "availability_pct": min(availability) if availability else None, "nat": report["nat"]["kind"],
            "last_speedtest": None if last is None else {
                "time": last["time"], "download_mbps": last["download_mbps"], "upload_mbps": last["upload_mbps"]}}


def _wifi_section(snap: Snapshot) -> Dict[str, Any]:
    if not snap.legacy_devices_available:
        return _unavailable()
    rows = radios(snap, DeviceIndex(snap))
    access_points = {r["mac"]: r["online"] for r in rows}
    live = [r for r in rows if r["band"] and r["online"]]
    satisfaction = [r["satisfaction"] for r in live if r["satisfaction"] is not None]
    utilization = [r["utilization"] for r in live if r["utilization"] is not None]
    return {"available": True, "access_points": len(access_points),
            "access_points_online": sum(access_points.values()), "radios": len([r for r in rows if r["band"]]),
            "wireless_clients": sum(c.get("type") == "WIRELESS" for c in snap.clients),
            "lowest_satisfaction": min(satisfaction) if satisfaction else None,
            "highest_utilization": max(utilization) if utilization else None}


def _events_section(snap: Snapshot) -> Dict[str, Any]:
    if not snap.events_available:
        return _unavailable()
    notable = [e for e in snap.events if str(e.get("severity") or "").lower() != "low"]
    return {"available": True, "window_seconds": snap.event_window_seconds, "total": len(snap.events),
            "truncated": snap.events_truncated, "notable_total": len(notable),
            "notable": [{"timestamp": e.get("timestamp"), "severity": str(e.get("severity") or "").lower(),
                         "category": e.get("category") or "", "event": e.get("event") or e.get("key") or "",
                         "message": render_message(e)} for e in notable[:NOTABLE_EVENTS]]}


def build_dashboard(snap: Snapshot, settings: Optional[DiagnoseSettings] = None,
                    entries: Optional[Mapping[str, Mapping[str, Any]]] = None, now: Optional[float] = None,
                    today: Optional[datetime.date] = None, stale: Sequence[str] = (),
                    now_ms: Optional[int] = None) -> Dict[str, Any]:
    """The dashboard of a snapshot read with ``documents.DASHBOARD_NEEDS`` (the reads of a full ``diagnose``).

    ``entries`` are the triage entries of the site (None: no triage file could be used, so the counts by state are
    ``null`` rather than zero); ``stale`` are the ``UniFiAPIError.kind`` of the reads that were answered from an older
    cache. ``now`` and ``now_ms`` fix the clocks, ``today`` the date ignore rules expire on, for tests."""
    settings = settings or DiagnoseSettings()
    moment = time.time() if now is None else now
    complete = not snap.degraded
    findings = _findings_section(snap, settings, entries, moment, today, complete)
    controller = stale_state(stale)
    return {
        "version": JSON_VERSION,
        "status": overall_status(findings["by_severity"], complete, bool(stale), controller),
        "complete": complete, "stale": bool(stale), "controller": {"state": controller},
        "site": {"id": str(snap.site.get("id") or ""), "name": str(snap.site.get("name") or "")},
        "findings": findings, "devices": _devices_section(snap), "clients": _clients_section(snap),
        "wan": _wan_section(snap, settings, now_ms), "wifi": _wifi_section(snap), "events": _events_section(snap),
    }
