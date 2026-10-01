"""Single-client troubleshooting view: `unifi-sentinel client <name|mac|ip>`."""

import ipaddress
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from .diagnose import (BANDS, Finding, apply_ignores, diagnose, format_findings)
from .export import _fmt_time, _mac, device_type_label
from .query import format_table
from .reservations import reservation_records
from .settings import DiagnoseSettings
from .snapshot import Snapshot

CANDIDATE_COLUMNS = ["Name", "MAC Address", "IP Address", "Status"]
MAX_CANDIDATES_SHOWN = 20


def _epoch(value: Any) -> str:
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S") if value else ""


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


# -- finding the client ----------------------------------------------------

def known_clients(snap: Snapshot) -> List[Dict[str, Any]]:
    """Every client the controller knows, merged by MAC from the connected-client list
    (Integration API), ``stat/sta`` and the full history (``stat/alluser``)."""
    device_macs = {_mac(d.get("macAddress")) for d in snap.devices}
    by_mac: Dict[str, Dict[str, Any]] = {}

    def entry(mac: str) -> Dict[str, Any]:
        return by_mac.setdefault(mac, {"mac": mac, "live": None, "sta": None, "user": None})

    for key, records, mac_key in (("user", snap.all_users, "mac"),
                                  ("sta", snap.legacy_clients, "mac"),
                                  ("live", snap.clients, "macAddress")):
        for record in records:
            mac = _mac(record.get(mac_key))
            if mac and mac not in device_macs:
                entry(mac)[key] = record

    result = []
    for e in by_mac.values():
        live, sta, user = e["live"], e["sta"] or {}, e["user"] or {}
        names = [n for n in ((live or {}).get("name"), sta.get("name"), sta.get("hostname"),
                             user.get("name"), user.get("hostname")) if n]
        wired = ((live or {}).get("type") == "WIRED" if live
                 else bool(sta.get("is_wired", user.get("is_wired"))))
        ips = {ip for ip in ((live or {}).get("ipAddress"), sta.get("ip"), user.get("last_ip")) if ip}
        e.update(names=names, name=names[0] if names else "Unknown", wired=wired, ips=ips,
                 ip=(live or {}).get("ipAddress") or sta.get("ip") or user.get("last_ip") or "",
                 online=bool(live or e["sta"]))
        result.append(e)
    return result


def _hex(text: str) -> str:
    return re.sub(r"[:\-.\s]", "", text).lower()


def find_clients(snap: Snapshot, query: str) -> List[Dict[str, Any]]:
    """Clients matching ``query``. Precedence: exact MAC (any separator or case), exact IP,
    a single exact name, then a case-insensitive substring of a name or hostname (or a
    MAC fragment of six or more hex digits). More than one result means it is ambiguous."""
    q = query.strip()
    records = known_clients(snap)
    hexq = _hex(q)
    is_hex = bool(re.fullmatch(r"[0-9a-f]+", hexq))

    if is_hex and len(hexq) == 12:
        exact = [r for r in records if _hex(r["mac"]) == hexq]
        if exact:
            return exact
    try:
        ip = str(ipaddress.ip_address(q))
    except ValueError:
        ip = ""
    if ip:
        exact = [r for r in records if ip in r["ips"]]
        if exact:
            return exact
    exact = [r for r in records if q.lower() in {n.lower() for n in r["names"]}]
    if len(exact) == 1:
        return exact

    needle = q.lower()
    matches = [r for r in records
               if (needle and any(needle in n.lower() for n in r["names"]))
               or (is_hex and len(hexq) >= 6 and hexq in _hex(r["mac"]))]
    return sorted(matches, key=lambda r: (r["name"].lower(), r["mac"]))


def candidate_rows(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"Name": r["name"], "MAC Address": r["mac"], "IP Address": r["ip"],
             "Status": "Online" if r["online"] else "Offline"} for r in matches]


# -- building the view -----------------------------------------------------

class DeviceIndex:
    """Name, type and state lookups over the snapshot's devices, keyed by upper-case MAC."""

    def __init__(self, snap: Snapshot):
        self.legacy = {(d.get("mac") or "").upper(): d for d in snap.legacy_devices}
        self.integration = {(d.get("macAddress") or "").upper(): d for d in snap.devices}
        self.by_id = {d.get("id"): d for d in snap.devices}
        self.by_name = {self.name(m).lower(): m for m in self.legacy}

    def name(self, mac: str) -> str:
        legacy = self.legacy.get(mac) or {}
        integ = self.integration.get(mac) or {}
        return legacy.get("name") or legacy.get("hostname") or integ.get("name") or mac

    def kind(self, mac: str) -> str:
        legacy, integ = self.legacy.get(mac) or {}, self.integration.get(mac) or {}
        return device_type_label(integ or {"model": legacy.get("model")}, legacy.get("type", ""))

    def offline(self, mac: str) -> bool:
        integ = self.integration.get(mac)
        return bool(integ) and integ.get("state") != "ONLINE"


def _hop(net: DeviceIndex, mac: str, port: Any = None, speed: Any = None, detail: str = "") -> Dict[str, Any]:
    return {"device": net.name(mac), "type": net.kind(mac), "port": port,
            "speed_mbps": _num(speed) or None, "detail": detail, "offline": net.offline(mac)}


def _uplink_chain(net: DeviceIndex, start_mac: str, subjects: Set[str]) -> List[Dict[str, Any]]:
    """Hops from ``start_mac`` up to the gateway: each parent, the parent's port the link
    plugs into, and the negotiated speed. Also records finding subjects on the path."""
    hops: List[Dict[str, Any]] = []
    seen = {start_mac}
    current = start_mac
    while True:
        up = (net.legacy.get(current) or {}).get("uplink") or {}
        parent = (up.get("uplink_mac") or "").upper()
        if not parent or parent in seen:
            return hops
        seen.add(parent)
        hops.append(_hop(net, parent, up.get("uplink_remote_port"), up.get("speed")))
        subjects.add(net.name(parent))
        if up.get("uplink_remote_port") is not None:
            subjects.add(f"{net.name(parent)} port {up['uplink_remote_port']}")
        if up.get("port_idx") is not None:  # the child's own uplink port
            subjects.add(f"{net.name(current)} port {up['port_idx']}")
        current = parent


def _attachment(snap: Snapshot, net: DeviceIndex, rec: Dict[str, Any]
                ) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], Set[str]]:
    """(hops, link quality, subjects of related findings) for the client."""
    live, sta, user = rec["live"], rec["sta"] or {}, rec["user"] or {}
    subjects: Set[str] = set()

    if rec["online"] and not rec["wired"]:
        ap_mac = (sta.get("ap_mac") or "").upper()
        if not ap_mac and live:
            ap_mac = next((m for m, d in net.integration.items()
                           if d.get("id") == live.get("uplinkDeviceId")), "")
        band = BANDS.get(sta.get("radio"), "")
        ssid = sta.get("essid") or ""
        detail = ", ".join(x for x in (band, f"channel {sta['channel']}" if sta.get("channel") else "",
                                       f"SSID {ssid}" if ssid else "") if x)
        hops: List[Dict[str, Any]] = []
        if ap_mac:
            hops.append(_hop(net, ap_mac, detail=detail))
            subjects.update({net.name(ap_mac), f"{net.name(ap_mac)} {band} radio"})
            hops += _uplink_chain(net, ap_mac, subjects)
        retries, attempts = _num(sta.get("wifi_tx_retries_percentage")), _num(sta.get("wifi_tx_attempts"))
        satisfaction = _num(sta.get("satisfaction"))
        if satisfaction is not None and satisfaction < 0:
            satisfaction = None
        link = {"kind": "wireless", "band": band, "channel": sta.get("channel"), "ssid": ssid,
                "signal_dbm": _num(sta.get("signal")), "noise_dbm": _num(sta.get("noise")),
                "tx_rate_mbps": (_num(sta.get("tx_rate")) or 0) / 1000 or None,
                "rx_rate_mbps": (_num(sta.get("rx_rate")) or 0) / 1000 or None,
                "retries_pct": retries, "tx_attempts": attempts,
                "satisfaction": satisfaction}
        return hops, link, subjects

    sw_mac, port_idx = (sta.get("sw_mac") or "").upper(), sta.get("sw_port")
    if rec["online"]:
        if not sw_mac and live:
            sw_mac = next((m for m, d in net.integration.items()
                           if d.get("id") == live.get("uplinkDeviceId")), "")
    else:  # offline: the last uplink the controller recorded
        sw_mac = (user.get("last_uplink_mac") or "").upper() or net.by_name.get(
            (user.get("last_uplink_name") or "").lower(), "")
        port_idx = user.get("last_uplink_remote_port")
    if not sw_mac:
        return [], None, subjects

    port = next((p for p in (net.legacy.get(sw_mac) or {}).get("port_table") or []
                 if p.get("port_idx") == port_idx), {}) if rec["online"] else {}
    hops = [_hop(net, sw_mac, port_idx, port.get("speed"), "last seen here" if not rec["online"] else "")]
    subjects.add(net.name(sw_mac))
    if port_idx is not None:
        subjects.add(f"{net.name(sw_mac)} port {port_idx}")
    hops += _uplink_chain(net, sw_mac, subjects)

    link = None
    if rec["online"] and port:
        link = {"kind": "wired", "speed_mbps": _num(port.get("speed")) or None,
                "full_duplex": port.get("full_duplex"),
                "rx_errors": int(_num(port.get("rx_errors")) or 0),
                "tx_errors": int(_num(port.get("tx_errors")) or 0),
                "rx_dropped": int(_num(port.get("rx_dropped")) or 0),
                "tx_dropped": int(_num(port.get("tx_dropped")) or 0)}
    return hops, link, subjects


def addressing(snap: Snapshot, rec: Dict[str, Any]) -> Dict[str, Any]:
    sta, user = rec["sta"] or {}, rec["user"] or {}
    mac = rec["mac"]

    reservation = None
    for u, net in reservation_records(snap):
        if _mac(u.get("mac")) == mac:
            current = rec["ip"] if rec["online"] else ""
            reservation = {"reserved_ip": u["fixed_ip"], "network": net.get("name") or "",
                           "matches_current": (current == u["fixed_ip"]) if current else None}
            break

    networks = {n.get("_id"): n for n in snap.networks}
    # Same rule as reservation_records: a network override wins over the last connection.
    override = user.get("virtual_network_override_id") if user.get("virtual_network_override_enabled") else None
    net_id = sta.get("network_id") or override or user.get("last_connection_network_id")
    net = networks.get(net_id) or {}
    vlan = sta.get("vlan")
    if vlan is None and net:
        vlan = net.get("vlan") if net.get("vlan_enabled") else 1
    network = sta.get("network") or net.get("name") or user.get("last_connection_network_name") or ""

    ids = user.get("network_members_group_ids") or sta.get("network_members_group_ids") or []
    if snap.client_groups is None:
        groups, unresolved = [], len(ids)
    else:
        names = {g.get("id"): g.get("name") for g in snap.client_groups}
        groups = [names[i] for i in ids if i in names]
        unresolved = 0
    return {"network": network, "vlan": vlan, "reservation": reservation, "groups": groups,
            "group_ids_unresolved": unresolved, "ungrouped": not groups and not unresolved}


def _related(findings: List[Finding], rec: Dict[str, Any], subjects: Set[str]) -> List[Finding]:
    """Findings that concern this client, its IP, or the devices and ports it depends on."""
    lowered = {s.lower() for s in subjects}
    names = {name.lower() for name in rec["names"]}
    mac, ips = rec["mac"], rec["ips"]
    related = []
    for f in findings:
        subject = f.subject.lower()
        by_subject = (subject in lowered or subject in names
                      or (mac and _mac(f.subject) == mac) or f.subject in ips)
        by_message = any(len(alias) >= 3 and alias in f.message.lower() for alias in names)
        if by_subject or by_message:
            related.append(f)
    return related


def build_client_detail(snap: Snapshot, rec: Dict[str, Any],
                        settings: Optional[DiagnoseSettings] = None) -> Dict[str, Any]:
    settings = settings or DiagnoseSettings()
    net = DeviceIndex(snap)
    live, sta, user = rec["live"], rec["sta"] or {}, rec["user"] or {}
    hops, link, subjects = _attachment(snap, net, rec)
    kept, ignored = apply_ignores(diagnose(snap, settings), settings.ignore)

    if rec["online"]:
        last_seen = "connected now"
        since = _fmt_time((live or {}).get("connectedAt"))
    else:
        last_seen, since = _epoch(user.get("last_seen")), ""

    return {
        "identity": {"name": rec["name"], "hostname": sta.get("hostname") or user.get("hostname") or "",
                     "mac": rec["mac"], "vendor": user.get("oui") or sta.get("oui") or "",
                     "ip": rec["ip"], "connection": "Wired" if rec["wired"] else "Wireless",
                     "status": "Online" if rec["online"] else "Offline",
                     "connected_since": since, "first_seen": _epoch(user.get("first_seen")),
                     "last_seen": last_seen},
        "addressing": addressing(snap, rec),
        "attachment": hops,
        "link": link,
        "findings": [{"severity": f.severity, "subject": f.subject, "message": f.message}
                     for f in _related(kept, rec, subjects)],
    }


# -- rendering -------------------------------------------------------------

def _chain_text(client_name: str, hops: List[Dict[str, Any]]) -> str:
    parts = [client_name]
    for h in hops:
        text = h["device"]
        if h["port"] is not None:
            text += f" port {h['port']}"
        extra = [f"{h['speed_mbps']:.0f} Mbps"] if h["speed_mbps"] else []
        if h["detail"]:
            extra.append(h["detail"])
        if h["offline"]:
            extra.append("OFFLINE")
        parts.append(text + (f" ({', '.join(extra)})" if extra else ""))
    return " -> ".join(parts)


def _link_text(link: Dict[str, Any]) -> str:
    if link["kind"] == "wired":
        duplex = {True: "full duplex", False: "half duplex"}.get(link["full_duplex"], "duplex unknown")
        speed = f"{link['speed_mbps']:.0f} Mbps" if link["speed_mbps"] else "speed unknown"
        errors = link["rx_errors"] + link["tx_errors"]
        dropped = link["rx_dropped"] + link["tx_dropped"]
        return f"{speed}, {duplex}, {errors} errors, {dropped} dropped packets on its port"
    bits = []
    if link["signal_dbm"] is not None:
        bits.append(f"signal {link['signal_dbm']:.0f} dBm")
    if link["noise_dbm"] is not None:
        bits.append(f"noise {link['noise_dbm']:.0f} dBm")
    if link["tx_rate_mbps"] and link["rx_rate_mbps"]:
        bits.append(f"{link['tx_rate_mbps']:.0f}/{link['rx_rate_mbps']:.0f} Mbps tx/rx")
    if link["retries_pct"] is not None:
        bits.append(f"{link['retries_pct']:.0f}% retried")
    if link["satisfaction"] is not None:
        bits.append(f"satisfaction {link['satisfaction']:.0f}%")
    return ", ".join(bits) or "no Wi-Fi quality data reported"


def render_detail(detail: Dict[str, Any], emoji: bool = True) -> str:
    i, a = detail["identity"], detail["addressing"]
    lines = [f"{i['name']}" + (f" ({i['hostname']})" if i["hostname"] and i["hostname"] != i["name"] else ""),
             f"  MAC:        {i['mac']}" + (f"  ({i['vendor']})" if i["vendor"] else ""),
             f"  Status:     {i['status']}" + (f", connected since {i['connected_since']}"
                                               if i["connected_since"] else ""),
             f"  Connection: {i['connection']}"]

    ip = f"  IP:         {i['ip'] or 'none'}"
    r = a["reservation"]
    if r:
        match = {True: "matches", False: "DIFFERS from the current IP", None: "client offline"}[r["matches_current"]]
        ip += f"  (reserved {r['reserved_ip']}, {match})"
    else:
        ip += "  (no DHCP reservation)"
    lines.append(ip)

    network = a["network"] or "unknown"
    lines.append(f"  Network:    {network}" + (f" (VLAN {a['vlan']:g})" if isinstance(a["vlan"], (int, float)) else ""))
    if a["groups"]:
        lines.append(f"  Groups:     {', '.join(a['groups'])}")
    elif a["group_ids_unresolved"]:
        lines.append(f"  Groups:     {a['group_ids_unresolved']} (names unavailable)")
    else:
        lines.append("  Groups:     none (not in any client group)")
    lines.append(f"  First seen: {i['first_seen'] or 'unknown'}")
    lines.append(f"  Last seen:  {i['last_seen'] or 'unknown'}")

    lines.append("")
    lines.append("Attached: " + (_chain_text(i["name"], detail["attachment"])
                                 if detail["attachment"] else "unknown (no uplink data)"))
    if detail["link"]:
        lines.append("Link:     " + _link_text(detail["link"]))

    lines.append("")
    findings = [Finding(f["severity"], f["subject"], f["message"]) for f in detail["findings"]]
    lines.append("Related findings:" if findings else "No related findings.")
    if findings:
        lines.append(format_findings(findings, emoji))
    return "\n".join(lines)


def render_candidates(query: str, matches: List[Dict[str, Any]]) -> str:
    if not matches:
        return f"No client matches '{query}'. Search by name, MAC address or IP address."
    shown = matches[:MAX_CANDIDATES_SHOWN]
    more = (f"\n... and {len(matches) - len(shown)} more" if len(matches) > len(shown) else "")
    return (f"{len(matches)} clients match '{query}'; use the MAC address (or a more specific "
            "name) to pick one:\n\n" + format_table(candidate_rows(shown), CANDIDATE_COLUMNS) + more)


def to_json(detail: Dict[str, Any]) -> str:
    return json.dumps(detail, indent=2)
