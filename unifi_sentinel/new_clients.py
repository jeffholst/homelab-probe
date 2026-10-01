"""Report of clients that are not in any client group, to spot new devices."""

import json
from datetime import datetime
from typing import Any, Dict, List

from .export import _fmt_time, _mac, client_location
from .query import _search, format_table
from .snapshot import Snapshot
from .util import is_randomized_mac

NEW_CLIENT_COLUMNS = ["Name", "MAC Address", "IP Address", "Vendor", "Connection Type",
                      "Where", "First Seen", "Last Seen", "Status", "Private MAC"]


def _epoch(value: Any) -> str:
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S") if value else ""


def _is_grouped(user: Dict[str, Any], defined_ids: set, definitions_available: bool) -> bool:
    """True if the client is in at least one group. When group definitions are
    available, ids that no longer exist do not count; otherwise trust the raw list."""
    ids = user.get("network_members_group_ids") or []
    return any(i in defined_ids for i in ids) if definitions_available else bool(ids)


def ungrouped_clients(snap: Snapshot) -> List[Dict[str, Any]]:
    """Every known client (connected or not) in no client group, newest first-seen first."""
    defined = {g.get("id") for g in snap.client_groups or []}
    connected = {_mac(c.get("macAddress")): c for c in snap.clients}
    device_macs = {_mac(d.get("macAddress")) for d in snap.devices}

    found = []
    for u in snap.all_users:
        mac = _mac(u.get("mac"))
        if not mac or mac in device_macs or _is_grouped(u, defined, snap.client_groups is not None):
            continue
        live = connected.get(mac)
        wired = u.get("is_wired")

        if live:
            where = client_location(snap, live)
            last_seen = _fmt_time(live.get("connectedAt"))
        else:
            where = ""
            if wired and u.get("last_uplink_name"):
                port = u.get("last_uplink_remote_port")
                where = f"Wired, {u['last_uplink_name']}" + (f" port {port}" if port is not None else "")
            last_seen = _epoch(u.get("last_seen"))

        found.append((u.get("first_seen") or 0, {
            "Name": u.get("name") or u.get("hostname") or "Unknown",
            "MAC Address": mac,
            "IP Address": (live or {}).get("ipAddress") or u.get("last_ip") or "",
            "Vendor": u.get("oui") or "",
            "Connection Type": "Wired" if wired else "Wireless",
            "Where": where,
            "First Seen": _epoch(u.get("first_seen")),
            "Last Seen": last_seen,
            "Status": "Online" if live else "Offline",
            "Private MAC": "yes" if is_randomized_mac(mac) else "",
        }))
    found.sort(key=lambda item: item[0], reverse=True)  # blank first-seen (0) ends up last
    return [row for _, row in found]


def render(rows: List[Dict[str, Any]], as_json: bool) -> str:
    if as_json:
        return json.dumps([{c: r.get(c, "") for c in NEW_CLIENT_COLUMNS} for r in rows], indent=2)
    return format_table(rows, NEW_CLIENT_COLUMNS) + f"\n\n{len(rows)} client(s) in no group"


def report(snap: Snapshot, search: str = "") -> List[Dict[str, Any]]:
    return _search(ungrouped_clients(snap), search)
