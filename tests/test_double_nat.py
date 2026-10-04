"""Is the gateway behind NAT? Judged from the address of its WAN port, with no outside lookup."""

import json

import pytest

from unifi_sentinel import cli
from unifi_sentinel.diagnose import WARNING, apply_ignores, diagnose
from unifi_sentinel.settings import IgnoreRule
from unifi_sentinel.snapshot import Needs, collect_snapshot
from unifi_sentinel.wan import build_wan, classify_wan_address, nat_status, render_text, to_json

# -- the classifier --------------------------------------------------------------------------


@pytest.mark.parametrize("ip, kind", [
    ("10.0.0.2", "private"), ("10.255.255.254", "private"),
    ("172.16.0.5", "private"), ("172.31.255.1", "private"),
    ("192.168.1.2", "private"), ("192.168.255.255", "private"),
    ("fd12:3456:789a::1", "private"), ("fc00::1", "private"),
    ("100.64.0.1", "cgnat"), ("100.100.100.100", "cgnat"), ("100.127.255.254", "cgnat"),
    ("169.254.12.34", "link_local"), ("fe80::1", "link_local"),
    ("192.0.2.10", "public"), ("203.0.113.9", "public"), ("198.51.100.7", "public"),     # documentation ranges
    ("8.8.8.8", "public"), ("2001:db8::10", "public"), ("2606:4700::1111", "public"),
    ("172.15.255.255", "public"), ("172.32.0.1", "public"),                                # just outside 172.16/12
    ("100.63.255.255", "public"), ("100.128.0.1", "public"),                                 # just outside 100.64/10
    ("192.169.0.1", "public"), ("11.0.0.1", "public"),
    ("127.0.0.1", "unknown"), ("224.0.0.1", "unknown"), ("0.0.0.1", "unknown"),
    ("::1", "unknown"), ("ff02::1", "unknown"),
    ("  192.168.0.9  ", "private"),
])
def test_classification_by_range(ip, kind):
    assert classify_wan_address(ip) == kind


@pytest.mark.parametrize("value", [None, "", "   ", "0.0.0.0", "::"])
def test_no_address_means_the_wan_is_down_not_nat(value):
    assert classify_wan_address(value) == "none"


@pytest.mark.parametrize("value", ["not-an-ip", "10.0.0", "10.0.0.256", "10.0.0.1/24", "dhcp", 12])
def test_malformed_values_are_unknown_and_never_flagged(value):
    assert classify_wan_address(value) == "unknown"


# -- the status ---------------------------------------------------------------------------------

def snap_with(fake_client, wan_ip, **extra):
    for entry in fake_client.session.fx["legacy"]["health"]:
        if entry.get("subsystem") == "wan":
            if wan_ip is None:
                entry.pop("wan_ip", None)
            else:
                entry["wan_ip"] = wan_ip
            entry.update(extra)
    return collect_snapshot(fake_client, "default", Needs(health=True, speedtests=True))


def test_the_fixture_has_a_public_address_and_nothing_to_say(fake_client):
    status = nat_status(snap_with(fake_client, "192.0.2.10"))
    assert status == {"wan_ip": "192.0.2.10", "kind": "public", "message": ""}


def test_a_private_address_is_double_nat(fake_client):
    status = nat_status(snap_with(fake_client, "192.168.1.2"))
    assert status["kind"] == "private" and status["wan_ip"] == "192.168.1.2"
    assert "192.168.1.2" in status["message"] and "double NAT" in status["message"] and "bridge mode" in status["message"]


def test_a_shared_address_is_carrier_grade_nat(fake_client):
    status = nat_status(snap_with(fake_client, "100.72.9.9"))
    assert status["kind"] == "cgnat"
    assert status["message"].startswith("WAN address 100.72.9.9 is in the carrier-grade NAT range (100.64.0.0/10)")
    assert "inbound port forwards and some VPNs will not work" in status["message"]


def test_a_link_local_address_means_no_address_from_the_isp(fake_client):
    status = nat_status(snap_with(fake_client, "169.254.7.7"))
    assert status["kind"] == "link_local" and "got no address from the ISP" in status["message"]


@pytest.mark.parametrize("wan_ip", [None, "", "0.0.0.0", "garbage"])
def test_a_missing_or_unusable_address_gives_no_message(fake_client, wan_ip):
    status = nat_status(snap_with(fake_client, wan_ip))
    assert status["message"] == "" and status["kind"] in ("none", "unknown")


def test_no_wan_health_entry_at_all(fake_client):
    snap = snap_with(fake_client, "10.0.0.2")
    snap.health = []
    assert nat_status(snap) == {"wan_ip": "", "kind": "none", "message": ""}


# -- the wan command ---------------------------------------------------------------------------------

def test_build_wan_carries_the_nat_status_in_json(fake_client):
    wan = build_wan(snap_with(fake_client, "100.72.9.9"))
    assert wan["nat"]["kind"] == "cgnat" and wan["nat"]["wan_ip"] == "100.72.9.9"
    assert json.loads(to_json(wan))["nat"]["kind"] == "cgnat"
    assert set(wan["nat"]) == {"wan_ip", "kind", "message"}


def test_wan_text_says_it_for_private_and_shared_addresses(fake_client):
    private = render_text(build_wan(snap_with(fake_client, "192.168.1.2")))
    assert "  NAT: WAN address 192.168.1.2 is private" in private and "double NAT" in private
    shared = render_text(build_wan(snap_with(fake_client, "100.72.9.9")))
    assert "  NAT: WAN address 100.72.9.9 is in the carrier-grade NAT range" in shared


def test_wan_text_is_honest_about_what_a_public_address_proves(fake_client):
    text = render_text(build_wan(snap_with(fake_client, "192.0.2.10")))
    assert "NAT: none seen (public WAN address; a modem doing NAT in front of the gateway cannot be seen)" in text


def test_wan_text_has_no_nat_line_without_an_address(fake_client):
    assert "NAT:" not in render_text(build_wan(snap_with(fake_client, None)))


# -- diagnose -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("wan_ip, code", [("192.168.1.2", "wan.double_nat"), ("10.20.30.40", "wan.double_nat"),
                                          ("100.72.9.9", "wan.cgnat"), ("169.254.7.7", "wan.link_local_address")])
def test_diagnose_warns_for_each_case(fake_client, wan_ip, code):
    found = [f for f in diagnose(snap_with(fake_client, wan_ip)) if f.code == code]
    assert len(found) == 1
    (f,) = found
    assert (f.severity, f.subject) == (WARNING, "wan") and wan_ip in f.message


def test_diagnose_is_quiet_for_public_missing_and_unknown_addresses(fake_client):
    for wan_ip in ("192.0.2.10", "8.8.8.8", None, "0.0.0.0", "garbage"):
        codes = {f.code for f in diagnose(snap_with(fake_client, wan_ip))}
        assert not codes & {"wan.double_nat", "wan.cgnat", "wan.link_local_address"}, wan_ip


def test_a_deliberate_double_nat_can_be_ignored(fake_client):
    found = [f for f in diagnose(snap_with(fake_client, "192.168.1.2")) if f.code == "wan.double_nat"]
    kept, ignored = apply_ignores(found, (IgnoreRule(subject="wan", message="double NAT", reason="ISP router, lab"),))
    assert kept == [] and len(ignored) == 1


def test_the_message_text_matches_the_issue_example_for_cgnat(fake_client):
    (f,) = [f for f in diagnose(snap_with(fake_client, "100.72.9.9")) if f.code == "wan.cgnat"]
    assert f.message == ("WAN address 100.72.9.9 is in the carrier-grade NAT range (100.64.0.0/10): the ISP shares "
                         "one public address between customers, so inbound port forwards and some VPNs will not work")


# -- the command line -------------------------------------------------------------------------------------

def run(fake_client, monkeypatch, argv):
    fake_client.session.fx["legacy"]["device"][0]["overheating"] = False
    monkeypatch.setenv("CONTROLLER_URL", "https://controller")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    return cli.main(argv)


def test_cli_wan_and_diagnose_end_to_end(fake_client, monkeypatch, capsys):
    for entry in fake_client.session.fx["legacy"]["health"]:
        if entry.get("subsystem") == "wan":
            entry["wan_ip"] = "100.72.9.9"
    assert run(fake_client, monkeypatch, ["wan"]) == 0
    assert "NAT: WAN address 100.72.9.9 is in the carrier-grade NAT range" in capsys.readouterr().out
    assert run(fake_client, monkeypatch, ["wan", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["nat"]["kind"] == "cgnat"
    assert run(fake_client, monkeypatch, ["diagnose", "--json", "--no-events"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert [f["severity"] for f in doc["findings"] if f["code"] == "wan.cgnat"] == ["warning"]
