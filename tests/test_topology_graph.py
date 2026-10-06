"""`topology --format mermaid|dot`: the graph formats of the uplink tree (issue #164).

The renderers take the topology document, so the first tests run them over the synthetic fixture. The rest is
about names: a device name is untrusted text, so every line of a graph is checked against a strict grammar of
what the renderer itself may write, and every label is decoded again and compared with the cleaned name.
"""

import re

import pytest
from golden_support import run_command
from test_output_safety import poison

from homelab_probe import cli
from homelab_probe.documents import topology_document
from homelab_probe.topology_graph import (
    GRAPH_RENDERERS,
    TOPOLOGY_FORMATS,
    _dot,
    _mermaid,
    render_dot,
    render_mermaid,
)
from homelab_probe.util import printable

# Names that try to close a string, start a statement, a comment, a link or markup in one of the syntaxes.
NASTY = [
    'x"]; click n0 call alert(1) "',
    'a --> b',
    'end',
    '}\n digraph evil { n0 -> n0 }',
    '<img src=x onerror=alert(1)>',
    '<b>bold</b> <a href="javascript:alert(1)">',
    '%% comment',
    '\\n\\N\\G "q" &amp; &#35;',
    '#35;',
    '`markdown`',
    '|pipe| [brackets] (parens) {braces} ; : @{ shape: x }',
    'n0',
    'subgraph x',
    'classDef a fill:red',
    'a" -> "b',
    "line\u2028break\u0085 and\ttab\r\nCRLF",
    "\u202eevil\x1b[31m\x07",
    "Café ☕ 日本語 שלום",
    "",
]


def document(fake_client, clients=True):
    return topology_document(fake_client, "default", with_clients=clients, echo=False).data


def device(name, children=(), **kw):
    node = {"name": name, "mac": "AA:BB", "type": "usw", "model": "Model", "online": True, "parent": "",
            "parent_port": 1, "port": 2, "speed_mbps": 1000.0, "supports_mbps": None,
            "clients": {"wired": 1, "wireless": 0, "total": 1}, "findings": [], "children": list(children),
            "wired_clients": [{"name": name, "ip": "192.0.2.5", "port": 3}]}
    node.update(kw)
    return node


def hostile_document():
    finding = {"severity": "critical", "code": "device.offline", "subject": "s", "message": "m"}
    root = device(NASTY[0], [device(n, findings=[{**finding, "severity": "warning"}]) for n in NASTY[1:]],
                  findings=[finding], model=NASTY[1])
    lone = device(NASTY[3], online=False, reason="uplink to unknown device " + NASTY[0])
    return {"version": 1, "roots": [root], "unattached": [lone], "summary": {}}


def devices_of(doc):
    def walk(nodes):
        for n in nodes:
            yield n
            yield from walk(n["children"])
    return list(walk(doc["roots"])) + list(doc["unattached"])


# -- the grammars: what a renderer is allowed to write ------------------------------------------

M_TEXT = r'(?:[A-Za-z0-9 .,\-_+/:]|[^\x00-\x7f]|#[0-9]+;)*'
M_LABEL = rf'{M_TEXT}(?:<br/>{M_TEXT})*'
M_LINES = [
    r"flowchart TD",
    rf'    n[0-9]+\["{M_LABEL}"\]',
    rf'    c[0-9]+\(\["{M_LABEL}"\]\)',
    rf'        n[0-9]+\["{M_LABEL}"\]',
    rf'        c[0-9]+\(\["{M_LABEL}"\]\)',
    r'    subgraph unattached\["Unattached"\]',
    r"    end",
    r"    %% No gateway found\.",
    rf'    [nc][0-9]+ (?:-->|-\.->|---)(?:\|"{M_TEXT}"\|)? [nc][0-9]+',
    r"    classDef (?:critical|warning|offline|client) [a-z0-9#:,\- ]+",
    r"    class [nc][0-9]+(?:,[nc][0-9]+)* (?:critical|warning|offline|client)",
]

D_STR = r'"(?:[^"\\]|\\\\|\\"|\\n)*"'
D_ATTR = r'(?:shape=ellipse|style=filled|style="[a-z,]+"|fillcolor="#[0-9a-f]{6}"|color="#[0-9a-f]{6}")'
D_EDGE = rf"(?:label={D_STR}|arrowhead=none|style=dashed)"
D_LINES = [
    r"digraph topology \{",
    r'    graph \[rankdir=TB, fontname="Helvetica"\];',
    r'    node \[shape=box, fontname="Helvetica"\];',
    r'    edge \[fontname="Helvetica"\];',
    rf"    [nc][0-9]+ \[label={D_STR}(?:, {D_ATTR})*\];",
    rf"        [nc][0-9]+ \[label={D_STR}(?:, {D_ATTR})*\];",
    r"    subgraph cluster_unattached \{",
    r'        label="Unattached";',
    r"    \}",
    rf"    [nc][0-9]+ -> [nc][0-9]+(?: \[{D_EDGE}(?:, {D_EDGE})*\])?;",
    r"    // No gateway found\.",
    r"\}",
]


def assert_grammar(text, grammar):
    for line in text.split("\n"):
        assert any(re.fullmatch(g, line) for g in grammar), f"a line outside the grammar: {line!r}"


def mermaid_labels(text):
    """{node id: [decoded lines]} of a Mermaid graph (entities and <br/> undone)."""
    found = {}
    for m in re.finditer(r'^ +([nc][0-9]+)\(?\["(.*)"\]\)?$', text, re.M):
        found[m.group(1)] = [re.sub(r"#([0-9]+);", lambda e: chr(int(e.group(1))), part)
                             for part in m.group(2).split("<br/>")]
    return found


def dot_labels(text):
    """{node id: [decoded lines]} of a DOT graph (escapes undone, `\\n` is the line separator)."""
    found = {}
    for m in re.finditer(rf"^ +([nc][0-9]+) \[label=({D_STR})", text, re.M):
        lines, current, body, i = [], "", m.group(2)[1:-1], 0
        while i < len(body):
            if body[i] == "\\":
                if body[i + 1] == "n":
                    lines.append(current)
                    current = ""
                else:
                    current += body[i + 1]
                i += 2
            else:
                current += body[i]
                i += 1
        found[m.group(1)] = [line.replace("&amp;", "&") for line in (*lines, current)]   # Graphviz decodes entities
    return found


# -- the fixture -------------------------------------------------------------------------------------

def test_the_mermaid_graph_of_the_fixture(fake_client):
    out = render_mermaid(document(fake_client, clients=False))
    assert out.splitlines()[0] == "flowchart TD"
    assert_grammar(out, M_LINES)
    assert mermaid_labels(out) == {
        "n0": ["Gateway", "UCG Max", "CRITICAL x2"],
        "n1": ["Office Switch", "USW-Lite-8-PoE", "1 client", "WARNING x8"],
        "n2": ["Office AP", "U7 Pro", "1 client"],
        "n3": ["Garage AP", "U6 Pro", "OFFLINE", "WARNING"],
    }
    assert 'n0 -->|"port 2, 100 Mbps, supports 1000"| n1' in out           # the parent's port and the speed
    assert 'n1 -->|"port 2"| n2' in out
    assert 'n1 -.->|"port 5"| n3' in out                                       # an offline device's link is dashed
    assert "class n0 critical" in out and "class n1,n3 warning" in out and "class n3 offline" in out
    assert "c0" not in out                                                     # no clients without --clients
    assert "subgraph" not in out                                               # nothing is unattached


def test_the_dot_graph_of_the_fixture(fake_client):
    out = render_dot(document(fake_client, clients=False))
    assert_grammar(out, D_LINES)
    assert dot_labels(out) == mermaid_labels(render_mermaid(document(fake_client, clients=False)))
    assert 'n0 -> n1 [label="port 2, 100 Mbps, supports 1000"];' in out
    assert 'n1 -> n3 [label="port 5", style=dashed];' in out
    assert 'n3 [label="Garage AP\\nU6 Pro\\nOFFLINE\\nWARNING", style="filled,dashed", fillcolor="#fff3cd"' in out
    assert 'n0 [label="Gateway\\nUCG Max\\nCRITICAL x2", style="filled", fillcolor="#f8d7da", color="#b00020"]' in out
    assert 'n2 [label="Office AP\\nU7 Pro\\n1 client", style="solid"' in out    # nothing wrong: no fill, not dashed
    assert "cluster" not in out and out.endswith("}")


@pytest.mark.parametrize("render, grammar", [(render_mermaid, M_LINES), (render_dot, D_LINES)],
                         ids=["mermaid", "dot"])
def test_clients_are_nodes_only_when_the_document_has_them(fake_client, render, grammar):
    plain, with_clients = render(document(fake_client, clients=False)), render(document(fake_client))
    assert_grammar(with_clients, grammar)
    assert "c0" not in plain
    assert "c0" in with_clients and with_clients.count("c0") >= 2              # the node and its edge
    labels = (mermaid_labels if render is render_mermaid else dot_labels)(with_clients)
    assert labels["c0"] == ["desktop"] and "port 3" in with_clients


@pytest.mark.parametrize("render", [render_mermaid, render_dot], ids=["mermaid", "dot"])
def test_the_output_is_deterministic_and_the_document_is_not_changed(fake_client, render):
    doc = document(fake_client)
    before = repr(doc)
    assert render(doc) == render(doc) == render(document(fake_client))
    assert repr(doc) == before


@pytest.mark.parametrize("render, grammar", [(render_mermaid, M_LINES), (render_dot, D_LINES)],
                         ids=["mermaid", "dot"])
def test_no_gateway_and_unattached_devices(render, grammar):
    empty = render({"version": 1, "roots": [], "unattached": [], "summary": {}})
    assert_grammar(empty, grammar)
    assert "No gateway found." in empty
    lone = device("Lonely", online=False, reason="no uplink information", wired_clients=[])
    out = render({"version": 1, "roots": [], "unattached": [lone], "summary": {}})
    assert_grammar(out, grammar)
    assert "Unattached" in out and "No gateway found" not in out
    labels = (mermaid_labels if render is render_mermaid else dot_labels)(out)
    assert labels == {"n0": ["Lonely", "Model", "1 client", "OFFLINE", "(no uplink information)"]}


def test_a_device_without_name_model_ports_or_counts():
    bare = device("", model="", parent_port=None, speed_mbps=None, clients=None, wired_clients=[
        {"name": "", "ip": "", "port": None}])
    kid = device("Kid", parent_port=None, speed_mbps=None, supports_mbps=None, clients={"wired": 2, "wireless": 0, "total": 2},
                 wired_clients=[])
    bare["children"] = [kid]
    doc = {"version": 1, "roots": [bare], "unattached": [], "summary": {}}
    mermaid, dot = render_mermaid(doc), render_dot(doc)
    assert_grammar(mermaid, M_LINES)
    assert_grammar(dot, D_LINES)
    assert mermaid_labels(mermaid) == dot_labels(dot) == {
        "n0": ["(unnamed)"], "c0": ["(unnamed)"], "n1": ["Kid", "Model", "2 clients"]}
    assert "n0 --> n1" in mermaid and "n0 -> n1;" in dot and "n0 --- c0" in mermaid


# -- hostile names -----------------------------------------------------------------------------------

@pytest.mark.parametrize("render, grammar, labels", [(render_mermaid, M_LINES, mermaid_labels),
                                                     (render_dot, D_LINES, dot_labels)], ids=["mermaid", "dot"])
def test_hostile_names_stay_inside_their_label(render, grammar, labels):
    doc = hostile_document()
    out = render(doc)
    assert_grammar(out, grammar)                                                   # nothing but our own statements
    devices = devices_of(doc)
    found = labels(out)
    assert sorted(k for k in found if k.startswith("n")) == sorted(f"n{i}" for i in range(len(devices)))
    for i, n in enumerate(devices):
        assert found[f"n{i}"][0] == (printable(n["name"]) or "(unnamed)")         # the name, cleaned, decoded
    assert len([k for k in found if k.startswith("c")]) == len(devices)           # one client each, no extras
    assert "\x1b" not in out and "\u202e" not in out and "\u2028" not in out
    assert len(out.splitlines()) == len(out.split("\n"))
    # the statements that exist: nothing a name could add (click, link, extra node, extra graph)
    outside = re.sub(r'"(?:[^"\\]|\\.)*"', '""', out)                            # the text with every string emptied
    assert outside.count("flowchart") + outside.count("digraph") == 1
    assert "click" not in outside and "href" not in outside and "alert" not in outside


def test_a_mermaid_label_has_no_character_with_a_meaning():
    for name in NASTY:
        label = _mermaid(printable(name))
        assert re.fullmatch(M_TEXT, label), label
        for bad in '"<>&|[]{}();#%`\\@':
            assert bad not in re.sub(r"#[0-9]+;", "", label), (bad, label)
        decoded = re.sub(r"#([0-9]+);", lambda e: chr(int(e.group(1))), label)
        assert decoded == printable(name)                                          # lossless


def test_a_dot_label_cannot_leave_its_string():
    for name in NASTY:
        text = _dot(printable(name))
        assert re.fullmatch(r'(?:[^"\\]|\\\\|\\")*', text), text
    assert _dot('a"b\\c&d') == 'a\\"b\\\\c&amp;d'


def test_node_ids_never_come_from_a_name(fake_client):
    for render in (render_mermaid, render_dot):
        doc = hostile_document()
        ids = set(re.findall(r"^ +([nc][0-9]+)\b", render(doc), re.M))
        assert ids == {f"n{i}" for i in range(len(devices_of(doc)))} | {f"c{i}" for i in range(len(devices_of(doc)))}
    # a device actually called like a node id is still just a label
    doc = {"version": 1, "roots": [device("n1", [device("n0")])], "unattached": [], "summary": {}}
    out = render_mermaid(doc)
    assert 'n0["n1<br/>' in out and 'n1["n0<br/>' in out and "n0 -->|\"port 1, 1000 Mbps\"| n1" in out


@pytest.mark.parametrize("name", NASTY[:6])
def test_the_command_with_a_hostile_name_prints_only_grammar_lines(fake_client, name, capsys):
    fake_client.session.fx = poison(fake_client.session.fx)
    fx = fake_client.session.fx
    for record in [*fx["devices"], *fx["legacy"]["device"], *fx["legacy"]["sta"]]:   # a name that is pure syntax
        record["name"] = name
    for fmt, grammar in (("mermaid", M_LINES), ("dot", D_LINES)):
        code, out, _ = run_command(fake_client, ["topology", "--format", fmt, "--clients"])
        assert code == 0
        assert_grammar(out.rstrip("\n"), grammar)


def test_the_fixture_with_the_hostile_suffix_is_still_clean(fake_client):
    fake_client.session.fx = poison(fake_client.session.fx)
    for fmt, grammar in (("mermaid", M_LINES), ("dot", D_LINES)):
        code, out, _ = run_command(fake_client, ["topology", "--format", fmt, "--clients"])
        assert code == 0
        assert_grammar(out.rstrip("\n"), grammar)
        assert "forged" in out and "\x1b" not in out and "\u202e" not in out
    names = {labels[0] for labels in mermaid_labels(run_command(fake_client, ["topology", "--format", "mermaid"])[1]).values()}
    assert any(name.startswith("Office Switch") and "forged" in name for name in names)


# -- the command line --------------------------------------------------------------------------------

def test_format_choices_and_the_registry():
    assert TOPOLOGY_FORMATS == ("text", "mermaid", "dot") and set(GRAPH_RENDERERS) == {"mermaid", "dot"}


def test_the_command_prints_each_format(fake_client):
    code, text, _ = run_command(fake_client, ["topology", "--no-emoji"])
    assert code == 0 and run_command(fake_client, ["topology", "--no-emoji", "--format", "text"])[1] == text
    for fmt, start in (("mermaid", "flowchart TD\n"), ("dot", "digraph topology {\n")):
        code, out, err = run_command(fake_client, ["topology", "--format", fmt])
        assert code == 0 and out.startswith(start) and "Findings on these devices" not in out
        assert fake_client.session.posts == []
        assert run_command(fake_client, ["topology", "--format", fmt, "--clients", "--no-emoji"])[1] != out


@pytest.mark.parametrize("fmt", ["mermaid", "dot"])
def test_json_and_a_graph_format_are_refused_with_exit_64(fake_client, monkeypatch, fmt, capsys):
    monkeypatch.setenv("UNIFI_URL", "https://controller")
    monkeypatch.setenv("UNIFI_API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    with pytest.raises(SystemExit) as caught:
        cli.main(["topology", "--json", "--format", fmt])
    assert caught.value.code == 64
    assert f"--json and --format {fmt} cannot be combined" in capsys.readouterr().err
    assert run_command(fake_client, ["topology", "--json", "--format", "text"])[0] == 0


def test_an_unknown_format_is_a_usage_error(fake_client, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.build_parser().parse_args(["topology", "--format", "svg"])
    assert caught.value.code == 64
    assert "invalid choice" in capsys.readouterr().err


def test_the_demo_draws_the_graph(capsys):
    assert cli.main(["--demo", "topology", "--format", "mermaid"]) == 0
    assert capsys.readouterr().out.startswith("flowchart TD\n")
