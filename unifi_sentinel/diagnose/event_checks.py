"""Checks over the event log: IP conflicts, repeated disconnects, roaming, unreachable devices."""

from datetime import datetime
from typing import Any, Dict, List, Tuple

from ..events import describe_duration, first_name, local_time, subjects
from ..reservations import reservation_records
from ..settings import DiagnoseSettings
from ..snapshot import Snapshot
from ..util import plural
from .model import INFO, WARNING, Finding


def _conflict_devices(events: List[Dict[str, Any]]) -> Tuple[List[Dict[str, str]], str]:
    """The devices an IP-conflict event names, merged across events and de-duplicated by MAC
    (or by name when an entry has no MAC), plus the network name the events give.

    The devices are in ``parameters["CLIENTS"]["clients"]``, a list inside the object, not
    a single ``CLIENT`` like connect and disconnect events. Anything malformed is skipped.
    """
    seen: Dict[str, Dict[str, str]] = {}
    network = ""
    for e in events:
        params = e.get("parameters") or {}
        net = params.get("NETWORK")
        if not network and isinstance(net, dict) and net.get("name"):
            network = str(net["name"])
        block = params.get("CLIENTS")
        listed = block.get("clients") if isinstance(block, dict) else None
        for c in listed if isinstance(listed, list) else []:
            if not isinstance(c, dict):
                continue
            mac = str(c.get("mac") or "").upper()
            name = str(c.get("name") or c.get("hostname") or mac or "")
            if mac or name:
                seen.setdefault(mac or name.lower(), {"mac": mac, "name": name or mac})
    devices = sorted(seen.values(), key=lambda d: (d["name"].lower(), d["mac"]))
    names = [d["name"].lower() for d in devices]
    for d in devices:                                    # two devices with one name: tell them apart
        if names.count(d["name"].lower()) > 1 and d["mac"]:
            d["name"] = f"{d['name']} ({d['mac'][-5:]})"
    return devices, network


def _join(names: List[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _conflict_findings(conflicts: Dict[str, List[Dict[str, Any]]], snap: Snapshot, window: str) -> List[Finding]:
    reserved = {(u.get("mac") or "").upper(): u["fixed_ip"] for u, _net in reservation_records(snap)}
    findings = []
    for ip, events in sorted(conflicts.items()):
        stamps = [e.get("timestamp") or 0 for e in events]
        devices, network = _conflict_devices(events)
        days = {datetime.fromtimestamp(t / 1000).date() for t in stamps if t}
        # across a long window, how many separate days it happened on says "recurring"
        spread = (f" on {len(days)} different days"
                  if len(events) > 1 and len(days) > 1 and snap.event_window_seconds >= 2 * 86400 else "")
        text = f"IP conflict reported {plural(len(events), 'time')}{spread} in the last {window}"
        if devices:
            text += f" between {_join([d['name'] for d in devices])}"
        if network:
            text += f" on {network}"
        text += f" (most recent {local_time(max(stamps))})"
        hints = []
        for d in devices:
            held = reserved.get(d["mac"])
            if held == ip:
                hints.append(f"{d['name']} holds the reservation for {ip}")
            elif held:
                hints.append(f"{d['name']} is reserved {held}")
        findings.append(Finding(WARNING, ip, text + "".join(f"; {h}" for h in hints),
                                code="event.ip_conflict"))
    return findings


def _event_findings(snap: Snapshot, settings: DiagnoseSettings) -> List[Finding]:
    """Findings from the recent event log: things that happened and may have gone away.

    Only the types listed here are read, so other events (admin access, settings changes,
    ordinary connects) never matter. Roaming is normal for phones (one roams about 30 times
    a day), so frequent roaming is only informational; repeated disconnects are the warning.
    """
    if not snap.events:
        return []
    window = describe_duration(snap.event_window_seconds or 86400)
    threshold = settings.event_flap_count
    findings: List[Finding] = []

    conflicts: Dict[str, List[Dict[str, Any]]] = {}
    disconnects: Dict[str, List[Any]] = {}       # client id -> [name, count]
    roams: Dict[str, List[Any]] = {}
    unreachable: Dict[str, List[Any]] = {}       # stable device identity -> [device, event name, count]
    latency = 0
    for e in snap.events:
        kind = str(e.get("event") or e.get("key") or "")
        if kind == "CLIENT_IP_CONFLICT":
            ip = str((((e.get("parameters") or {}).get("IP") or {}).get("name")) or "unknown IP")
            conflicts.setdefault(ip, []).append(e)
        elif kind.startswith("CLIENT_DISCONNECTED") or kind == "CLIENT_ROAMED":
            who = (subjects(e, "CLIENT") or [{}])[0]
            ident = str(who.get("id") or who.get("name") or "")
            if ident:
                table = roams if kind == "CLIENT_ROAMED" else disconnects
                table.setdefault(ident, [first_name(e, "CLIENT") or ident, 0])[1] += 1
        elif kind == "DEVICE_UNREACHABLE":
            who = (subjects(e, "DEVICE") or [{}])[0]
            device_id = str(who.get("id") or "")
            device_ip = str(who.get("ip") or "")
            device = next((d for d in snap.devices if device_id and d.get("id") == device_id), None)
            if device is None:
                device = next((d for d in snap.devices if device_ip and d.get("ipAddress") == device_ip), None)
            if device and device.get("id"):
                identity = f"id:{device['id']}"
            elif device_ip:
                identity = f"ip:{device_ip}"
            elif device_id:
                identity = f"id:{device_id}"
            else:
                continue
            unreachable.setdefault(identity, [device, str(who.get("name") or ""), 0])[2] += 1
        elif kind == "ISP_HIGH_LATENCY":
            latency += 1

    findings.extend(_conflict_findings(conflicts, snap, window))
    for name, n in sorted(disconnects.values()):
        if n >= threshold:
            findings.append(Finding(WARNING, name, f"disconnected {plural(n, 'time')} in the last {window}",
                                    code="event.client_disconnects"))
    for name, n in sorted(roams.values()):
        if n >= threshold:
            findings.append(Finding(
                INFO, name, f"roamed {plural(n, 'time')} in the last {window} (normal for a mobile device)",
                code="event.client_roams"))

    for _identity, (device, event_name, n) in sorted(unreachable.items()):
        if device and device.get("state") != "ONLINE":
            continue                              # the offline finding already reports it
        name = (device or {}).get("name") or event_name or "?"
        target_mac = (device or {}).get("macAddress")
        target_mac = target_mac.upper() if target_mac else None
        if n >= threshold:
            findings.append(Finding(WARNING, name, f"was unreachable {plural(n, 'time')} in the last {window}",
                                    target_mac, code="event.device_unreachable"))
        elif device and device.get("state") == "ONLINE":
            findings.append(Finding(
                INFO, name, f"was unreachable {plural(n, 'time')} in the last {window}; online now",
                target_mac, code="event.device_unreachable"))
        else:
            findings.append(Finding(INFO, name, f"was unreachable {plural(n, 'time')} in the last {window}", target_mac,
                                    code="event.device_unreachable"))
    if latency:
        findings.append(Finding(
            INFO, "internet", f"high latency was reported {plural(latency, 'time')} in the last {window}",
            code="event.internet_latency"))
    if snap.events_truncated:
        findings.append(Finding(
            INFO, "controller", "the event log read was cut off at its cap; event counts may be low",
            code="event.log_truncated"))
    return findings
