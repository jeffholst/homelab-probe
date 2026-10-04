"""Checks on DHCP reservations: wrong IP, outside the subnet, in the pool, offline for long."""

import ipaddress
from datetime import datetime
from typing import Dict, List, Optional

from ..reservations import dhcp_pool, offline_reservations, reservation_records
from ..settings import DiagnoseSettings
from ..snapshot import Snapshot
from ..util import describe_age, normalize_mac
from .addresses import ip_holders
from .model import (
    CRITICAL,
    INFO,
    WARNING,
    Finding,
)


def _reservation_findings(snap: Snapshot) -> List[Finding]:
    findings: List[Finding] = []
    connected = {normalize_mac(c.get("macAddress")): c for c in snap.clients}
    by_ip: Dict[str, List[str]] = {}

    for user, net in reservation_records(snap):
        mac = normalize_mac(user.get("mac"))
        name = user.get("name") or user.get("hostname") or mac
        reserved = user["fixed_ip"]
        by_ip.setdefault(reserved, []).append(name)

        # A client with no IP at all is reported by the client IP check instead.
        current = (connected.get(mac) or {}).get("ipAddress") or ""
        if current and current != reserved:
            findings.append(Finding(
                WARNING, name, f"current IP {current} differs from its reservation {reserved}",
                code="reservation.ip_mismatch"))

        subnet = net.get("ip_subnet")
        if subnet:
            try:
                inside = ipaddress.ip_address(reserved) in ipaddress.ip_network(subnet, strict=False)
            except ValueError:
                continue  # unparsable address or subnet: nothing reliable to say
            if not inside:
                findings.append(Finding(
                    WARNING, name,
                    f"reserved IP {reserved} is outside network {net.get('name') or '?'} ({subnet})",
                    code="reservation.outside_subnet"))

    for ip, names in by_ip.items():
        if len(names) > 1:
            findings.append(Finding(
                WARNING, ip, f"reserved for {len(names)} clients: {', '.join(sorted(names))}",
                code="reservation.duplicate"))
    return findings


def _pool_findings(snap: Snapshot) -> List[Finding]:
    """Reservations that lie inside their network's dynamic DHCP range.

    The reservation itself is honoured by the gateway, but an address in the pool is also
    offered to other clients (and a device configured statically in the pool collides with
    them), so keeping reservations outside the range is the safe layout. It is a warning, and
    critical when another client is using the address right now. Networks where the controller
    does not serve DHCP are skipped; DHCP on with an unusable range is one info per network
    (only if it has reservations) instead of a guess.
    """
    findings: List[Finding] = []
    holders = ip_holders(snap)
    unknown: Dict[str, str] = {}
    for user, net in reservation_records(snap):
        if not net:
            continue                                  # network unresolved: nothing reliable to say
        state, pool = dhcp_pool(net)
        if state == "unknown":
            unknown.setdefault(net.get("_id") or net.get("name") or "?", net.get("name") or "?")
            continue
        if pool is None:
            continue
        try:
            reserved = ipaddress.ip_address(str(user["fixed_ip"]).strip())
        except ValueError:
            continue
        if reserved.version != pool[0].version or not int(pool[0]) <= int(reserved) <= int(pool[1]):
            continue
        mac = normalize_mac(user.get("mac"))
        name = user.get("name") or user.get("hostname") or mac
        text = f"reserved IP {reserved} is inside the DHCP pool {pool[0]}-{pool[1]} of {net.get('name') or '?'}"
        reservation_mac = mac.replace("-", ":")
        others = {
            m: who
            for m, who in holders.get(str(reserved), {}).items()
            if m.replace("-", ":") != reservation_mac
        }
        if others:
            findings.append(Finding(
                CRITICAL, name, f"{text}; also in use by {', '.join(sorted(others.values()))}",
                code="reservation.in_dhcp_pool"))
        else:
            findings.append(Finding(WARNING, name, text, code="reservation.in_dhcp_pool"))
    for network in sorted(unknown.values()):
        findings.append(Finding(
            INFO, network,
            "DHCP is enabled but its address range is missing or invalid; reservations on this network "
            "cannot be checked against the pool",
            code="reservation.pool_unknown"))
    return findings


def _offline_reservation_findings(snap: Snapshot, settings: DiagnoseSettings,
                                  now: Optional[float] = None) -> List[Finding]:
    """A reserved client that is offline is usually a server or appliance that went quiet.

    Warning after ``reserved_offline_warn_days``, critical after ``reserved_offline_critical_days``;
    a reservation with no last-seen time is reported once as info. Devices meant to be off go in
    the ignore list.
    """
    findings: List[Finding] = []
    for r in offline_reservations(snap, settings.reserved_offline_warn_days, now):
        user, net = r["user"], r["net"]
        name = user.get("name") or user.get("hostname") or r["mac"]
        network = net.get("name") or user.get("last_connection_network_name") or ""
        where = f" ({network})" if network else ""
        seconds = r["offline_seconds"]
        if seconds is None:
            findings.append(Finding(
                INFO, name, f"reserved IP {user['fixed_ip']}{where} has no last-seen time (never connected?)",
                code="reservation.never_seen"))
            continue
        level = CRITICAL if seconds >= settings.reserved_offline_critical_days * 86400 else WARNING
        when = datetime.fromtimestamp(r["last_seen"]).strftime("%Y-%m-%d %H:%M")
        findings.append(Finding(
            level, name,
            f"reserved IP {user['fixed_ip']}{where} is offline: last seen {describe_age(int(seconds))} ago ({when})",
            code="reservation.offline"))
    return findings
