"""DHCP fixed IP reservations."""

import ipaddress
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .export import _fmt_time, _mac
from .snapshot import Snapshot
from .util import describe_age

RESERVATION_COLUMNS = ["Name", "MAC Address", "Reserved IP", "Network", "VLAN",
                       "Current IP", "Status", "Last Seen"]
OFFLINE_RESERVATION_COLUMNS = RESERVATION_COLUMNS + ["Offline For"]


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


def offline_reservations(snap: Snapshot, min_days: float, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Enabled reservations whose client is not connected and was last seen at least ``min_days``
    ago, or has no last-seen time at all (``offline_seconds`` is then None).

    Joined by MAC with the connected clients; UniFi devices are skipped (the device checks
    report them). ``last_seen`` is the controller's epoch-seconds value from ``stat/alluser``.
    ``diagnose`` and ``query reservations --offline`` both use this, so the listing is exactly
    what the check reports.
    """
    now = time.time() if now is None else now
    connected = {_mac(c.get("macAddress")) for c in snap.clients}
    devices = {_mac(d.get("macAddress")) for d in snap.devices}
    found = []
    for user, net in reservation_records(snap):
        mac = _mac(user.get("mac"))
        if mac in connected or mac in devices:
            continue
        seen = user.get("last_seen")
        seen = float(seen) if isinstance(seen, (int, float)) and not isinstance(seen, bool) and seen > 0 else None
        offline = None if seen is None else max(0.0, now - seen)
        if offline is not None and offline < min_days * 86400:
            continue
        found.append({"user": user, "net": net, "mac": mac, "last_seen": seen, "offline_seconds": offline})
    return found


def offline_reservation_rows(snap: Snapshot, min_days: float, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """The ``query reservations`` rows for ``offline_reservations``, with an "Offline For" column."""
    offline = {r["mac"]: r for r in offline_reservations(snap, min_days, now)}
    rows = []
    for row in build_reservations(snap):
        record = offline.get(row["MAC Address"])
        if record is not None:
            seconds = record["offline_seconds"]
            rows.append({**row, "Offline For": "never seen" if seconds is None else describe_age(int(seconds))})
    return rows
