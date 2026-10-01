"""Read-only health checks over a snapshot."""

from dataclasses import dataclass
import ipaddress
from datetime import datetime
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from .events import describe_duration, first_name, local_time, subjects
from .export import client_location, device_type_label
from .query import format_uptime
from .reservations import reservation_records
from .wan import (SPEEDTEST_BASELINE_DAYS, describe_age, median_download, monitoring,
                  speedtests_for_baseline)
from .settings import DiagnoseSettings, IgnoreRule
from .snapshot import Snapshot

CRITICAL, WARNING, INFO = "critical", "warning", "info"
SEVERITY_ORDER = {CRITICAL: 0, WARNING: 1, INFO: 2}
EMOJI = {CRITICAL: "\U0001F6D1", WARNING: "\u26A0\uFE0F", INFO: "\u2139\uFE0F"}

# Process exit codes for `diagnose` (see exit_code). Tool errors use cli.EXIT_ERROR.
EXIT_OK, EXIT_WARNING, EXIT_CRITICAL = 0, 1, 2

LINK_LOCAL_PREFIX = "169.254."
GATEWAY_TYPES = {"Gateway", "Dream Machine"}
# Subsystems whose controller status just reflects disconnected devices we already report.
DEVICE_SUBSYSTEMS = {"lan", "wlan"}
@dataclass(frozen=True)
class Finding:
    severity: str  # CRITICAL, WARNING or INFO
    subject: str
    message: str
    target_mac: Optional[str] = None


def _uplink_parents(snap: Snapshot) -> Dict[str, int]:
    """{device id: number of devices that uplink through it}."""
    id_by_mac = {(d.get("macAddress") or "").upper(): d.get("id") for d in snap.devices}
    legacy_uplink = {
        (d.get("mac") or "").upper(): ((d.get("uplink") or {}).get("uplink_mac") or "").upper()
        for d in snap.legacy_devices
    }
    counts: Dict[str, int] = {}
    for d in snap.devices:
        parent = ((snap.device_details.get(d.get("id")) or {}).get("uplink") or {}).get("deviceId")
        if not parent:
            parent = id_by_mac.get(legacy_uplink.get((d.get("macAddress") or "").upper(), ""))
        if parent:
            counts[parent] = counts.get(parent, 0) + 1
    return counts


def _client_ip_findings(snap: Snapshot) -> List[Finding]:
    findings: List[Finding] = []
    for c in snap.clients:
        ip = c.get("ipAddress") or ""
        if ip and not ip.startswith(LINK_LOCAL_PREFIX):
            continue
        subject = c.get("name") or c.get("macAddress") or "?"
        where = client_location(snap, c)
        if ip:
            message = f"link-local address {ip}, DHCP probably failed ({where})"
        else:
            message = f"no IP address ({where})"
        findings.append(Finding(WARNING, subject, message))
    return findings


def _normalize_ip(value: Any) -> str:
    """Canonical IP text, or '' when missing or unparsable."""
    try:
        return str(ipaddress.ip_address(str(value).strip()))
    except ValueError:
        return ""


def _ip_holders(snap: Snapshot) -> Dict[str, Dict[str, str]]:
    """{ip: {mac: description}} for connected clients and UniFi devices using each IP."""
    holders: Dict[str, Dict[str, str]] = {}
    for c in snap.clients:
        ip = _normalize_ip(c.get("ipAddress"))
        if ip:
            name = c.get("name") or c.get("macAddress") or "?"
            holders.setdefault(ip, {})[(c.get("macAddress") or name).upper()] = (
                f"{name} ({client_location(snap, c)})")
    for d in snap.devices:
        if d.get("state") != "ONLINE":
            continue
        ip = _normalize_ip(d.get("ipAddress"))
        if ip:
            name = d.get("name") or d.get("macAddress") or "?"
            holders.setdefault(ip, {})[(d.get("macAddress") or name).upper()] = (
                f"{name} (UniFi device)")
    return holders


def _duplicate_ip_findings(snap: Snapshot) -> List[Finding]:
    """IPs in use by several clients/devices, and reservations whose IP someone else uses.

    Comparing the address itself covers every VLAN at once.
    """
    holders = _ip_holders(snap)
    findings = [
        Finding(WARNING, ip, f"in use by {', '.join(sorted(who.values()))}")
        for ip, who in holders.items() if len(who) > 1
    ]
    for user, _net in reservation_records(snap):
        reserved = _normalize_ip(user.get("fixed_ip"))
        mac = (user.get("mac") or "").upper()
        who = holders.get(reserved, {})
        # If the owner holds the IP too, the duplicate finding above already covers it.
        if who and mac not in who:
            name = user.get("name") or user.get("hostname") or mac
            findings.append(Finding(
                WARNING, name,
                f"reserved IP {reserved} is in use by {', '.join(sorted(who.values()))}"))
    return findings


def _reservation_findings(snap: Snapshot) -> List[Finding]:
    findings: List[Finding] = []
    connected = {(c.get("macAddress") or "").upper(): c for c in snap.clients}
    by_ip: Dict[str, List[str]] = {}

    for user, net in reservation_records(snap):
        mac = (user.get("mac") or "").upper()
        name = user.get("name") or user.get("hostname") or mac
        reserved = user["fixed_ip"]
        by_ip.setdefault(reserved, []).append(name)

        # A client with no IP at all is reported by the client IP check instead.
        current = (connected.get(mac) or {}).get("ipAddress") or ""
        if current and current != reserved:
            findings.append(Finding(
                WARNING, name, f"current IP {current} differs from its reservation {reserved}"))

        subnet = net.get("ip_subnet")
        if subnet:
            try:
                inside = ipaddress.ip_address(reserved) in ipaddress.ip_network(subnet, strict=False)
            except ValueError:
                continue  # unparsable address or subnet: nothing reliable to say
            if not inside:
                findings.append(Finding(
                    WARNING, name,
                    f"reserved IP {reserved} is outside network {net.get('name') or '?'} ({subnet})"))

    for ip, names in by_ip.items():
        if len(names) > 1:
            findings.append(Finding(
                WARNING, ip, f"reserved for {len(names)} clients: {', '.join(sorted(names))}"))
    return findings


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _health_findings(snap: Snapshot, settings: DiagnoseSettings) -> List[Finding]:
    """Findings from the controller's own subsystem health (``stat/health``).

    lan/wlan go "error"/"warning" merely because devices are disconnected, which the
    device checks already report, so that case is a single info line; an unexplained
    status keeps the controller's severity. Other subsystems map error to critical.
    """
    findings: List[Finding] = []
    severity_of = {"error": CRITICAL, "warning": WARNING}
    pending = 0

    for h in snap.health:
        name = h.get("subsystem") or "?"
        status = h.get("status")
        pending += int(_number(h.get("num_pending")))

        if status in severity_of:
            disconnected = int(_number(h.get("num_disconnected")))
            if name in DEVICE_SUBSYSTEMS and disconnected:
                findings.append(Finding(
                    INFO, name, f"{name} subsystem reports {status}: {disconnected} "
                                "device(s) disconnected (see the device findings)"))
            else:
                gateway = f" (gateway {h['gw_name']})" if h.get("gw_name") else ""
                findings.append(Finding(
                    severity_of[status], name, f"{name} subsystem is in {status} state{gateway}"))

        if name == "www":
            latency, drops = _number(h.get("latency")), _number(h.get("drops"))
            if latency and latency >= settings.wan_latency_warn_ms:
                findings.append(Finding(WARNING, name, f"internet latency {latency:.0f} ms"))
            if drops and drops >= settings.wan_drops_warn:
                findings.append(Finding(WARNING, name, f"internet reports {drops:.0f} drops"))
            speedtest = str(h.get("speedtest_status") or "")
            if any(word in speedtest.lower() for word in ("fail", "error")):
                findings.append(Finding(INFO, name, f"last speedtest: {speedtest}"))

    if pending:
        findings.append(Finding(INFO, "controller", f"{pending} device(s) waiting to be adopted"))
    return findings


def _pct_text(pct: float) -> str:
    """Percentage text that keeps tiny rates readable ('0.0025', not '0.00')."""
    return f"{pct:.2f}" if pct >= 0.1 else f"{pct:.4f}".rstrip("0").rstrip(".")


def _switch_name(sw: Dict[str, Any]) -> str:
    return sw.get("name") or sw.get("hostname") or sw.get("mac", "?")


def _port_health_findings(snap: Snapshot, settings: DiagnoseSettings) -> List[Finding]:
    """Flapping links, dropped packets, non-forwarding STP ports and PoE budget use.

    ``link_down_count`` is cumulative since the switch booted, so the message says how
    long the switch has been up; several ports sharing one count usually mean a single
    switch-wide event. Drops are judged as a percentage of packets because a raw count
    says little on a busy port. ``poe_good`` is deliberately not used: it is false on
    every PoE-capable port that simply has no PoE device attached.
    """
    findings: List[Finding] = []
    for sw in snap.legacy_devices:
        if sw.get("type") != "usw":
            continue
        name = _switch_name(sw)
        uptime = format_uptime(sw.get("uptime"))

        for port in sw.get("port_table") or []:
            label = f"{name} port {port.get('port_idx')}"
            flaps = int(_number(port.get("link_down_count")))
            if flaps >= settings.link_flap_count:
                since = f", switch up {uptime}" if uptime else ""
                findings.append(Finding(
                    WARNING, label, f"link has gone down {flaps} times since boot{since}",
                    (sw.get("mac") or "").upper()))

            if not port.get("up"):
                continue
            for direction in ("rx", "tx"):
                packets = _number(port.get(f"{direction}_packets"))
                dropped = _number(port.get(f"{direction}_dropped"))
                if packets >= settings.min_packets_for_drop_pct and dropped:
                    pct = dropped / packets * 100
                    if pct >= settings.port_drop_pct:
                        findings.append(Finding(
                            WARNING, label,
                            f"dropping {_pct_text(pct)}% of {direction} packets "
                            f"({dropped:.0f} of {packets:.0f})",
                            (sw.get("mac") or "").upper()))
            stp = port.get("stp_state")
            if stp and stp != "forwarding":
                findings.append(Finding(
                    WARNING, label, f"STP state is {stp}, not forwarding",
                    (sw.get("mac") or "").upper()))

        budget, used = _number(sw.get("total_max_power")), _number(sw.get("total_used_power"))
        if budget > 0:
            pct = used / budget * 100
            if pct >= settings.poe_warn_pct:
                level = CRITICAL if pct >= settings.poe_critical_pct else WARNING
                findings.append(Finding(
                    level, name,
                    f"PoE budget {used:.1f} W of {budget:.0f} W used ({int(pct)}%)",
                    (sw.get("mac") or "").upper()))
    return findings


BANDS = {"ng": "2.4 GHz", "na": "5 GHz", "6e": "6 GHz"}


def _times(n: int) -> str:
    return f"{n} time" + ("" if n == 1 else "s")


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
        text = f"IP conflict reported {_times(len(events))}{spread} in the last {window}"
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
        findings.append(Finding(WARNING, ip, text + "".join(f"; {h}" for h in hints)))
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
            findings.append(Finding(WARNING, name, f"disconnected {_times(n)} in the last {window}"))
    for name, n in sorted(roams.values()):
        if n >= threshold:
            findings.append(Finding(
                INFO, name, f"roamed {_times(n)} in the last {window} (normal for a mobile device)"))

    for _identity, (device, event_name, n) in sorted(unreachable.items()):
        if device and device.get("state") != "ONLINE":
            continue                              # the offline finding already reports it
        name = (device or {}).get("name") or event_name or "?"
        if n >= threshold:
            findings.append(Finding(WARNING, name, f"was unreachable {_times(n)} in the last {window}"))
        elif device and device.get("state") == "ONLINE":
            findings.append(Finding(
                INFO, name, f"was unreachable {_times(n)} in the last {window}; online now"))
        else:
            findings.append(Finding(INFO, name, f"was unreachable {_times(n)} in the last {window}"))
    if latency:
        findings.append(Finding(
            INFO, "internet", f"high latency was reported {_times(latency)} in the last {window}"))
    if snap.events_truncated:
        findings.append(Finding(
            INFO, "controller", "the event log read was cut off at its cap; event counts may be low"))
    return findings


def _wan_findings(snap: Snapshot, settings: DiagnoseSettings) -> List[Finding]:
    """The controller's own 24-hour internet monitoring, and the last speedtest.

    ``alerting_monitors`` is the list of monitors configured to raise alerts, not the ones
    currently alerting (on a healthy network every one of them reads 100%), so each
    monitor is judged by its own availability.
    """
    findings: List[Finding] = []
    all_wans = monitoring(snap)
    for m in all_wans:
        which = f" ({m['name']})" if len(all_wans) > 1 else ""
        window = describe_duration(m["period_s"]) if m["period_s"] else "24h"
        if m["availability"] is not None and m["availability"] < settings.wan_availability_warn_pct:
            findings.append(Finding(
                WARNING, "wan", f"internet availability{which} {m['availability']:.1f}% over the last {window}"))
        for t in m["targets"]:
            if t["availability"] is not None and t["availability"] < settings.wan_availability_warn_pct:
                latency = f", latency {t['latency_ms']:.0f} ms" if t["latency_ms"] is not None else ""
                findings.append(Finding(
                    WARNING, "wan",
                    f"monitor {t['target']} ({t['type']}) availability {t['availability']:.1f}% "
                    f"over the last {window}{latency}"))

    now_ms = int(time.time() * 1000)
    recent = speedtests_for_baseline(snap.speedtests, SPEEDTEST_BASELINE_DAYS, now_ms)
    median = median_download(recent)
    last = recent[-1] if recent else {}
    download = last.get("download_mbps")
    if (median and isinstance(download, (int, float)) and not isinstance(download, bool)
            and download < median * settings.wan_speed_drop_pct / 100):
        age = describe_age(max(0, int((now_ms - last["time"]) / 1000)))
        findings.append(Finding(
            WARNING, "wan",
            f"last speedtest download {download:.0f} Mbps ({age} ago) is {download / median * 100:.0f}% "
            f"of the {SPEEDTEST_BASELINE_DAYS}-day median ({median:.0f} Mbps)"))
    return findings


def _known_percent(value: Any) -> Optional[float]:
    """A 0-100 quality value, or None when missing or unknown (the controller uses -1)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def _wifi_findings(snap: Snapshot, settings: DiagnoseSettings) -> List[Finding]:
    """Weak signal, retries and low satisfaction for Wi-Fi clients, and busy or
    retry-heavy AP radios.

    Many connected clients report no signal or satisfaction at all, and some APs report
    radio satisfaction as -1 (unknown), so missing values are never flagged. The
    ``anomalies`` field is deliberately not used: it is present on nearly every client.
    """
    findings: List[Finding] = []
    ap_name = {
        (d.get("macAddress") or "").upper(): d.get("name") or d.get("macAddress") or "?"
        for d in snap.devices
    }
    ap_name.update({(d.get("mac") or "").upper(): _switch_name(d) for d in snap.legacy_devices})

    for c in snap.legacy_clients:
        if c.get("is_wired"):
            continue
        name = c.get("name") or c.get("hostname") or c.get("mac") or "?"
        ap = ap_name.get((c.get("ap_mac") or "").upper())
        place = ", ".join(x for x in (BANDS.get(c.get("radio"), ""), f"on {ap}" if ap else "") if x)
        where = f" ({place})" if place else ""

        signal = _number(c.get("signal"))
        if signal < 0 and signal <= settings.wifi_weak_signal_dbm:
            findings.append(Finding(WARNING, name, f"weak Wi-Fi signal {signal:.0f} dBm{where}"))

        attempts = _number(c.get("wifi_tx_attempts"))
        retries = _known_percent(c.get("wifi_tx_retries_percentage"))
        if (attempts >= settings.wifi_min_attempts and retries is not None
                and retries >= settings.wifi_retry_pct):
            findings.append(Finding(
                WARNING, name, f"{retries:.0f}% of Wi-Fi transmissions retried{where}"))

        satisfaction = _known_percent(c.get("satisfaction"))
        if satisfaction is not None and satisfaction < settings.wifi_satisfaction_warn:
            findings.append(Finding(WARNING, name, f"Wi-Fi satisfaction {satisfaction:.0f}%{where}"))

    for ap in snap.legacy_devices:
        if ap.get("type") != "uap":
            continue
        for radio in ap.get("radio_table_stats") or []:
            band = BANDS.get(radio.get("radio"), str(radio.get("radio") or "?"))
            label = f"{_switch_name(ap)} {band} radio"
            channel = radio.get("channel")
            on = f" (channel {channel})" if channel not in (None, "") else ""

            util = _known_percent(radio.get("cu_total"))
            if util is not None and util >= settings.radio_util_warn_pct:
                level = CRITICAL if util >= settings.radio_util_critical_pct else WARNING
                findings.append(Finding(
                    level, label, f"channel utilization {util:.0f}%{on}",
                    (ap.get("mac") or "").upper()))
            retries = _number(radio.get("tx_retries_pct"))
            if retries >= settings.wifi_retry_pct:
                findings.append(Finding(
                    WARNING, label, f"{retries:.0f}% of transmissions retried{on}",
                    (ap.get("mac") or "").upper()))
            satisfaction = _known_percent(radio.get("satisfaction"))
            if satisfaction is not None and satisfaction < settings.wifi_satisfaction_warn:
                findings.append(Finding(
                    WARNING, label, f"satisfaction {satisfaction:.0f}%{on}",
                    (ap.get("mac") or "").upper()))
    return findings


def uplink_speeds(snap: Snapshot, device: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """``(negotiated, capability)`` Mbps for a legacy device's uplink, or None when the link
    is down or either end's maximum is unknown.

    The child's own port capability is the uplink's ``max_speed``; the parent's comes from
    the Integration API port detail. Access points and end clients are not compared with a
    port maximum (a gigabit AP on a 2.5G port is normal), so only these two are used.
    """
    up = device.get("uplink") or {}
    parent_mac = (up.get("uplink_mac") or "").upper()
    speed, child_max = _number(up.get("speed")), _number(up.get("max_speed"))
    if not (up.get("up") and parent_mac and speed and child_max):
        return None
    id_by_mac = {(d.get("macAddress") or "").upper(): d.get("id") for d in snap.devices}
    parent_ports = ((snap.device_details.get(id_by_mac.get(parent_mac)) or {})
                    .get("interfaces") or {}).get("ports") or []
    parent_max = next((_number(p.get("maxSpeedMbps")) for p in parent_ports
                       if p.get("idx") == up.get("uplink_remote_port")), 0.0)
    if not parent_max:
        return None
    return speed, min(child_max, parent_max)


def _uplink_speed_findings(snap: Snapshot) -> List[Finding]:
    """An uplink negotiated below what both ends of the link support."""
    findings: List[Finding] = []
    name_by_mac = {(d.get("mac") or "").upper(): _switch_name(d) for d in snap.legacy_devices}
    for d in snap.legacy_devices:
        speeds = uplink_speeds(snap, d)
        if speeds and speeds[0] < speeds[1]:
            parent_mac = ((d.get("uplink") or {}).get("uplink_mac") or "").upper()
            findings.append(Finding(
                WARNING, _switch_name(d),
                f"uplink to {name_by_mac.get(parent_mac, parent_mac)} negotiated at "
                f"{speeds[0]:.0f} Mbps but both ends support {speeds[1]:.0f} Mbps",
                (d.get("mac") or "").upper()))
    return findings


def diagnose(snap: Snapshot, settings: Optional[DiagnoseSettings] = None) -> List[Finding]:
    settings = settings or DiagnoseSettings()
    findings: List[Finding] = []

    parents = _uplink_parents(snap)
    legacy_type = {(d.get("mac") or "").upper(): d.get("type", "") for d in snap.legacy_devices}

    for d in snap.devices:
        if d.get("state") == "ONLINE":
            continue
        name = d.get("name") or d.get("macAddress", "?")
        message = f"device is {str(d.get('state', 'unknown')).lower()}"
        kind = device_type_label(d, legacy_type.get((d.get("macAddress") or "").upper(), ""))
        downstream = parents.get(d.get("id"), 0)
        if kind in GATEWAY_TYPES:
            findings.append(Finding(
                CRITICAL, name, f"{message} (gateway)", (d.get("macAddress") or "").upper()))
        elif downstream:
            findings.append(Finding(
                CRITICAL, name, f"{message} ({downstream} device(s) uplink through it)",
                (d.get("macAddress") or "").upper()))
        else:
            findings.append(Finding(WARNING, name, message, (d.get("macAddress") or "").upper()))

    for d in snap.devices:
        st = snap.device_stats.get(d.get("id")) or {}
        for key, label in (("cpuUtilizationPct", "CPU"), ("memoryUtilizationPct", "memory")):
            pct = st.get(key) or 0
            if pct >= settings.resource_warn_pct:
                level = CRITICAL if pct >= settings.resource_critical_pct else WARNING
                findings.append(Finding(
                    level, d.get("name") or d.get("macAddress", "?"),
                    f"{label} utilization {st[key]:.0f}%",
                    (d.get("macAddress") or "").upper()))

    findings.extend(_health_findings(snap, settings))
    findings.extend(_wan_findings(snap, settings))
    findings.extend(_client_ip_findings(snap))
    findings.extend(_reservation_findings(snap))
    findings.extend(_duplicate_ip_findings(snap))

    if not snap.legacy_devices:
        findings.append(Finding(
            INFO, "controller",
            "legacy device data unavailable; port checks were skipped"))

    for sw in snap.legacy_devices:
        name = sw.get("name") or sw.get("hostname") or sw.get("mac", "?")
        for port in sw.get("port_table") or []:
            if not port.get("up"):
                continue
            label = f"{name} port {port.get('port_idx')}"
            errors = (port.get("rx_errors") or 0) + (port.get("tx_errors") or 0)
            if errors > 0:
                findings.append(Finding(
                    WARNING, label, f"{errors} rx/tx errors", (sw.get("mac") or "").upper()))
            if port.get("full_duplex") is False:
                findings.append(Finding(
                    WARNING, label, "link is half duplex", (sw.get("mac") or "").upper()))
            if 0 < (port.get("speed") or 0) <= settings.slow_link_mbps:
                findings.append(Finding(
                    INFO, label, f"negotiated at {port['speed']} Mbps",
                    (sw.get("mac") or "").upper()))

    findings.extend(_port_health_findings(snap, settings))
    findings.extend(_uplink_speed_findings(snap))
    findings.extend(_wifi_findings(snap, settings))
    findings.extend(_event_findings(snap, settings))

    return sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], f.subject))


def apply_ignores(
    findings: List[Finding], rules: Tuple[IgnoreRule, ...]
) -> Tuple[List[Finding], List[Tuple[Finding, IgnoreRule]]]:
    """Split findings into (kept, [(ignored finding, the rule that matched)])."""
    kept: List[Finding] = []
    ignored: List[Tuple[Finding, IgnoreRule]] = []
    for f in findings:
        rule = next((r for r in rules if r.matches(f.subject, f.message)), None)
        if rule:
            ignored.append((f, rule))
        else:
            kept.append(f)
    return kept, ignored


def exit_code(findings: List[Finding], fail_on: str = WARNING) -> int:
    """Exit code for a set of findings.

    Critical findings always give EXIT_CRITICAL. Warnings (and info) give
    EXIT_WARNING only when ``fail_on`` is at or below their severity; otherwise 0.
    """
    if not findings:
        return EXIT_OK
    worst = min(SEVERITY_ORDER[f.severity] for f in findings)
    if worst == SEVERITY_ORDER[CRITICAL]:
        return EXIT_CRITICAL
    return EXIT_WARNING if worst <= SEVERITY_ORDER[fail_on] else EXIT_OK


def format_findings(findings: List[Finding], emoji: bool = True, ignored: int = 0) -> str:
    """Render findings. ``emoji=False`` uses text labels (logs, pipes, old terminals).
    ``ignored`` is how many findings the ignore list suppressed (noted in the summary)."""
    note = f" ({ignored} ignored)" if ignored else ""
    if not findings:
        return "No issues found." + note

    def label(severity: str) -> str:
        return EMOJI[severity] if emoji else f"[{severity.upper():8}]"

    lines = [f"{label(f.severity)} {f.subject}: {f.message}" for f in findings]
    counts = {sev: sum(f.severity == sev for f in findings) for sev in SEVERITY_ORDER}
    words = {CRITICAL: "critical", WARNING: "warning", INFO: "info"}
    parts = []
    for sev in SEVERITY_ORDER:
        if counts[sev]:
            word = words[sev] + ("s" if sev == WARNING and counts[sev] != 1 else "")
            prefix = f"{EMOJI[sev]} " if emoji else ""
            parts.append(f"{prefix}{counts[sev]} {word}")
    return "\n".join(lines) + "\n\n" + ", ".join(parts) + note


def format_ignored(ignored: List[Tuple[Finding, IgnoreRule]]) -> str:
    """The findings the ignore list suppressed, with each rule's reason."""
    lines = [f"  {f.subject}: {f.message}  (ignored: {r.reason})" for f, r in ignored]
    return f"Ignored ({len(ignored)}):\n" + "\n".join(lines)


def stream_supports_emoji(stream: Any) -> bool:
    """True for an interactive UTF-8 terminal; otherwise text labels are safer."""
    encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
    return bool(getattr(stream, "isatty", lambda: False)()) and encoding == "utf8"
