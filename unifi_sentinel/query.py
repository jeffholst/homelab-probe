"""Query the inventory: filter and print devices, clients, reservations, switch ports, networks and Wi-Fi networks."""

import csv
import io
import ipaddress
import json
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from .export import INVENTORY_COLUMNS, build_inventory, build_offline_clients, build_switch_ports
from .reservations import (
    OFFLINE_RESERVATION_COLUMNS,
    RESERVATION_COLUMNS,
    build_reservations,
    dhcp_pool,
    offline_reservation_rows,
)
from .snapshot import Snapshot
from .util import csv_safe, is_randomized_mac, normalize_mac, printable, record_for, search_rows

TABLE_COLUMNS = ["Name", "MAC Address", "IP Address", "Model", "Connection Type",
                 "Switch", "Port", "Status"]
DEVICE_EXTRA_COLUMNS = ["Firmware", "Update Available", "Uptime", "Uptime (s)"]
CLIENT_EXTRA_COLUMNS = ["Private MAC"]   # "yes" for a randomized (locally administered) MAC address

PORT_TABLE_COLUMNS = ["Switch", "Port", "Status", "Speed", "Full Duplex", "PoE Power (W)",
                      "Connected Name", "Connected MAC", "RX Errors", "TX Errors"]
# Every column of a port row (the table shows a subset); --json and --csv give all of them, in this order.
PORT_COLUMNS = ["Switch", "Port", "Port Index", "Status", "Speed", "Full Duplex", "PoE Enabled", "PoE Power (W)",
                "PoE Class", "Connected Type", "Connected Name", "Connected MAC", "Connected Model", "RX Bytes",
                "TX Bytes", "RX Packets", "TX Packets", "RX Errors", "TX Errors"]


# Networks (legacy rest/networkconf) and Wi-Fi networks (rest/wlanconf); the table, --json and --csv give every column.
NETWORK_COLUMNS = ["Name", "Purpose", "VLAN", "Subnet", "Gateway", "DHCP", "DHCP Range", "Clients"]
WLAN_COLUMNS = ["Name", "Enabled", "Security", "Bands", "Network", "VLAN", "Guest", "Client Isolation", "Hidden",
                "Clients"]
BANDS = {"2g": "2.4 GHz", "5g": "5 GHz", "6g": "6 GHz"}


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
    by_mac = {normalize_mac(d.get("macAddress")): d for d in snap.devices}
    for row in rows:
        dev = by_mac.get(row["MAC Address"])
        if row["Type"].startswith("Device") and dev:
            detail = record_for(snap.device_details, dev.get("id"))
            uptime = (
                record_for(snap.device_stats, dev.get("id")).get("uptimeSec")
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


def network_vlan(net: Dict[str, Any]) -> Any:
    """The VLAN of a network record: its tag when VLANs are on, 1 (the default untagged VLAN) when they are off,
    and blank when the controller does not say (WAN and VPN networks have no ``vlan_enabled``)."""
    enabled, vlan = net.get("vlan_enabled"), net.get("vlan")
    if enabled is True:
        return vlan if isinstance(vlan, int) and not isinstance(vlan, bool) else ""
    return 1 if enabled is False else ""


def _subnet(net: Dict[str, Any]) -> Tuple[str, str]:
    """(network address with prefix, gateway address) from ``ip_subnet``, which is the gateway's own address with
    the prefix. Text that is not an address is shown as it came, with no gateway."""
    text = str(net.get("ip_subnet") or "").strip()
    if not text:
        return "", ""
    try:
        interface = ipaddress.ip_interface(text)
    except ValueError:
        return text, ""
    return str(interface.network), str(interface.ip)


def _dhcp(net: Dict[str, Any]) -> Tuple[str, str]:
    """(who serves DHCP, the dynamic range): ``Relay``, ``Server`` (with the range when it is valid), ``Off``, or
    blank when the record says nothing (WAN and VPN networks)."""
    if net.get("dhcp_relay_enabled") is True:
        return "Relay", ""
    if net.get("dhcpd_enabled") is True:
        _state, pool = dhcp_pool(net)
        return "Server", f"{pool[0]} - {pool[1]}" if pool else ""
    return ("Off", "") if net.get("dhcpd_enabled") is False else ("", "")


def _clients_known(snap: Snapshot) -> bool:
    """False when the connected-client list could not be read, so a count of 0 would be a guess."""
    return snap.legacy_clients_available


def network_rows(snap: Snapshot) -> List[Dict[str, Any]]:
    """One row per network, in the controller's order. ``Clients`` counts the connected clients whose
    ``network_id`` is the network's id (blank when the client list could not be read)."""
    counts = Counter(c.get("network_id") for c in snap.legacy_clients)
    known = _clients_known(snap)
    rows = []
    for net in snap.networks:
        subnet, gateway = _subnet(net)
        dhcp, dhcp_range = _dhcp(net)
        rows.append({"Name": net.get("name") or "", "Purpose": net.get("purpose") or "", "VLAN": network_vlan(net),
                     "Subnet": subnet, "Gateway": gateway, "DHCP": dhcp, "DHCP Range": dhcp_range,
                     "Clients": counts.get(net.get("_id"), 0) if known else ""})
    return rows


def security_label(wlan: Dict[str, Any]) -> str:
    """How a Wi-Fi network is secured, in words. ``wpapsk`` with ``wpa3_support`` and ``wpa3_transition`` both true
    is WPA2/WPA3 (the mixed mode); with only ``wpa3_support`` it is WPA3. A value this tool has not seen is
    shown as the controller wrote it, never guessed. The passphrase is in the same record and is never read."""
    security = wlan.get("security")
    if security == "open":
        return "Open"
    if security == "wep":
        return "WEP"
    if security == "wpapsk":
        if wlan.get("wpa3_support") is True:
            return "WPA2/WPA3" if wlan.get("wpa3_transition") is True else "WPA3"
        return "WPA" if wlan.get("wpa_mode") == "wpa" else "WPA2"
    return str(security) if security else ""


def _yes(value: Any) -> str:
    return "Yes" if value is True else "No"


def wlan_rows(snap: Snapshot) -> List[Dict[str, Any]]:
    """One row per Wi-Fi network (SSID), in the controller's order. ``Clients`` counts the connected clients that are
    on it: those with an ``essid`` whose ``wlanconf_id`` is this network's id, or, when the client has no id, whose
    ``essid`` is the network's name (a client with no ``essid`` is not on Wi-Fi, whatever its ``wlanconf_id``
    says). Only the fields shown are read; the record's passphrase never leaves it."""
    networks = {n.get("_id"): n for n in snap.networks}
    by_id = Counter(c.get("wlanconf_id") for c in snap.legacy_clients if c.get("essid") and c.get("wlanconf_id"))
    by_name = Counter(c.get("essid") for c in snap.legacy_clients if c.get("essid") and not c.get("wlanconf_id"))
    known = _clients_known(snap)
    rows = []
    for wlan in snap.wlans or []:
        net = networks.get(wlan.get("networkconf_id")) or {}
        bands = wlan.get("wlan_bands")
        rows.append({
            "Name": wlan.get("name") or "",
            "Enabled": "No" if wlan.get("enabled") is False else "Yes",
            "Security": security_label(wlan),
            "Bands": ", ".join(BANDS.get(str(b), str(b)) for b in bands) if isinstance(bands, list) else "",
            "Network": net.get("name") or "",
            "VLAN": network_vlan(net) if net else "",
            "Guest": _yes(wlan.get("is_guest")),
            "Client Isolation": _yes(wlan.get("l2_isolation")),
            "Hidden": _yes(wlan.get("hide_ssid")),
            "Clients": by_id.get(wlan.get("_id"), 0) + by_name.get(wlan.get("name"), 0) if known else "",
        })
    return rows


def filter_clients(rows: List[Dict[str, Any]], snap: Snapshot, network: str = "", ssid: str = "",
                   ap: str = "") -> List[Dict[str, Any]]:
    """Keep the client rows whose current attachment matches every filter given: ``network`` and ``ssid`` are
    case-insensitive substrings of the network name and of the SSID the client is on, ``ap`` of the name of the
    access point it is on. The network name comes from the client's ``network_id`` (so a renamed network still
    matches), or from the client's own ``network`` text when the id is unknown. A row with no connected-client
    record (an offline client, or a UniFi device) has no attachment and is dropped."""
    records = {normalize_mac(c.get("mac")): c for c in snap.legacy_clients}
    networks = {n.get("_id"): n.get("name") for n in snap.networks}
    devices = {normalize_mac(d.get("mac")): d.get("name") for d in snap.legacy_devices}
    devices.update({normalize_mac(d.get("macAddress")): d.get("name") for d in snap.devices})
    kept = []
    for row in rows:
        record = records.get(normalize_mac(row.get("MAC Address")))
        if record is None:
            continue
        name = networks.get(record.get("network_id")) or record.get("network") or ""
        if network and network.lower() not in str(name).lower():
            continue
        if ssid and ssid.lower() not in str(record.get("essid") or "").lower():
            continue
        if ap and ap.lower() not in str(devices.get(normalize_mac(record.get("ap_mac"))) or "").lower():
            continue
        kept.append(row)
    return kept


def query_rows(
    snap: Snapshot,
    kind: str,
    search: str = "",
    include_offline: bool = False,
    switch: str = "",
    down: bool = False,
    errors: bool = False,
    offline_days: Optional[float] = None,
    network: str = "",
    ssid: str = "",
    ap: str = "",
) -> List[Dict[str, Any]]:
    """Rows for ``kind`` ('devices', 'clients', 'reservations', 'ports', 'networks', 'wlans' or 'all'),
    optionally filtered by a case-insensitive substring match against any field.
    ``switch``, ``down`` and ``errors`` apply to 'ports' only; ``offline_days`` to
    'reservations' only (keep the reservations ``diagnose`` reports as offline); ``network``,
    ``ssid`` and ``ap`` to 'clients' only (see ``filter_clients``)."""
    if kind == "networks":
        return search_rows(network_rows(snap), search)
    if kind == "wlans":
        return search_rows(wlan_rows(snap), search)
    if kind == "reservations":
        rows = build_reservations(snap) if offline_days is None else offline_reservation_rows(snap, offline_days)
        return search_rows(rows, search)
    if kind == "ports":
        return search_rows(port_rows(snap, switch, down, errors), search)
    rows = build_inventory(snap.devices, snap.clients, snap.legacy_devices, snap.legacy_clients,
                           snap.device_details, snap.device_stats)
    if include_offline:
        rows += build_offline_clients(snap.clients, snap.devices, snap.all_users)
    if kind == "devices":
        rows = [r for r in rows if r["Type"].startswith("Device")]
        _add_device_details(rows, snap)
    elif kind == "clients":
        rows = [{**r, "Private MAC": "yes" if is_randomized_mac(r["MAC Address"]) else ""}
                for r in rows if r["Type"] == "Client"]
        if network or ssid or ap:
            rows = filter_clients(rows, snap, network, ssid, ap)
    return search_rows(rows, search)


def format_table(rows: List[Dict[str, Any]], columns: List[str] = TABLE_COLUMNS) -> str:
    """A text table. Cells are cleaned with ``printable`` so a name cannot break the layout
    or smuggle in terminal control characters."""
    cells = [{c: printable(r.get(c, "")) for c in columns} for r in rows]
    widths = {c: max([len(c)] + [len(r[c]) for r in cells]) for c in columns}

    def line(vals):
        return "  ".join(str(v).ljust(widths[c]) for c, v in zip(columns, vals, strict=True)).rstrip()

    out = [line(columns), line(["-" * widths[c] for c in columns])]
    out += [line([r[c] for c in columns]) for r in cells]
    return "\n".join(out)


def data_columns(kind: str = "all", offline: bool = False) -> List[str]:
    """The columns of ``--json`` and ``--csv`` for ``kind`` (the table shows fewer for some kinds)."""
    if kind == "ports":
        return PORT_COLUMNS
    if kind == "networks":
        return NETWORK_COLUMNS
    if kind == "wlans":
        return WLAN_COLUMNS
    if kind == "reservations":
        return OFFLINE_RESERVATION_COLUMNS if offline else RESERVATION_COLUMNS
    if kind == "devices":
        return INVENTORY_COLUMNS + DEVICE_EXTRA_COLUMNS
    if kind == "clients":
        return INVENTORY_COLUMNS + CLIENT_EXTRA_COLUMNS
    return INVENTORY_COLUMNS


def csv_cell(value: Any) -> Any:
    """A cell a spreadsheet cannot run as a formula. The text is cleaned first (control and invisible characters
    removed, line breaks flattened), then ``csv_safe`` looks at what is left: the other way round, the
    terminal-safety filter of ``say`` would strip an escape character from in front of a ``=`` after the check
    and leave a live formula. Numbers stay numbers."""
    return csv_safe(printable(value)) if isinstance(value, str) else value


def render_csv(rows: List[Dict[str, Any]], kind: str = "all", offline: bool = False) -> str:
    """The rows of ``--json`` as CSV: a header row, then one line per row, quoted by the stdlib writer."""
    columns = data_columns(kind, offline)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows([csv_cell(row.get(c, "")) for c in columns] for row in rows)
    return out.getvalue().rstrip("\n")


def render(rows: List[Dict[str, Any]], as_json: bool, kind: str = "all", offline: bool = False) -> str:
    if as_json:
        columns = data_columns(kind, offline)
        return json.dumps([{c: r.get(c, "") for c in columns} for r in rows], indent=2)
    columns = {
        "reservations": OFFLINE_RESERVATION_COLUMNS if offline else RESERVATION_COLUMNS,
        "ports": PORT_TABLE_COLUMNS,
        "networks": NETWORK_COLUMNS,
        "wlans": WLAN_COLUMNS,
        "devices": TABLE_COLUMNS + DEVICE_EXTRA_COLUMNS[:3],
        "clients": TABLE_COLUMNS + CLIENT_EXTRA_COLUMNS,
    }.get(kind, TABLE_COLUMNS)
    return format_table(rows, columns) + f"\n\n{len(rows)} row(s)"
