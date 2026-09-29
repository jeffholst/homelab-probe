"""CSV export of clients, UniFi devices and per-switch port maps."""

import csv
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .client import UniFiAPIError, UniFiClient

INVENTORY_COLUMNS = [
    "Type", "Name", "MAC Address", "IP Address", "Model", "Connection Type",
    "Switch", "Port", "Last Seen", "Status",
]

# Legacy device type codes (used for switch port tables).
DEVICE_TYPE_MAP = {
    "usw": "Switch",
    "uap": "Access Point",
    "ugw": "Gateway",
    "udm": "Dream Machine",
    "uxg": "Gateway",
    "ucg": "Gateway",
    "ubb": "Building Bridge",
    "ulte": "LTE",
    "pdu": "PDU",
}


def _mac(value: Optional[str]) -> str:
    return (value or "").upper()


def _fmt_time(value: Optional[str]) -> str:
    """Format an ISO-8601 timestamp from the Integration API."""
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except ValueError:
        return value


def _warn(msg: str) -> None:
    print(f"Warning: {msg}", file=sys.stderr)


def _write_csv(path: Path, columns: List[str], rows: List[Dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _legacy_or_empty(client: UniFiClient, site_ref: str, resource: str) -> List[Dict[str, Any]]:
    """Legacy data supplies switch/port mapping; failure degrades gracefully."""
    try:
        return client.legacy_stat(site_ref, resource)
    except UniFiAPIError as e:
        _warn(f"legacy stat/{resource} unavailable, port mapping will be incomplete: {e}")
        return []


def build_inventory(
    devices: List[Dict[str, Any]],
    clients: List[Dict[str, Any]],
    legacy_devices: List[Dict[str, Any]],
    legacy_clients: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    names_by_mac = {_mac(d.get("macAddress")): d.get("name") or d.get("macAddress") for d in devices}
    names_by_id = {d.get("id"): d.get("name") or d.get("macAddress") for d in devices}
    legacy_client_by_mac = {_mac(c.get("mac")): c for c in legacy_clients}
    legacy_device_by_mac = {_mac(d.get("mac")): d for d in legacy_devices}

    rows: List[Dict[str, Any]] = []

    for c in clients:
        mac = _mac(c.get("macAddress"))
        wired = c.get("type") == "WIRED"
        switch = names_by_id.get(c.get("uplinkDeviceId"), "")
        port = ""
        legacy = legacy_client_by_mac.get(mac)
        if wired and legacy:
            switch = names_by_mac.get(_mac(legacy.get("sw_mac")), switch)
            if legacy.get("sw_port") is not None:
                port = str(legacy["sw_port"])
        rows.append({
            "Type": "Client",
            "Name": c.get("name") or "Unknown",
            "MAC Address": mac,
            "IP Address": c.get("ipAddress", ""),
            "Model": "",
            "Connection Type": "Wired" if wired else "Wireless",
            "Switch": switch if wired else "",
            "Port": port,
            "Last Seen": _fmt_time(c.get("connectedAt")),
            # The clients endpoint lists only currently connected clients.
            "Status": "Online",
        })

    for d in devices:
        mac = _mac(d.get("macAddress"))
        switch, port = "", ""
        uplink = (legacy_device_by_mac.get(mac) or {}).get("uplink") or {}
        if uplink.get("uplink_mac"):
            switch = names_by_mac.get(_mac(uplink["uplink_mac"]), _mac(uplink["uplink_mac"]))
        if uplink.get("uplink_remote_port") is not None:
            port = str(uplink["uplink_remote_port"])
        legacy_type = (legacy_device_by_mac.get(mac) or {}).get("type", "")
        device_type = d.get("type") or legacy_type
        friendly = DEVICE_TYPE_MAP.get(device_type.lower()) or device_type.upper() or "Unknown"
        rows.append({
            "Type": f"Device - {friendly}",
            "Name": d.get("name") or "Unknown",
            "MAC Address": mac,
            "IP Address": d.get("ipAddress", ""),
            "Model": d.get("model", ""),
            "Connection Type": "Wired",
            "Switch": switch,
            "Port": port,
            "Last Seen": "",
            "Status": "Online" if d.get("state") == "ONLINE" else "Offline",
        })
    return rows


def build_offline_clients(
    known_clients: List[Dict[str, Any]],
    devices: List[Dict[str, Any]],
    all_users: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Rows for previously seen clients that are not currently connected.

    ``all_users`` comes from legacy ``stat/alluser``, which lists every client
    the controller has ever seen.
    """
    connected = {_mac(c.get("macAddress")) for c in known_clients}
    device_macs = {_mac(d.get("macAddress")) for d in devices}
    rows: List[Dict[str, Any]] = []
    for u in all_users:
        mac = _mac(u.get("mac"))
        if not mac or mac in connected or mac in device_macs:
            continue
        last_seen = u.get("last_seen")
        rows.append({
            "Type": "Client",
            "Name": u.get("name") or u.get("hostname") or "Unknown",
            "MAC Address": mac,
            "IP Address": u.get("last_ip") or u.get("ip") or "",
            "Model": "",
            "Connection Type": "Wired" if u.get("is_wired") else "Wireless",
            "Switch": (u.get("last_uplink_name") or "") if u.get("is_wired") else "",
            "Port": (
                str(u["last_uplink_remote_port"])
                if u.get("is_wired") and u.get("last_uplink_remote_port") is not None
                else ""
            ),
            "Last Seen": (
                datetime.fromtimestamp(last_seen).strftime("%Y-%m-%d %H:%M:%S")
                if last_seen
                else ""
            ),
            "Status": "Offline",
        })
    return rows


def build_switch_ports(
    legacy_devices: List[Dict[str, Any]], legacy_clients: List[Dict[str, Any]]
) -> Dict[str, List[Dict[str, Any]]]:
    """Return {switch name: port rows}, indexed once instead of scanned per port."""
    client_by_port: Dict[Tuple[str, Any], Dict[str, Any]] = {}
    for c in legacy_clients:
        if c.get("sw_mac") and c.get("sw_port") is not None:
            client_by_port.setdefault((_mac(c["sw_mac"]), c["sw_port"]), c)

    device_by_uplink: Dict[Tuple[str, Any], Dict[str, Any]] = {}
    for dev in legacy_devices:
        up = dev.get("uplink") or {}
        if up.get("uplink_mac") and up.get("uplink_remote_port") is not None:
            device_by_uplink.setdefault((_mac(up["uplink_mac"]), up["uplink_remote_port"]), dev)

    result: Dict[str, List[Dict[str, Any]]] = {}
    for sw in legacy_devices:
        if sw.get("type") != "usw" or not sw.get("port_table"):
            continue
        sw_mac = _mac(sw.get("mac"))
        rows = []
        for port in sw["port_table"]:
            key = (sw_mac, port.get("port_idx"))
            ctype = cname = cmac = cmodel = ""
            # mac_table_count is null on some models (e.g. USW Ultra), so match on link state.
            if port.get("up"):
                if key in client_by_port:
                    c = client_by_port[key]
                    ctype, cname, cmac = "Client", c.get("name") or c.get("hostname") or "", _mac(c.get("mac"))
                elif key in device_by_uplink:
                    dev = device_by_uplink[key]
                    friendly = DEVICE_TYPE_MAP.get(dev.get("type", ""), dev.get("type", "").upper())
                    ctype = f"Device - {friendly}"
                    cname = dev.get("name") or dev.get("hostname") or ""
                    cmac, cmodel = _mac(dev.get("mac")), dev.get("model", "")
            rows.append({
                "Port": port.get("name") or f"Port {port.get('port_idx', '')}",
                "Port Index": port.get("port_idx", ""),
                "Status": "Up" if port.get("up") else "Down",
                "Speed": f"{port['speed']} Mbps" if port.get("speed") else "",
                "Full Duplex": "Yes" if port.get("full_duplex") else "No",
                "PoE Enabled": "Yes" if port.get("poe_enable") else "No",
                "PoE Power (W)": port.get("poe_power", ""),
                "PoE Class": port.get("poe_class", ""),
                "Connected Type": ctype,
                "Connected Name": cname,
                "Connected MAC": cmac,
                "Connected Model": cmodel,
                "RX Bytes": port.get("rx_bytes", ""),
                "TX Bytes": port.get("tx_bytes", ""),
                "RX Packets": port.get("rx_packets", ""),
                "TX Packets": port.get("tx_packets", ""),
                "RX Errors": port.get("rx_errors", ""),
                "TX Errors": port.get("tx_errors", ""),
            })
        result[sw.get("name") or sw.get("hostname") or sw_mac] = rows
    return result


def run_export(
    client: UniFiClient, site: str, output_dir: Path, include_offline: bool = False
) -> None:
    site_info = client.resolve_site(site)
    site_id = site_info["id"]
    site_ref = site_info.get("internalReference") or site
    print(f"Site: {site_info.get('name')} ({site_id})")

    devices = client.devices(site_id)
    clients = client.clients(site_id)
    print(f"Found {len(devices)} device(s), {len(clients)} connected client(s)")

    legacy_devices = _legacy_or_empty(client, site_ref, "device")
    legacy_clients = _legacy_or_empty(client, site_ref, "sta")

    output_dir.mkdir(parents=True, exist_ok=True)

    rows = build_inventory(devices, clients, legacy_devices, legacy_clients)
    if include_offline:
        all_users = _legacy_or_empty(client, site_ref, "alluser")
        rows += build_offline_clients(clients, devices, all_users)
    inventory_path = output_dir / "unifi_clients.csv"
    _write_csv(inventory_path, INVENTORY_COLUMNS, rows)
    print(f"Wrote {len(rows)} entries -> {inventory_path}")

    for name, port_rows in build_switch_ports(legacy_devices, legacy_clients).items():
        safe = re.sub(r"[^\w \-]", "_", name)
        path = output_dir / f"switch_{safe}.csv"
        _write_csv(path, list(port_rows[0].keys()), port_rows)
        print(f"   -> {path} ({len(port_rows)} ports)")
