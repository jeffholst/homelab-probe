"""CSV export of clients, UniFi devices and per-switch port maps."""

import csv
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .snapshot import Snapshot
from .util import csv_safe, format_time, normalize_mac, printable, record_for

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


# Model prefixes, longest first, for when the legacy type code is unavailable.
# The Integration API `features` list cannot tell gateways from switches
# (a UCG Max reports only "switching"), so the model is checked first.
MODEL_PREFIX_TYPES = [
    ("UCG", "Gateway"), ("UXG", "Gateway"), ("UDM", "Dream Machine"),
    ("UDR", "Dream Machine"), ("UDW", "Dream Machine"), ("UX", "Dream Machine"),
    ("UBB", "Building Bridge"), ("ULTE", "LTE"), ("USP", "PDU"),
    ("USW", "Switch"), ("USL", "Switch"), ("USM", "Switch"), ("US-", "Switch"),
    ("USPM", "Switch"),
    ("UAP", "Access Point"), ("U6", "Access Point"), ("U7", "Access Point"),
    ("UWB", "Access Point"), ("E7", "Access Point"),
]
FEATURE_TYPES = {"accessPoint": "Access Point", "switching": "Switch"}


def device_type_label(device: Dict[str, Any], legacy_type: str = "") -> str:
    """Friendly device type: legacy type code, then model prefix, then features."""
    if legacy_type.lower() in DEVICE_TYPE_MAP:
        return DEVICE_TYPE_MAP[legacy_type.lower()]
    model = (device.get("model") or "").upper()
    for prefix, label in sorted(MODEL_PREFIX_TYPES, key=lambda x: -len(x[0])):
        if model.startswith(prefix):
            return label
    for feature in device.get("features") or []:
        if feature in FEATURE_TYPES:
            return FEATURE_TYPES[feature]
    return legacy_type.upper() or "Unknown"


def _write_csv(path: Path, columns: List[str], rows: List[Dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows({k: csv_safe(v) for k, v in row.items()} for row in rows)


def build_inventory(
    devices: List[Dict[str, Any]],
    clients: List[Dict[str, Any]],
    legacy_devices: List[Dict[str, Any]],
    legacy_clients: List[Dict[str, Any]],
    device_details: Optional[Dict[str, Dict[str, Any]]] = None,
    device_stats: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    device_details = device_details or {}
    device_stats = device_stats or {}
    names_by_mac = {normalize_mac(d.get("macAddress")): d.get("name") or d.get("macAddress") for d in devices}
    names_by_id = {d.get("id"): d.get("name") or d.get("macAddress") for d in devices}
    legacy_client_by_mac = {normalize_mac(c.get("mac")): c for c in legacy_clients}
    legacy_device_by_mac = {normalize_mac(d.get("mac")): d for d in legacy_devices}

    rows: List[Dict[str, Any]] = []

    for c in clients:
        mac = normalize_mac(c.get("macAddress"))
        wired = c.get("type") == "WIRED"
        switch = names_by_id.get(c.get("uplinkDeviceId"), "")
        port = ""
        legacy = legacy_client_by_mac.get(mac)
        if wired and legacy:
            switch = names_by_mac.get(normalize_mac(legacy.get("sw_mac")), switch)
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
            "Last Seen": format_time(c.get("connectedAt")),
            # The clients endpoint lists only currently connected clients.
            "Status": "Online",
        })

    for d in devices:
        mac = normalize_mac(d.get("macAddress"))
        switch, port = "", ""
        uplink = (legacy_device_by_mac.get(mac) or {}).get("uplink") or {}
        if uplink.get("uplink_mac"):
            switch = names_by_mac.get(normalize_mac(uplink["uplink_mac"]), normalize_mac(uplink["uplink_mac"]))
        if uplink.get("uplink_remote_port") is not None:
            port = str(uplink["uplink_remote_port"])
        if not switch:
            # Integration API uplink (no port number, but works without legacy data).
            parent_id = (record_for(device_details, d.get("id")).get("uplink") or {}).get("deviceId")
            switch = names_by_id.get(parent_id or "", "")
        legacy_type = (legacy_device_by_mac.get(mac) or {}).get("type", "")
        friendly = device_type_label(d, legacy_type)
        rows.append({
            "Type": f"Device - {friendly}",
            "Name": d.get("name") or "Unknown",
            "MAC Address": mac,
            "IP Address": d.get("ipAddress", ""),
            "Model": d.get("model", ""),
            "Connection Type": "Wired",
            "Switch": switch,
            "Port": port,
            "Last Seen": format_time(record_for(device_stats, d.get("id")).get("lastHeartbeatAt")),
            "Status": "Online" if d.get("state") == "ONLINE" else "Offline",
        })
    return rows


class LocationIndex:
    """Where clients attach, with the device names and the legacy client records indexed once.

    A caller that locates many clients builds one index and calls ``of`` for each; building the lookups
    per client made the analysis quadratic (8,000 clients took seconds). ``client_location`` is the
    one-off form.
    """

    def __init__(self, snap: Snapshot) -> None:
        self._names_by_id = {d.get("id"): d.get("name") or d.get("macAddress") for d in snap.devices}
        self._names_by_mac = {(d.get("macAddress") or "").upper(): d.get("name") or d.get("macAddress")
                              for d in snap.devices}
        self._legacy: Dict[str, Dict[str, Any]] = {}
        for c in snap.legacy_clients:
            self._legacy.setdefault((c.get("mac") or "").upper(), c)      # the first record of a MAC, as before

    def of(self, client: Dict[str, Any]) -> str:
        """Where a client attaches: 'Wired, Switch port 3' or 'Wireless, via AP'."""
        legacy = self._legacy.get((client.get("macAddress") or "").upper(), {})
        uplink = self._names_by_id.get(client.get("uplinkDeviceId"))
        if client.get("type") == "WIRED":
            switch = self._names_by_mac.get((legacy.get("sw_mac") or "").upper()) or uplink
            port = legacy.get("sw_port")
            if switch and port is not None:
                return f"Wired, {switch} port {port}"
            return f"Wired, {switch}" if switch else "Wired"
        ap = uplink or self._names_by_mac.get((legacy.get("ap_mac") or "").upper())
        return f"Wireless, via {ap}" if ap else "Wireless"


def client_location(snap: Snapshot, client: Dict[str, Any]) -> str:
    """Where one client attaches (builds the index; use ``LocationIndex`` to locate many)."""
    return LocationIndex(snap).of(client)


def build_offline_clients(
    known_clients: List[Dict[str, Any]],
    devices: List[Dict[str, Any]],
    all_users: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Rows for previously seen clients that are not currently connected.

    ``all_users`` comes from legacy ``stat/alluser``, which lists every client
    the controller has ever seen.
    """
    connected = {normalize_mac(c.get("macAddress")) for c in known_clients}
    device_macs = {normalize_mac(d.get("macAddress")) for d in devices}
    rows: List[Dict[str, Any]] = []
    for u in all_users:
        mac = normalize_mac(u.get("mac"))
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
) -> Dict[str, Tuple[str, List[Dict[str, Any]]]]:
    """Return {switch MAC: (switch name, port rows)}, indexed once per port."""
    client_by_port: Dict[Tuple[str, Any], Dict[str, Any]] = {}
    for c in legacy_clients:
        if c.get("sw_mac") and c.get("sw_port") is not None:
            client_by_port.setdefault((normalize_mac(c["sw_mac"]), c["sw_port"]), c)

    device_by_uplink: Dict[Tuple[str, Any], Dict[str, Any]] = {}
    for dev in legacy_devices:
        up = dev.get("uplink") or {}
        if up.get("uplink_mac") and up.get("uplink_remote_port") is not None:
            device_by_uplink.setdefault((normalize_mac(up["uplink_mac"]), up["uplink_remote_port"]), dev)

    device_by_mac = {normalize_mac(d.get("mac")): d for d in legacy_devices}

    result: Dict[str, Tuple[str, List[Dict[str, Any]]]] = {}
    for sw in legacy_devices:
        if sw.get("type") != "usw" or not sw.get("port_table"):
            continue
        sw_mac = normalize_mac(sw.get("mac"))
        rows = []
        for port in sw["port_table"]:
            key = (sw_mac, port.get("port_idx"))
            ctype = cname = cmac = cmodel = ""
            # mac_table_count is null on some models (e.g. USW Ultra), so match on link state.
            uplink_dev = device_by_mac.get(normalize_mac((sw.get("uplink") or {}).get("uplink_mac")))
            if port.get("up"):
                if port.get("is_uplink") and uplink_dev:
                    dev = uplink_dev
                    friendly = DEVICE_TYPE_MAP.get(dev.get("type", ""), dev.get("type", "").upper())
                    ctype = f"Device - {friendly}"
                    cname = dev.get("name") or dev.get("hostname") or ""
                    cmac, cmodel = normalize_mac(dev.get("mac")), dev.get("model", "")
                elif key in client_by_port:
                    c = client_by_port[key]
                    ctype, cname, cmac = "Client", c.get("name") or c.get("hostname") or "", normalize_mac(c.get("mac"))
                elif key in device_by_uplink:
                    dev = device_by_uplink[key]
                    friendly = DEVICE_TYPE_MAP.get(dev.get("type", ""), dev.get("type", "").upper())
                    ctype = f"Device - {friendly}"
                    cname = dev.get("name") or dev.get("hostname") or ""
                    cmac, cmodel = normalize_mac(dev.get("mac")), dev.get("model", "")
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
        name = sw.get("name") or sw.get("hostname") or sw_mac
        switch_id = sw_mac or f"switch-{len(result) + 1}"
        result[switch_id] = (name, rows)
    return result


def run_export(snap: Snapshot, output_dir: Path) -> None:
    """Write the inventory and per-switch CSVs. ``snap.all_users`` (if collected)
    adds previously seen, not-connected clients."""
    print(f"Site: {printable(snap.site.get('name'))} ({printable(snap.site.get('id'))})")
    print(f"Found {len(snap.devices)} device(s), {len(snap.clients)} connected client(s)")

    output_dir.mkdir(parents=True, exist_ok=True)

    rows = build_inventory(snap.devices, snap.clients, snap.legacy_devices, snap.legacy_clients,
                           snap.device_details, snap.device_stats)
    rows += build_offline_clients(snap.clients, snap.devices, snap.all_users)
    inventory_path = output_dir / "unifi_clients.csv"
    _write_csv(inventory_path, INVENTORY_COLUMNS, rows)
    print(f"Wrote {len(rows)} entries -> {inventory_path}")

    switches = build_switch_ports(snap.legacy_devices, snap.legacy_clients)
    name_counts = Counter(name for name, _ in switches.values())
    for mac, (name, port_rows) in switches.items():
        safe = re.sub(r"[^\w \-]", "_", name)
        if name_counts[name] > 1:
            safe += "_" + re.sub(r"[^\w \-]", "_", mac)
        path = output_dir / f"switch_{safe}.csv"
        _write_csv(path, list(port_rows[0].keys()), port_rows)
        print(f"   -> {path} ({len(port_rows)} ports)")
