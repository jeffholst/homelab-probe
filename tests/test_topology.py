import json

import pytest

from unifi_sentinel import cli
from unifi_sentinel.diagnose import Finding
from unifi_sentinel.settings import DiagnoseSettings, IgnoreRule
from unifi_sentinel.snapshot import Snapshot, collect_snapshot
from unifi_sentinel.topology import _assign_findings, build_topology, render_text, to_json


def dev(mac, name, parent=None, port=None, speed=None, kind="usw", own=None, **kw):
    """A legacy device; ``parent`` is the parent's MAC."""
    d = {"mac": mac, "name": name, "type": kind, "model": name.upper(), "port_table": []}
    if parent:
        d["uplink"] = {"uplink_mac": parent, "uplink_remote_port": port, "speed": speed,
                       "max_speed": speed, "up": True, "port_idx": own}
    d.update(kw)
    return d


def integ(mac, name, state="ONLINE"):
    return {"id": name.lower(), "macAddress": mac, "name": name, "model": name, "state": state}


def tree(legacy, integration=(), clients=(), **kw):
    snap = Snapshot(site={}, devices=list(integration), clients=[], legacy_devices=list(legacy),
                    legacy_clients=list(clients), **kw)
    return build_topology(snap, kw.pop("settings", None))


def flat(nodes):
    for n in nodes:
        yield n
        yield from flat(n["children"])


def names(nodes):
    return [n["name"] for n in nodes]


GW = dev("AA:00", "GW", kind="udm")


# -- the fixture tree --------------------------------------------------------

@pytest.fixture
def fixture_tree(fake_client):
    return build_topology(collect_snapshot(fake_client, "default"), with_clients=True)


def test_fixture_tree_shape_counts_and_flags(fixture_tree):
    (root,) = fixture_tree["roots"]
    assert root["name"] == "Gateway" and root["model"] == "UCG Max" and root["parent"] == ""
    (switch,) = root["children"]
    assert (switch["name"], switch["parent"], switch["parent_port"]) == ("Office Switch", "Gateway", 2)
    assert names(switch["children"]) == ["Office AP", "Garage AP"]           # ordered by the switch's port 2, 5
    ap, garage = switch["children"]
    assert garage["online"] is False and ap["online"] is True
    assert (switch["clients"]["wired"], ap["clients"]["wireless"]) == (1, 1)
    assert [c["name"] for c in switch["wired_clients"]] == ["desktop"]
    assert fixture_tree["unattached"] == []


def test_fixture_link_speed_below_capability_is_reported(fixture_tree):
    switch = fixture_tree["roots"][0]["children"][0]
    assert (switch["speed_mbps"], switch["supports_mbps"]) == (100, 1000)
    assert fixture_tree["summary"]["below_max"] == 1


def test_only_warnings_and_criticals_flag_a_device(fixture_tree):
    switch = fixture_tree["roots"][0]["children"][0]
    severities = {f["severity"] for f in switch["findings"]}
    assert severities <= {"warning", "critical"} and severities                # the 100 Mbps info is left out
    assert any(f["subject"] == "Office Switch port 2" for f in switch["findings"])
    assert not any("negotiated at 100 Mbps" in f["message"] and f["severity"] == "info"
                   for n in flat(fixture_tree["roots"]) for f in n["findings"])
    garage = switch["children"][1]
    assert [f["message"] for f in garage["findings"]] == ["device is offline"]
    assert fixture_tree["summary"] == {"devices": 4, "offline": 1, "with_findings": 3, "below_max": 1,
                                       "unattached": 0, "clients": 2}


# -- structure and edge cases ----------------------------------------------

def test_children_are_ordered_by_parent_port_then_name_with_unknown_ports_last():
    legacy = [GW, dev("B1", "Zed", "AA:00", 3), dev("B2", "Alpha", "AA:00", 3),
              dev("B3", "Mid", "AA:00", 1), dev("B4", "NoPort", "AA:00", None)]
    assert names(tree(legacy)["roots"][0]["children"]) == ["Mid", "Alpha", "Zed", "NoPort"]


def test_deep_chains_nest_and_the_root_wan_link_is_not_drawn():
    gw = dict(GW, uplink={"uplink_mac": "ISP", "speed": 1000, "up": True, "uplink_remote_port": 1})
    t = tree([gw, dev("B1", "Core", "AA:00", 1, 2500), dev("B2", "Edge", "B1", 7, 1000),
              dev("B3", "AP", "B2", 2, 1000, kind="uap")])
    root = t["roots"][0]
    assert root["speed_mbps"] is None                                         # a gateway's uplink is its WAN
    chain = [root]
    while chain[-1]["children"]:
        chain.append(chain[-1]["children"][0])
    assert names(chain) == ["GW", "Core", "Edge", "AP"]
    assert [n["parent_port"] for n in chain[1:]] == [1, 7, 2]


def test_a_device_with_no_uplink_information_is_unattached():
    t = tree([GW, dev("B1", "Lonely")], integration=[integ("AA:00", "GW"), integ("B1", "Lonely")])
    assert names(t["roots"][0]["children"]) == []
    (lonely,) = t["unattached"]
    assert lonely["name"] == "Lonely" and lonely["reason"] == "no uplink information"
    assert t["summary"]["unattached"] == 1 and t["summary"]["devices"] == 2


def test_an_uplink_to_an_unknown_device_is_unattached_with_the_mac():
    (n,) = tree([GW, dev("B1", "Orphan", "ZZ:99", 4)])["unattached"]
    assert n["reason"] == "uplink to unknown device ZZ:99"


def test_uplink_loops_and_self_parents_cannot_hang_or_hide_devices():
    legacy = [GW, dev("B1", "A", "B2", 1), dev("B2", "B", "B1", 2), dev("B3", "Self", "B3", 1)]
    t = tree(legacy)
    assert names(t["roots"][0]["children"]) == []
    assert sorted(names(t["unattached"])) == ["A", "B", "Self"]               # every device is accounted for
    assert {n["reason"] for n in t["unattached"]} == {
        "not reachable from a gateway (uplink loop or detached branch)"}
    assert t["summary"]["devices"] == 4


def test_no_gateway_means_no_roots_and_everything_is_unattached():
    t = tree([dev("B1", "SW1"), dev("B2", "SW2", "B1", 1)])
    assert t["roots"] == [] and sorted(names(t["unattached"])) == ["SW1", "SW2"]
    assert "No gateway found." in render_text(t, emoji=False)


def test_an_integration_only_parent_gives_the_tree_without_ports():
    legacy = [GW, dev("B1", "AP", kind="uap")]                                # no legacy uplink at all
    t = tree(legacy, integration=[integ("AA:00", "GW"), integ("B1", "AP")],
             device_details={"ap": {"uplink": {"deviceId": "gw"}}})
    (ap,) = t["roots"][0]["children"]
    assert ap["name"] == "AP" and ap["parent_port"] is None and ap["speed_mbps"] is None
    assert "`-- AP" in render_text(t, emoji=False)                             # no "port N ->" prefix


def test_offline_state_comes_from_the_integration_api():
    t = tree([GW, dev("B1", "SW", "AA:00", 1)],
             integration=[integ("AA:00", "GW"), integ("B1", "SW", state="OFFLINE")])
    assert [n["online"] for n in flat(t["roots"])] == [True, False]


# -- client counts ---------------------------------------------------------

def test_client_counts_per_device_and_unavailable_client_data():
    legacy = [GW, dev("B1", "SW", "AA:00", 1), dev("B2", "AP", "B1", 2, kind="uap")]
    clients = [{"mac": "C1", "name": "pc2", "is_wired": True, "sw_mac": "b1", "sw_port": 4, "ip": "10.0.0.2"},
               {"mac": "C2", "name": "pc1", "is_wired": True, "sw_mac": "B1", "sw_port": 2, "ip": "10.0.0.1"},
               {"mac": "C3", "name": "phone", "is_wired": False, "ap_mac": "b2"},
               {"mac": "C4", "name": "tv", "is_wired": False, "ap_mac": "B2"},
               {"mac": "C5", "name": "stray", "is_wired": True, "sw_mac": "ZZ:99"}]
    snap = Snapshot(site={}, devices=[], clients=[], legacy_devices=legacy, legacy_clients=clients)
    t = build_topology(snap, with_clients=True)
    gw, = t["roots"]
    sw, = gw["children"]
    ap, = sw["children"]
    assert sw["clients"] == {"wired": 2, "wireless": 0, "total": 2}
    assert ap["clients"] == {"wired": 0, "wireless": 2, "total": 2} and gw["clients"]["total"] == 0
    assert [(c["port"], c["name"]) for c in sw["wired_clients"]] == [(2, "pc1"), (4, "pc2")]   # by port
    assert t["summary"]["clients"] == 4                                        # the stray one is on no known device
    assert "wired_clients" not in build_topology(snap)["roots"][0]             # only with --clients

    nodata = build_topology(Snapshot(site={}, devices=[], clients=[], legacy_devices=legacy))
    assert nodata["roots"][0]["clients"] is None and nodata["summary"]["clients"] is None
    assert "client" not in render_text(nodata, emoji=False).split("\n")[1]


# -- findings assignment ---------------------------------------------------

def test_findings_go_to_their_exact_device_identity():
    names_ = {"1": "SW", "2": "SW 2"}
    def f(subject, mac=None):
        return Finding("warning", subject, "m", mac)

    got = _assign_findings([f("SW", "1"), f("SW port 1", "1"), f("SW 2", "2"),
                            f("same name", "missing"), f("client named SW")], names_)
    assert {mac: [x.subject for x in fs] for mac, fs in got.items()} == {
        "1": ["SW", "SW port 1"], "2": ["SW 2"]}


def test_the_ignore_list_hides_findings_from_the_tree(fake_client):
    snap = collect_snapshot(fake_client, "default")
    settings = DiagnoseSettings(ignore=(IgnoreRule(subject="Garage AP", reason="spare"),))
    garage = build_topology(snap, settings)["roots"][0]["children"][0]["children"][1]
    assert garage["findings"] == [] and garage["online"] is False             # still shown offline


# -- rendering ---------------------------------------------------------------

def test_ascii_and_unicode_trees(fixture_tree):
    ascii_text = render_text(fixture_tree, emoji=False)
    assert ascii_text.splitlines()[:5] == [
        "Gateway (UCG Max)   [CRITICAL]",
        "`-- port 2 -> Office Switch (100 Mbps, supports 1000)   1 client   [WARNING x8]",
        "    +-- port 2 -> Office AP   1 client",
        "    `-- port 5 -> Garage AP   [OFFLINE]   [WARNING]",
        ""]
    assert "└" not in ascii_text and "⚠" not in ascii_text
    uni = render_text(fixture_tree, emoji=True).splitlines()
    assert uni[1].startswith("└── port 2 -> Office Switch") and uni[1].endswith("⚠️ 8")
    assert uni[2].startswith("    ├── port 2 -> Office AP") and uni[3].startswith("    └── port 5 -> Garage AP")


def test_summary_findings_section_and_unattached_section(fixture_tree):
    text = render_text(fixture_tree, emoji=False)
    assert "Findings on these devices:" in text
    assert "  [WARNING ] Garage AP: device is offline" in text
    assert text.rstrip().splitlines()[-1] == (
        "4 devices, 2 clients, 1 offline, 1 link(s) below capability, 3 with findings")
    t = tree([GW, dev("B1", "Lonely")])
    out = render_text(t, emoji=False)
    assert "Unattached (not reachable from a gateway):" in out
    assert "Lonely" in out and "(no uplink information)" in out
    assert render_text(tree([GW]), emoji=False).splitlines()[0] == "GW (GW)"      # root shows its model


def test_clients_listing_lines_up_with_the_child_connectors(fixture_tree):
    lines = render_text(fixture_tree, emoji=False, with_clients=True).splitlines()
    assert lines[1].startswith("`-- port 2 -> Office Switch")
    assert lines[2] == "    - port 3: desktop"                                  # under the switch, same indent as children
    assert lines[3].startswith("    +-- port 2 -> Office AP")


def test_json_is_nested_and_complete(fixture_tree):
    parsed = json.loads(to_json(fixture_tree))
    assert set(parsed) == {"version", "roots", "unattached", "summary"}
    node = parsed["roots"][0]["children"][0]
    assert {"name", "mac", "type", "model", "online", "parent", "parent_port", "speed_mbps",
            "supports_mbps", "clients", "findings", "children", "wired_clients"} <= set(node)
    assert node["children"][1]["name"] == "Garage AP"


# -- command line ----------------------------------------------------------

def _run(fake_client, monkeypatch, argv):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    return cli.main(argv)


def test_cli_topology_text_clients_and_json(fake_client, monkeypatch, capsys):
    assert _run(fake_client, monkeypatch, ["topology", "--no-emoji"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Gateway (UCG Max)   [CRITICAL]\n`-- port 2 -> Office Switch") and "desktop" not in out

    assert _run(fake_client, monkeypatch, ["topology", "--no-emoji", "--clients"]) == 0
    assert "- port 3: desktop" in capsys.readouterr().out

    assert _run(fake_client, monkeypatch, ["topology", "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["roots"][0]["name"] == "Gateway" and "wired_clients" not in parsed["roots"][0]
    assert fake_client.session.posts == []                                     # topology never POSTs


def test_cli_topology_uses_the_settings_file(fake_client, monkeypatch, capsys, tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[[ignore]]\nsubject = "Garage AP"\nreason = "spare AP"\n')
    assert _run(fake_client, monkeypatch, ["topology", "--no-emoji", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "Garage AP   [OFFLINE]" in out                                      # still drawn, as offline
    assert "[WARNING] Garage AP" not in out and "Garage AP: device is offline" not in out   # flag and finding hidden

    cfg.write_text("[thresholds]\nbogus = 1\n")
    assert _run(fake_client, monkeypatch, ["topology", "--config", str(cfg)]) == cli.EXIT_ERROR
    assert "unknown [thresholds] key" in capsys.readouterr().err
