"""Firewall view: `hlp firewall`.

Answers "what does my firewall allow, and what is reachable from the internet?" from data the controller
keeps (all read with GET): the zone-based firewall's policies, zones and zone matrix (legacy v2 endpoints; the
Integration API lists fewer policies and no ports or hit counts) and the port forwards (legacy ``rest/portforward``).

Checked against one controller on Network 10.6.106 that uses the zone-based firewall: policies, zones and the
matrix are as read here, and it has no port forwards, so the port forward fields are the legacy ones and are not
verified against live data. The classic firewall (rules and groups) is not shown: its endpoints answered with
nothing on that controller, so there was nothing to base a reader on.

Findings reuse the ``Finding`` of ``diagnose`` but have their own codes (``FIREWALL_CODES``); this command never
changes the exit code (0 unless the controller cannot be read).
"""

from typing import Any, Dict, List, Optional, Set

from .diagnose.addresses import ip_holders, normalize_ip
from .diagnose.model import INFO, WARNING, Finding
from .diagnose.output import findings_document, format_findings
from .query import format_table
from .reservations import reservation_records
from .snapshot import FirewallData, Snapshot
from .util import clean_data, normalize_mac, plural, search_rows

JSON_VERSION = 1

FIREWALL_CODES = {
    "firewall.forward_target_offline": "A port forward points at an address nothing is using",
    "firewall.forward_no_reservation": "A port forward points at a client that has no DHCP reservation",
    "firewall.forward_duplicate": "Two enabled port forwards use the same external port",
    "firewall.allow_any_from_external": "An enabled rule of your own allows all traffic from the External zone",
    "firewall.rule_missing_network": "A rule matches specific networks but none of them exists any more",
    "firewall.disabled_rules": "Rules of your own that are switched off",
}

POLICY_COLUMNS = ["Name", "Action", "On", "From", "To", "Source", "Destination", "Protocol", "Hits"]
FORWARD_COLUMNS = ["Name", "On", "Protocol", "External port", "Forwards to", "Interface", "Only from"]
MATRIX_CELLS = {"allow_all": "A", "block_all": "B", "return_traffic": "R"}
INTERFACES = {"wan": "WAN", "wan2": "WAN 2", "both": "WAN and WAN 2"}
PROTOCOLS = {"all": "any", "tcp_udp": "TCP/UDP"}
UNKNOWN_ZONE = "(unknown zone)"


def _policies(n: int, built_in: bool = False) -> str:
    return f"{n} {'built-in ' if built_in else ''}" + ("policy" if n == 1 else "policies")


def _zone_names(fw: FirewallData) -> Dict[str, str]:
    return {z["_id"]: str(z.get("name") or UNKNOWN_ZONE) for z in fw.zones or [] if isinstance(z.get("_id"), str)}


def _zone(zones: Dict[str, str], zone_id: Any, default: str = UNKNOWN_ZONE) -> str:
    """The name of a zone by id; ``default`` for an id that is missing or unknown."""
    return zones.get(zone_id, default) if isinstance(zone_id, str) else default


def _network_names(snap: Snapshot) -> Dict[str, str]:
    return {n["_id"]: str(n.get("name") or "a network") for n in snap.networks if isinstance(n.get("_id"), str)}


def _port(side: Dict[str, Any]) -> str:
    """' port 53' for a side that matches specific ports, else ''."""
    if side.get("port_matching_type") != "SPECIFIC" or not side.get("port"):
        return ""
    return f" port {'not ' if side.get('match_opposite_ports') else ''}{side['port']}"


def _side_text(side: Dict[str, Any], networks: Dict[str, str]) -> str:
    """What one end of a policy matches, in words: ``any``, the networks, the addresses, or the kind of target."""
    target = side.get("matching_target") or "ANY"
    if target == "NETWORK":
        ids = side.get("network_ids") or []
        text = ", ".join(networks.get(i, "a network that no longer exists") for i in ids) or "no network left"
        text = ("not " if side.get("match_opposite_networks") else "") + text
    elif target == "IP":
        text = ", ".join(str(i) for i in side.get("ips") or []) or "no address"
        text = ("not " if side.get("match_opposite_ips") else "") + text
    elif target == "ANY":
        text = "any"
    else:
        text = str(target).lower().replace("_", " ")              # a client, a region, a group...
    return text + _port(side)


def _count(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def policy_rows(snap: Snapshot, fw: FirewallData) -> List[Dict[str, Any]]:
    zones, networks = _zone_names(fw), _network_names(snap)
    rows = []
    for p in fw.policies or []:
        source, destination = p.get("source") or {}, p.get("destination") or {}
        protocol = str(p.get("protocol") or "all")
        rows.append({
            "Name": str(p.get("name") or ""),
            "Action": str(p.get("action") or "").lower(),
            "On": "yes" if p.get("enabled") else "no",
            "From": _zone(zones, source.get("zone_id")),
            "To": _zone(zones, destination.get("zone_id")),
            "Source": _side_text(source, networks),
            "Destination": _side_text(destination, networks),
            "Protocol": PROTOCOLS.get(protocol, protocol.upper()),
            "Hits": "" if _count(p.get("hits")) is None else p["hits"],
            "Built in": bool(p.get("predefined")),
            "index": _count(p.get("index")) or 0,
        })
    rows.sort(key=lambda r: (r["From"], r["To"], r["index"]))
    return rows


def forward_rows(fw: FirewallData) -> List[Dict[str, Any]]:
    rows = []
    for f in fw.port_forwards or []:
        protocol = str(f.get("proto") or "tcp_udp")
        interface = str(f.get("pfwd_interface") or "")
        origin = str(f.get("src") or "any")
        rows.append({
            "Name": str(f.get("name") or ""),
            "On": "yes" if f.get("enabled") else "no",
            "Protocol": PROTOCOLS.get(protocol, protocol.upper()),
            "External port": str(f.get("dst_port") or ""),
            "Forwards to": f"{f.get('fwd') or '?'}:{f.get('fwd_port') or '?'}",
            "Interface": INTERFACES.get(interface, interface),
            "Only from": "" if origin == "any" else origin,
            "_ip": str(f.get("fwd") or ""),
            "_proto": protocol,
            "_interface": interface,
        })
    return rows


def zone_rows(snap: Snapshot, fw: FirewallData) -> List[Dict[str, Any]]:
    networks = _network_names(snap)
    return [{"Zone": str(z.get("name") or UNKNOWN_ZONE), "Built in": bool(z.get("zone_key")),
             "Networks": [networks.get(i, "a network") for i in z.get("network_ids") or []]}
            for z in fw.zones or []]


def matrix_rows(fw: FirewallData) -> List[Dict[str, Any]]:
    """One row per source zone: ``{"From": zone, "cells": {destination id: {"action", "policies"}}}``."""
    zones = _zone_names(fw)
    rows = []
    for m in fw.matrix or []:
        cells = {}
        for c in m.get("data") or []:
            count = _count(c.get("policy_count")) or 0
            action = MATRIX_CELLS.get(c.get("action"), "C" if count else "-")
            zone_id = c.get("_id")
            cells[zone_id if isinstance(zone_id, str) else UNKNOWN_ZONE] = {"action": action, "policies": count}
        rows.append({"From": _zone(zones, m.get("_id"), str(m.get("name") or UNKNOWN_ZONE)), "cells": cells})
    return rows


# -- findings ---------------------------------------------------------------------------------------

def _own(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [r for r in rows if not r["Built in"]]


def _external_zone_ids(fw: FirewallData) -> set:
    return {z.get("_id") for z in fw.zones or [] if z.get("zone_key") == "external"}


def forward_findings(snap: Snapshot, forwards: List[Dict[str, Any]]) -> List[Finding]:
    findings: List[Finding] = []
    enabled = [f for f in forwards if f["On"] == "yes"]
    holders = ip_holders(snap)
    reserved: Dict[str, Set[str]] = {}
    for user, _net in reservation_records(snap):
        ip, mac = normalize_ip(user.get("fixed_ip")), normalize_mac(user.get("mac"))
        if ip and mac:
            reserved.setdefault(ip, set()).add(mac)
    for f in enabled:
        ip = normalize_ip(f["_ip"])
        where = f"{f['Protocol']} port {f['External port']} to {f['Forwards to']}"
        if ip and ip not in holders:
            findings.append(Finding(WARNING, f["Name"] or f["Forwards to"],
                                    f"{where}, but nothing is using that address now",
                                    code="firewall.forward_target_offline"))
        elif ip and snap.all_users:
            clients = [c for c in snap.clients if normalize_ip(c.get("ipAddress")) == ip]
            unreserved = any(not normalize_mac(c.get("macAddress")) or
                             normalize_mac(c.get("macAddress")) not in reserved.get(ip, set())
                             for c in clients)
            if unreserved:
                findings.append(Finding(INFO, f["Name"] or f["Forwards to"],
                                        f"{where}; that client has no DHCP reservation, so the forward breaks if its "
                                        "address changes", code="firewall.forward_no_reservation"))
    seen: Dict[tuple, Dict[str, Any]] = {}
    for f in enabled:
        if not f["External port"]:
            continue
        for protocol in ("tcp", "udp") if f["_proto"] == "tcp_udp" else (f["_proto"],):
            key = (protocol, f["External port"], f["_interface"])
            first = seen.setdefault(key, f)
            if first is not f:
                findings.append(Finding(WARNING, f["Name"] or f["External port"],
                                        f"uses {protocol.upper()} external port {f['External port']} like "
                                        f"'{first['Name']}'", code="firewall.forward_duplicate"))
    return findings


def policy_findings(snap: Snapshot, fw: FirewallData, rows: List[Dict[str, Any]]) -> List[Finding]:
    findings: List[Finding] = []
    external = _external_zone_ids(fw)
    networks = _network_names(snap)
    zones = _zone_names(fw)
    own = [p for p in fw.policies or [] if not p.get("predefined")]
    for p in own:
        name = str(p.get("name") or "(unnamed)")
        source, destination = p.get("source") or {}, p.get("destination") or {}
        enabled = bool(p.get("enabled"))
        if (enabled and p.get("action") == "ALLOW" and source.get("zone_id") in external
                and (source.get("matching_target") or "ANY") == "ANY"
                and source.get("port_matching_type") != "SPECIFIC"
                and (destination.get("matching_target") or "ANY") == "ANY"
                and destination.get("port_matching_type") != "SPECIFIC" and (p.get("protocol") or "all") == "all"):
            to = _zone(zones, destination.get("zone_id"))
            findings.append(Finding(WARNING, name, f"allows all traffic from the External zone to {to}",
                                    code="firewall.allow_any_from_external"))
        for end, side in (("source", source), ("destination", destination)):
            if side.get("matching_target") != "NETWORK":
                continue
            ids = side.get("network_ids") or []
            gone = [i for i in ids if networks and i not in networks]
            if ids and not gone:
                continue
            why = ("matches specific networks but lists none (the network was probably deleted)" if not ids
                   else "matches a network that no longer exists" if len(gone) == 1
                   else f"matches {len(gone)} networks that no longer exist")
            findings.append(Finding(WARNING if enabled else INFO, name,
                                    f"{end} {why}" + ("" if enabled else "; the rule is switched off"),
                                    code="firewall.rule_missing_network"))
    off = [p for p in own if not p.get("enabled")]
    if off:
        findings.append(Finding(INFO, "policies", f"{plural(len(off), 'rule')} of your own "
                                f"{'is' if len(off) == 1 else 'are'} switched off", code="firewall.disabled_rules"))
    return findings


# -- the report --------------------------------------------------------------------------------------

def build_firewall(snap: Snapshot, show_all: bool = False, search: str = "") -> Dict[str, Any]:
    fw = snap.firewall or FirewallData()
    zone_based = bool(fw.policies)
    policies = policy_rows(snap, fw) if zone_based else []
    forwards = forward_rows(fw)
    findings = (policy_findings(snap, fw, policies) if zone_based else []) + forward_findings(snap, forwards)
    serialized_findings = findings_document(findings, [], areas=[])["findings"]
    shown = search_rows(policies if show_all else _own(policies), search)
    notes = []
    if not zone_based:
        notes.append("No zone-based firewall policies were found. This controller may use the classic firewall, "
                     "which this version does not show.")
    return {
        "version": JSON_VERSION,
        "style": "zone-based" if zone_based else "unknown",
        "policies": [{k: v for k, v in r.items() if k != "index"} for r in shown],
        "built_in_policies": sum(r["Built in"] for r in policies),
        "built_in_hidden": 0 if show_all else sum(r["Built in"] for r in policies),
        "port_forwards": None if fw.port_forwards is None else [
            {k: v for k, v in r.items() if not k.startswith("_")} for r in search_rows(forwards, search)],
        "zones": zone_rows(snap, fw),
        "matrix": matrix_rows(fw),
        "_zone_names": _zone_names(fw),
        "findings": serialized_findings,
        "notes": notes,
    }


def _matrix_table(matrix: List[Dict[str, Any]], zone_names: Dict[str, str]) -> str:
    zone_ids = list(dict.fromkeys(zone_id for row in matrix for zone_id in row["cells"]))
    names = [_zone(zone_names, zone_id) for zone_id in zone_ids]
    columns = ["From \\ To"]
    for name in names:                                   # two zones may share a name
        columns.append(name if name not in columns else f"{name} ({len(columns)})")
    rows = [{"From \\ To": r["From"], **{col: r["cells"].get(name, {}).get("action", "")
                                         for col, name in zip(columns[1:], zone_ids, strict=True)}} for r in matrix]
    return format_table(rows, columns)


def render_text(report: Dict[str, Any], zones: bool = False, emoji: bool = True,
                zone_names: Optional[Dict[str, str]] = None) -> str:
    report = clean_data(report)
    zone_names = clean_data(zone_names if zone_names is not None else report.get("_zone_names", {}))
    policies, forwards = report["policies"], report["port_forwards"]
    counts = [f"{plural(len(report['zones']), 'zone')}", f"{_policies(len(policies))} shown"]
    lines = [f"Firewall: {report['style']} ({', '.join(counts)})" if report["style"] != "unknown"
             else "Firewall: not shown"]
    lines += ["", *report["notes"]] if report["notes"] else []

    lines += ["", "Port forwards"]
    if forwards is None:
        lines.append("  unavailable (see the warning above)")
    elif not forwards:
        lines.append("  none configured")
    else:
        lines.append(format_table(forwards, FORWARD_COLUMNS))

    if report["style"] != "unknown":
        hidden = report["built_in_hidden"]
        note = (f" ({_policies(hidden, True)} not shown, use --all)" if hidden else "")
        lines += ["", f"Policies{'' if hidden == 0 else ' of your own'}{note}"]
        lines.append(format_table(policies, POLICY_COLUMNS) if policies else "  none")
    if zones and report["style"] != "unknown":
        lines += ["", "Zones"]
        for z in report["zones"]:
            lines.append(f"  {z['Zone']}{'' if z['Built in'] else ' (yours)'}: "
                         + (", ".join(z["Networks"]) or "no networks"))
        lines += ["", "Zone matrix (what traffic from a zone may do in another: A allow all, B block all, "
                      "R return traffic only, C custom rules, - none)",
                   _matrix_table(report["matrix"], zone_names)]

    findings = [Finding(f["severity"], f["subject"], f["message"], code=f["code"]) for f in report["findings"]]
    lines += ["", "Findings", format_findings(findings, emoji) if findings else "No issues found."]
    return "\n".join(lines)
