"""Checks on client addresses: no IP, duplicate IPs, randomized MAC addresses, and new devices."""

import ipaddress
import time
from typing import Any, Dict, List, Optional

from ..export import LocationIndex
from ..new_clients import select_new
from ..reservations import reservation_records
from ..settings import DiagnoseSettings
from ..snapshot import Snapshot
from ..util import describe_age, is_randomized_mac, normalize_mac
from .model import (
    INFO,
    LINK_LOCAL_PREFIX,
    WARNING,
    Finding,
)


def _client_ip_findings(snap: Snapshot) -> List[Finding]:
    findings: List[Finding] = []
    locate = LocationIndex(snap).of
    for c in snap.clients:
        ip = c.get("ipAddress") or ""
        if ip and not ip.startswith(LINK_LOCAL_PREFIX):
            continue
        subject = c.get("name") or c.get("macAddress") or "?"
        where = locate(c)
        if ip:
            message = f"link-local address {ip}, DHCP probably failed ({where})"
        else:
            message = f"no IP address ({where})"
        findings.append(Finding(WARNING, subject, message,
                                code="client.link_local_ip" if ip else "client.no_ip"))
    return findings


def normalize_ip(value: Any) -> str:
    """Canonical IP text, or '' when missing or unparsable."""
    try:
        return str(ipaddress.ip_address(str(value).strip()))
    except ValueError:
        return ""


def ip_holders(snap: Snapshot) -> Dict[str, Dict[str, str]]:
    """{ip: {mac: description}} for connected clients and UniFi devices using each IP."""
    holders: Dict[str, Dict[str, str]] = {}
    locate = LocationIndex(snap).of
    for c in snap.clients:
        ip = normalize_ip(c.get("ipAddress"))
        if ip:
            name = c.get("name") or c.get("macAddress") or "?"
            holders.setdefault(ip, {})[(normalize_mac(c.get("macAddress")) or f"client:{id(c)}")] = (
                f"{name} ({locate(c)})")
    for d in snap.devices:
        if d.get("state") != "ONLINE":
            continue
        ip = normalize_ip(d.get("ipAddress"))
        if ip:
            name = d.get("name") or d.get("macAddress") or "?"
            holders.setdefault(ip, {})[(normalize_mac(d.get("macAddress")) or f"device:{d.get('id') or id(d)}")] = (
                f"{name} (UniFi device)")
    return holders


def _duplicate_ip_findings(snap: Snapshot) -> List[Finding]:
    """IPs in use by several clients/devices, and reservations whose IP someone else uses.

    Comparing the address itself covers every VLAN at once.
    """
    holders = ip_holders(snap)
    findings = [
        Finding(WARNING, ip, f"in use by {', '.join(sorted(who.values()))}", code="ip.duplicate")
        for ip, who in holders.items() if len(who) > 1
    ]
    for user, _net in reservation_records(snap):
        reserved = normalize_ip(user.get("fixed_ip"))
        mac = normalize_mac(user.get("mac"))
        who = holders.get(reserved, {})
        # If the owner holds the IP too, the duplicate finding above already covers it.
        if who and mac not in who:
            name = user.get("name") or user.get("hostname") or mac
            findings.append(Finding(
                WARNING, name,
                f"reserved IP {reserved} is in use by {', '.join(sorted(who.values()))}",
                code="reservation.ip_in_use"))
    return findings


def _new_client_findings(snap: Snapshot, settings: DiagnoseSettings, now: Optional[float]) -> List[Finding]:
    """A client the controller first saw within ``new_client_window_hours``, as information: a new device.

    This is the controller's own ``first_seen``, so nobody has to tag anything; a client without a usable
    first-seen time is unknown, never new, and the history that was not read (``stat/alluser`` failed, already
    warned about) yields nothing rather than a claim. The subject is the MAC address so that renaming the device
    (what an admin does next) does not make it a different finding for ``--notify``.
    """
    if settings.new_client_window_hours <= 0:
        return []
    moment = time.time() if now is None else now
    connected = {normalize_mac(c.get("macAddress")): c for c in snap.clients}
    found, _unknown = select_new(snap, int(settings.new_client_window_hours * 3600), False, moment)
    findings: List[Finding] = []
    for seen, user, mac in found:
        live = connected.get(mac) or {}
        how = "wired" if user.get("is_wired") else "wireless"
        extras = [how] + (["private MAC"] if is_randomized_mac(mac) else [])
        ip = live.get("ipAddress") or user.get("last_ip")
        findings.append(Finding(
            INFO, mac,
            f"new device {user.get('name') or user.get('hostname') or 'Unknown'!r} first seen "
            f"{describe_age(max(0, int(moment - seen)))} ago ({', '.join(extras)}{', ' + ip if ip else ''})",
            target_mac=mac, code="client.new_device"))
    return findings


def _private_mac_findings(snap: Snapshot) -> List[Finding]:
    """Randomized (private) MAC addresses, as information only: they are normal for phones.

    A reservation is tied to one MAC, so a device that rotates its address stops matching it;
    that is reported per reservation. The connected clients that use one are counted once.
    Locally administered addresses are also used by virtual machines, containers and bridges,
    so this is a hint. The ignore list can silence either finding.
    """
    findings: List[Finding] = []
    for user, _net in reservation_records(snap):
        mac = normalize_mac(user.get("mac"))
        if is_randomized_mac(mac):
            name = user.get("name") or user.get("hostname") or mac
            findings.append(Finding(
                INFO, name,
                f"reserved IP {user['fixed_ip']} is tied to a randomized (private) MAC address; "
                "if the device changes its address the reservation stops applying",
                code="reservation.private_mac"))
    clients = [c for c in snap.clients if c.get("macAddress")]
    private = sum(is_randomized_mac(c["macAddress"]) for c in clients)
    if private:
        findings.append(Finding(
            INFO, "clients",
            f"{private} of {len(clients)} connected clients use randomized (private) MAC addresses "
            "(normal for phones; reservations and history may not hold for them)",
            code="client.private_mac_summary"))
    return findings
