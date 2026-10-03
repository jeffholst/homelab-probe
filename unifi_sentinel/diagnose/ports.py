"""Checks on switch ports and links: errors, flapping, drops, STP, PoE and uplink speed."""

from typing import Any, Dict, List, Optional, Tuple

from ..query import format_uptime
from ..settings import DiagnoseSettings
from ..snapshot import Snapshot
from ..util import normalize_mac, number_or_zero, record_for
from .model import CRITICAL, INFO, WARNING, Finding


def _pct_text(pct: float) -> str:
    """Percentage text that keeps tiny rates readable ('0.0025', not '0.00')."""
    return f"{pct:.2f}" if pct >= 0.1 else f"{pct:.4f}".rstrip("0").rstrip(".")


def switch_name(sw: Dict[str, Any]) -> str:
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
        name = switch_name(sw)
        uptime = format_uptime(sw.get("uptime"))

        for port in sw.get("port_table") or []:
            label = f"{name} port {port.get('port_idx')}"
            flaps = int(number_or_zero(port.get("link_down_count")))
            if flaps >= settings.link_flap_count:
                since = f", switch up {uptime}" if uptime else ""
                findings.append(Finding(
                    WARNING, label, f"link has gone down {flaps} times since boot{since}",
                    normalize_mac(sw.get("mac")), code="port.link_flaps"))

            if not port.get("up"):
                continue
            for direction in ("rx", "tx"):
                packets = number_or_zero(port.get(f"{direction}_packets"))
                dropped = number_or_zero(port.get(f"{direction}_dropped"))
                if packets >= settings.min_packets_for_drop_pct and dropped:
                    pct = dropped / packets * 100
                    if pct >= settings.port_drop_pct:
                        findings.append(Finding(
                            WARNING, label,
                            f"dropping {_pct_text(pct)}% of {direction} packets "
                            f"({dropped:.0f} of {packets:.0f})",
                            normalize_mac(sw.get("mac")), code="port.drops"))
            stp = port.get("stp_state")
            if stp and stp != "forwarding":
                findings.append(Finding(
                    WARNING, label, f"STP state is {stp}, not forwarding",
                    normalize_mac(sw.get("mac")), code="port.stp"))

        budget, used = number_or_zero(sw.get("total_max_power")), number_or_zero(sw.get("total_used_power"))
        if budget > 0:
            pct = used / budget * 100
            if pct >= settings.poe_warn_pct:
                level = CRITICAL if pct >= settings.poe_critical_pct else WARNING
                findings.append(Finding(
                    level, name,
                    f"PoE budget {used:.1f} W of {budget:.0f} W used ({int(pct)}%)",
                    normalize_mac(sw.get("mac")), code="port.poe_budget"))
    return findings


def uplink_speeds(snap: Snapshot, device: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """``(negotiated, capability)`` Mbps for a legacy device's uplink, or None when the link
    is down or either end's maximum is unknown.

    The child's own port capability is the uplink's ``max_speed``; the parent's comes from
    the Integration API port detail. Access points and end clients are not compared with a
    port maximum (a gigabit AP on a 2.5G port is normal), so only these two are used.
    """
    up = device.get("uplink") or {}
    parent_mac = normalize_mac(up.get("uplink_mac"))
    speed, child_max = number_or_zero(up.get("speed")), number_or_zero(up.get("max_speed"))
    if not (up.get("up") and parent_mac and speed and child_max):
        return None
    id_by_mac = {normalize_mac(d.get("macAddress")): d.get("id") for d in snap.devices}
    parent_ports = (record_for(snap.device_details, id_by_mac.get(parent_mac)).get("interfaces") or {}).get(
        "ports") or []
    parent_max = next((number_or_zero(p.get("maxSpeedMbps")) for p in parent_ports
                       if p.get("idx") == up.get("uplink_remote_port")), 0.0)
    if not parent_max:
        return None
    return speed, min(child_max, parent_max)


def _uplink_speed_findings(snap: Snapshot) -> List[Finding]:
    """An uplink negotiated below what both ends of the link support."""
    findings: List[Finding] = []
    name_by_mac = {normalize_mac(d.get("mac")): switch_name(d) for d in snap.legacy_devices}
    for d in snap.legacy_devices:
        speeds = uplink_speeds(snap, d)
        if speeds and speeds[0] < speeds[1]:
            parent_mac = normalize_mac((d.get("uplink") or {}).get("uplink_mac"))
            findings.append(Finding(
                WARNING, switch_name(d),
                f"uplink to {name_by_mac.get(parent_mac, parent_mac)} negotiated at "
                f"{speeds[0]:.0f} Mbps but both ends support {speeds[1]:.0f} Mbps",
                normalize_mac(d.get("mac")), code="link.below_capability"))
    return findings


def _legacy_unavailable_findings(snap: Snapshot) -> List[Finding]:
    """One info line when the legacy device data could not be read, because the port and overheating checks need it."""
    if snap.legacy_devices:
        return []
    return [Finding(INFO, "controller", "legacy device data unavailable; port and overheating checks were skipped",
                    code="controller.legacy_unavailable")]


def _port_basic_findings(snap: Snapshot, settings: DiagnoseSettings) -> List[Finding]:
    """Ports that are up but have errors, run half duplex, or negotiated a slow speed."""
    findings: List[Finding] = []
    for sw in snap.legacy_devices:
        name = sw.get("name") or sw.get("hostname") or sw.get("mac", "?")
        for port in sw.get("port_table") or []:
            if not port.get("up"):
                continue
            label = f"{name} port {port.get('port_idx')}"
            errors = (port.get("rx_errors") or 0) + (port.get("tx_errors") or 0)
            if errors > 0:
                findings.append(Finding(
                    WARNING, label, f"{errors} rx/tx errors", normalize_mac(sw.get("mac")),
                    code="port.errors"))
            if port.get("full_duplex") is False:
                findings.append(Finding(
                    WARNING, label, "link is half duplex", normalize_mac(sw.get("mac")),
                    code="port.half_duplex"))
            if 0 < (port.get("speed") or 0) <= settings.slow_link_mbps:
                findings.append(Finding(
                    INFO, label, f"negotiated at {port['speed']} Mbps",
                    normalize_mac(sw.get("mac")), code="port.slow_link"))
    return findings
