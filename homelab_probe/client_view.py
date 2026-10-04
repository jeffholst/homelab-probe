"""Single-client troubleshooting view: `hlp client <name|mac|ip>`."""

import ipaddress
import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from .diagnose import BANDS, Finding, apply_ignores, diagnose, format_findings
from .events import describe_duration, event_json, make_filter
from .events import subjects as event_subjects
from .export import device_type_label
from .query import format_table
from .reservations import reservation_records
from .settings import DiagnoseSettings
from .snapshot import Snapshot
from .util import clean_data, epoch_text, format_time, hex_digits, is_randomized_mac, normalize_mac, number, printable

JSON_VERSION = 1          # the format of `client --json`; it changes only when a field is removed or renamed

MAX_CLIENT_EVENTS = 10      # the client's own events listed before "... and N more"
MAX_DEVICE_EVENTS = 5       # events about the devices it depends on
CANDIDATE_COLUMNS = ["Name", "MAC Address", "IP Address", "Status"]
MAX_CANDIDATES_SHOWN = 20


# -- finding the client ----------------------------------------------------

def known_clients(snap: Snapshot) -> List[Dict[str, Any]]:
    """Every client the controller knows, merged by MAC from the connected-client list
    (Integration API), ``stat/sta`` and the full history (``stat/alluser``)."""
    device_macs = {normalize_mac(d.get("macAddress")) for d in snap.devices}
    by_mac: Dict[str, Dict[str, Any]] = {}

    def entry(mac: str) -> Dict[str, Any]:
        return by_mac.setdefault(mac, {"mac": mac, "live": None, "sta": None, "user": None})

    for key, records, mac_key in (("user", snap.all_users, "mac"),
                                  ("sta", snap.legacy_clients, "mac"),
                                  ("live", snap.clients, "macAddress")):
        for record in records:
            mac = normalize_mac(record.get(mac_key))
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


def find_clients(snap: Snapshot, query: str) -> List[Dict[str, Any]]:
    """Clients matching ``query``. Precedence: exact MAC (any separator or case), exact IP,
    a single exact name, then a case-insensitive substring of a name or hostname (or a
    MAC fragment of six or more hex digits). More than one result means it is ambiguous."""
    q = query.strip()
    records = known_clients(snap)
    hexq = hex_digits(q)
    is_hex = bool(re.fullmatch(r"[0-9a-f]+", hexq))

    if is_hex and len(hexq) == 12:
        exact = [r for r in records if hex_digits(r["mac"]) == hexq]
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
               or (is_hex and len(hexq) >= 6 and hexq in hex_digits(r["mac"]))]
    return sorted(matches, key=lambda r: (r["name"].lower(), r["mac"]))


def candidate_rows(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"Name": r["name"], "MAC Address": r["mac"], "IP Address": r["ip"],
             "Status": "Online" if r["online"] else "Offline"} for r in matches]


# -- building the view -----------------------------------------------------

class DeviceIndex:
    """Name, type and state lookups over the snapshot's devices, keyed by upper-case MAC."""

    def __init__(self, snap: Snapshot):
        self.legacy = {normalize_mac(d.get("mac")): d for d in snap.legacy_devices}
        self.integration = {normalize_mac(d.get("macAddress")): d for d in snap.devices}
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
        if not integ:
            return False
        return integ.get("state") != "ONLINE"


def _hop(net: DeviceIndex, mac: str, port: Any = None, speed: Any = None, detail: str = "") -> Dict[str, Any]:
    return {"id": (net.integration.get(mac) or {}).get("id"), "device": net.name(mac),
            "type": net.kind(mac), "port": port,
            "speed_mbps": number(speed) or None, "detail": detail, "offline": net.offline(mac)}


def _uplink_chain(net: DeviceIndex, start_mac: str, subjects: Set[str]) -> List[Dict[str, Any]]:
    """Hops from ``start_mac`` up to the gateway: each parent, the parent's port the link
    plugs into, and the negotiated speed. Also records finding subjects on the path."""
    hops: List[Dict[str, Any]] = []
    seen = {start_mac}
    current = start_mac
    while True:
        up = (net.legacy.get(current) or {}).get("uplink") or {}
        parent = normalize_mac(up.get("uplink_mac"))
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
        ap_mac = normalize_mac(sta.get("ap_mac"))
        if not ap_mac and live:
            ap_mac = next((m for m, d in net.integration.items()
                           if d.get("id") == live.get("uplinkDeviceId")), "")
        band = BANDS.get(str(sta.get("radio") or ""), "")
        ssid = sta.get("essid") or ""
        detail = ", ".join(x for x in (band, f"channel {sta['channel']}" if sta.get("channel") else "",
                                       f"SSID {ssid}" if ssid else "") if x)
        hops: List[Dict[str, Any]] = []
        if ap_mac:
            hops.append(_hop(net, ap_mac, detail=detail))
            subjects.update({net.name(ap_mac), f"{net.name(ap_mac)} {band} radio"})
            hops += _uplink_chain(net, ap_mac, subjects)
        retries, attempts = number(sta.get("wifi_tx_retries_percentage")), number(sta.get("wifi_tx_attempts"))
        satisfaction = number(sta.get("satisfaction"))
        if satisfaction is not None and satisfaction < 0:
            satisfaction = None
        link: Optional[Dict[str, Any]] = {"kind": "wireless", "band": band, "channel": sta.get("channel"), "ssid": ssid,
                "signal_dbm": number(sta.get("signal")), "noise_dbm": number(sta.get("noise")),
                "tx_rate_mbps": (number(sta.get("tx_rate")) or 0) / 1000 or None,
                "rx_rate_mbps": (number(sta.get("rx_rate")) or 0) / 1000 or None,
                "retries_pct": retries, "tx_attempts": attempts,
                "satisfaction": satisfaction}
        return hops, link, subjects

    sw_mac, port_idx = normalize_mac(sta.get("sw_mac")), sta.get("sw_port")
    if rec["online"]:
        if not sw_mac and live:
            sw_mac = next((m for m, d in net.integration.items()
                           if d.get("id") == live.get("uplinkDeviceId")), "")
    else:  # offline: the last uplink the controller recorded
        sw_mac = normalize_mac(user.get("last_uplink_mac")) or net.by_name.get(
            (user.get("last_uplink_name") or "").lower(), "")
        port_idx = user.get("last_uplink_remote_port")
    if not sw_mac:
        return [], None, subjects

    port: Dict[str, Any] = next((p for p in (net.legacy.get(sw_mac) or {}).get("port_table") or []
                                 if p.get("port_idx") == port_idx), {}) if rec["online"] else {}
    hops = [_hop(net, sw_mac, port_idx, port.get("speed"), "last seen here" if not rec["online"] else "")]
    subjects.add(net.name(sw_mac))
    if port_idx is not None:
        subjects.add(f"{net.name(sw_mac)} port {port_idx}")
    hops += _uplink_chain(net, sw_mac, subjects)

    link = None
    if rec["online"] and port:
        link = {"kind": "wired", "speed_mbps": number(port.get("speed")) or None,
                "full_duplex": port.get("full_duplex"),
                "rx_errors": int(number(port.get("rx_errors")) or 0),
                "tx_errors": int(number(port.get("tx_errors")) or 0),
                "rx_dropped": int(number(port.get("rx_dropped")) or 0),
                "tx_dropped": int(number(port.get("tx_dropped")) or 0)}
    return hops, link, subjects


class AddressingIndex:
    """The lookups ``addressing`` needs, built once per snapshot: each MAC's reservation, the networks by id and
    the client group names. A caller that describes many clients (``history.capture``) builds one and passes it
    in; rebuilding them per client made the work quadratic."""

    def __init__(self, snap: Snapshot) -> None:
        self.reservations: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
        for u, net in reservation_records(snap):
            self.reservations.setdefault(normalize_mac(u.get("mac")), (u, net))      # the first one, as before
        self.networks = {n.get("_id"): n for n in snap.networks}
        self.group_names: Optional[Dict[Any, Any]] = (
            None if snap.client_groups is None else {g.get("id"): g.get("name") for g in snap.client_groups})


def addressing(snap: Snapshot, rec: Dict[str, Any], index: Optional[AddressingIndex] = None) -> Dict[str, Any]:
    index = index or AddressingIndex(snap)
    sta, user = rec["sta"] or {}, rec["user"] or {}
    mac = rec["mac"]

    reservation = None
    if mac in index.reservations:
        u, net = index.reservations[mac]
        current = rec["ip"] if rec["online"] else ""
        reservation = {"reserved_ip": u["fixed_ip"], "network": net.get("name") or "",
                       "matches_current": (current == u["fixed_ip"]) if current else None}

    networks = index.networks
    # Same rule as reservation_records: a network override wins over the last connection.
    override = user.get("virtual_network_override_id") if user.get("virtual_network_override_enabled") else None
    net_id = sta.get("network_id") or override or user.get("last_connection_network_id")
    net = networks.get(net_id) or {}
    vlan = sta.get("vlan")
    if vlan is None and net:
        vlan = net.get("vlan") if net.get("vlan_enabled") else 1
    network = sta.get("network") or net.get("name") or user.get("last_connection_network_name") or ""

    ids = user.get("network_members_group_ids") or sta.get("network_members_group_ids") or []
    if index.group_names is None:
        groups, unresolved = [], len(ids)
    else:
        groups = [index.group_names[i] for i in ids if i in index.group_names]
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
                      or (mac and normalize_mac(f.subject) == mac) or f.subject in ips)
        by_message = any(len(alias) >= 3 and alias in f.message.lower() for alias in names)
        if by_subject or by_message:
            related.append(f)
    return related


def _recent_events(snap: Snapshot, rec: Dict[str, Any], hops: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The client's recent events, and events about the devices it depends on.

    Matches the client by MAC (an event's CLIENT id), so a similar name never mixes
    clients up. Device events are the device-state events (category UNIFI_DEVICES: a device
    going unreachable or reconnecting) with no CLIENT in them, so never another client's
    connect on the same access point, and never internet-latency events, which also name the
    gateway but say nothing about why a client dropped. A device is matched by its exact
    name. Device events use stable IDs when available, then exact names as a fallback.
    ``available`` is None when events were not requested.
    """
    requested = snap.event_window_seconds > 0
    result: Dict[str, Any] = {
        "available": snap.events_available if requested else None,
        "window": describe_duration(snap.event_window_seconds) if requested else "",
        "truncated": snap.events_truncated if requested and snap.events_available else None,
        "client": [], "client_more": 0, "devices": [], "devices_more": 0}
    if not (requested and snap.events_available):
        return result

    mine = make_filter(client=rec["mac"])
    assert mine is not None                      # a client was given, so there is a filter
    own = [e for e in snap.events if mine(e)]
    path = [(h.get("id"), h["device"].lower()) for h in hops]

    def on_path(device: Dict[str, Any]) -> bool:
        name = str(device.get("name") or "").lower()
        device_id = device.get("id")
        return any((device_id == path_id if device_id not in (None, "") and path_id not in (None, "")
                    else name == path_name)
                   for path_id, path_name in path)

    about = [e for e in snap.events if e.get("category") == "UNIFI_DEVICES"
             and not event_subjects(e, "CLIENT") and
             any(on_path(d) for d in event_subjects(e, "DEVICE"))]
    result.update(client=own[:MAX_CLIENT_EVENTS], client_more=max(0, len(own) - MAX_CLIENT_EVENTS),
                  devices=about[:MAX_DEVICE_EVENTS], devices_more=max(0, len(about) - MAX_DEVICE_EVENTS))
    return result


def build_client_detail(snap: Snapshot, rec: Dict[str, Any],
                        settings: Optional[DiagnoseSettings] = None) -> Dict[str, Any]:
    settings = settings or DiagnoseSettings()
    net = DeviceIndex(snap)
    live, sta, user = rec["live"], rec["sta"] or {}, rec["user"] or {}
    hops, link, path_subjects = _attachment(snap, net, rec)
    kept, ignored = apply_ignores(diagnose(snap, settings), settings.ignore)

    if rec["online"]:
        last_seen = "connected now"
        since = format_time((live or {}).get("connectedAt"))
    else:
        last_seen, since = epoch_text(user.get("last_seen")), ""

    recent = _recent_events(snap, rec, hops)
    return {
        "identity": {"name": rec["name"], "hostname": sta.get("hostname") or user.get("hostname") or "",
                     "mac": rec["mac"], "private_mac": is_randomized_mac(rec["mac"]),
                     "vendor": user.get("oui") or sta.get("oui") or "",
                     "ip": rec["ip"], "connection": "Wired" if rec["wired"] else "Wireless",
                     "status": "Online" if rec["online"] else "Offline",
                     "connected_since": since, "first_seen": epoch_text(user.get("first_seen")),
                     "last_seen": last_seen},
        "addressing": addressing(snap, rec),
        "attachment": hops,
        "link": link,
        "findings": [{"severity": f.severity, "code": f.code, "subject": f.subject, "message": f.message}
                     for f in _related(kept, rec, path_subjects)],
        # null when events were not requested (--no-events), false when the log could not be read
        "events_available": recent["available"],
        "events_window": recent["window"],
        "events_truncated": recent["truncated"],
        "events": [event_json(e) for e in recent["client"]],
        "device_events": [event_json(e) for e in recent["devices"]],
        "events_omitted": {"client": recent["client_more"], "devices": recent["devices_more"]},
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


def _events_text(detail: Dict[str, Any], mac: str) -> List[str]:
    available = detail["events_available"]
    if available is None:                       # --no-events
        return []
    window = detail["events_window"]
    if not available:
        return ["", "Recent events: unavailable (the event log could not be read)"]

    def line(e: Dict[str, Any]) -> str:
        return f"  {e['Time']}  {e['Event']}: {e['Message']}"

    lines = ["", f"Recent events (last {window}, newest first):"]
    if detail["events"]:
        lines += [line(e) for e in detail["events"]]
    elif detail["events_truncated"]:
        lines.append("  no matching events found before the 20,000-event read cap")
    else:
        lines.append(f"  none about this client in the last {window}")
    more = detail["events_omitted"]["client"]
    if more:
        count = f"at least {more}" if detail["events_truncated"] else str(more)
        lines.append(f"  ... and {count} more; run: hlp events --client {mac} --since {window}")
    if detail["device_events"]:
        lines += ["", "Events about the devices it depends on:"]
        lines += [line(e) for e in detail["device_events"]]
        if detail["events_omitted"]["devices"]:
            more = detail["events_omitted"]["devices"]
            count = f"at least {more}" if detail["events_truncated"] else str(more)
            lines.append(f"  ... and {count} more")
    if detail["events_truncated"]:
        lines.append("  (the 20,000-event read cap was reached; omission counts are incomplete)")
    return lines


def render_detail(detail: Dict[str, Any], emoji: bool = True) -> str:
    detail = clean_data(detail)
    i, a = detail["identity"], detail["addressing"]
    lines = [f"{i['name']}" + (f" ({i['hostname']})" if i["hostname"] and i["hostname"] != i["name"] else ""),
             f"  MAC:        {i['mac']}" + (f"  ({i['vendor']})" if i["vendor"] else "")
             + ("  [randomized MAC: reservations and history may not hold]" if i.get("private_mac") else ""),
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

    lines += _events_text(detail, i["mac"])

    lines.append("")
    findings = [Finding(f["severity"], f["subject"], f["message"], code=f.get("code", "")) for f in detail["findings"]]
    lines.append("Related findings:" if findings else "No related findings.")
    if findings:
        lines.append(format_findings(findings, emoji))
    return "\n".join(lines)


def render_candidates(query: str, matches: List[Dict[str, Any]]) -> str:
    query = printable(query)
    if not matches:
        return f"No client matches '{query}'. Search by name, MAC address or IP address."
    shown = matches[:MAX_CANDIDATES_SHOWN]
    more = (f"\n... and {len(matches) - len(shown)} more" if len(matches) > len(shown) else "")
    return (f"{len(matches)} clients match '{query}'; use the MAC address (or a more specific "
            "name) to pick one:\n\n" + format_table(candidate_rows(shown), CANDIDATE_COLUMNS) + more)


def to_json(detail: Dict[str, Any]) -> str:
    return json.dumps({"version": JSON_VERSION, **detail}, indent=2)
