"""Checks on UniFi devices: offline devices, overheating, and CPU or memory use."""

from typing import Dict, List

from ..export import device_type_label
from ..settings import DiagnoseSettings
from ..snapshot import Snapshot
from ..util import normalize_mac, record_for
from .model import CRITICAL, GATEWAY_TYPES, WARNING, Finding


def _uplink_parents(snap: Snapshot) -> Dict[str, int]:
    """{device id: number of devices that uplink through it}."""
    id_by_mac = {normalize_mac(d.get("macAddress")): d.get("id") for d in snap.devices}
    legacy_uplink = {
        normalize_mac(d.get("mac")): normalize_mac((d.get("uplink") or {}).get("uplink_mac"))
        for d in snap.legacy_devices
    }
    counts: Dict[str, int] = {}
    for d in snap.devices:
        parent = (record_for(snap.device_details, d.get("id")).get("uplink") or {}).get("deviceId")
        if not parent:
            parent = id_by_mac.get(legacy_uplink.get(normalize_mac(d.get("macAddress")), ""))
        if parent:
            counts[parent] = counts.get(parent, 0) + 1
    return counts


def _offline_device_findings(snap: Snapshot) -> List[Finding]:
    """A device that is not online: critical for a gateway or one that others uplink through, else a warning."""
    findings: List[Finding] = []
    parents = _uplink_parents(snap)
    legacy_type = {normalize_mac(d.get("mac")): d.get("type", "") for d in snap.legacy_devices}
    for d in snap.devices:
        if d.get("state") == "ONLINE":
            continue
        name = d.get("name") or d.get("macAddress", "?")
        message = f"device is {str(d.get('state', 'unknown')).lower()}"
        kind = device_type_label(d, legacy_type.get(normalize_mac(d.get("macAddress")), ""))
        downstream = parents.get(d.get("id") or "", 0)
        if kind in GATEWAY_TYPES:
            findings.append(Finding(
                CRITICAL, name, f"{message} (gateway)", normalize_mac(d.get("macAddress")),
                code="device.offline"))
        elif downstream:
            findings.append(Finding(
                CRITICAL, name, f"{message} ({downstream} device(s) uplink through it)",
                normalize_mac(d.get("macAddress")), code="device.offline"))
        else:
            findings.append(Finding(WARNING, name, message, normalize_mac(d.get("macAddress")),
                                    code="device.offline"))
    return findings


def _resource_findings(snap: Snapshot, settings: DiagnoseSettings) -> List[Finding]:
    """CPU or memory use at or above the thresholds (critical from the higher one)."""
    findings: List[Finding] = []
    for d in snap.devices:
        st = record_for(snap.device_stats, d.get("id"))
        for key, label in (("cpuUtilizationPct", "CPU"), ("memoryUtilizationPct", "memory")):
            pct = st.get(key) or 0
            if pct >= settings.resource_warn_pct:
                level = CRITICAL if pct >= settings.resource_critical_pct else WARNING
                findings.append(Finding(
                    level, d.get("name") or d.get("macAddress", "?"),
                    f"{label} utilization {st[key]:.0f}%",
                    normalize_mac(d.get("macAddress")),
                    code="device.cpu_high" if key == "cpuUtilizationPct" else "device.memory_high"))
    return findings


def _overheating_findings(snap: Snapshot) -> List[Finding]:
    """A device that says it is overheating (the legacy ``overheating`` flag), critical: a device that cannot cool
    itself can shut down. Access points and some switches do not have the flag, and a missing or non-boolean
    value means "unknown", never a finding. Only online devices count: the legacy record of an offline device
    keeps what it last reported."""
    online = {normalize_mac(d.get("macAddress")): d for d in snap.devices if d.get("state") == "ONLINE"}
    findings: List[Finding] = []
    for legacy in snap.legacy_devices:
        mac = normalize_mac(legacy.get("mac"))
        device = online.get(mac)
        if legacy.get("overheating") is not True or device is None:
            continue
        name = device.get("name") or legacy.get("name") or mac or "?"
        findings.append(Finding(CRITICAL, name, "reports that it is overheating", mac, code="device.overheating"))
    return findings
