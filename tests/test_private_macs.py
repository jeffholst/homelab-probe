"""Randomized (private) MAC addresses: detection, the flag in the listings, and the diagnose info."""

import json

import pytest

from unifi_sentinel import cli
from unifi_sentinel.client_view import build_client_detail, find_clients, render_detail
from unifi_sentinel.diagnose import INFO, WARNING, _private_mac_findings, apply_ignores, diagnose
from unifi_sentinel.new_clients import report
from unifi_sentinel.query import query_rows, render
from unifi_sentinel.snapshot import Needs, collect_snapshot
from unifi_sentinel.util import is_randomized_mac

PRIVATE = "02:00:00:00:00:21"      # locally administered unicast (second digit 2)


# -- the detector ----------------------------------------------------------------------

@pytest.mark.parametrize("mac", [
    "02:00:00:00:00:01", "06:11:22:33:44:55", "0a:11:22:33:44:55", "0E:11:22:33:44:55",
    "fa:11:22:33:44:55", "d2:00:00:00:00:00", "2a-bb-cc-dd-ee-ff", "2A:BB:CC:DD:EE:FF",
    "02bb.ccdd.eeff", "02 00 00 00 00 01", "020000000001", "  02:00:00:00:00:01  "])
def test_locally_administered_unicast_addresses_are_randomized(mac):
    assert is_randomized_mac(mac) is True


@pytest.mark.parametrize("mac", [
    "00:11:22:33:44:55",          # a vendor-assigned address
    "dc:a6:32:00:00:01",          # Raspberry Pi Trading, universally administered
    "f0:18:98:00:00:01",
    "01:00:5e:00:00:01",          # multicast (low bit set)
    "03:00:00:00:00:01", "07:00:00:00:00:01", "0b:00:00:00:00:01", "bb:00:00:00:00:01",   # local bit AND multicast bit
    "ff:ff:ff:ff:ff:ff",
])
def test_vendor_and_multicast_addresses_are_not_randomized(mac):
    assert is_randomized_mac(mac) is False


@pytest.mark.parametrize("mac", [None, "", "  ", "02:00:00:00:00", "02:00:00:00:00:01:02", "zz:00:00:00:00:01",
                                 "02:00:00:00:00:0g", 2, 0x020000000001, ["02:00:00:00:00:01"], "randomized"])
def test_malformed_values_are_never_randomized(mac):
    assert is_randomized_mac(mac) is False


def test_the_digit_rule_in_the_issue_is_the_bit_rule():
    for digit in "0123456789abcdef":
        expected = digit in "26ae"
        assert is_randomized_mac(f"0{digit}:00:00:00:00:01") is expected, digit


# -- listings --------------------------------------------------------------------------------

def add_client(fake_client, mac=PRIVATE, name="pixel", ip="10.0.0.80", reserved=None, connected=True):
    fx = fake_client.session.fx
    if connected:
        fx["clients"].append({"id": "c-private", "name": name, "type": "WIRELESS", "macAddress": mac,
                              "ipAddress": ip, "connectedAt": "2026-01-01T09:00:00Z", "uplinkDeviceId": "ap1"})
        fx["legacy"]["sta"].append({"mac": mac, "name": name, "ip": ip, "is_wired": False, "ap_mac": "aa:00:00:00:00:03",
                                    "essid": "Home", "radio": "ng", "channel": 6})
    user = {"mac": mac, "name": name, "last_ip": ip, "is_wired": False, "first_seen": 1, "last_seen": 2}
    if reserved:
        user.update(use_fixedip=True, fixed_ip=reserved, last_connection_network_id="net-1")
    fx["legacy"]["alluser"].append(user)


def test_query_clients_flags_private_macs(fake_client):
    add_client(fake_client)
    snap = collect_snapshot(fake_client, "default")
    rows = {r["Name"]: r for r in query_rows(snap, "clients")}
    assert rows["pixel"]["Private MAC"] == "yes"
    assert rows["desktop"]["Private MAC"] == "" and rows["phone"]["Private MAC"] == ""
    table = render(list(rows.values()), False, "clients")
    assert "Private MAC" in table.splitlines()[0] and table.count("yes") == 1
    as_json = {r["Name"]: r for r in json.loads(render(list(rows.values()), True, "clients"))}
    assert as_json["pixel"]["Private MAC"] == "yes" and as_json["desktop"]["Private MAC"] == ""


def test_query_clients_flags_offline_private_macs_too(fake_client):
    add_client(fake_client, name="old-pixel", connected=False)
    snap = collect_snapshot(fake_client, "default", Needs(offline=True))
    assert {r["Name"]: r["Private MAC"] for r in query_rows(snap, "clients", include_offline=True)}["old-pixel"] == "yes"


def test_devices_and_the_csv_export_do_not_change(fake_client):
    from unifi_sentinel.export import INVENTORY_COLUMNS
    snap = collect_snapshot(fake_client, "default")
    assert "Private MAC" not in INVENTORY_COLUMNS
    assert all("Private MAC" not in r for r in query_rows(snap, "devices"))
    assert "Private MAC" not in render(query_rows(snap, "devices"), False, "devices")


def test_new_clients_shows_the_flag(fake_client):
    add_client(fake_client, name="stray-pixel", mac="06:00:00:00:00:30", connected=False)
    add_client(fake_client, name="stray-laptop", mac="dc:00:00:00:00:31", connected=False)
    snap = collect_snapshot(fake_client, "default", Needs(groups=True))
    rows = {r["Name"]: r["Private MAC"] for r in report(snap)}
    assert rows["stray-pixel"] == "yes" and rows["stray-laptop"] == ""


def test_the_client_view_notes_a_randomized_mac(fake_client):
    add_client(fake_client)
    snap = collect_snapshot(fake_client, "default", Needs(reservations=True, groups=True))
    detail = build_client_detail(snap, find_clients(snap, "pixel")[0])
    assert detail["identity"]["private_mac"] is True
    assert "[randomized MAC: reservations and history may not hold]" in render_detail(detail, emoji=False)
    other = build_client_detail(snap, find_clients(snap, "desktop")[0])
    assert other["identity"]["private_mac"] is False
    assert "randomized MAC" not in render_detail(other, emoji=False)


# -- diagnose -----------------------------------------------------------------------------------

def snapshot(fake_client):
    return collect_snapshot(fake_client, "default", Needs(reservations=True))


def test_a_reservation_on_a_private_mac_is_an_info_finding(fake_client):
    add_client(fake_client, reserved="10.0.0.80")
    (f, summary) = _private_mac_findings(snapshot(fake_client))
    assert (f.severity, f.code, f.subject) == (INFO, "reservation.private_mac", "pixel")
    assert "reserved IP 10.0.0.80" in f.message and "randomized (private) MAC" in f.message
    assert summary.code == "client.private_mac_summary"


def test_the_summary_counts_connected_private_clients(fake_client):
    add_client(fake_client)
    add_client(fake_client, mac="0a:00:00:00:00:22", name="tablet", ip="10.0.0.81")
    (summary,) = _private_mac_findings(snapshot(fake_client))
    assert (summary.severity, summary.subject, summary.code) == (INFO, "clients", "client.private_mac_summary")
    assert summary.message.startswith("2 of 4 connected clients use randomized (private) MAC addresses")


def test_no_private_clients_means_no_findings(fake_client):
    assert _private_mac_findings(snapshot(fake_client)) == []
    assert not [f for f in diagnose(snapshot(fake_client)) if "private_mac" in f.code]


def test_a_private_client_without_a_reservation_is_only_counted(fake_client):
    add_client(fake_client)
    assert [f.code for f in _private_mac_findings(snapshot(fake_client))] == ["client.private_mac_summary"]


def test_an_offline_private_reservation_is_reported_but_not_counted_as_connected(fake_client):
    add_client(fake_client, connected=False, reserved="10.0.0.82", name="sleeping-phone")
    assert [f.code for f in _private_mac_findings(snapshot(fake_client))] == ["reservation.private_mac"]


def test_a_reservation_on_a_vendor_mac_is_not_flagged(fake_client):
    add_client(fake_client, mac="dc:00:00:00:00:40", name="pi", reserved="10.0.0.83")
    assert _private_mac_findings(snapshot(fake_client)) == []


def test_the_findings_are_info_only(fake_client):
    add_client(fake_client, reserved="10.0.0.80")
    fake_client.session.fx["devices"] = [d for d in fake_client.session.fx["devices"] if d["name"] != "Garage AP"]
    found = diagnose(snapshot(fake_client))
    assert {f.severity for f in found if "private_mac" in f.code} == {INFO}
    assert WARNING not in {f.severity for f in found if f.subject in ("pixel", "clients")}


def test_the_ignore_list_can_silence_them(fake_client):
    from unifi_sentinel.settings import IgnoreRule
    add_client(fake_client, reserved="10.0.0.80")
    findings = _private_mac_findings(snapshot(fake_client))
    kept, ignored = apply_ignores(findings, (IgnoreRule(subject="clients", message="randomized", reason="phones"),
                                             IgnoreRule(subject="pixel", message="randomized", reason="known")))
    assert kept == [] and len(ignored) == 2


def run(fake_client, monkeypatch, argv):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    return cli.main(argv)


def test_cli_end_to_end(fake_client, monkeypatch, capsys):
    add_client(fake_client, reserved="10.0.0.80")
    run(fake_client, monkeypatch, ["diagnose", "--json", "--no-events"])
    doc = json.loads(capsys.readouterr().out)
    codes = {f["code"]: f for f in doc["findings"]}
    assert codes["reservation.private_mac"]["severity"] == "info"
    assert codes["client.private_mac_summary"]["message"].startswith("1 of 3 connected clients")

    assert run(fake_client, monkeypatch, ["query", "clients"]) == 0
    assert "Private MAC" in capsys.readouterr().out
    assert run(fake_client, monkeypatch, ["new-clients"]) == 0
    assert "Private MAC" in capsys.readouterr().out.splitlines()[0]
    assert run(fake_client, monkeypatch, ["client", "pixel", "--no-emoji"]) == 0
    assert "[randomized MAC" in capsys.readouterr().out
    assert run(fake_client, monkeypatch, ["client", "pixel", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["identity"]["private_mac"] is True
