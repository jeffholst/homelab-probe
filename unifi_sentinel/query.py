"""Query the inventory: filter and print devices, clients, reservations and switch ports."""

import json
from typing import Any, Dict, List

from .export import INVENTORY_COLUMNS, build_inventory, build_offline_clients, build_switch_ports
from .reservations import RESERVATION_COLUMNS, build_reservations
from .snapshot import Snapshot

TABLE_COLUMNS = ["Name", "MAC Address", "IP Address", "Model", "Connection Type",
                 "Switch", "Port", "Status"]
PORT_TABLE_COLUMNS = ["Switch", "Port", "Status", "Speed", "Full Duplex", "PoE Power (W)",
                      "Connected Name", "Connected MAC", "RX Errors", "TX Errors"]


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
        for name, ports in build_switch_ports(snap.legacy_devices, snap.legacy_clients).items()
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
        return json.dumps([{c: r.get(c, "") for c in columns} for r in rows], indent=2)
    columns = {"reservations": RESERVATION_COLUMNS, "ports": PORT_TABLE_COLUMNS}.get(kind, TABLE_COLUMNS)
    return format_table(rows, columns) + f"\n\n{len(rows)} row(s)"
