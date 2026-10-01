"""Reserved clients that are offline: the `diagnose` check and `query reservations --offline`."""

import json
import time

import pytest

from unifi_sentinel import cli, wan
from unifi_sentinel.config import ConfigError
from unifi_sentinel.diagnose import CRITICAL, INFO, WARNING, _offline_reservation_findings, apply_ignores, diagnose
from unifi_sentinel.query import query_rows, render
from unifi_sentinel.reservations import offline_reservation_rows, offline_reservations
from unifi_sentinel.settings import DiagnoseSettings, IgnoreRule, load_settings
from unifi_sentinel.snapshot import collect_snapshot
from unifi_sentinel.util import describe_age

DAY = 86400
NOW = float(int(time.time()))      # the fixture's own last-seen times are relative to the real clock


def reserved(mac, name, ip, seen, **extra):
    """A legacy `stat/alluser` record with an enabled reservation (seen: seconds ago, or None)."""
    record = {"mac": mac, "name": name, "use_fixedip": True, "fixed_ip": ip,
              "last_connection_network_id": "net-1", **extra}
    if seen is not None:
        record["last_seen"] = int(NOW - seen)
    return record


def snapshot_with(fake_client, *users):
    snap = collect_snapshot(fake_client, "default", include_reservations=True)
    snap.all_users = list(snap.all_users) + list(users)
    return snap


def findings(snap, settings=None):
    return _offline_reservation_findings(snap, settings or DiagnoseSettings(), NOW)


# -- which reservations count ----------------------------------------------------------

def test_the_fixture_has_no_offline_reservation_over_the_threshold(fake_client):
    """old-printer is reserved and offline, but was last seen hours ago."""
    snap = collect_snapshot(fake_client, "default", include_reservations=True)
    assert offline_reservations(snap, 1) == []
    assert [f for f in diagnose(snap) if f.code.startswith("reservation.offline")] == []
    assert [r["mac"] for r in offline_reservations(snap, 0)] == ["BB:00:00:00:00:03"]   # offline, just not for long


def test_only_clients_offline_for_the_threshold_or_more_are_listed(fake_client):
    snap = snapshot_with(
        fake_client,
        reserved("bb:00:00:00:00:10", "just-under", "10.0.0.60", DAY - 1),
        reserved("bb:00:00:00:00:11", "exactly-a-day", "10.0.0.61", DAY),
        reserved("bb:00:00:00:00:12", "long-gone", "10.0.0.62", 30 * DAY))
    got = {r["user"]["name"]: r["offline_seconds"] for r in offline_reservations(snap, 1, NOW)}
    assert got == {"exactly-a-day": DAY, "long-gone": 30 * DAY}


def test_a_connected_client_is_never_offline_whatever_its_last_seen(fake_client):
    snap = snapshot_with(fake_client, reserved("bb:00:00:00:00:02", "phone", "10.0.0.11", 90 * DAY))
    assert "BB:00:00:00:00:02" not in {r["mac"] for r in offline_reservations(snap, 1, NOW)}


def test_a_disabled_reservation_is_ignored(fake_client):
    snap = snapshot_with(fake_client, {**reserved("bb:00:00:00:00:13", "stale", "10.0.0.63", 90 * DAY),
                                       "use_fixedip": False})
    assert "BB:00:00:00:00:13" not in {r["mac"] for r in offline_reservations(snap, 1, NOW)}


def test_a_unifi_device_with_a_reservation_is_left_to_the_device_checks(fake_client):
    snap = snapshot_with(fake_client, reserved("aa:00:00:00:00:04", "Garage AP", "10.0.0.4", 90 * DAY))
    assert "AA:00:00:00:00:04" not in {r["mac"] for r in offline_reservations(snap, 1, NOW)}


def test_a_reservation_matches_the_connected_client_by_mac_in_any_format(fake_client):
    snap = snapshot_with(fake_client, reserved("BB-00-00-00-00-01", "desktop again", "10.0.0.12", 90 * DAY))
    assert "BB:00:00:00:00:01" not in {r["mac"] for r in offline_reservations(snap, 1, NOW)}


@pytest.mark.parametrize("seen", [None, 0, -5, "yesterday", True])
def test_no_usable_last_seen_means_never_seen(fake_client, seen):
    user = reserved("bb:00:00:00:00:14", "ghost", "10.0.0.64", None)
    if seen is not None:
        user["last_seen"] = seen
    (r,) = [r for r in offline_reservations(snapshot_with(fake_client, user), 1, NOW) if r["mac"].endswith(":14")]
    assert r["offline_seconds"] is None and r["last_seen"] is None


def test_a_last_seen_in_the_future_is_clock_skew_not_an_outage(fake_client):
    snap = snapshot_with(fake_client, reserved("bb:00:00:00:00:15", "skewed", "10.0.0.65", -3600))
    assert "BB:00:00:00:00:15" not in {r["mac"] for r in offline_reservations(snap, 1, NOW)}


# -- the findings ----------------------------------------------------------------------

def test_warning_after_a_day_and_critical_after_a_week(fake_client):
    snap = snapshot_with(
        fake_client,
        reserved("bb:00:00:00:00:10", "quiet-for-hours", "10.0.0.60", 5 * 3600),
        reserved("bb:00:00:00:00:11", "gone-two-days", "10.0.0.61", 2 * DAY),
        reserved("bb:00:00:00:00:12", "gone-a-week", "10.0.0.62", 7 * DAY),
        reserved("bb:00:00:00:00:13", "gone-a-month", "10.0.0.63", 30 * DAY))
    got = {f.subject: f.severity for f in findings(snap)}
    assert got == {"gone-two-days": WARNING, "gone-a-week": CRITICAL, "gone-a-month": CRITICAL}


def test_the_message_names_the_ip_network_and_how_long(fake_client):
    snap = snapshot_with(fake_client, reserved("bb:00:00:00:00:11", "media-box", "10.0.0.61", 6 * DAY + 3600))
    (f,) = findings(snap)
    assert f.subject == "media-box" and f.code == "reservation.offline" and f.severity == WARNING
    assert f.message.startswith("reserved IP 10.0.0.61 (Main) is offline: last seen 6d ago (")
    assert f.message.endswith(")") and f.target_mac is None


def test_the_subject_falls_back_to_hostname_then_mac(fake_client):
    snap = snapshot_with(
        fake_client,
        reserved("bb:00:00:00:00:11", None, "10.0.0.61", 2 * DAY, hostname="nas"),
        reserved("bb:00:00:00:00:12", None, "10.0.0.62", 2 * DAY))
    assert sorted(f.subject for f in findings(snap)) == ["BB:00:00:00:00:12", "nas"]


def test_a_reservation_that_was_never_seen_is_one_info_finding(fake_client):
    snap = snapshot_with(fake_client, reserved("bb:00:00:00:00:14", "ghost", "10.0.0.64", None))
    (f,) = findings(snap)
    assert f.severity == INFO and f.code == "reservation.never_seen" and f.subject == "ghost"
    assert "no last-seen time" in f.message and "10.0.0.64" in f.message


def test_two_clients_with_the_same_name_are_both_reported(fake_client):
    snap = snapshot_with(fake_client, reserved("bb:00:00:00:00:11", "pi", "10.0.0.61", 3 * DAY),
                         reserved("bb:00:00:00:00:12", "pi", "10.0.0.62", 3 * DAY))
    got = findings(snap)
    assert [f.subject for f in got] == ["pi", "pi"] and {"10.0.0.61", "10.0.0.62"} == {
        f.message.split()[2] for f in got}


def test_the_thresholds_come_from_the_settings(fake_client):
    snap = snapshot_with(fake_client, reserved("bb:00:00:00:00:11", "box", "10.0.0.61", 3 * DAY))
    assert findings(snap, DiagnoseSettings(reserved_offline_warn_days=5, reserved_offline_critical_days=9)) == []
    assert findings(snap, DiagnoseSettings(reserved_offline_warn_days=2, reserved_offline_critical_days=9))[
        0].severity == WARNING
    assert findings(snap, DiagnoseSettings(reserved_offline_warn_days=1, reserved_offline_critical_days=3))[
        0].severity == CRITICAL


def test_the_ignore_list_can_silence_a_client_that_is_meant_to_be_off(fake_client):
    snap = snapshot_with(fake_client, reserved("bb:00:00:00:00:11", "laptop", "10.0.0.61", 20 * DAY),
                         reserved("bb:00:00:00:00:12", "server", "10.0.0.62", 20 * DAY))
    kept, ignored = apply_ignores(findings(snap), (IgnoreRule(subject="laptop", reason="travels"),))
    assert [f.subject for f in kept] == ["server"] and [f.subject for f, _ in ignored] == ["laptop"]


def test_diagnose_includes_the_check_and_accepts_a_clock(fake_client):
    snap = snapshot_with(fake_client, reserved("bb:00:00:00:00:11", "box", "10.0.0.61", 3 * DAY))
    assert [f.code for f in diagnose(snap, now=NOW) if f.subject == "box"] == ["reservation.offline"]
    assert not [f for f in diagnose(snap, now=NOW - 365 * DAY) if f.subject == "box"]     # earlier "now": not yet gone


# -- the settings --------------------------------------------------------------------------

def test_defaults_and_the_toml_file(tmp_path):
    assert (DiagnoseSettings().reserved_offline_warn_days, DiagnoseSettings().reserved_offline_critical_days) == (1, 7)
    path = tmp_path / "s.toml"
    path.write_text("[thresholds]\nreserved_offline_warn_days = 0.5\nreserved_offline_critical_days = 14\n")
    loaded = load_settings(path)
    assert (loaded.reserved_offline_warn_days, loaded.reserved_offline_critical_days) == (0.5, 14)


@pytest.mark.parametrize("body, message", [
    ("reserved_offline_warn_days = 10\nreserved_offline_critical_days = 2", "must not exceed"),
    ("reserved_offline_warn_days = -1", "at least 0"),
    ("reserved_offline_critical_days = 'week'", "must be a number"),
    ("reserved_offline_warn_days = true", "must be a number"),
    ("reserved_offline_warn_days = inf", "must be finite"),
])
def test_bad_thresholds_are_config_errors(tmp_path, body, message):
    path = tmp_path / "s.toml"
    path.write_text(f"[thresholds]\n{body}\n")
    with pytest.raises(ConfigError, match=message):
        load_settings(path)


# -- query reservations --offline -------------------------------------------------------------

def test_the_listing_is_exactly_what_the_check_reports(fake_client):
    snap = snapshot_with(
        fake_client,
        reserved("bb:00:00:00:00:10", "quiet-for-hours", "10.0.0.60", 5 * 3600),
        reserved("bb:00:00:00:00:11", "gone-two-days", "10.0.0.61", 2 * DAY),
        reserved("bb:00:00:00:00:14", "ghost", "10.0.0.64", None))
    rows = offline_reservation_rows(snap, 1, NOW)
    reported = {f.subject for f in findings(snap)}
    assert {r["Name"] for r in rows} == reported == {"gone-two-days", "ghost"}
    by_name = {r["Name"]: r for r in rows}
    assert by_name["gone-two-days"]["Offline For"] == "2d" and by_name["ghost"]["Offline For"] == "never seen"
    assert by_name["ghost"]["Reserved IP"] == "10.0.0.64" and by_name["ghost"]["Status"] == "Offline"


def test_query_rows_and_render_add_the_column_only_for_offline(fake_client):
    snap = snapshot_with(fake_client, reserved("bb:00:00:00:00:11", "gone-two-days", "10.0.0.61", 2 * DAY))
    plain = render(query_rows(snap, "reservations"), False, "reservations")
    assert "Offline For" not in plain
    offline = render(query_rows(snap, "reservations", offline_days=0), False, "reservations", True)
    assert "Offline For" in offline.splitlines()[0]
    empty = render(query_rows(snap, "reservations", offline_days=10_000), False, "reservations", True)
    assert "Offline For" in empty.splitlines()[0] and empty.endswith("0 row(s)")
    as_json = json.loads(render(query_rows(snap, "reservations", offline_days=0), True, "reservations", True))
    assert all("Offline For" in row for row in as_json)


# -- the command line -----------------------------------------------------------------------------

def run(fake_client, monkeypatch, argv):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    return cli.main(argv)


def add_users(fake_client, *users):
    now = time.time()
    for mac, name, ip, days in users:
        record = {"mac": mac, "name": name, "use_fixedip": True, "fixed_ip": ip,
                  "last_connection_network_id": "net-1"}
        if days is not None:
            record["last_seen"] = int(now - days * DAY)
        fake_client.session.fx["legacy"]["alluser"].append(record)


def test_diagnose_json_reports_a_warning_then_a_critical(fake_client, monkeypatch, capsys):
    add_users(fake_client, ("bb:00:00:00:00:11", "media-box", "10.0.0.61", 3))
    assert run(fake_client, monkeypatch, ["diagnose", "--json", "--no-events"]) == 1
    doc = json.loads(capsys.readouterr().out)
    (f,) = [f for f in doc["findings"] if f["code"] == "reservation.offline"]
    assert f["severity"] == "warning" and f["subject"] == "media-box"

    add_users(fake_client, ("bb:00:00:00:00:12", "backup-box", "10.0.0.62", 10))
    assert run(fake_client, monkeypatch, ["diagnose", "--no-events", "--no-emoji"]) == 2
    out = capsys.readouterr().out
    assert "[CRITICAL] backup-box: reserved IP 10.0.0.62 (Main) is offline: last seen 10d ago" in out
    assert "[WARNING ] media-box: reserved IP 10.0.0.61 (Main) is offline: last seen 3d ago" in out


def test_diagnose_uses_the_thresholds_file_and_the_ignore_list(fake_client, monkeypatch, capsys, tmp_path):
    add_users(fake_client, ("bb:00:00:00:00:11", "media-box", "10.0.0.61", 3),
              ("bb:00:00:00:00:12", "travel-laptop", "10.0.0.62", 40))
    config = tmp_path / "s.toml"
    config.write_text("[thresholds]\nreserved_offline_warn_days = 5\n"
                      '[[ignore]]\nsubject = "travel-laptop"\nreason = "away"\n')
    assert run(fake_client, monkeypatch, ["diagnose", "--no-events", "--config", str(config), "--json"]) in (0, 1)
    doc = json.loads(capsys.readouterr().out)
    assert not [f for f in doc["findings"] if f["code"].startswith("reservation.offline")]   # 3 days < 5; laptop ignored
    assert doc["summary"]["ignored"] == 1


def test_query_reservations_offline_lists_the_reported_set(fake_client, monkeypatch, capsys):
    add_users(fake_client, ("bb:00:00:00:00:11", "media-box", "10.0.0.61", 3),
              ("bb:00:00:00:00:12", "fresh-box", "10.0.0.62", 0.1), ("bb:00:00:00:00:14", "ghost", "10.0.0.64", None))
    assert run(fake_client, monkeypatch, ["query", "reservations", "--offline"]) == 0
    out = capsys.readouterr().out
    assert "Offline For" in out and "media-box" in out and "3d" in out and "ghost" in out and "never seen" in out
    assert "fresh-box" not in out and "old-printer" not in out        # under a day
    assert out.rstrip().endswith("2 row(s)")


def test_query_reservations_offline_uses_the_config_threshold_and_json(fake_client, monkeypatch, capsys, tmp_path):
    add_users(fake_client, ("bb:00:00:00:00:11", "media-box", "10.0.0.61", 3))
    config = tmp_path / "s.toml"
    config.write_text("[thresholds]\nreserved_offline_warn_days = 0\n")
    assert run(fake_client, monkeypatch, ["query", "reservations", "--offline", "--config", str(config), "--json"]) == 0
    names = {row["Name"] for row in json.loads(capsys.readouterr().out)}
    assert {"media-box", "old-printer"} <= names                      # a zero threshold lists every offline reservation


def test_query_reservations_without_offline_is_unchanged(fake_client, monkeypatch, capsys):
    add_users(fake_client, ("bb:00:00:00:00:11", "media-box", "10.0.0.61", 3))
    assert run(fake_client, monkeypatch, ["query", "reservations"]) == 0
    out = capsys.readouterr().out
    assert "Offline For" not in out and "media-box" in out and "desktop" in out


@pytest.mark.parametrize("argv", [["query", "devices", "--offline"], ["query", "--offline"],
                                  ["query", "reservations", "--config", "x.toml"],
                                  ["query", "devices", "--config", "x.toml"]])
def test_offline_and_config_are_only_for_reservations(fake_client, monkeypatch, argv):
    with pytest.raises(SystemExit) as caught:
        run(fake_client, monkeypatch, argv)
    assert caught.value.code == cli.EXIT_USAGE


def test_a_bad_config_stops_query_offline_before_any_request(fake_client, monkeypatch, capsys, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("[thresholds]\nreserved_offline_warn_days = 9\nreserved_offline_critical_days = 1\n")
    assert run(fake_client, monkeypatch, ["query", "reservations", "--offline", "--config", str(bad)]) == cli.EXIT_ERROR
    assert "must not exceed" in capsys.readouterr().err and fake_client.session.calls == []


def test_describe_age_lives_in_util_and_wan_still_exposes_it():
    assert wan.describe_age is describe_age
    assert [describe_age(s) for s in (30, 600, 7200, 90_000, 3 * DAY, 400 * DAY)] == [
        "1m", "10m", "2h", "25h", "3d", "400d"]
