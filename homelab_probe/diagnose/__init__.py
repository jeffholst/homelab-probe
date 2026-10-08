"""Read-only health checks over a snapshot.

``diagnose(snapshot, settings)`` runs every check and returns the findings, worst first. The checks are
grouped by topic, one module each: ``devices`` (offline, CPU and memory), ``health`` (controller subsystems
and the internet connection), ``addresses`` (client IPs, duplicates, randomized MACs), ``reserved`` (DHCP
reservations), ``ports`` (switch ports and uplinks), ``wireless`` (Wi-Fi quality), ``event_checks`` (the event
log) and ``output`` (ignoring, exit codes, text and JSON). ``model`` holds the severities, the catalogue of
finding codes and ``Finding``. A new check is a function ``(snap, settings) -> List[Finding]`` in the module of its
topic, a code in ``model.CODES``, and one line in ``diagnose()``.
"""

from collections.abc import Iterable
from typing import List, Optional

from ..settings import DiagnoseSettings
from ..snapshot import Snapshot
from .addresses import _new_client_findings, _private_mac_findings
from .areas import AREA_NAMES, AREAS, CHECKS, area_of, codes_of, needs_for, parse_areas
from .event_checks import _event_findings
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
    findings_document,
    findings_from_document,
    findings_json,
    format_findings,
    format_ignored,
    render_findings,
    stream_supports_emoji,
)
from .ports import uplink_speeds
from .reserved import _offline_reservation_findings, _pool_findings
from .wireless import BANDS

__all__ = [
    "AREAS", "AREA_NAMES", "area_of", "codes_of", "needs_for", "parse_areas",
    "BANDS", "CODES", "CRITICAL", "DEVICE_SUBSYSTEMS", "EMOJI", "EXIT_CRITICAL", "EXIT_OK", "EXIT_WARNING",
    "GATEWAY_TYPES", "INFO", "JSON_VERSION", "LINK_LOCAL_PREFIX", "SEVERITY_ORDER", "WARNING", "Finding",
    "apply_ignores", "diagnose", "exit_code", "findings_document", "findings_from_document", "findings_json",
    "format_findings", "format_ignored", "render_findings", "stream_supports_emoji", "uplink_speeds",
    # the individual checks, importable for focused tests
    "_event_findings", "_new_client_findings", "_offline_reservation_findings", "_pool_findings",
    "_private_mac_findings",
]


def diagnose(snap: Snapshot, settings: Optional[DiagnoseSettings] = None, now: Optional[float] = None,
             areas: Optional[Iterable[str]] = None) -> List[Finding]:
    """Run the checks (every one, or only those of ``areas``; see ``areas.py``). The order of ``CHECKS`` is the
    order of findings with the same severity and subject, so it is part of the output: change it only on
    purpose (the golden files will show it)."""
    settings = settings or DiagnoseSettings()
    chosen = None if areas is None else set(areas)
    findings: List[Finding] = []
    for check, emits in CHECKS:
        if chosen is None or chosen.intersection(emits):
            findings += check(snap, settings, now)
    if chosen is not None:
        findings = [f for f in findings if area_of(f.code) in chosen]
    return sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], f.subject))
