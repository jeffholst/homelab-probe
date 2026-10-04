"""The firewall view: policies, port forwards, the zone matrix and the findings (zone-based firewall)."""

import json
import re
from pathlib import Path

import pytest
from docs_support import all_docs_text

from homelab_probe import cli
from homelab_probe.client import UniFiAPIError
from homelab_probe.firewall import FIREWALL_CODES, build_firewall, render_text, to_json
from homelab_probe.snapshot import FirewallData, Needs, Snapshot, collect_snapshot, describe_snapshot

NEEDS = Needs(firewall=True, reservations=True)


def snapshot(fake_client, change=None):
    """A snapshot of the fixture's firewall, after ``change(fx)`` altered the controller's data."""
    if change:
        change(fake_client.session.fx)
    return collect_snapshot(fake_client, "default", NEEDS)


def policy_named(fx, name):
    return next(p for p in fx["legacy_v2"]["firewall-policies"] if p["name"] == name)


def findings(report):
    return {(f["code"], f["subject"]): f for f in report["findings"]}


def row(report, name):
    return next(r for r in report["policies"] if r["Name"] == name)


# -- the view ------------------------------------------------------------------------------------------

def test_the_view_lists_your_own_policies_and_counts_the_built_in_ones(fake_client):
    report = build_firewall(snapshot(fake_client))
    assert report["style"] == "zone-based" and report["notes"] == []
    assert len(report["policies"]) == 6 and report["built_in_hidden"] == 6 and report["built_in_policies"] == 6
    assert not any(r["Built in"] for r in report["policies"])
    everything = build_firewall(snapshot(fake_client), show_all=True)
    assert len(everything["policies"]) == 12 and everything["built_in_hidden"] == 0
    assert everything["built_in_policies"] == 6


def test_a_policy_row_says_what_each_end_matches(fake_client):
    report = build_firewall(snapshot(fake_client))
    dns = row(report, "Allow IoT DNS")
    assert (dns["From"], dns["To"], dns["Action"], dns["On"]) == ("IoT Zone", "Internal", "allow", "yes")
    assert (dns["Source"], dns["Destination"], dns["Protocol"], dns["Hits"]) == (
        "IoT", "10.0.0.53 port 53", "TCP/UDP", 42)
    ssh = row(report, "Admin SSH")
    assert ssh["Source"] == "10.0.0.10 port 49152-65535" and ssh["Destination"] == "any port 22"
    assert row(report, "Open Inbound")["Hits"] == "" and row(report, "Open Inbound")["Protocol"] == "any"
    assert "index" not in dns


def test_inverted_matches_other_targets_and_unknown_names_are_spelled_out(fake_client):
    def change(fx):
        p = policy_named(fx, "Allow IoT DNS")
        p["source"].update(match_opposite_networks=True, network_ids=["net-1", "net-gone"])
        p["destination"].update(match_opposite_ips=True, match_opposite_ports=True)
        q = policy_named(fx, "Admin SSH")
        q["source"] = {**q["source"], "matching_target": "CLIENT", "port_matching_type": "ANY"}
        q["destination"]["zone_id"] = "z-unknown"
        q["protocol"] = "icmpv6"
        r = policy_named(fx, "Guest Printer")
        r["destination"] = {**r["destination"], "matching_target": "IP", "ips": []}

    report = build_firewall(snapshot(fake_client, change))
    assert row(report, "Allow IoT DNS")["Source"] == "not Main, a network that no longer exists"
    assert row(report, "Allow IoT DNS")["Destination"] == "not 10.0.0.53 port not 53"
    ssh = row(report, "Admin SSH")
    assert (ssh["Source"], ssh["To"], ssh["Protocol"]) == ("client", "(unknown zone)", "ICMPV6")
    assert row(report, "Guest Printer")["Destination"] == "no address"


def test_a_search_keeps_the_matching_policies_and_forwards(fake_client):
    report = build_firewall(snapshot(fake_client), search="game")
    assert report["policies"] == [] and [f["Name"] for f in report["port_forwards"]] == ["Game Server",
                                                                                          "Game Server Backup"]
    assert [r["Name"] for r in build_firewall(snapshot(fake_client), search="iot")["policies"]] == [
        "Guest Printer", "Allow IoT DNS", "Old Camera Access"]


def test_the_port_forwards_are_listed_with_their_target_and_interface(fake_client):
    def change(fx):
        fx["legacy_rest"]["portforward"][0].update(pfwd_interface="both", src="203.0.113.9", proto="icmp")
        fx["legacy_rest"]["portforward"][1].update(pfwd_interface="wan2", dst_port="", fwd_port=None, proto=None)
        del fx["legacy_rest"]["portforward"][2]["fwd"]

    report = build_firewall(snapshot(fake_client, change))
    first, second, third = report["port_forwards"][:3]
    assert (first["Interface"], first["Only from"], first["Protocol"]) == ("WAN and WAN 2", "203.0.113.9", "ICMP")
    assert (second["Interface"], second["External port"], second["Forwards to"], second["Protocol"]) == (
        "WAN 2", "", "10.0.0.11:?", "TCP/UDP")
    assert third["Forwards to"] == "?:27015"
    assert not any(key.startswith("_") for key in first)


def test_zones_and_the_matrix(fake_client):
    report = build_firewall(snapshot(fake_client))
    zones = {z["Zone"]: z for z in report["zones"]}
    assert zones["Internal"] == {"Zone": "Internal", "Built in": True, "Networks": ["Main"]}
    assert zones["IoT Zone"]["Built in"] is False and zones["IoT Zone"]["Networks"] == ["IoT"]
    cells = {r["From"]: r["cells"] for r in report["matrix"]}
    assert cells["Internal"]["z-ext"] == {"action": "A", "policies": 1}
    assert cells["Internal"]["z-gw"] == {"action": "C", "policies": 2}
    assert cells["External"]["z-gw"] == {"action": "B", "policies": 0}
    assert cells["External"]["z-int"]["action"] == "C"


def test_a_matrix_cell_without_policies_or_action_is_empty_and_unknown_zones_are_named(fake_client):
    def change(fx):
        fx["legacy_v2"]["firewall/zone-matrix"][0]["data"][1].update(action=None, policy_count=0)
        fx["legacy_v2"]["firewall/zone-matrix"][1]["data"][0]["_id"] = "z-unknown"
        fx["legacy_v2"]["firewall/zone"].append({"_id": "z-two", "name": "Internal", "zone_key": None, "network_ids": []})
        fx["legacy_v2"]["firewall/zone-matrix"][2]["data"][0].pop("policy_count")
        fx["legacy_v2"]["firewall/zone-matrix"][3]["_id"] = "z-lost"
        fx["legacy_v2"]["firewall/zone-matrix"].append({"_id": "z-two", "name": "Internal", "data": []})
        fx["legacy_v2"]["firewall/zone-matrix"][0]["data"].append(
            {"_id": "z-two", "action": "block_all", "policy_count": 0})

    report = build_firewall(snapshot(fake_client, change))
    cells = {r["From"]: r["cells"] for r in report["matrix"] if r["cells"]}
    assert cells["Internal"]["z-ext"] == {"action": "-", "policies": 0}
    assert "z-unknown" in cells["External"] and "z-vpn" in cells["External"]
    first_internal = next(r for r in report["matrix"] if r["From"] == "Internal")
    assert first_internal["cells"]["z-int"]["action"] == "A"
    assert first_internal["cells"]["z-two"]["action"] == "B"
    header = next(line for line in render_text(report, zones=True).splitlines() if line.startswith("From \\ To"))
    assert "Internal (8)" in header


# -- when the controller does not answer ---------------------------------------------------------------

def failing(fake_client, monkeypatch, *resources):
    real = fake_client.legacy_v2, fake_client.legacy_rest

    def v2(site_ref, resource):
        if resource in resources:
            raise UniFiAPIError(f"HTTP 404 for {resource}")
        return real[0](site_ref, resource)

    def rest(site_ref, resource):
        if resource in resources:
            raise UniFiAPIError(f"HTTP 404 for {resource}")
        return real[1](site_ref, resource)

    monkeypatch.setattr(fake_client, "legacy_v2", v2)
    monkeypatch.setattr(fake_client, "legacy_rest", rest)


def test_without_zone_based_policies_the_view_says_so_and_still_lists_port_forwards(fake_client, monkeypatch, capsys):
    failing(fake_client, monkeypatch, "firewall-policies", "firewall/zone", "firewall/zone-matrix")
    snap = snapshot(fake_client)
    err = capsys.readouterr().err
    assert "firewall policies unavailable; this controller may use the classic firewall" in err
    assert "firewall zones unavailable; zone names are missing" in err and "zone matrix unavailable" in err
    assert err.index("firewall policies") < err.index("firewall zones") < err.index("zone matrix")
    report = build_firewall(snap, show_all=True)
    assert report["style"] == "unknown" and report["policies"] == [] and "classic firewall" in report["notes"][0]
    assert len(report["port_forwards"]) == 5
    assert {f["code"] for f in report["findings"]} == {"firewall.forward_target_offline", "firewall.forward_no_reservation",
                                                       "firewall.forward_duplicate"}
    text = render_text(report, zones=True)
    assert text.startswith("Firewall: not shown") and "Policies" not in text and "Zone matrix" not in text
    assert "Web Server" in text


def test_port_forwards_that_cannot_be_read_are_not_the_same_as_none(fake_client, monkeypatch, capsys):
    failing(fake_client, monkeypatch, "portforward")
    snap = snapshot(fake_client)
    assert "port forwards unavailable; port forwards are not shown" in capsys.readouterr().err
    report = build_firewall(snap)
    assert report["port_forwards"] is None and "unavailable (see the warning above)" in render_text(report)


def test_no_port_forwards_is_stated(fake_client):
    report = build_firewall(snapshot(fake_client, lambda fx: fx["legacy_rest"].update(portforward=[])))
    assert report["port_forwards"] == [] and "none configured" in render_text(report)


def test_a_snapshot_without_a_firewall_read_has_nothing_to_show():
    report = build_firewall(Snapshot(site={}, devices=[], clients=[]))
    assert report["style"] == "unknown" and report["port_forwards"] is None and report["findings"] == []
    empty = Snapshot(site={}, devices=[], clients=[], firewall=FirewallData(policies=[], zones=[]))
    assert build_firewall(empty)["style"] == "unknown"


def test_a_response_that_is_not_a_list_counts_as_nothing(fake_client, monkeypatch):
    monkeypatch.setattr(fake_client, "legacy_v2", lambda site_ref, resource: {"unexpected": True})
    snap = snapshot(fake_client)
    assert snap.firewall.policies == [] and snap.firewall.zones == [] and snap.firewall.matrix == []


def test_the_snapshot_description_mentions_the_firewall(fake_client):
    text = describe_snapshot(snapshot(fake_client))
    assert "12 firewall policies" in text and "5 port forwards" in text


# -- findings ------------------------------------------------------------------------------------------

def test_the_fixture_has_each_kind_of_finding(fake_client):
    found = findings(build_firewall(snapshot(fake_client)))
    assert {code for code, _ in found} == set(FIREWALL_CODES)
    assert found["firewall.allow_any_from_external", "Open Inbound"]["severity"] == "warning"
    assert found["firewall.rule_missing_network", "Guest Printer"]["severity"] == "warning"
    assert found["firewall.rule_missing_network", "Old Camera Access"]["severity"] == "info"
    assert found["firewall.disabled_rules", "policies"]["message"] == "2 rules of your own are switched off"
    assert found["firewall.forward_target_offline", "Game Server"]["severity"] == "warning"
    assert found["firewall.forward_duplicate", "Game Server Backup"]["message"].endswith("like 'Game Server'")
    assert found["firewall.forward_no_reservation", "Phone Test"]["severity"] == "info"
    assert ("firewall.forward_no_reservation", "Web Server") not in found


@pytest.mark.parametrize("change", [
    lambda p: p.update(enabled=False),
    lambda p: p.update(predefined=True),
    lambda p: p.update(action="BLOCK"),
    lambda p: p.update(protocol="tcp"),
    lambda p: p["destination"].update(matching_target="IP", ips=["10.0.0.9"]),
    lambda p: p["destination"].update(port_matching_type="SPECIFIC", port="443"),
    lambda p: p["source"].update(matching_target="NETWORK", network_ids=["net-1"]),
    lambda p: p["source"].update(zone_id="z-int"),
])
def test_only_a_plain_allow_everything_from_external_is_flagged(fake_client, change):
    snap = snapshot(fake_client, lambda fx: change(policy_named(fx, "Open Inbound")))
    assert not any(f["code"] == "firewall.allow_any_from_external" for f in build_firewall(snap)["findings"])


def test_a_rule_with_two_missing_networks_and_one_that_is_fine(fake_client):
    def change(fx):
        policy_named(fx, "Guest Printer")["destination"]["network_ids"] = ["net-gone", "net-also-gone"]
        policy_named(fx, "Allow IoT DNS")["destination"] = {
            **policy_named(fx, "Allow IoT DNS")["destination"], "matching_target": "NETWORK", "network_ids": ["net-1"]}

    found = findings(build_firewall(snapshot(fake_client, change)))
    assert found["firewall.rule_missing_network", "Guest Printer"]["message"] == (
        "destination matches 2 networks that no longer exist")
    assert ("firewall.rule_missing_network", "Allow IoT DNS") not in found


def test_without_the_network_list_only_an_empty_match_is_called_missing(fake_client):
    snap = snapshot(fake_client)
    snap.networks = []
    found = findings(build_firewall(snap))
    assert ("firewall.rule_missing_network", "Old Camera Access") in found
    assert ("firewall.rule_missing_network", "Guest Printer") not in found


def test_a_single_switched_off_rule_is_worded_in_the_singular(fake_client):
    snap = snapshot(fake_client, lambda fx: policy_named(fx, "Legacy VPN Allow").update(enabled=True))
    assert findings(build_firewall(snap))["firewall.disabled_rules", "policies"]["message"] == (
        "1 rule of your own is switched off")


def test_no_switched_off_rules_no_finding(fake_client):
    def change(fx):
        for p in fx["legacy_v2"]["firewall-policies"]:
            p["enabled"] = True

    codes = {f["code"] for f in build_firewall(snapshot(fake_client, change))["findings"]}
    assert "firewall.disabled_rules" not in codes


def test_port_forward_checks(fake_client):
    def change(fx):
        forwards = fx["legacy_rest"]["portforward"]
        forwards[0]["fwd"] = "10.0.0.2"            # a UniFi device: no reservation needed
        forwards[1]["fwd"] = "not an address"      # nothing to judge
        forwards[3].update(proto="tcp_udp", dst_port="27015")      # overlaps the UDP forward of the same port
        forwards.append({"name": "Other Interface", "enabled": True, "proto": "udp", "dst_port": "27015",
                         "fwd": "10.0.0.10", "fwd_port": "1", "pfwd_interface": "wan2"})
        forwards.append({"name": "Disabled Copy", "enabled": False, "proto": "udp", "dst_port": "27015",
                         "fwd": "10.0.0.10", "fwd_port": "1", "pfwd_interface": "wan"})

    found = findings(build_firewall(snapshot(fake_client, change)))
    assert ("firewall.forward_no_reservation", "Web Server") not in found
    assert not any(subject == "Phone Test" for _, subject in found)
    assert ("firewall.forward_duplicate", "Game Server Backup") in found
    assert not any(subject in ("Other Interface", "Disabled Copy") for _, subject in found)


def test_a_reservation_for_another_mac_does_not_exempt_the_forward_target(fake_client):
    def change(fx):
        fx["legacy"]["alluser"][1].update(fixed_ip="10.0.0.11")

    found = findings(build_firewall(snapshot(fake_client, change)))
    assert ("firewall.forward_no_reservation", "Phone Test") in found


def test_a_reservation_record_without_a_mac_or_a_usable_address_is_skipped(fake_client):
    def change(fx):
        fx["legacy"]["alluser"].append({"use_fixedip": True, "fixed_ip": "10.0.0.11"})            # no MAC
        fx["legacy"]["alluser"].append({"use_fixedip": True, "fixed_ip": "garbage", "mac": "bb:00:00:00:00:02"})

    found = findings(build_firewall(snapshot(fake_client, change)))
    assert ("firewall.forward_no_reservation", "Phone Test") in found


def test_an_offline_device_does_not_exempt_a_client_without_a_reservation(fake_client):
    def change(fx):
        fx["devices"][3]["ipAddress"] = "10.0.0.11"

    found = findings(build_firewall(snapshot(fake_client, change)))
    assert ("firewall.forward_no_reservation", "Phone Test") in found


def test_without_the_client_history_no_reservation_is_claimed(fake_client):
    snap = snapshot(fake_client)
    snap.all_users = []
    assert "firewall.forward_no_reservation" not in {f["code"] for f in build_firewall(snap)["findings"]}


def test_every_code_used_is_listed_and_every_listed_code_is_used():
    source = (Path(__file__).parent.parent / "homelab_probe" / "firewall.py").read_text()
    used = set(re.findall(r'code="(firewall\.[a-z_]+)"', source))
    assert used == set(FIREWALL_CODES)
    readme = all_docs_text()
    assert all(f"`{code}`" in readme for code in FIREWALL_CODES)
    assert all(re.fullmatch(r"firewall\.[a-z_]+", code) for code in FIREWALL_CODES)


# -- rendering and the command --------------------------------------------------------------------------

def run(fake_client, monkeypatch, argv):
    monkeypatch.setenv("UNIFI_URL", "https://controller.example")
    monkeypatch.setenv("UNIFI_API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    return cli.main(argv)


def test_the_command_prints_the_view_and_always_exits_zero(fake_client, monkeypatch, capsys):
    assert run(fake_client, monkeypatch, ["firewall", "--no-emoji"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Firewall: zone-based (7 zones, 6 policies shown)")
    assert "Zone matrix" not in out and "Policies of your own (6 built-in policies not shown, use --all)" in out
    assert "[WARNING ] Open Inbound" in out


def test_zones_all_and_search_are_honored(fake_client, monkeypatch, capsys):
    assert run(fake_client, monkeypatch, ["firewall", "--zones", "--all", "--no-emoji", "--search", "block"]) == 0
    out = capsys.readouterr().out
    assert "Block All Traffic" in out and "Open Inbound" not in out.split("Findings")[0].split("Zones")[0]
    assert "Zone matrix" in out and "IoT Zone (yours): IoT" in out
    assert "\nPolicies\n" in out


def test_json_output_is_complete_and_raw(fake_client, monkeypatch, capsys):
    assert run(fake_client, monkeypatch, ["firewall", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["version"] == 1 and data["style"] == "zone-based" and len(data["findings"]) == 7
    assert set(data) == {"version", "style", "policies", "built_in_policies", "built_in_hidden", "port_forwards",
                         "zones", "matrix", "findings", "notes"}
    assert to_json(data) == json.dumps(data, indent=2)


def test_the_command_reads_the_firewall_and_nothing_else_it_does_not_declare(fake_client, monkeypatch, capsys):
    run(fake_client, monkeypatch, ["firewall"])
    capsys.readouterr()
    assert not fake_client.session.posts
    assert any(path.endswith("/firewall-policies") for path in fake_client.session.calls)
    assert not any(path.endswith("/stat/health") or path.endswith("/speedtest") for path in fake_client.session.calls)


def test_hostile_policy_and_forward_names_cannot_forge_output(fake_client, monkeypatch, capsys):
    def change(fx):
        policy_named(fx, "Open Inbound")["name"] = "Open\x1b[31m\n[CRITICAL] forged: all clear\u202e"
        fx["legacy_rest"]["portforward"][2]["name"] = "Game\r\x07 Server"

    change(fake_client.session.fx)
    assert run(fake_client, monkeypatch, ["firewall", "--no-emoji", "--all"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\u202e" not in out and "\x07" not in out
    assert not any(line.startswith("[CRITICAL] forged") for line in out.splitlines())
