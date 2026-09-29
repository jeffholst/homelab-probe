"""Query the inventory: filter and print devices and clients."""

import json
from typing import Any, Dict, List

from .export import INVENTORY_COLUMNS, build_inventory, build_offline_clients
from .reservations import RESERVATION_COLUMNS, build_reservations
from .snapshot import Snapshot

TABLE_COLUMNS = ["Name", "MAC Address", "IP Address", "Model", "Connection Type",
                 "Switch", "Port", "Status"]


def query_rows(
    snap: Snapshot, kind: str, search: str = "", include_offline: bool = False
) -> List[Dict[str, Any]]:
    """Rows for ``kind`` ('devices', 'clients', 'reservations' or 'all'), optionally
    filtered by a case-insensitive substring match against any field."""
    if kind == "reservations":
        rows = build_reservations(snap)
        if search:
            needle = search.lower()
            rows = [r for r in rows if any(needle in str(v).lower() for v in r.values())]
        return rows
    rows = build_inventory(snap.devices, snap.clients, snap.legacy_devices, snap.legacy_clients,
                           snap.device_details, snap.device_stats)
    if include_offline:
        rows += build_offline_clients(snap.clients, snap.devices, snap.all_users)
    if kind == "devices":
        rows = [r for r in rows if r["Type"].startswith("Device")]
    elif kind == "clients":
        rows = [r for r in rows if r["Type"] == "Client"]
    if search:
        needle = search.lower()
        rows = [r for r in rows if any(needle in str(v).lower() for v in r.values())]
    return rows


def format_table(rows: List[Dict[str, Any]], columns: List[str] = TABLE_COLUMNS) -> str:
    widths = {c: max([len(c)] + [len(str(r.get(c, ""))) for r in rows]) for c in columns}
    line = lambda vals: "  ".join(str(v).ljust(widths[c]) for c, v in zip(columns, vals)).rstrip()
    out = [line(columns), line(["-" * widths[c] for c in columns])]
    out += [line([r.get(c, "") for c in columns]) for r in rows]
    return "\n".join(out)


def render(rows: List[Dict[str, Any]], as_json: bool, kind: str = "all") -> str:
    reservations = kind == "reservations"
    if as_json:
        columns = RESERVATION_COLUMNS if reservations else INVENTORY_COLUMNS
        return json.dumps([{c: r.get(c, "") for c in columns} for r in rows], indent=2)
    table = format_table(rows, RESERVATION_COLUMNS) if reservations else format_table(rows)
    return table + f"\n\n{len(rows)} row(s)"
