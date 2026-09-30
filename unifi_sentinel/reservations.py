"""DHCP fixed IP reservations."""

import ipaddress
from datetime import datetime
from typing import Any, Dict, List, Tuple

from .export import _fmt_time, _mac
from .snapshot import Snapshot

RESERVATION_COLUMNS = ["Name", "MAC Address", "Reserved IP", "Network", "VLAN",
                       "Current IP", "Status", "Last Seen"]


def _ip_sort_key(ip: str) -> Tuple[int, Any]:
    try:
        return (0, ipaddress.ip_address(ip))
    except ValueError:
        return (1, ip)


def reservation_records(snap: Snapshot) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """(client record, network config) for each enabled reservation, connected or not.

    A reservation is ``use_fixedip`` true with ``fixed_ip`` set. ``fixed_ip`` alone
    is not enough: disabled reservations keep a stale value. The network is ``{}``
    when it cannot be resolved.
    """
    networks = {n.get("_id"): n for n in snap.networks}
    records = []
    for u in snap.all_users:
        if not (u.get("use_fixedip") and u.get("fixed_ip")):
            continue
        net_id = (u.get("virtual_network_override_id")
                  if u.get("virtual_network_override_enabled")
                  else u.get("last_connection_network_id"))
        records.append((u, networks.get(net_id) or {}))
    return records


def build_reservations(snap: Snapshot) -> List[Dict[str, Any]]:
    """One row per enabled reservation, connected or not."""
    connected = {_mac(c.get("macAddress")): c for c in snap.clients}

    rows: List[Dict[str, Any]] = []
    for u, net in reservation_records(snap):
        mac = _mac(u.get("mac"))
        live = connected.get(mac)

        # Networks without VLAN tagging are on the default untagged VLAN 1.
        vlan = (net.get("vlan") if net.get("vlan_enabled") else 1) if net else ""

        if live:
            last_seen = _fmt_time(live.get("connectedAt"))
        elif u.get("last_seen"):
            last_seen = datetime.fromtimestamp(u["last_seen"]).strftime("%Y-%m-%d %H:%M:%S")
        else:
            last_seen = ""

        rows.append({
            "Name": u.get("name") or u.get("hostname") or "Unknown",
            "MAC Address": mac,
            "Reserved IP": u["fixed_ip"],
            "Network": net.get("name") or u.get("last_connection_network_name") or "",
            "VLAN": vlan,
            "Current IP": live.get("ipAddress", "") if live else "",
            "Status": "Online" if live else "Offline",
            "Last Seen": last_seen,
        })
    return sorted(rows, key=lambda r: _ip_sort_key(r["Reserved IP"]))
