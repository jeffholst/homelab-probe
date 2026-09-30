"""Query the inventory: filter and print devices, clients, reservations and switch ports."""

import json
from typing import Any, Dict, List

from .export import INVENTORY_COLUMNS, build_inventory, build_offline_clients, build_switch_ports
from .reservations import RESERVATION_COLUMNS, build_reservations
from .snapshot import Snapshot

TABLE_COLUMNS = ["Name", "MAC Address", "IP Address", "Model", "Connection Type",
                 "Switch", "Port", "Status"]
DEVICE_EXTRA_COLUMNS = ["Firmware", "Update Available", "Uptime", "Uptime (s)"]

PORT_TABLE_COLUMNS = ["Switch", "Port", "Status", "Speed", "Full Duplex", "PoE Power (W)",
                      "Connected Name", "Connected MAC", "RX Errors", "TX Errors"]


def format_uptime(seconds: Any) -> str:
    """Compact uptime such as '2d 7h', '3h 12m' or '5m'; blank when unknown."""
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return ""
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m" if minutes else f"{seconds}s"


def _add_device_details(rows: List[Dict[str, Any]], snap: Snapshot) -> None:
    """Add firmware, update availability and uptime to the device rows in place."""
    by_mac = {(d.get("macAddress") or "").upper(): d for d in snap.devices}
    for row in rows:
        dev = by_mac.get(row["MAC Address"])
        if row["Type"].startswith("Device") and dev:
            detail = snap.device_details.get(dev.get("id")) or {}
            uptime = (
                (snap.device_stats.get(dev.get("id")) or {}).get("uptimeSec")
                if row["Status"] == "Online"
                else None
            )
            updatable = detail.get("firmwareUpdatable", dev.get("firmwareUpdatable"))
            row["Firmware"] = detail.get("firmwareVersion") or dev.get("firmwareVersion") or ""
            row["Update Available"] = "" if updatable is None else ("Yes" if updatable else "No")
            row["Uptime"] = format_uptime(uptime)
            row["Uptime (s)"] = (
                uptime
                if isinstance(uptime, (int, float)) and uptime >= 0
                else ""
            )


def _search(rows: List[Dict[str, Any]], search: str) -> List[Dict[str, Any]]:
    if not search:
        return rows
    needle = search.lower()
    return [r for r in rows if any(needle in str(v).lower() for v in r.values())]


def _errors(row: Dict[str, Any]) -> int:
    return sum(int(row.get(k) or 0) for k in ("RX Errors", "TX Errors"))


def port_rows(
    snap: Snapshot, switch: str = "", down: bool = False, errors: bool = False
) -> List[Dict[str, Any]]:
    """Switch port rows (legacy port tables), with the switch name as first column.

    ``switch`` is a case-insensitive substring of the switch name; ``down`` keeps only
    ports that are down; ``errors`` keeps only ports with rx/tx errors.
    """
    rows = [
        {"Switch": name, **row}
        for name, ports in build_switch_ports(snap.legacy_devices, snap.legacy_clients).values()
        for row in ports
    ]
    if switch:
        rows = [r for r in rows if switch.lower() in r["Switch"].lower()]
    if down:
        rows = [r for r in rows if r["Status"] == "Down"]
    if errors:
        rows = [r for r in rows if _errors(r) > 0]
    return rows


def query_rows(
    snap: Snapshot,
    kind: str,
    search: str = "",
    include_offline: bool = False,
    switch: str = "",
    down: bool = False,
    errors: bool = False,
) -> List[Dict[str, Any]]:
    """Rows for ``kind`` ('devices', 'clients', 'reservations', 'ports' or 'all'),
    optionally filtered by a case-insensitive substring match against any field.
    ``switch``, ``down`` and ``errors`` apply to 'ports' only."""
    if kind == "reservations":
        return _search(build_reservations(snap), search)
    if kind == "ports":
        return _search(port_rows(snap, switch, down, errors), search)
    rows = build_inventory(snap.devices, snap.clients, snap.legacy_devices, snap.legacy_clients,
                           snap.device_details, snap.device_stats)
    if include_offline:
        rows += build_offline_clients(snap.clients, snap.devices, snap.all_users)
    if kind == "devices":
        rows = [r for r in rows if r["Type"].startswith("Device")]
        _add_device_details(rows, snap)
    elif kind == "clients":
        rows = [r for r in rows if r["Type"] == "Client"]
    return _search(rows, search)


def format_table(rows: List[Dict[str, Any]], columns: List[str] = TABLE_COLUMNS) -> str:
    widths = {c: max([len(c)] + [len(str(r.get(c, ""))) for r in rows]) for c in columns}
    line = lambda vals: "  ".join(str(v).ljust(widths[c]) for c, v in zip(columns, vals)).rstrip()
    out = [line(columns), line(["-" * widths[c] for c in columns])]
    out += [line([r.get(c, "") for c in columns]) for r in rows]
    return "\n".join(out)


def render(rows: List[Dict[str, Any]], as_json: bool, kind: str = "all") -> str:
    if as_json:
        if kind == "ports":  # every port column, not just the table subset
            return json.dumps(rows, indent=2)
        columns = RESERVATION_COLUMNS if kind == "reservations" else INVENTORY_COLUMNS
        if kind == "devices":
            columns = INVENTORY_COLUMNS + DEVICE_EXTRA_COLUMNS
        return json.dumps([{c: r.get(c, "") for c in columns} for r in rows], indent=2)
    columns = {
        "reservations": RESERVATION_COLUMNS,
        "ports": PORT_TABLE_COLUMNS,
        "devices": TABLE_COLUMNS + DEVICE_EXTRA_COLUMNS[:3],
    }.get(kind, TABLE_COLUMNS)
    return format_table(rows, columns) + f"\n\n{len(rows)} row(s)"
