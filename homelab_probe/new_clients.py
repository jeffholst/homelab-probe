"""Report of new clients: the ones the controller first saw within a time window (``recent_clients``), or the ones
nobody has put in a client group (``ungrouped_clients``), to spot new devices."""

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from .events import parse_duration
from .export import LocationIndex
from .query import format_table
from .snapshot import Snapshot
from .util import epoch_text, format_time, is_randomized_mac, normalize_mac, number, search_rows

DEFAULT_SINCE = "7d"   # what `new-clients` means by new when neither --since nor --ungrouped is given

NEW_CLIENT_COLUMNS = ["Name", "MAC Address", "IP Address", "Vendor", "Connection Type",
                      "Where", "First Seen", "Last Seen", "Status", "Private MAC"]


def _is_grouped(user: Dict[str, Any], defined_ids: set, definitions_available: bool) -> bool:
    """True if the client is in at least one group. When group definitions are
    available, ids that no longer exist do not count; otherwise trust the raw list."""
    ids = user.get("network_members_group_ids") or []
    return any(i in defined_ids for i in ids) if definitions_available else bool(ids)


def first_seen_seconds(user: Dict[str, Any]) -> Optional[float]:
    """When the controller first saw the client (epoch seconds), or None when it says nothing usable
    (missing, zero, negative or not a number): such a client is unknown, never new."""
    seen = number(user.get("first_seen"))
    return seen if seen is not None and seen > 0 else None


def _row(snap: Snapshot, user: Dict[str, Any], mac: str, connected: Dict[str, Any], locate: Any) -> Dict[str, Any]:
    live = connected.get(mac)
    wired = user.get("is_wired")
    if live:
        where = locate(live)
        last_seen = format_time(live.get("connectedAt"))
    else:
        where = ""
        if wired and user.get("last_uplink_name"):
            port = user.get("last_uplink_remote_port")
            where = f"Wired, {user['last_uplink_name']}" + (f" port {port}" if port is not None else "")
        last_seen = epoch_text(user.get("last_seen"))
    return {
        "Name": user.get("name") or user.get("hostname") or "Unknown",
        "MAC Address": mac,
        "IP Address": (live or {}).get("ipAddress") or user.get("last_ip") or "",
        "Vendor": user.get("oui") or "",
        "Connection Type": "Wired" if wired else "Wireless",
        "Where": where,
        "First Seen": epoch_text(user.get("first_seen")),
        "Last Seen": last_seen,
        "Status": "Online" if live else "Offline",
        "Private MAC": "yes" if is_randomized_mac(mac) else "",
    }


def resolve_since(since: Optional[int], ungrouped: bool) -> Optional[int]:
    """The age limit in seconds: the one given; else none for ``ungrouped`` alone (every client in no group, as
    before) and the default window otherwise."""
    if since is not None:
        return since
    return None if ungrouped else parse_duration(DEFAULT_SINCE)


def window_text(seconds: int) -> str:
    """A window as the whole units it is made of: 7d, 36h, 90m."""
    for size, unit in ((86400, "d"), (3600, "h")):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{max(1, seconds // 60)}m"


def select_new(snap: Snapshot, since: Optional[int] = None, ungrouped: bool = False,
               now: Optional[float] = None) -> Tuple[List[Tuple[float, Dict[str, Any], str]], int]:
    """``(first-seen seconds or 0, user record, normalized MAC)`` of the known clients that count as new, newest
    first-seen first, and how many known clients were left out because the controller gave no first-seen time.

    ``since`` (seconds) keeps the clients first seen within that long; None applies no age limit. ``ungrouped``
    also requires that the client is in no client group. The controller's own devices and records without a MAC
    are never clients. A first-seen time in the future is clock skew and counts as now. With ``since`` and no
    usable first-seen time the client is unknown: counted, not listed.
    """
    now = time.time() if now is None else now
    device_macs = {normalize_mac(d.get("macAddress")) for d in snap.devices}
    defined = {g.get("id") for g in snap.client_groups or []}
    found: List[Tuple[float, Dict[str, Any], str]] = []
    unknown = 0
    for u in snap.all_users:
        mac = normalize_mac(u.get("mac"))
        if not mac or mac in device_macs or (ungrouped and _is_grouped(u, defined, snap.client_groups is not None)):
            continue
        seen = first_seen_seconds(u)
        if since is not None:
            if seen is None:
                unknown += 1
                continue
            if now - seen > since:
                continue
        found.append((seen or 0, u, mac))
    found.sort(key=lambda item: item[0], reverse=True)  # blank first-seen (0) ends up last
    return found, unknown


def new_client_rows(snap: Snapshot, since: Optional[int] = None, ungrouped: bool = False,
                    now: Optional[float] = None) -> Tuple[List[Dict[str, Any]], int]:
    """The table rows of ``select_new`` and the number of known clients with no first-seen time."""
    found, unknown = select_new(snap, since, ungrouped, now)
    connected = {normalize_mac(c.get("macAddress")): c for c in snap.clients}
    locate = LocationIndex(snap).of
    return [_row(snap, u, mac, connected, locate) for _, u, mac in found], unknown


def recent_clients(snap: Snapshot, since: int, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Clients the controller first saw within the last ``since`` seconds."""
    return new_client_rows(snap, since, False, now)[0]


def ungrouped_clients(snap: Snapshot) -> List[Dict[str, Any]]:
    """Every known client (connected or not) in no client group, newest first-seen first."""
    return new_client_rows(snap, None, True)[0]


def new_clients_data(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """What ``new-clients --json`` prints: a bare array with the columns of the table."""
    return [{c: r.get(c, "") for c in NEW_CLIENT_COLUMNS} for r in rows]


def table_footer(rows: List[Dict[str, Any]], since: Optional[int] = None, ungrouped: bool = False,
                 unknown: int = 0) -> str:
    """The line under the table: what was counted, how many use a private MAC, and what could not be judged."""
    what = "client(s)"
    if since is not None:
        what += f" first seen in the last {window_text(since)}"
    if ungrouped:
        what += " in no group"
    line = f"{len(rows)} {what}"
    private = sum(1 for r in rows if r.get("Private MAC"))
    if private:
        line += f" ({private} with a private MAC)"
    if unknown:
        line += f"; {unknown} known client(s) have no first-seen time and are not counted"
    return line


def render_table(rows: List[Dict[str, Any]], since: Optional[int] = None, ungrouped: bool = False,
                 unknown: int = 0) -> str:
    return format_table(rows, NEW_CLIENT_COLUMNS) + "\n\n" + table_footer(rows, since, ungrouped, unknown)


def render(rows: List[Dict[str, Any]], as_json: bool) -> str:
    """``new_clients_data`` as JSON or the table, for a caller that has rows rather than a document."""
    return json.dumps(new_clients_data(rows), indent=2) if as_json else render_table(rows)


def report(snap: Snapshot, search: str = "", since: Optional[int] = None, ungrouped: bool = False,
           now: Optional[float] = None) -> Tuple[List[Dict[str, Any]], int]:
    """The rows (filtered by ``search``) and the number of known clients with no first-seen time."""
    rows, unknown = new_client_rows(snap, since, ungrouped, now)
    return search_rows(rows, search), unknown
