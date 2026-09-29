"""Read-only health checks over a snapshot."""

from dataclasses import dataclass
from typing import Any, Dict, List, Set

from .export import device_type_label
from .snapshot import Snapshot

CRITICAL, WARNING, INFO = "critical", "warning", "info"
SEVERITY_ORDER = {CRITICAL: 0, WARNING: 1, INFO: 2}
EMOJI = {CRITICAL: "\U0001F6D1", WARNING: "\u26A0\uFE0F", INFO: "\u2139\uFE0F"}

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
        parent = (snap.device_details.get(d.get("id")) or {}).get("uplink", {}).get("deviceId")
        if not parent:
            parent = id_by_mac.get(legacy_uplink.get((d.get("macAddress") or "").upper(), ""))
        if parent:
            counts[parent] = counts.get(parent, 0) + 1
    return counts


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
