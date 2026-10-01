import copy
import json
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from unifi_sentinel import cli
from unifi_sentinel.config import ConfigError
from unifi_sentinel.history import (FILE_PREFIX, MAX_LISTED, SCHEMA_VERSION, _where, capture,
                                    diff_snapshots, label_for, list_snapshots, load_snapshot, prune,
                                    render_diff, resolve, save_snapshot)
from unifi_sentinel.client_view import DeviceIndex, known_clients
from unifi_sentinel.snapshot import Snapshot, collect_snapshot

NOW = datetime(2026, 9, 30, 20, 15, 30, tzinfo=timezone(timedelta(hours=-5)))


@pytest.fixture
def snap(fake_client):
    return collect_snapshot(fake_client, "default", include_reservations=True, include_groups=True)


@pytest.fixture
def record(snap):
    return capture(snap, "10.0.0", now=NOW)


def by_mac(records, mac):
    return next(r for r in records if r["mac"] == mac)


# -- capturing ---------------------------------------------------------------

def test_capture_has_the_documented_shape(record):
    assert record["schema_version"] == SCHEMA_VERSION == 1
    assert record["captured_at"] == "2026-09-30T20:15:30-05:00" and record["tool_version"]
    assert record["site"] == {"name": "Default", "id": "site-1"}
    assert record["controller"] == {"application_version": "10.0.0"}
    assert [len(record[k]) for k in ("devices", "clients", "reservations")] == [4, 4, 2]
    for section in ("devices", "clients", "reservations"):
        macs = [r["mac"] for r in record[section]]
        assert macs == sorted(macs)                                              # stable order
    json.dumps(record)                                                           # plain JSON types only


def test_devices_record_firmware_state_and_uplink(record):
    switch = by_mac(record["devices"], "AA:00:00:00:00:02")
    assert (switch["name"], switch["type"], switch["firmware"], switch["state"]) == (
        "Office Switch", "Switch", "7.0.0", "Online")
    assert (switch["uplink"], switch["uplink_port"]) == ("Gateway", "2")
    assert by_mac(record["devices"], "AA:00:00:00:00:04")["state"] == "Offline"
    assert by_mac(record["devices"], "AA:00:00:00:00:01")["uplink"] == ""


def test_clients_record_network_where_and_group_names(record):
    desktop = by_mac(record["clients"], "BB:00:00:00:00:01")
    assert (desktop["connection"], desktop["status"], desktop["network"], desktop["vlan"]) == (
        "Wired", "Online", "Main", 1)
    assert (desktop["uplink"], desktop["uplink_port"], desktop["groups"]) == ("Office Switch", "3", ["Desktops"])
    phone = by_mac(record["clients"], "BB:00:00:00:00:02")
    assert (phone["connection"], phone["uplink"], phone["uplink_port"]) == ("Wireless", "Office AP", "")
    printer = by_mac(record["clients"], "BB:00:00:00:00:03")                     # offline: last uplink
    assert (printer["status"], printer["uplink"], printer["uplink_port"]) == ("Offline", "Office Switch", "6")
    tablet = by_mac(record["clients"], "BB:00:00:00:00:04")                      # offline Wi-Fi: unknown
    assert (tablet["uplink"], tablet["uplink_port"]) == ("", "")


def test_reservations_and_no_constantly_changing_values(record):
    assert by_mac(record["reservations"], "BB:00:00:00:00:03") == {
        "mac": "BB:00:00:00:00:03", "name": "old-printer", "reserved_ip": "10.0.0.50", "network": "IoT"}
    text = json.dumps(record).lower()
    for noisy in ("uptime", "last_seen", "lastseen", "last seen", "rx_bytes", "connectedat", "first_seen"):
        assert noisy not in text


def test_where_never_crashes_on_missing_data(snap):
    idx = DeviceIndex(snap)
    base = {"live": None, "sta": None, "user": None, "wired": False, "online": True}
    assert _where(snap, idx, base) == ("", "")                                   # wireless, nothing known (was a crash)
    assert _where(snap, idx, {**base, "wired": True}) == ("", "")
    assert _where(snap, idx, {**base, "sta": {"ap_mac": None}}) == ("", "")
    assert _where(snap, idx, {**base, "online": False, "user": {}}) == ("", "")
    assert _where(snap, idx, {**base, "online": False, "wired": True,
                              "user": {"last_uplink_name": "SW", "last_uplink_remote_port": 4}}) == ("SW", "4")
    live = {"uplinkDeviceId": "gw1"}                                             # Integration API fallback
    assert _where(snap, idx, {**base, "live": live, "wired": True}) == ("Gateway", "")
    assert all(isinstance(known_clients(snap), list) for _ in range(1))


# -- files -------------------------------------------------------------------

def test_save_load_round_trip_default_name_and_permissions(record, tmp_path):
    path = save_snapshot(record, directory=tmp_path / "snaps")                    # creates the directory
    assert path.name == "snapshot-20260930-201530.json" and path.parent == tmp_path / "snaps"
    assert load_snapshot(path) == record
    assert stat.S_IMODE(path.stat().st_mode) == 0o600                            # owner-only: it holds real MACs and IPs


def test_default_names_never_overwrite(record, tmp_path):
    first = save_snapshot(record, directory=tmp_path)
    second = save_snapshot(record, directory=tmp_path)
    third = save_snapshot(record, directory=tmp_path)
    assert [p.name for p in (first, second, third)] == [
        "snapshot-20260930-201530.json", "snapshot-20260930-201530-1.json", "snapshot-20260930-201530-2.json"]
    assert list_snapshots(tmp_path) == [first, second, third]                      # chronological, not text order
    third.rename(tmp_path / "snapshot-20260930-201530-10.json")                  # -10 is newer than -2, not older
    assert [p.name for p in list_snapshots(tmp_path)][-1] == "snapshot-20260930-201530-10.json"


def test_explicit_path_needs_force_to_replace_and_fixes_permissions(record, tmp_path):
    target = tmp_path / "deep" / "dir" / "mine.json"
    save_snapshot(record, target)
    assert target.exists()
    with pytest.raises(ConfigError, match="already exists"):
        save_snapshot(record, target)
    assert load_snapshot(target)["captured_at"] == record["captured_at"]
    target.chmod(0o644)
    save_snapshot({**record, "tool_version": "replaced"}, target, force=True)
    assert load_snapshot(target)["tool_version"] == "replaced"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_prune_keeps_the_newest_protects_the_new_file_and_ignores_other_files(record, tmp_path):
    names = [f"{FILE_PREFIX}2026090{d}-120000.json" for d in range(1, 6)]
    for n in names:
        (tmp_path / n).write_text("{}")
    other = [tmp_path / "notes.txt", tmp_path / "snapshot-bogus.json", tmp_path / "snapshot-20260901-120000.json.bak"]
    for p in other:
        p.write_text("keep")
    removed = prune(tmp_path, keep=2, protect=tmp_path / names[0])
    assert sorted(p.name for p in removed) == names[1:3]                          # the oldest, except the protected one
    assert [p.name for p in list_snapshots(tmp_path)] == [names[0], names[3], names[4]]
    assert all(p.exists() for p in other)
    assert prune(tmp_path, keep=10) == []                                         # fewer than N: nothing to do


def test_prune_never_deletes_the_newest_of_same_second_snapshots(tmp_path):
    names = ["snapshot-20260930-201530.json", "snapshot-20260930-201530-1.json",
             "snapshot-20260930-201530-2.json", "snapshot-20260930-201530-10.json"]
    for n in names:
        (tmp_path / n).write_text("{}")
    removed = prune(tmp_path, keep=1)
    assert sorted(p.name for p in removed) == sorted(names[:3])                  # -10 is the newest and survives
    assert [p.name for p in list_snapshots(tmp_path)] == [names[3]]


def test_list_snapshots_handles_missing_directories_and_odd_names(tmp_path):
    assert list_snapshots(tmp_path / "nope") == []
    (tmp_path / "snapshot-20260901-120000.json").write_text("{}")
    (tmp_path / "snapshot-x.json").write_text("{}")
    (tmp_path / "other.json").write_text("{}")
    (tmp_path / "snapshot-20260901-130000.json").mkdir()                         # a directory with a matching name
    assert [p.name for p in list_snapshots(tmp_path)] == ["snapshot-20260901-120000.json"]


@pytest.mark.parametrize("content, message", [
    ("not json at all", "invalid JSON"),
    ("[1, 2]", "no schema_version"),
    ('{"devices": []}', "no schema_version"),
    ('{"schema_version": 2}', "uses snapshot format 2"),
    ('{"schema_version": "1"}', "uses snapshot format '1'"),
    ('{"schema_version": 1, "devices": [], "clients": []}', "'reservations' is missing"),
    ('{"schema_version": 1, "devices": {}, "clients": [], "reservations": []}', "'devices' is missing or not a list"),
])
def test_load_rejects_bad_files_with_a_clear_message(tmp_path, content, message):
    path = tmp_path / "bad.json"
    path.write_text(content)
    with pytest.raises(ConfigError, match=re.escape(message)) as exc:
        load_snapshot(path)
    assert str(path) in str(exc.value)


def test_load_missing_or_unreadable(tmp_path):
    with pytest.raises(ConfigError, match="cannot read snapshot"):
        load_snapshot(tmp_path / "missing.json")
    with pytest.raises(ConfigError, match="cannot read snapshot"):
        load_snapshot(tmp_path)                                                   # a directory


def test_resolve_finds_a_path_or_a_name_inside_the_directory(tmp_path):
    inside = tmp_path / "snaps"
    inside.mkdir()
    (inside / "a.json").write_text("{}")
    elsewhere = tmp_path / "b.json"
    elsewhere.write_text("{}")
    assert resolve("a.json", inside) == inside / "a.json"
    assert resolve(str(elsewhere), inside) == elsewhere
    with pytest.raises(ConfigError, match="snapshot not found: c.json"):
        resolve("c.json", inside)


# -- comparing ---------------------------------------------------------------

def rec(**kw):
    return {"schema_version": 1, "site": {"id": "s"}, "controller": {"application_version": "1.0"},
            "devices": [], "clients": [], "reservations": [], **kw}


def dev(mac, name="SW", **kw):
    return {"mac": mac, "name": name, "ip": "10.0.0.2", "model": "USW", "type": "Switch", "firmware": "1.0",
            "state": "Online", "uplink": "GW", "uplink_port": "2", **kw}


def cli_rec(mac, name="pc", **kw):
    return {"mac": mac, "name": name, "ip": "10.0.0.9", "connection": "Wired", "status": "Online",
            "network": "Main", "vlan": 1, "uplink": "SW", "uplink_port": "3", "groups": [], **kw}


def changes(diff, kind):
    freeze = lambda v: tuple(v) if isinstance(v, list) else v
    return {(c["name"], ch["field"], freeze(ch["old"]), freeze(ch["new"]))
            for c in diff[kind]["changed"] for ch in c["changes"]}


def test_identical_snapshots_have_no_changes(record):
    diff = diff_snapshots(record, copy.deepcopy(record))
    assert diff["total"] == 0 and diff["same_site"]
    assert render_diff(diff, "old", "new").endswith("No changes.")


def test_device_changes_of_every_kind():
    old = rec(devices=[dev("A1", "Old Name"), dev("A2", "Gone"), dev("A3", "Same")])
    new = rec(devices=[dev("A1", "New Name", ip="10.0.0.9", firmware="2.0", state="Offline",
                           uplink="Other", uplink_port="7", model="USW2"),
                       dev("A3", "Same"), dev("A4", "Fresh")])
    d = diff_snapshots(old, new)
    assert [x["name"] for x in d["devices"]["added"]] == ["Fresh"]
    assert [x["name"] for x in d["devices"]["removed"]] == ["Gone"]
    assert changes(d, "devices") == {
        ("New Name", "name", "Old Name", "New Name"), ("New Name", "ip", "10.0.0.2", "10.0.0.9"),
        ("New Name", "firmware", "1.0", "2.0"), ("New Name", "state", "Online", "Offline"),
        ("New Name", "location", "GW port 2", "Other port 7"), ("New Name", "model", "USW", "USW2")}
    assert d["total"] == 8


def test_client_changes_including_groups_and_moves():
    old = rec(clients=[cli_rec("C1", groups=["A"]), cli_rec("C2"), cli_rec("C3"), cli_rec("C4", uplink="SW")])
    new = rec(clients=[cli_rec("C1", ip="10.0.0.99", connection="Wireless", network="IoT", vlan=20,
                               groups=["A", "B"], status="Offline", name="renamed"),
                       cli_rec("C2", uplink="AP", uplink_port=""),               # moved
                       cli_rec("C4", uplink="", uplink_port="")])                # location now unknown
    d = diff_snapshots(old, new)
    assert [x["mac"] for x in d["clients"]["removed"]] == ["C3"]
    got = changes(d, "clients")
    assert ("renamed", "ip", "10.0.0.9", "10.0.0.99") in got
    assert ("renamed", "groups", ("A",), ("A", "B")) in got
    assert ("renamed", "status", "Online", "Offline") in got
    assert ("renamed", "network", "Main", "IoT") in got and ("renamed", "vlan", 1, 20) in got
    assert ("pc", "location", "SW port 3", "AP") in got                          # C2
    assert not any(name == "pc" and field == "location" and new == "" for name, field, _o, new in got)  # C4: not a move


def test_unknown_location_is_never_reported_as_a_move():
    old = rec(clients=[cli_rec("C1", uplink="AP", uplink_port="")])
    new = rec(clients=[cli_rec("C1", uplink="", uplink_port="")])                # went offline: Wi-Fi location unknown
    assert diff_snapshots(old, new)["total"] == 0


def test_reservation_and_controller_changes_and_different_sites():
    old = rec(reservations=[{"mac": "R1", "name": "nas", "reserved_ip": "10.0.0.5", "network": "Main"},
                            {"mac": "R2", "name": "old", "reserved_ip": "10.0.0.6", "network": "Main"}])
    new = rec(reservations=[{"mac": "R1", "name": "nas", "reserved_ip": "10.0.0.7", "network": "IoT"},
                            {"mac": "R3", "name": "tv", "reserved_ip": "10.0.0.8", "network": "Main"}],
              controller={"application_version": "2.0"}, site={"id": "other"})
    d = diff_snapshots(old, new)
    assert changes(d, "reservations") == {("nas", "reserved_ip", "10.0.0.5", "10.0.0.7"),
                                          ("nas", "network", "Main", "IoT")}
    assert [x["name"] for x in d["reservations"]["added"]] == ["tv"]
    assert d["controller"] == [{"field": "application_version", "old": "1.0", "new": "2.0"}]
    assert not d["same_site"]
    text = render_diff(d, "old", "new")
    assert "different sites" in text and "Application version: 1.0 -> 2.0" in text
    # an unknown version on either side is not a change
    assert diff_snapshots(rec(controller={"application_version": ""}), rec())["controller"] == []


# -- rendering ---------------------------------------------------------------

def test_render_groups_changes_by_type():
    old = rec(devices=[dev("A1", firmware="1.0"), dev("A2", "Gone")],
              clients=[cli_rec("C1", groups=["A"])], reservations=[])
    new = rec(devices=[dev("A1", firmware="2.0"), dev("A5", "Fresh")],
              clients=[cli_rec("C1", groups=[], ip="10.0.0.77"), cli_rec("C9", "newbie")],
              reservations=[{"mac": "R1", "name": "nas", "reserved_ip": "10.0.0.5", "network": "Main"}])
    text = render_diff(diff_snapshots(old, new), "old.json", "new.json")
    assert text.startswith("Comparing old.json -> new.json")
    for expected in ("Devices", "  New devices (1):", "    Fresh (USW, 10.0.0.2)", "  Missing devices (1):",
                     "    Gone (USW, 10.0.0.2)", "  Firmware changed (1):", "    SW: 1.0 -> 2.0",
                     "Clients", "  New clients (1):", "    newbie (10.0.0.9, Wired)",
                     "  IP changed (1):", "    pc: 10.0.0.9 -> 10.0.0.77", "    pc: A -> none",
                     "DHCP reservations", "  New reservations (1):", "    nas (10.0.0.5, Main)"):
        assert expected in text, expected
    assert text.rstrip().endswith("change(s)")
    assert text.index("Devices") < text.index("Clients") < text.index("DHCP reservations")


def test_connect_and_disconnect_churn_is_capped_unless_all_is_requested():
    n = MAX_LISTED + 5
    old = rec(clients=[cli_rec(f"C{i:02d}", f"c{i:02d}", status="Offline") for i in range(n)])
    new = rec(clients=[cli_rec(f"C{i:02d}", f"c{i:02d}", status="Online") for i in range(n)])
    diff = diff_snapshots(old, new)
    capped = render_diff(diff, "o", "n")
    assert f"Went online ({n}):" in capped and "... and 5 more (use --all)" in capped
    assert capped.count("    c") == MAX_LISTED
    full = render_diff(diff, "o", "n", show_all=True)
    assert "more (use --all)" not in full and full.count("    c") == n
    assert "Went offline" not in full


def test_label_for_shows_the_capture_time():
    assert label_for({"captured_at": "2026-09-30T20:15:30-05:00"}, "a.json") == "a.json (captured 2026-09-30 20:15)"
    assert label_for({"captured_at": "garbage"}, "a.json") == "a.json" and label_for({}, "a.json") == "a.json"


# -- command line ------------------------------------------------------------

def _run(fake_client, monkeypatch, argv):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    return cli.main(argv)


def test_cli_snapshot_then_diff_against_the_live_network(fake_client, monkeypatch, capsys, tmp_path):
    assert _run(fake_client, monkeypatch, ["snapshot", "--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Saved 4 devices, 4 clients and 2 reservations to" in out
    (saved,) = list_snapshots(tmp_path)
    assert load_snapshot(saved)["controller"]["application_version"] == "10.0.0"

    assert _run(fake_client, monkeypatch, ["diff", "--dir", str(tmp_path)]) == 0       # nothing changed yet
    assert "No changes." in capsys.readouterr().out

    fx = fake_client.session.fx                                                   # now change the "network"
    fx["devices"][1]["name"] = "Renamed Switch"
    fx["devices"][1]["ipAddress"] = "10.0.0.22"
    fx["clients"][0]["ipAddress"] = "10.0.0.99"
    fx["clients"].append({"id": "c9", "name": "newcomer", "type": "WIRELESS",
                          "macAddress": "bb:00:00:00:00:09", "ipAddress": "10.0.0.19",
                          "connectedAt": "2026-01-01T10:00:00Z", "uplinkDeviceId": "ap1"})
    fx["info"]["applicationVersion"] = "10.1.0"
    assert _run(fake_client, monkeypatch, ["diff", "--dir", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "Comparing" in text and "the network right now" in text
    assert "Application version: 10.0.0 -> 10.1.0" in text
    assert "Renamed Switch" in text and "Office Switch -> Renamed Switch" in text
    assert "newcomer (10.0.0.19, Wireless)" in text and "desktop: 10.0.0.10 -> 10.0.0.99" in text
    assert fake_client.session.posts == []                                        # snapshot and diff never POST


def test_cli_diff_of_two_files_by_name_by_last_two_and_as_json(fake_client, monkeypatch, capsys, tmp_path):
    _run(fake_client, monkeypatch, ["snapshot", "--dir", str(tmp_path)])
    fake_client.session.fx["clients"][0]["ipAddress"] = "10.0.0.99"
    _run(fake_client, monkeypatch, ["snapshot", "--dir", str(tmp_path)])
    first, second = list_snapshots(tmp_path)
    capsys.readouterr()
    calls_before = len(fake_client.session.calls)

    assert _run(fake_client, monkeypatch, ["diff", "--last-two", "--dir", str(tmp_path)]) == 0
    assert "desktop: 10.0.0.10 -> 10.0.0.99" in capsys.readouterr().out
    assert _run(fake_client, monkeypatch, ["diff", first.name, second.name, "--dir", str(tmp_path)]) == 0
    assert "desktop: 10.0.0.10 -> 10.0.0.99" in capsys.readouterr().out
    assert len(fake_client.session.calls) == calls_before                         # two files: the controller is not contacted

    assert _run(fake_client, monkeypatch, ["diff", str(first), str(second), "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["total"] == 1 and parsed["clients"]["changed"][0]["changes"][0]["field"] == "ip"
    assert set(parsed) == {"same_site", "controller", "devices", "clients", "reservations", "total"}


def test_cli_snapshot_options_keep_output_and_force(fake_client, monkeypatch, capsys, tmp_path):
    for _ in range(3):
        assert _run(fake_client, monkeypatch, ["snapshot", "--dir", str(tmp_path)]) == 0
    assert len(list_snapshots(tmp_path)) == 3
    capsys.readouterr()
    assert _run(fake_client, monkeypatch, ["snapshot", "--dir", str(tmp_path), "--keep", "2"]) == 0
    out = capsys.readouterr().out
    assert out.count("Removed old snapshot") == 2 and len(list_snapshots(tmp_path)) == 2

    target = tmp_path / "mine.json"
    assert _run(fake_client, monkeypatch, ["snapshot", "-o", str(target)]) == 0
    assert _run(fake_client, monkeypatch, ["snapshot", "-o", str(target)]) == cli.EXIT_ERROR
    assert "already exists" in capsys.readouterr().err
    assert _run(fake_client, monkeypatch, ["snapshot", "-o", str(target), "--force"]) == 0
    assert target.name not in [p.name for p in list_snapshots(tmp_path)]          # -o files are never pruned


def test_cli_diff_errors_and_usage(fake_client, monkeypatch, capsys, tmp_path):
    assert _run(fake_client, monkeypatch, ["diff", "--dir", str(tmp_path)]) == cli.EXIT_ERROR
    assert "no saved snapshots" in capsys.readouterr().err
    assert _run(fake_client, monkeypatch, ["diff", "--last-two", "--dir", str(tmp_path)]) == cli.EXIT_ERROR
    assert "need at least two saved snapshots" in capsys.readouterr().err
    assert _run(fake_client, monkeypatch, ["diff", "nope.json", "--dir", str(tmp_path)]) == cli.EXIT_ERROR
    assert "snapshot not found" in capsys.readouterr().err
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema_version": 99}')
    assert _run(fake_client, monkeypatch, ["diff", str(bad)]) == cli.EXIT_ERROR
    assert "uses snapshot format 99" in capsys.readouterr().err
    for argv in (["diff", "a", "b", "c"], ["diff", "--last-two", "a"], ["snapshot", "--keep", "0"]):
        with pytest.raises(SystemExit) as exc:
            _run(fake_client, monkeypatch, argv)
        assert exc.value.code == cli.EXIT_USAGE
        capsys.readouterr()
