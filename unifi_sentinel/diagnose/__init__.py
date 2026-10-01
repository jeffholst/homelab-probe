"""Read-only health checks over a snapshot.

``diagnose(snapshot, settings)`` runs every check and returns the findings, worst first. The checks are
grouped by topic, one module each: ``devices`` (offline, CPU and memory), ``health`` (controller subsystems
and the internet connection), ``addresses`` (client IPs, duplicates, randomized MACs), ``reserved`` (DHCP
reservations), ``ports`` (switch ports and uplinks), ``wireless`` (Wi-Fi quality), ``event_checks`` (the event
log) and ``output`` (ignoring, exit codes, text and JSON). ``model`` holds the severities, the catalogue of
finding codes and ``Finding``. A new check is a function ``(snap, settings) -> List[Finding]`` in the module of its
topic, a code in ``model.CODES``, and one line in ``diagnose()``.
"""

from typing import List, Optional

from ..settings import DiagnoseSettings
from ..snapshot import Snapshot
from .addresses import _client_ip_findings, _duplicate_ip_findings, _private_mac_findings
from .devices import _offline_device_findings, _resource_findings
from .event_checks import _event_findings
from .health import _health_findings, _wan_findings
from .model import (
    CODES,
    CRITICAL,
    DEVICE_SUBSYSTEMS,
    EMOJI,
    EXIT_CRITICAL,
    EXIT_OK,
    EXIT_WARNING,
    GATEWAY_TYPES,
    INFO,
    LINK_LOCAL_PREFIX,
    SEVERITY_ORDER,
    WARNING,
    Finding,
)
from .output import (
    JSON_VERSION,
    apply_ignores,
    exit_code,
    findings_json,
    format_findings,
    format_ignored,
    stream_supports_emoji,
)
from .ports import (
    _legacy_unavailable_findings,
    _port_basic_findings,
    _port_health_findings,
    _uplink_speed_findings,
    uplink_speeds,
)
from .reserved import _offline_reservation_findings, _pool_findings, _reservation_findings
from .wireless import BANDS, _wifi_findings

__all__ = [
    "BANDS", "CODES", "CRITICAL", "DEVICE_SUBSYSTEMS", "EMOJI", "EXIT_CRITICAL", "EXIT_OK", "EXIT_WARNING",
    "GATEWAY_TYPES", "INFO", "JSON_VERSION", "LINK_LOCAL_PREFIX", "SEVERITY_ORDER", "WARNING", "Finding",
    "apply_ignores", "diagnose", "exit_code", "findings_json", "format_findings", "format_ignored",
    "stream_supports_emoji", "uplink_speeds",
    # the individual checks, importable for focused tests
    "_event_findings", "_offline_reservation_findings", "_pool_findings", "_private_mac_findings",
]


def diagnose(snap: Snapshot, settings: Optional[DiagnoseSettings] = None,
             now: Optional[float] = None) -> List[Finding]:
    """Run every check. The order below is the order of findings with the same severity and subject, so it
    is part of the output: change it only on purpose (the golden files will show it)."""
    settings = settings or DiagnoseSettings()
    findings: List[Finding] = []
    findings += _offline_device_findings(snap)
    findings += _resource_findings(snap, settings)
    findings += _health_findings(snap, settings)
    findings += _wan_findings(snap, settings)
    findings += _client_ip_findings(snap)
    findings += _reservation_findings(snap)
    findings += _pool_findings(snap)
    findings += _offline_reservation_findings(snap, settings, now)
    findings += _private_mac_findings(snap)
    findings += _duplicate_ip_findings(snap)
    findings += _legacy_unavailable_findings(snap)
    findings += _port_basic_findings(snap, settings)
    findings += _port_health_findings(snap, settings)
    findings += _uplink_speed_findings(snap)
    findings += _wifi_findings(snap, settings)
    findings += _event_findings(snap, settings)
    return sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], f.subject))
