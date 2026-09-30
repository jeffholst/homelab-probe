import json

import pytest

from unifi_sentinel import cli
from unifi_sentinel.client import UniFiAPIError
from unifi_sentinel.new_clients import render, report, ungrouped_clients
from unifi_sentinel.snapshot import Snapshot, collect_snapshot


def snapshot(users, clients=(), groups=None, devices=()):
    return Snapshot(site={"id": "s"}, devices=list(devices), clients=list(clients),
                    all_users=list(users),
                    client_groups=list(groups) if groups is not None else None)


def user(mac, name, groups=(), **kw):
    return {"mac": mac, "name": name, "network_members_group_ids": list(groups), **kw}


GROUPS = [{"id": "g1", "name": "Servers"}, {"id": "g2", "name": "Kids"}]


def names(rows):
    return [r["Name"] for r in rows]


def test_only_clients_in_no_group_are_listed():
    snap = snapshot([user("aa:01", "grouped", ["g1"]), user("aa:02", "empty"),
                     user("aa:03", "two", ["g1", "g2"])], groups=GROUPS)
    assert names(ungrouped_clients(snap)) == ["empty"]


def test_id_of_a_deleted_group_does_not_count_as_membership():
    snap = snapshot([user("aa:01", "stale", ["deleted"]), user("aa:02", "ok", ["g2"])], groups=GROUPS)
    assert names(ungrouped_clients(snap)) == ["stale"]


def test_empty_group_definitions_do_not_trust_stale_ids():
    snap = snapshot([user("aa:01", "stale", ["deleted"])], groups=[])
    assert names(ungrouped_clients(snap)) == ["stale"]


def test_without_group_definitions_the_raw_id_list_is_trusted():
    snap = snapshot([user("aa:01", "raw-grouped", ["whatever"]), user("aa:02", "empty")])
    assert names(ungrouped_clients(snap)) == ["empty"]


def test_unifi_devices_are_skipped():
    snap = snapshot([user("aa:01", "switch"), user("aa:02", "phone")],
                    devices=[{"macAddress": "AA:01", "name": "switch"}])
    assert names(ungrouped_clients(snap)) == ["phone"]


def test_online_and_offline_rows():
    snap = snapshot(
        [user("aa:01", "laptop", is_wired=True, oui="Dell", last_ip="10.0.0.9", first_seen=100,
              last_seen=200),
         user("aa:02", "printer", is_wired=True, last_ip="10.0.0.50", first_seen=50, last_seen=60,
              last_uplink_name="Rack Switch", last_uplink_remote_port=6),
         {"mac": "aa:03", "hostname": "tablet", "is_wired": False, "first_seen": 10, "last_seen": 20}],
        clients=[{"macAddress": "aa:01", "type": "WIRED", "ipAddress": "10.0.0.77",
                  "connectedAt": "2026-01-01T10:00:00Z", "uplinkDeviceId": "sw"}],
        devices=[{"id": "sw", "macAddress": "bb:01", "name": "Office Switch"}])
    snap.legacy_clients = [{"mac": "aa:01", "sw_mac": "bb:01", "sw_port": 3}]
    rows = {r["Name"]: r for r in ungrouped_clients(snap)}

    laptop = rows["laptop"]
    assert (laptop["Status"], laptop["IP Address"], laptop["Vendor"]) == ("Online", "10.0.0.77", "Dell")
    assert laptop["Where"] == "Wired, Office Switch port 3" and laptop["MAC Address"] == "AA:01"
    printer = rows["printer"]
    assert (printer["Status"], printer["IP Address"]) == ("Offline", "10.0.0.50")
    assert printer["Where"] == "Wired, Rack Switch port 6" and printer["Last Seen"] != ""
    tablet = rows["tablet"]  # name falls back to hostname; offline wireless has no location
    assert (tablet["Connection Type"], tablet["Where"], tablet["Vendor"]) == ("Wireless", "", "")


def test_sorted_newest_first_seen_with_unknown_last():
    snap = snapshot([user("aa:01", "old", first_seen=100), user("aa:02", "unknown"),
                     user("aa:03", "newest", first_seen=300), user("aa:04", "mid", first_seen=200)])
    assert names(ungrouped_clients(snap)) == ["newest", "mid", "old", "unknown"]


def test_search_json_and_table_rendering():
    snap = snapshot([user("aa:01", "Kitchen Echo", oui="Amazon"), user("aa:02", "desk")])
    assert names(report(snap, "amazon")) == ["Kitchen Echo"]
    rows = report(snap)
    assert "2 client(s) in no group" in render(rows, False)
    assert set(json.loads(render(rows, True))[0]) == {
        "Name", "MAC Address", "IP Address", "Vendor", "Connection Type", "Where",
        "First Seen", "Last Seen", "Status"}


def test_fixture_report_from_fake_controller(fake_client):
    snap = collect_snapshot(fake_client, "default", include_groups=True)
    assert {g["name"] for g in snap.client_groups} == {"Desktops", "Unused"}
    rows = ungrouped_clients(snap)
    # 'desktop' is in a group; the tablet was first seen after the printer
    assert names(rows) == ["old-tablet", "old-printer"]
    assert [r["Status"] for r in rows] == ["Offline", "Offline"]
    assert rows[1]["Where"] == "Wired, Office Switch port 6" and rows[0]["Where"] == ""


def test_group_definition_failure_warns_and_falls_back(fake_client, monkeypatch, capsys):
    def boom(*a, **k):
        raise UniFiAPIError("nope")

    monkeypatch.setattr(fake_client, "legacy_v2", boom)
    snap = collect_snapshot(fake_client, "default", include_groups=True)
    assert snap.client_groups is None
    warning = capsys.readouterr().err
    assert "membership cannot be validated against deleted groups" in warning
    assert "raw group IDs will be trusted" in warning
    assert names(ungrouped_clients(snap)) == ["old-tablet", "old-printer"]  # raw id lists used


def test_alluser_failure_propagates_when_collecting_groups(fake_client, monkeypatch):
    legacy_stat = fake_client.legacy_stat

    def fail_alluser(site_ref, resource):
        if resource == "alluser":
            raise UniFiAPIError("alluser unavailable")
        return legacy_stat(site_ref, resource)

    monkeypatch.setattr(fake_client, "legacy_stat", fail_alluser)
    with pytest.raises(UniFiAPIError, match="alluser unavailable"):
        collect_snapshot(fake_client, "default", include_groups=True)


def test_cli_new_clients(fake_client, monkeypatch, capsys):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, cfg: fake_client))

    assert cli.main(["new-clients"]) == 0
    out = capsys.readouterr().out
    assert "old-printer" in out and "old-tablet" in out and "desktop" not in out
    assert "2 client(s) in no group" in out

    assert cli.main(["new-clients", "-s", "printer", "--json"]) == 0
    assert [r["Name"] for r in json.loads(capsys.readouterr().out)] == ["old-printer"]
