"""Configuration audit: `unifi-sentinel audit`.

`diagnose` says what is wrong now; this says what is configured in a way you probably did not intend. It reads
settings, not health: the Wi-Fi networks (legacy ``rest/wlanconf``), the device list and the client history. All
of it is read with GET. Findings are the ``Finding`` of ``diagnose`` with their own codes (``AUDIT_CODES``); the
ignore list, ``--json`` and the exit codes work the same way.

Checked against one controller on Network 10.6.106 (``tests/contract.py``): three Wi-Fi networks, all WPA2/WPA3,
so the Wi-Fi findings below are written from the field names and values the controller uses (``security`` is
``wpapsk`` there; ``open`` and ``wep`` are the legacy API's other values and were not seen live), and no device
there had an update pending or a default name. What was dropped, and why, is in docs/diagnose.md.
"""

import re
from typing import Any, Dict, List

from .diagnose.model import INFO, WARNING, Finding
from .snapshot import Snapshot
from .util import hex_digits, normalize_mac

AUDIT_AREAS = ("wifi", "devices", "clients")        # reported in the JSON ``areas``
MAX_LISTED = 5                                       # unnamed clients spelled out in the one summary finding

AUDIT_CODES = {
    "audit.wifi_open": "an enabled Wi-Fi network has no password (a warning; information for a guest network)",
    "audit.wifi_weak_encryption": "an enabled Wi-Fi network uses WEP",
    "audit.wifi_no_wpa3": "an enabled WPA2 network does not offer WPA3",
    "audit.wifi_guest_no_isolation": "a guest Wi-Fi network lets its clients reach each other",
    "audit.wifi_unavailable": "the Wi-Fi settings could not be read, so the Wi-Fi checks did not run",
    "audit.default_device_name": "a UniFi device still has the name it came with (or none)",
    "audit.firmware_update": "a UniFi device has a firmware update available",
    "audit.unnamed_clients": "known clients that have neither a name nor a hostname",
}


def _wifi_findings(snap: Snapshot) -> List[Finding]:
    if snap.wlans is None:
        return [Finding(INFO, "Wi-Fi", "the Wi-Fi settings could not be read, so the Wi-Fi checks did not run",
                        code="audit.wifi_unavailable")]
    findings: List[Finding] = []
    for wlan in snap.wlans:
        if wlan.get("enabled") is False:
            continue
        name = str(wlan.get("name") or "(unnamed network)")
        security, guest = wlan.get("security"), wlan.get("is_guest") is True
        if security == "open":
            findings.append(Finding(
                INFO if guest else WARNING, name,
                "is an open guest network (no password; fine behind a portal)" if guest
                else "is an open network: anyone in range can join and read unencrypted traffic",
                code="audit.wifi_open"))
        elif security == "wep":
            findings.append(Finding(WARNING, name, "uses WEP, which can be broken in minutes; use WPA2 or WPA3",
                                    code="audit.wifi_weak_encryption"))
        elif security == "wpapsk" and wlan.get("wpa3_support") is not True:
            findings.append(Finding(INFO, name, "offers WPA2 only; WPA3 is not enabled", code="audit.wifi_no_wpa3"))
        if guest and wlan.get("l2_isolation") is not True:
            findings.append(Finding(WARNING, name, "is a guest network whose clients can reach each other "
                                    "(client isolation is off)", code="audit.wifi_guest_no_isolation"))
    return findings


def _default_name(device: Dict[str, Any]) -> str:
    """Why a device's name looks like the one it came with, or '' when it was named by someone."""
    name = str(device.get("name") or "").strip()
    if not name:
        return "has no name"
    if name.lower() == str(device.get("model") or "").strip().lower():
        return "is still named after its model"
    mac = hex_digits(device.get("macAddress") or "")
    if normalize_mac(name) == normalize_mac(device.get("macAddress")) or (
            len(mac) == 12 and re.search(r"(?i)[0-9a-f]{2}[:\-.]?[0-9a-f]{2}[:\-.]?[0-9a-f]{2}$", name)
            and hex_digits(name)[-6:] == mac[-6:]):
        return "is named after its MAC address"
    return ""


def _device_findings(snap: Snapshot) -> List[Finding]:
    findings: List[Finding] = []
    for d in snap.devices:
        mac = normalize_mac(d.get("macAddress")) or None
        subject = str(d.get("name") or "").strip() or mac or "?"
        reason = _default_name(d)
        if reason:
            findings.append(Finding(INFO, subject, f"{reason}; give it a name that says where it is", mac,
                                    code="audit.default_device_name"))
        if d.get("firmwareUpdatable") is True:
            findings.append(Finding(INFO, subject, "firmware update available", mac, code="audit.firmware_update"))
    return findings


def _client_findings(snap: Snapshot) -> List[Finding]:
    device_macs = {normalize_mac(d.get("macAddress")) for d in snap.devices}
    unnamed = [u for u in snap.all_users
               if not str(u.get("name") or "").strip() and not str(u.get("hostname") or "").strip()
               and normalize_mac(u.get("mac")) not in device_macs]
    if not unnamed:
        return []
    listed = [f"{normalize_mac(u.get('mac')) or '?'}" + (f" ({u['oui']})" if u.get("oui") else "")
              for u in unnamed[:MAX_LISTED]]
    more = f" and {len(unnamed) - MAX_LISTED} more" if len(unnamed) > MAX_LISTED else ""
    one = len(unnamed) == 1
    return [Finding(INFO, "clients", f"{len(unnamed)} known client{'' if one else 's'} {'has' if one else 'have'} "
                    f"neither a name nor a hostname: {', '.join(listed)}{more}", code="audit.unnamed_clients")]


def audit(snap: Snapshot) -> List[Finding]:
    """Every audit finding, worst first (then by subject)."""
    findings = _wifi_findings(snap) + _device_findings(snap) + _client_findings(snap)
    order = {WARNING: 0, INFO: 1}
    return sorted(findings, key=lambda f: (order[f.severity], f.subject))
