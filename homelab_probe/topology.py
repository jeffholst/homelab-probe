"""Uplink topology: how the gateway, switches and access points are wired together.

`hlp topology` draws the tree from the gateway down, with the port each device
plugs into, the negotiated link speed, client counts and anything `diagnose` flags.
"""

from collections import Counter
from typing import Any, Dict, List, Optional, Set, Tuple, TypedDict

from .client_view import DeviceIndex
from .diagnose import EMOJI, INFO, SEVERITY_ORDER, Finding, apply_ignores, diagnose, uplink_speeds
from .settings import DiagnoseSettings
from .snapshot import Snapshot
from .util import clean_data, normalize_mac, number, record_for

GATEWAY_KINDS = {"Gateway", "Dream Machine"}
JSON_VERSION = 1          # the format of `topology --json`; it changes only when a field is removed or renamed


class ClientCounts(TypedDict):
    wired: int
    wireless: int
    total: int


class NodeFinding(TypedDict):
    """A finding of ``diagnose`` that flags a device (the same four keys as ``Finding.to_dict`` minus the MAC)."""

    severity: str
    code: str
    subject: str
    message: str


class WiredClient(TypedDict):
    name: str
    ip: str
    port: Optional[int]


class _NodeBase(TypedDict):
    name: str
    mac: str
    type: str
    model: str
    online: bool
    parent: str                              # the parent's name, "" for a gateway or an unattached device
    parent_port: Optional[int]               # the port of the PARENT it plugs into
    port: Optional[int]                      # its own uplink port
    speed_mbps: Optional[float]
    supports_mbps: Optional[float]           # set only when the link negotiated below what both ends support
    clients: Optional[ClientCounts]          # None when the connected-client data is unavailable
    findings: List[NodeFinding]
    children: List["Node"]


class Node(_NodeBase, total=False):
    """One device of the tree. The two keys below exist only sometimes: ``wired_clients`` with ``--clients``,
    ``reason`` on an unattached device."""

    wired_clients: List[WiredClient]
    reason: str


class Summary(TypedDict):
    devices: int
    offline: int
    with_findings: int
    below_max: int
    unattached: int
    clients: Optional[int]


class Topology(TypedDict):
    """The result of ``build_topology``."""

    roots: List[Node]
    unattached: List[Node]
    summary: Summary


def _link(snap: Snapshot, idx: DeviceIndex, mac: str) -> Tuple[str, Optional[int], Optional[int], Optional[float]]:
    """(parent MAC, parent's port, this device's own uplink port, negotiated Mbps).

    The legacy uplink has the ports and speed; the Integration API's ``uplink.deviceId``
    names the parent when the legacy data does not.
    """
    legacy = idx.legacy.get(mac) or {}
    up = legacy.get("uplink") or {}
    parent = normalize_mac(up.get("uplink_mac"))
    if not parent:
        detail = record_for(snap.device_details, (idx.integration.get(mac) or {}).get("id"))
        wanted = (detail.get("uplink") or {}).get("deviceId")
        parent = next((m for m, d in idx.integration.items() if wanted and d.get("id") == wanted), "")
    port, own = up.get("uplink_remote_port"), up.get("port_idx")
    return parent, port, own, number(up.get("speed"))


class DeviceLink(TypedDict):
    """Where one device is plugged in, as the controller last reported it (see ``device_links``)."""

    name: str
    mac: str
    online: Optional[bool]                   # None when the controller reports no state for it
    parent: str                              # the parent's MAC, "" when no uplink is known
    parent_port: Optional[int]               # the port of the PARENT it plugs into
    port: Optional[int]                      # its own uplink port


def device_links(snap: Snapshot) -> Dict[str, DeviceLink]:
    """The uplink of every device by MAC, without building the tree or running any check. **An offline device keeps
    its last known uplink** in the controller's data, so for one that is not online the link says where it was
    connected, not where it is connected now. ``online`` is None when the controller gives no state (a device only
    the legacy data lists, or a record without one): unknown is never taken for offline. ``parent`` is "" when the
    data names no parent; it can name a device that is not in the snapshot, or the device itself, and a caller that
    follows it must not trust it blindly."""
    idx = DeviceIndex(snap)
    links: Dict[str, DeviceLink] = {}
    for mac in sorted(set(idx.legacy) | set(idx.integration)):
        parent, port, own, _speed = _link(snap, idx, mac)
        state = (idx.integration.get(mac) or {}).get("state")
        links[mac] = {"name": idx.name(mac), "mac": mac, "online": None if not state else state == "ONLINE",
                      "parent": parent, "parent_port": port, "port": own}
    return links


def _assign_findings(findings: List[Finding], names: Dict[str, str]) -> Dict[str, List[Finding]]:
    """Give device findings to their exact target MAC."""
    result: Dict[str, List[Finding]] = {}
    for f in findings:
        if f.target_mac and f.target_mac in names:
            result.setdefault(f.target_mac, []).append(f)
    return result


def _client_info(snap: Snapshot) -> Tuple[Optional[Dict[str, Counter]], Dict[str, List[WiredClient]]]:
    """(counts per device MAC as {'wired': n, 'wireless': n}, wired clients per device MAC).
    The counts are None when the connected-client data is unavailable."""
    if not snap.legacy_clients:
        return None, {}
    counts: Dict[str, Counter] = {}
    wired: Dict[str, List[WiredClient]] = {}
    for c in snap.legacy_clients:
        if c.get("is_wired") and c.get("sw_mac"):
            mac = normalize_mac(c["sw_mac"])
            counts.setdefault(mac, Counter())["wired"] += 1
            wired.setdefault(mac, []).append({
                "name": c.get("name") or c.get("hostname") or normalize_mac(c.get("mac")),
                "ip": c.get("ip") or "", "port": c.get("sw_port")})
        elif not c.get("is_wired") and c.get("ap_mac"):
            counts.setdefault(normalize_mac(c["ap_mac"]), Counter())["wireless"] += 1
    return counts, wired


def build_topology(snap: Snapshot, settings: Optional[DiagnoseSettings] = None,
                   with_clients: bool = False) -> Topology:
    """The wiring tree as nested dicts: ``{"roots": [...], "unattached": [...], "summary": {...}}``.

    Roots are gateways. Any device that cannot be reached from a gateway, whether it has
    no uplink information, an uplink to an unknown device, or sits in an uplink loop,
    is listed under ``unattached`` with the reason, so nothing silently disappears.
    """
    settings = settings or DiagnoseSettings()
    idx = DeviceIndex(snap)
    macs = sorted(set(idx.legacy) | set(idx.integration))
    names = {m: idx.name(m) for m in macs}
    kept, _ignored = apply_ignores(diagnose(snap, settings), settings.ignore)
    # Only warnings and criticals flag a device; info findings (such as slow ports) stay in `diagnose`.
    findings = _assign_findings([f for f in kept if f.severity != INFO], names)
    counts, wired = _client_info(snap)

    links = {m: _link(snap, idx, m) for m in macs}
    children: Dict[str, List[str]] = {}
    for mac, (parent, *_rest) in links.items():
        if parent and parent in names and parent != mac:
            children.setdefault(parent, []).append(mac)

    def node(mac: str) -> Node:
        parent, port, own, speed = links[mac]
        legacy, integ = idx.legacy.get(mac) or {}, idx.integration.get(mac) or {}
        in_tree = bool(parent and parent in names and parent != mac)   # the root's uplink is its WAN
        speeds = uplink_speeds(snap, legacy) if legacy and in_tree else None
        mine = findings.get(mac, [])
        count = None if counts is None else dict(counts.get(mac, Counter()))
        result: Node = {
            "name": names[mac], "mac": mac, "type": idx.kind(mac),
            "model": integ.get("model") or legacy.get("model") or "",
            "online": not idx.offline(mac),
            "parent": names.get(parent, "") if parent else "",
            "parent_port": port, "port": own,
            "speed_mbps": (speed or None) if in_tree else None,
            "supports_mbps": speeds[1] if speeds and speeds[0] < speeds[1] else None,
            "clients": None if count is None else {
                "wired": count.get("wired", 0), "wireless": count.get("wireless", 0),
                "total": count.get("wired", 0) + count.get("wireless", 0)},
            "findings": [{"severity": f.severity, "code": f.code, "subject": f.subject, "message": f.message}
                         for f in mine],
            "children": [],
        }
        if with_clients:
            result["wired_clients"] = sorted(
                wired.get(mac, []), key=lambda c: (c["port"] is None, c["port"] or 0, c["name"].lower()))
        return result

    def order(mac: str) -> Tuple[bool, int, str]:
        port = links[mac][1]
        return (port is None, port if isinstance(port, int) else 0, names[mac].lower())

    visited: Set[str] = set()

    def grow(mac: str) -> Node:
        visited.add(mac)
        result = node(mac)
        for child in sorted(children.get(mac, []), key=order):
            if child not in visited:  # pragma: no branch  (a device has one parent, so a loop cannot reach here)
                result["children"].append(grow(child))
        return result

    roots = [grow(m) for m in sorted((m for m in macs if idx.kind(m) in GATEWAY_KINDS
                                      and not (links[m][0] and links[m][0] in names)),
                                     key=lambda m: names[m].lower())]

    unattached: List[Node] = []
    for mac in sorted((m for m in macs if m not in visited), key=lambda m: names[m].lower()):
        parent = links[mac][0]
        if not parent:
            reason = "no uplink information"
        elif parent not in names:
            reason = f"uplink to unknown device {parent}"
        else:
            reason = "not reachable from a gateway (uplink loop or detached branch)"
        item = node(mac)
        item["reason"] = reason
        unattached.append(item)

    def walk(nodes: List[Node]):
        for n in nodes:
            yield n
            yield from walk(n["children"])

    every = list(walk(roots)) + unattached
    return {"roots": roots, "unattached": unattached, "summary": {
        "devices": len(every),
        "offline": sum(not n["online"] for n in every),
        "with_findings": sum(bool(n["findings"]) for n in every),
        "below_max": sum(n["supports_mbps"] is not None for n in every),
        "unattached": len(unattached),
        "clients": None if counts is None else sum(c["wired"] + c["wireless"] for c in
                                                   (n["clients"] for n in every) if c),
    }}


# -- rendering ---------------------------------------------------------------

_UNICODE = {"tee": "├── ", "last": "└── ", "pipe": "│   ", "gap": "    "}
_ASCII = {"tee": "+-- ", "last": "`-- ", "pipe": "|   ", "gap": "    "}


def worst_severity(findings: List[NodeFinding]) -> str:
    """The most serious severity among ``findings`` ("" when there are none)."""
    return min((f["severity"] for f in findings), key=lambda s: SEVERITY_ORDER[s], default="")


def _marker(severity: str, count: int, emoji: bool) -> str:
    if emoji:
        return f"{EMOJI[severity]} {count}"
    return f"[{severity.upper()}{' x' + str(count) if count > 1 else ''}]"


def _line(n: Node, emoji: bool, root: bool) -> str:
    head = n["name"]
    if not root and n["parent_port"] is not None:
        head = f"port {n['parent_port']} -> {n['name']}"
    if root and n["model"]:
        head += f" ({n['model']})"
    if n["speed_mbps"]:
        speed = f"{n['speed_mbps']:.0f} Mbps"
        if n["supports_mbps"]:
            speed += f", supports {n['supports_mbps']:.0f}"
        head += f" ({speed})"
    parts = [head]
    if n["clients"] is not None and n["clients"]["total"]:
        total = n["clients"]["total"]
        parts.append(f"{total} client{'s' if total != 1 else ''}")
    if not n["online"]:
        parts.append("[OFFLINE]")
    worst = worst_severity(n["findings"])
    if worst:
        parts.append(_marker(worst, len(n["findings"]), emoji))
    return "   ".join(parts)


def render_text(document: Dict[str, Any], emoji: bool = True, with_clients: bool = False) -> str:
    """Draw the tree from a topology document (the ``--json`` dict: the tree plus its ``version``)."""
    topology: Topology = clean_data(document)
    style = _UNICODE if emoji else _ASCII
    lines: List[str] = []

    def draw(n: Node, prefix: str, connector: str, root: bool) -> None:
        lines.append(prefix + connector + _line(n, emoji, root))
        branch = "" if root else (style["gap"] if connector == style["last"] else style["pipe"])
        inner = prefix + branch
        if with_clients:  # aligned with the child connectors below
            for c in n.get("wired_clients", []):
                where = f"port {c['port']}: " if c["port"] is not None else ""
                ip = f" ({c['ip']})" if c["ip"] else ""
                lines.append(f"{inner}- {where}{c['name']}{ip}")
        for i, child in enumerate(n["children"]):
            last = i == len(n["children"]) - 1
            draw(child, inner, style["last"] if last else style["tee"], False)

    for root in topology["roots"]:
        draw(root, "", "", True)
    if not topology["roots"]:
        lines.append("No gateway found.")

    if topology["unattached"]:
        lines += ["", "Unattached (not reachable from a gateway):"]
        for n in topology["unattached"]:
            lines.append(f"  {_line(n, emoji, True)}  ({n['reason']})")

    flagged = [n for n in _flatten(topology) if n["findings"]]
    if flagged:
        lines += ["", "Findings on these devices:"]
        for n in flagged:
            for f in sorted(n["findings"], key=lambda f: SEVERITY_ORDER[f["severity"]]):
                label = EMOJI[f["severity"]] if emoji else f"[{f['severity'].upper():8}]"
                lines.append(f"  {label} {f['subject']}: {f['message']}")

    s = topology["summary"]
    bits = [f"{s['devices']} device{'s' if s['devices'] != 1 else ''}"]
    if s["clients"] is not None:
        bits.append(f"{s['clients']} client{'s' if s['clients'] != 1 else ''}")
    for count, text in ((s["offline"], "offline"), (s["below_max"], "link(s) below capability"),
                        (s["with_findings"], "with findings"), (s["unattached"], "unattached")):
        if count:
            bits.append(f"{count} {text}")
    lines += ["", ", ".join(bits)]
    return "\n".join(lines)


def _flatten(topology: Topology) -> List[Node]:
    out: List[Node] = []

    def walk(nodes: List[Node]) -> None:
        for n in nodes:
            out.append(n)
            walk(n["children"])

    walk(topology["roots"])
    return out + topology["unattached"]
