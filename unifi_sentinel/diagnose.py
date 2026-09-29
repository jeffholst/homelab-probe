"""Read-only health checks over a snapshot."""

from dataclasses import dataclass
from typing import Any, Dict, List

from .snapshot import Snapshot

SEVERITY_ORDER = {"warning": 0, "info": 1}


@dataclass(frozen=True)
class Finding:
    severity: str  # "warning" or "info"
    subject: str
    message: str


def diagnose(snap: Snapshot) -> List[Finding]:
    findings: List[Finding] = []

    for d in snap.devices:
        if d.get("state") != "ONLINE":
            findings.append(Finding(
                "warning", d.get("name") or d.get("macAddress", "?"),
                f"device is {str(d.get('state', 'unknown')).lower()}"))

    if not snap.legacy_devices:
        findings.append(Finding(
            "info", "controller",
            "legacy device data unavailable; port checks were skipped"))

    for sw in snap.legacy_devices:
        name = sw.get("name") or sw.get("hostname") or sw.get("mac", "?")
        for port in sw.get("port_table") or []:
            if not port.get("up"):
                continue
            label = f"{name} port {port.get('port_idx')}"
            errors = (port.get("rx_errors") or 0) + (port.get("tx_errors") or 0)
            if errors > 0:
                findings.append(Finding("warning", label, f"{errors} rx/tx errors"))
            if port.get("full_duplex") is False:
                findings.append(Finding("warning", label, "link is half duplex"))
            if 0 < (port.get("speed") or 0) <= 100:
                findings.append(Finding(
                    "info", label, f"negotiated at {port['speed']} Mbps"))

    return sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], f.subject))


def format_findings(findings: List[Finding]) -> str:
    if not findings:
        return "No issues found."
    lines = [f"[{f.severity.upper():7}] {f.subject}: {f.message}" for f in findings]
    warnings = sum(f.severity == "warning" for f in findings)
    return "\n".join(lines) + f"\n\n{warnings} warning(s), {len(findings) - warnings} info"
