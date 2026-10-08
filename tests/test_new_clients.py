import json

import pytest

from homelab_probe import cli
from homelab_probe.client import UniFiAPIError
from homelab_probe.new_clients import (
    new_client_rows,
    recent_clients,
    render,
    report,
    resolve_since,
    table_footer,
    ungrouped_clients,
)
from homelab_probe.snapshot import Needs, Snapshot, collect_snapshot


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
    assert names(report(snap, "amazon", None, True)[0]) == ["Kitchen Echo"]
    rows = report(snap, "", None, True)[0]
    assert render(rows, False).endswith("2 client(s)")
    assert set(json.loads(render(rows, True))[0]) == {
        "Name", "MAC Address", "IP Address", "Vendor", "Connection Type", "Where",
        "First Seen", "Last Seen", "Status", "Private MAC"}


def test_fixture_report_from_fake_controller(fake_client):
    snap = collect_snapshot(fake_client, "default", Needs(groups=True))
    assert {g["name"] for g in snap.client_groups} == {"Desktops", "Unused"}
    rows = ungrouped_clients(snap)
    # 'desktop' is in a group; the phone joined after the tablet, which was first seen after the printer
    assert names(rows) == ["guest-phone", "old-tablet", "old-printer"]
    assert [r["Status"] for r in rows] == ["Offline", "Offline", "Offline"]
    assert rows[2]["Where"] == "Wired, Office Switch port 6" and rows[1]["Where"] == ""


def test_group_definition_failure_warns_and_falls_back(fake_client, monkeypatch, capsys):
    def boom(*a, **k):
        raise UniFiAPIError("nope")

    monkeypatch.setattr(fake_client, "legacy_v2", boom)
    snap = collect_snapshot(fake_client, "default", Needs(groups=True))
    assert snap.client_groups is None
    warning = capsys.readouterr().err
    assert "membership cannot be validated against deleted groups" in warning
    assert "raw group IDs will be trusted" in warning
    assert names(ungrouped_clients(snap)) == ["guest-phone", "old-tablet", "old-printer"]  # raw id lists used


def break_alluser(fake_client, monkeypatch):
    legacy_stat = fake_client.legacy_stat

    def fail_alluser(site_ref, resource):
        if resource == "alluser":
            raise UniFiAPIError("alluser unavailable")
        return legacy_stat(site_ref, resource)

    monkeypatch.setattr(fake_client, "legacy_stat", fail_alluser)


def test_alluser_failure_propagates_when_it_is_required(fake_client, monkeypatch):
    break_alluser(fake_client, monkeypatch)
    with pytest.raises(UniFiAPIError, match="alluser unavailable"):
        collect_snapshot(fake_client, "default", Needs(groups=True, users_required=True))


def test_alluser_failure_degrades_with_a_warning_when_it_is_optional(fake_client, monkeypatch, capsys):
    break_alluser(fake_client, monkeypatch)
    snap = collect_snapshot(fake_client, "default", Needs(groups=True, reservations=True))
    assert snap.all_users == [] and len(snap.clients) == 2          # connected clients still work
    warning = capsys.readouterr().err
    assert "legacy stat/alluser unavailable" in warning
    assert "reservations" in warning
    assert "port mapping" not in warning


def test_cli_new_clients(fake_client, monkeypatch, capsys):
    monkeypatch.setenv("UNIFI_URL", "https://controller")
    monkeypatch.setenv("UNIFI_API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, cfg: fake_client))

    assert cli.main(["new-clients", "--ungrouped"]) == 0
    out = capsys.readouterr().out
    assert "old-printer" in out and "old-tablet" in out and "desktop" not in out
    assert "3 client(s) in no group (1 with a private MAC)" in out

    assert cli.main(["new-clients", "--ungrouped", "-s", "printer", "--json"]) == 0
    assert [r["Name"] for r in json.loads(capsys.readouterr().out)] == ["old-printer"]


# -- new means recently first seen (no human tagging) ---------------------------------------------------------

NOW = 1_000_000.0
DAY = 86400


def aged(*pairs):
    """Users first seen the given number of seconds before NOW: ("name", age) pairs."""
    return [user(f"aa:{i:02x}", name, first_seen=NOW - age) for i, (name, age) in enumerate(pairs, 1)]


def test_recent_clients_are_those_first_seen_within_the_window_newest_first():
    snap = snapshot(aged(("old", 30 * DAY), ("yesterday", DAY), ("hour", 3600), ("week", 7 * DAY)))
    assert names(recent_clients(snap, 7 * DAY, NOW)) == ["hour", "yesterday", "week"]   # the edge itself is in
    assert names(recent_clients(snap, 7 * DAY - 1, NOW)) == ["hour", "yesterday"]


def test_a_client_without_a_usable_first_seen_is_unknown_never_new():
    users = aged(("fresh", 60)) + [
        user("bb:01", "missing"), user("bb:02", "zero", first_seen=0), user("bb:03", "text", first_seen="1000000"),
        user("bb:04", "none", first_seen=None), user("bb:05", "flag", first_seen=True),
        user("bb:06", "negative", first_seen=-5)]
    rows, unknown = new_client_rows(snapshot(users), 7 * DAY, now=NOW)
    assert names(rows) == ["fresh"] and unknown == 6
    # with no age limit nothing is unknown: every client is listed, the blank ones last
    rows, unknown = new_client_rows(snapshot(users), None, now=NOW)
    assert len(rows) == 7 and rows[0]["Name"] == "fresh" and unknown == 0


def test_a_first_seen_in_the_future_is_clock_skew_and_counts_as_now():
    snap = snapshot([user("aa:01", "skewed", first_seen=NOW + 3600)])
    assert names(recent_clients(snap, 60, NOW)) == ["skewed"]


def test_unifi_devices_and_blank_macs_are_never_new():
    snap = snapshot([user("aa:bb:cc:00:00:01", "switch", first_seen=NOW), user("", "blank", first_seen=NOW),
                     user("aa:bb:cc:00:00:02", "phone", first_seen=NOW)],
                    devices=[{"macAddress": "AA-BB-CC-00-00-01", "name": "sw"}])
    assert names(recent_clients(snap, DAY, NOW)) == ["phone"]


def test_groups_do_not_decide_what_is_new_unless_asked():
    snap = snapshot([user("aa:01", "grouped", ["g1"], first_seen=NOW - 60), user("aa:02", "loose", first_seen=NOW - 60),
                     user("aa:03", "old loose", first_seen=NOW - 30 * DAY)], groups=GROUPS)
    assert names(new_client_rows(snap, DAY, False, NOW)[0]) == ["grouped", "loose"]
    assert names(new_client_rows(snap, DAY, True, NOW)[0]) == ["loose"]             # new AND in no group
    assert names(new_client_rows(snap, None, True, NOW)[0]) == ["loose", "old loose"]


def test_the_default_window_and_ungrouped_alone():
    assert resolve_since(None, False) == 7 * DAY
    assert resolve_since(None, True) is None            # --ungrouped alone is the old list, without an age limit
    assert resolve_since(3600, True) == 3600 and resolve_since(3600, False) == 3600


def test_the_footer_says_what_was_counted_and_what_was_not():
    rows = [{"Private MAC": "yes"}, {"Private MAC": ""}]
    assert table_footer(rows) == "2 client(s) (1 with a private MAC)"
    assert table_footer([{"Private MAC": ""}]) == "1 client(s)"
    assert table_footer(rows, 7 * DAY) == "2 client(s) first seen in the last 7d (1 with a private MAC)"
    assert table_footer(rows, 36 * 3600, True, 3) == (
        "2 client(s) first seen in the last 36h in no group (1 with a private MAC); "
        "3 known client(s) have no first-seen time and are not counted")
    assert "90m" in table_footer([], 5400)


def test_the_command_lists_recent_clients_from_the_demo_controller(capsys):
    assert cli.main(["--demo", "new-clients"]) == 0
    out = capsys.readouterr().out
    assert "guest-phone" in out and "old-tablet" not in out and "first seen in the last 7d" in out
    assert "(1 with a private MAC)" in out
    assert cli.main(["--demo", "new-clients", "--ungrouped"]) == 0
    assert "old-tablet" in capsys.readouterr().out
    assert cli.main(["--demo", "new-clients", "--since", "1m", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


@pytest.mark.parametrize("value", ["0", "soon", "-1d", "7"])
def test_a_bad_window_is_a_usage_error(value, capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(["--demo", "new-clients", f"--since={value}"])
    assert stop.value.code == 64 and "invalid duration" in capsys.readouterr().err
