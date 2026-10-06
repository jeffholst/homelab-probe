"""Graph formats of the uplink topology: Mermaid and Graphviz DOT.

Both renderers take the topology document (the dict ``topology --json`` prints) and return text, so the tree,
the JSON and the graphs cannot drift apart. Names and every other value in the document come from devices
and clients on the network, so nothing from it is ever used as syntax:

* node identifiers are generated (``n0``, ``n1`` ... in tree order, ``c0`` ... for wired clients), never taken
  from a name;
* the document is passed through ``clean_data`` (no control characters, no line breaks) and each text value is
  then escaped for the target syntax: a Mermaid label is a quoted string in which every ASCII punctuation
  character that could mean something is written as a numeric entity (``#35;``), a DOT label is a quoted
  string in which ``\\``, ``"`` and ``&`` are escaped. The only markup in the output is what this module
  writes itself (``<br/>`` in Mermaid, ``\\n`` in DOT);
* colors, class names and keywords come from the fixed tables below.
"""

from typing import Any, Dict, List, NamedTuple

from .diagnose import CRITICAL, WARNING
from .topology import Node, Topology, worst_severity
from .util import clean_data

# Severity -> colors (fixed words, never data).
_FILL = {CRITICAL: "#f8d7da", WARNING: "#fff3cd"}
_STROKE = {CRITICAL: "#b00020", WARNING: "#b26a00"}
_LABEL = {CRITICAL: "CRITICAL", WARNING: "WARNING"}
_OFFLINE_STROKE = "#6c757d"
_CLIENT_FILL, _CLIENT_STROKE = "#f8f9fa", "#adb5bd"

# ASCII punctuation kept as it is in a Mermaid label; inside quotes none of it is syntax.
_MERMAID_PLAIN = frozenset(" .,-_+/:")


class _Item(NamedTuple):
    """One node of the graph in the order it is written, before any syntax is applied."""

    id: str                 # n0, n1 ... for devices, c0, c1 ... for wired clients
    lines: List[str]        # the text of the node, one entry per line (not yet escaped)
    client: bool
    unattached: bool
    severity: str           # "critical", "warning" or "" (devices only)
    offline: bool
    parent: str             # the id this node hangs from, "" for a root or an unattached device
    edge: str               # the label of the edge from the parent (not yet escaped), may be empty


def _details(n: Node) -> List[str]:
    """The lines under a device's name: model, client count, state and its worst finding."""
    lines: List[str] = []
    if n["model"]:
        lines.append(str(n["model"]))
    if n["clients"] is not None and n["clients"]["total"]:
        total = n["clients"]["total"]
        lines.append(f"{total} client{'s' if total != 1 else ''}")
    if not n["online"]:
        lines.append("OFFLINE")
    worst = worst_severity(n["findings"])
    if worst in _LABEL:
        lines.append(_LABEL[worst] + (f" x{len(n['findings'])}" if len(n["findings"]) > 1 else ""))
    return lines


def _link(n: Node) -> str:
    """'port 2, 100 Mbps, supports 1000' for the uplink of ``n`` (the parent's port, as the text view says)."""
    parts: List[str] = []
    if n["parent_port"] is not None:
        parts.append(f"port {n['parent_port']}")
    if n["speed_mbps"]:
        parts.append(f"{n['speed_mbps']:.0f} Mbps")
        if n["supports_mbps"]:
            parts.append(f"supports {n['supports_mbps']:.0f}")
    return ", ".join(parts)


def _plan(document: Dict[str, Any]) -> List[_Item]:
    """Every device (and, with ``--clients``, wired client) of the document in drawing order: the tree from each
    gateway in tree order, then the unattached devices. Ids are numbered in that order, so equal input gives
    equal output."""
    topology: Topology = clean_data(document)
    items: List[_Item] = []
    counts = {"n": 0, "c": 0}

    def new_id(prefix: str) -> str:
        counts[prefix] += 1
        return f"{prefix}{counts[prefix] - 1}"

    def visit(n: Node, parent: str, unattached: bool) -> None:
        nid = new_id("n")
        severity = worst_severity(n["findings"])
        extra = [f"({n['reason']})"] if n.get("reason") else []
        items.append(_Item(nid, [n["name"] or "(unnamed)", *_details(n), *extra], False, unattached,
                           severity if severity in _FILL else "", not n["online"], parent, _link(n) if parent else ""))
        for c in n.get("wired_clients", []):
            port = c["port"]
            items.append(_Item(new_id("c"), [str(c["name"]) or "(unnamed)", *([str(c["ip"])] if c["ip"] else [])],
                               True, unattached, "", False, nid, "" if port is None else f"port {port}"))
        for child in n["children"]:
            visit(child, nid, unattached)

    for root in topology["roots"]:
        visit(root, "", False)
    for lone in topology["unattached"]:
        visit(lone, "", True)
    return items


# -- Mermaid -----------------------------------------------------------------

def _mermaid(text: str) -> str:
    """Text for inside a quoted Mermaid string: ASCII punctuation outside a small safe set becomes a decimal
    entity (``"`` ``#`` ``<`` ``>`` ``&`` ``|`` brackets, ``;``, ``%``, a backtick, ``\\``, ``@`` ...), so the
    text can neither close the string, start a comment, add markup nor reach another statement."""
    return "".join(c if c in _MERMAID_PLAIN or not c.isascii() or c.isalnum() else f"#{ord(c)};" for c in text)


def render_mermaid(document: Dict[str, Any]) -> str:
    """A Mermaid ``flowchart`` of the topology document: a node per device (client counts and state in its
    label), an edge from the parent labelled with the parent's port and the negotiated speed (dashed when the
    device is offline), and the classes ``critical``, ``warning`` and ``offline``. With ``--clients`` the wired
    clients are nodes too; unattached devices are in a subgraph of their own."""
    items = _plan(document)
    lines = ["flowchart TD"]
    for unattached in (False, True):
        group = [i for i in items if i.unattached == unattached]
        if unattached and group:
            lines.append('    subgraph unattached["Unattached"]')
        indent = "        " if unattached else "    "
        for i in group:
            text = "<br/>".join(_mermaid(t) for t in i.lines)
            lines.append(f'{indent}{i.id}(["{text}"])' if i.client else f'{indent}{i.id}["{text}"]')
        if unattached and group:
            lines.append("    end")
    if not items:
        lines.append("    %% No gateway found.")
    for i in items:
        if i.parent:
            arrow = "---" if i.client else "-.->" if i.offline else "-->"
            label = f'|"{_mermaid(i.edge)}"|' if i.edge else ""
            lines.append(f"    {i.parent} {arrow}{label} {i.id}")
    lines += [
        f"    classDef critical fill:{_FILL[CRITICAL]},stroke:{_STROKE[CRITICAL]},color:#000",
        f"    classDef warning fill:{_FILL[WARNING]},stroke:{_STROKE[WARNING]},color:#000",
        f"    classDef offline stroke:{_OFFLINE_STROKE},stroke-dasharray:5 5,color:#555",
        f"    classDef client fill:{_CLIENT_FILL},stroke:{_CLIENT_STROKE},color:#000",
    ]
    for name, members in (("critical", [i.id for i in items if i.severity == CRITICAL]),
                          ("warning", [i.id for i in items if i.severity == WARNING]),
                          ("offline", [i.id for i in items if i.offline]),
                          ("client", [i.id for i in items if i.client])):
        if members:
            lines.append(f"    class {','.join(members)} {name}")
    return "\n".join(lines)


# -- Graphviz DOT ------------------------------------------------------------

def _dot(text: str) -> str:
    """Text for inside a quoted DOT string. ``\\`` starts the label escapes (``\\n``, ``\\N``, ``\\G`` ...) and
    ``"`` ends the string, so both are escaped; ``&`` becomes ``&amp;`` because Graphviz decodes entities in
    labels and the name must show as it is."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("&", "&amp;")


def _dot_attributes(i: _Item) -> str:
    text = "\\n".join(_dot(t) for t in i.lines)
    if i.client:
        return f'label="{text}", shape=ellipse, style=filled, fillcolor="{_CLIENT_FILL}", color="{_CLIENT_STROKE}"'
    fill = f', fillcolor="{_FILL[i.severity]}"' if i.severity else ""
    color = _STROKE[i.severity] if i.severity else _OFFLINE_STROKE if i.offline else "#343a40"
    style = ",".join(s for s in ("filled" if i.severity else "", "dashed" if i.offline else "") if s) or "solid"
    return f'label="{text}", style="{style}"{fill}, color="{color}"'


def render_dot(document: Dict[str, Any]) -> str:
    """A Graphviz ``digraph`` of the topology document, with the same nodes, edges and flags as
    ``render_mermaid``: critical and warning devices are filled and outlined in their color, offline devices
    and their uplinks are dashed. Unattached devices are in a cluster of their own."""
    items = _plan(document)
    lines = ["digraph topology {",
             '    graph [rankdir=TB, fontname="Helvetica"];',
             '    node [shape=box, fontname="Helvetica"];',
             '    edge [fontname="Helvetica"];']
    for unattached in (False, True):
        group = [i for i in items if i.unattached == unattached]
        if unattached and group:
            lines += ["    subgraph cluster_unattached {", '        label="Unattached";']
        indent = "        " if unattached else "    "
        for i in group:
            lines.append(f"{indent}{i.id} [{_dot_attributes(i)}];")
        if unattached and group:
            lines.append("    }")
    if not items:
        lines.append("    // No gateway found.")
    for i in items:
        if i.parent:
            attributes = ([f'label="{_dot(i.edge)}"'] if i.edge else []) + (
                ["arrowhead=none"] if i.client else ["style=dashed"] if i.offline else [])
            lines.append(f"    {i.parent} -> {i.id}" + (f" [{', '.join(attributes)}];" if attributes else ";"))
    lines.append("}")
    return "\n".join(lines)


GRAPH_RENDERERS = {"mermaid": render_mermaid, "dot": render_dot}
TOPOLOGY_FORMATS = ("text", *GRAPH_RENDERERS)    # the choices of `topology --format`
