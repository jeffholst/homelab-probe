"""The fixture recorder (tools/record_fixture.py), run against the fake controller instead of a real one."""

import json
import os
import stat
import sys

import pytest
from conftest import FIXTURE, FakeSession
from contract import CONTRACT, RecordingSession
from record_fixture import DEFAULT_OUTPUT, build_fixture, main, project, record, write_private
from sanitize import LeakError

from unifi_sentinel.client import UniFiClient
from unifi_sentinel.diagnose import diagnose
from unifi_sentinel.events import render_message
from unifi_sentinel.snapshot import EventQuery, Needs, collect_snapshot

EVERYTHING = Needs(offline=True, reservations=True, groups=True, health=True, speedtests=True, neighbors=True, firewall=True, wlans=True,
                   events=EventQuery(since_seconds=7 * 86400))


def fake_client(session=None):
    client = UniFiClient("https://controller", "key")
    client.session = session or FakeSession()
    return client


@pytest.fixture(scope="module")
def recorded():
    fixture, missing, now_ms = record(fake_client(), "default", 7)
    return fixture, missing, now_ms


def test_every_field_the_code_reads_was_recorded(recorded):
    _, missing, _ = recorded
    assert missing == {}


def test_nothing_from_the_source_survives_in_the_recording(recorded):
    fixture, _, _ = recorded
    text = json.dumps(fixture).lower()
    source = json.dumps(FIXTURE).lower()
    for real in ("office switch", "office ap", "garage ap", "desktop", "old-printer", "bb:00:00:00:00:01",
                 "aa:00:00:00:00:02", "10.0.0.10", "10.0.0.50", "homenet"):
        assert real in source and real not in text, real


def test_only_the_fields_of_the_contract_are_kept(recorded):
    fixture, _, _ = recorded
    assert "example printers inc" in json.dumps(fixture).lower()       # a vendor name is public, so it is kept
    assert set(fixture["info"]) <= {"applicationVersion"}
    for device in fixture["legacy"]["device"]:
        assert set(device) <= {"mac", "type", "name", "model", "uptime", "uplink", "port_table", "radio_table_stats",
                               "vap_table", "wan1", "total_max_power", "total_used_power", "overheating"}


def test_the_recording_can_be_replayed_and_gives_the_same_findings(recorded):
    fixture, _, _ = recorded
    original = collect_snapshot(fake_client(), "default", EVERYTHING)
    replay = collect_snapshot(fake_client(FakeSession(fixture)), "default", EVERYTHING)
    assert len(replay.devices) == len(original.devices) and len(replay.clients) == len(original.clients)
    assert len(replay.events) == len(original.events) and len(replay.speedtests) == len(original.speedtests)
    assert len(replay.all_users) == len(original.all_users) and len(replay.neighbors) == len(original.neighbors)
    codes = [(f.severity, f.code) for f in diagnose(original)]
    assert sorted((f.severity, f.code) for f in diagnose(replay)) == sorted(codes)


def test_a_title_only_event_survives_recording_and_replay():
    now_ms = 1_000_000_000
    event = {"key": "TITLE_ONLY", "timestamp": now_ms, "category": "AUDIT", "severity": "LOW",
             "title_raw": "Synthetic title only"}
    fixture = build_fixture([
        ("POST", "/proxy/network/v2/api/site/default/system-log/all", {"data": [event]})
    ], now_ms)
    replay = FakeSession(fixture)
    assert render_message(replay.events[0]) == "Synthetic title only"


def test_times_are_stored_as_ages(recorded):
    fixture, _, _ = recorded
    assert all("age_s" in e and "timestamp" not in e for e in fixture["system_log"])
    assert all("age_s" in t and "time" not in t for t in fixture["legacy_v2"]["speedtest"]["data"])
    assert all("last_seen" not in u for u in fixture["legacy"]["alluser"])


def test_the_site_becomes_the_one_the_fake_controller_routes_by(recorded):
    fixture, _, _ = recorded
    assert [s["id"] for s in fixture["sites"]] == ["site-1"]
    assert fixture["sites"][0]["internalReference"] == "default"


def test_a_field_the_controller_stopped_returning_is_reported_by_name():
    session = FakeSession()
    for user in session.fx["legacy"]["sta"]:
        user.pop("signal", None)
    _, missing, _ = record(fake_client(session), "default", 7)
    assert missing == {"legacy/stat/sta": ["legacy/stat/sta: no record has 'signal'"]}


def test_the_recording_session_refuses_anything_but_reads():
    recording = RecordingSession(FakeSession())
    with pytest.raises(AssertionError, match="only the event log query"):
        recording.post("https://controller/proxy/network/api/s/default/cmd/devmgr", json={"cmd": "restart"})
    for method in (recording.put, recording.patch, recording.delete, recording.request):
        with pytest.raises(AssertionError, match="read-only"):
            method("https://controller/x")
    assert recording.exchanges == []


def test_a_failed_response_is_not_recorded():
    session = FakeSession()
    session.status = 500
    recording = RecordingSession(session)
    assert not recording.get("https://controller/proxy/network/integration/v1/sites").ok
    assert recording.exchanges == []


def test_project_keeps_only_what_is_named():
    record_ = {"a": 1, "b": {"c": 2, "d": 3}, "e": [{"f": 1, "g": 2}], "h": {"x": {"k": 1, "z": 2}, "y": {"k": 3}}}
    specs = [["a"], ["b", "c"], ["e", "[]", "f"], ["h", "*", "k"]]
    assert project(record_, specs) == {"a": 1, "b": {"c": 2}, "e": [{"f": 1}], "h": {"x": {"k": 1}, "y": {"k": 3}}}
    assert project(record_, [["b"]])["b"] == {"c": 2, "d": 3}                 # a declared container is kept whole
    assert project({"e": [1, 2]}, [["z"]]) == {} and project({"e": [1]}, [["e", "[]", "f"]]) == {"e": [1]}
    assert project({"e": [{"f": 1}]}, [["e", "x"]]) == {"e": []}


def test_build_fixture_ignores_endpoints_outside_the_contract():
    exchanges = [("GET", "/proxy/network/api/s/default/stat/something-new", {"data": [{"secret": 1}]}),
                 ("GET", "/proxy/network/integration/v1/info", {"applicationVersion": "1.2.3", "extra": "x"})]
    fixture = build_fixture(exchanges, 0)
    assert fixture["legacy"] == {} and fixture["info"] == {"applicationVersion": "1.2.3"}


def test_a_leak_stops_the_recording_and_nothing_is_written(monkeypatch, tmp_path, capsys):
    import record_fixture

    def leaky(fixture, exchanges):
        raise LeakError("1 leak(s) in the sanitised data:\n  x: a real name is still present")

    monkeypatch.setattr(record_fixture, "sanitise", leaky)
    monkeypatch.setattr(record_fixture.UniFiClient, "from_config", lambda config: fake_client())
    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("API_KEY", "key")
    output = tmp_path / "out.json"
    assert main(["--output", str(output)]) == 4
    assert not output.exists() and "nothing written" in capsys.readouterr().err


def test_main_writes_an_owner_only_file_and_prints_counts_only(monkeypatch, tmp_path, capsys):
    import record_fixture

    monkeypatch.setattr(record_fixture.UniFiClient, "from_config", lambda config: fake_client())
    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("API_KEY", "key")
    output = tmp_path / "sub" / "controller.json"
    assert main(["--output", str(output), "--event-days", "1"]) == 0
    printed = capsys.readouterr().out
    assert "leak check passed" in printed and "every field the code reads was present" in printed
    assert "Office" not in printed and "10.0.0" not in printed
    assert stat.S_IMODE(os.stat(output).st_mode) == 0o600
    assert set(json.loads(output.read_text())) >= {"sites", "devices", "legacy", "system_log"}


def test_main_reports_a_missing_configuration(capsys):
    assert main([]) == 3
    assert "CONTROLLER_URL" in capsys.readouterr().err


def test_main_reports_a_controller_that_cannot_be_read(monkeypatch, capsys):
    import record_fixture

    session = FakeSession()
    session.status = 500
    monkeypatch.setattr(record_fixture.UniFiClient, "from_config", lambda config: fake_client(session))
    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("API_KEY", "key")
    assert main([]) == 3
    assert "ERROR" in capsys.readouterr().err


def test_the_default_output_is_git_ignored():
    assert DEFAULT_OUTPUT.parent.name == "recorded"
    assert "tools/recorded/" in (DEFAULT_OUTPUT.parent.parent.parent / ".gitignore").read_text().split()


def test_write_private_replaces_a_looser_file(tmp_path):
    path = tmp_path / "f.json"
    path.write_text("{}")
    os.chmod(path, 0o644)
    write_private(path, {"a": 1})
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600 and json.loads(path.read_text()) == {"a": 1}


def test_every_contract_endpoint_has_a_place_in_the_recording(recorded):
    fixture, _, _ = recorded
    assert CONTRACT and sys.modules["record_fixture"] and fixture["devices"] and fixture["legacy"]["health"]
