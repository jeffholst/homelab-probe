"""Read-only health checks over a snapshot."""

from dataclasses import dataclass
import ipaddress
from typing import Any, Dict, List, Set

from .export import device_type_label
from .reservations import reservation_records
from .snapshot import Snapshot

CRITICAL, WARNING, INFO = "critical", "warning", "info"
SEVERITY_ORDER = {CRITICAL: 0, WARNING: 1, INFO: 2}
EMOJI = {CRITICAL: "\U0001F6D1", WARNING: "\u26A0\uFE0F", INFO: "\u2139\uFE0F"}

# Process exit codes for `diagnose` (see exit_code). Tool errors use cli.EXIT_ERROR.
EXIT_OK, EXIT_WARNING, EXIT_CRITICAL = 0, 1, 2

LINK_LOCAL_PREFIX = "169.254."
RESOURCE_WARN_PCT = 90
RESOURCE_CRITICAL_PCT = 98
GATEWAY_TYPES = {"Gateway", "Dream Machine"}


@dataclass(frozen=True)
class Finding:
    severity: str  # CRITICAL, WARNING or INFO
    subject: str
    message: str


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


def _client_location(snap: Snapshot, client: Dict[str, Any]) -> str:
    """Where a client attaches: 'Wired, Switch port 3' or 'Wireless, via AP'."""
    names_by_id = {d.get("id"): d.get("name") or d.get("macAddress") for d in snap.devices}
    names_by_mac = {(d.get("macAddress") or "").upper(): d.get("name") or d.get("macAddress")
                    for d in snap.devices}
    mac = (client.get("macAddress") or "").upper()
    legacy = next((c for c in snap.legacy_clients if (c.get("mac") or "").upper() == mac), {})
    uplink = names_by_id.get(client.get("uplinkDeviceId"))

    if client.get("type") == "WIRED":
        switch = names_by_mac.get((legacy.get("sw_mac") or "").upper()) or uplink
        port = legacy.get("sw_port")
        if switch and port is not None:
            return f"Wired, {switch} port {port}"
        return f"Wired, {switch}" if switch else "Wired"
    ap = uplink or names_by_mac.get((legacy.get("ap_mac") or "").upper())
    return f"Wireless, via {ap}" if ap else "Wireless"


def _client_ip_findings(snap: Snapshot) -> List[Finding]:
    findings: List[Finding] = []
    for c in snap.clients:
        ip = c.get("ipAddress") or ""
        if ip and not ip.startswith(LINK_LOCAL_PREFIX):
            continue
        subject = c.get("name") or c.get("macAddress") or "?"
        where = _client_location(snap, c)
        if ip:
            message = f"link-local address {ip}, DHCP probably failed ({where})"
        else:
            message = f"no IP address ({where})"
        findings.append(Finding(WARNING, subject, message))
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


def diagnose(snap: Snapshot) -> List[Finding]:
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
            findings.append(Finding(CRITICAL, name, f"{message} (gateway)"))
        elif downstream:
            findings.append(Finding(
                CRITICAL, name, f"{message} ({downstream} device(s) uplink through it)"))
        else:
            findings.append(Finding(WARNING, name, message))

    for d in snap.devices:
        st = snap.device_stats.get(d.get("id")) or {}
        for key, label in (("cpuUtilizationPct", "CPU"), ("memoryUtilizationPct", "memory")):
            pct = st.get(key) or 0
            if pct >= RESOURCE_WARN_PCT:
                level = CRITICAL if pct >= RESOURCE_CRITICAL_PCT else WARNING
                findings.append(Finding(
                    level, d.get("name") or d.get("macAddress", "?"),
                    f"{label} utilization {st[key]:.0f}%"))

    findings.extend(_client_ip_findings(snap))
    findings.extend(_reservation_findings(snap))

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
                findings.append(Finding(WARNING, label, f"{errors} rx/tx errors"))
            if port.get("full_duplex") is False:
                findings.append(Finding(WARNING, label, "link is half duplex"))
            if 0 < (port.get("speed") or 0) <= 100:
                findings.append(Finding(
                    INFO, label, f"negotiated at {port['speed']} Mbps"))

    return sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], f.subject))


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


def format_findings(findings: List[Finding], emoji: bool = True) -> str:
    """Render findings. ``emoji=False`` uses text labels (logs, pipes, old terminals)."""
    if not findings:
        return "No issues found."

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
    return "\n".join(lines) + "\n\n" + ", ".join(parts)


def stream_supports_emoji(stream: Any) -> bool:
    """True for an interactive UTF-8 terminal; otherwise text labels are safer."""
    encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
    return bool(getattr(stream, "isatty", lambda: False)()) and encoding == "utf8"
