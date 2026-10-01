import re

import pytest

from unifi_sentinel import cli
from unifi_sentinel.client import UniFiAPIError
from unifi_sentinel.config import ConfigError
from unifi_sentinel.diagnose import diagnose, exit_code
from unifi_sentinel.settings import DiagnoseSettings, load_settings
from unifi_sentinel.snapshot import Needs, Snapshot, collect_snapshot


def snap(*health):
    # A legacy device keeps the unrelated "legacy device data unavailable" info finding out.
    return Snapshot(site={"id": "s"}, devices=[], clients=[], health=list(health),
                    legacy_devices=[{"mac": "aa:01", "type": "usw"}])


def found(snapshot, settings=None):
    return {(f.severity, f.subject, f.message) for f in diagnose(snapshot, settings)}


def sub(name, status="ok", **kw):
    return {"subsystem": name, "status": status, **kw}


def test_healthy_controller_adds_nothing():
    assert found(snap(sub("wlan"), sub("lan"), sub("wan"), sub("www", latency=20, drops=0),
                      sub("vpn"))) == set()


def test_wan_error_is_critical_and_warning_is_warning():
    got = found(snap(sub("wan", "error", gw_name="Cloud Gateway Max"), sub("vpn", "warning")))
    assert ("critical", "wan", "wan subsystem is in error state (gateway Cloud Gateway Max)") in got
    assert ("warning", "vpn", "vpn subsystem is in warning state") in got


def test_lan_wlan_status_explained_by_disconnected_devices_is_only_info():
    got = found(snap(sub("lan", "error", num_disconnected=1), sub("wlan", "warning", num_disconnected=2)))
    assert got == {
        ("info", "lan", "lan subsystem reports error: 1 device(s) disconnected (see the device findings)"),
        ("info", "wlan", "wlan subsystem reports warning: 2 device(s) disconnected (see the device findings)"),
    }
    assert exit_code(diagnose(snap(sub("lan", "error", num_disconnected=1)))) == 0


def test_unexplained_lan_or_wlan_problem_keeps_the_controller_severity():
    got = found(snap(sub("lan", "error", num_disconnected=0), sub("wlan", "warning")))
    assert ("critical", "lan", "lan subsystem is in error state") in got
    assert ("warning", "wlan", "wlan subsystem is in warning state") in got


def test_unknown_or_missing_status_is_skipped():
    assert found(snap(sub("vpn", "unknown"), {"subsystem": "wan"}, {})) == set()


def test_wan_latency_and_drops_thresholds():
    assert found(snap(sub("www", latency=99, drops=9))) == set()                      # below defaults
    got = found(snap(sub("www", latency=140, drops=12)))
    assert ("warning", "www", "internet latency 140 ms") in got
    assert ("warning", "www", "internet reports 12 drops") in got
    boundary = found(snap(sub("www", latency=100, drops=10)))                          # "at or above"
    assert len(boundary) == 2
    tuned = DiagnoseSettings(wan_latency_warn_ms=10, wan_drops_warn=5)
    assert len(found(snap(sub("www", latency=20, drops=5)), tuned)) == 2
    loose = DiagnoseSettings(wan_latency_warn_ms=500, wan_drops_warn=100)
    assert found(snap(sub("www", latency=140, drops=12)), loose) == set()


def test_missing_none_and_odd_values_do_not_crash():
    weird = sub("www", latency=None, drops="lots", speedtest_status=None, num_pending=None)
    assert found(snap(weird, sub("lan", "error", num_disconnected=None, num_pending="x"))) == {
        ("critical", "lan", "lan subsystem is in error state")}


def test_speedtest_failure_is_info_but_other_states_are_not():
    assert ("info", "www", "last speedtest: Failed") in found(snap(sub("www", speedtest_status="Failed")))
    for state in ("Success", "Idle", "Running", ""):
        assert found(snap(sub("www", speedtest_status=state))) == set()


def test_pending_adoption_is_reported_once_across_subsystems():
    got = found(snap(sub("wlan", num_pending=1), sub("lan", num_pending=2), sub("wan", num_pending=0)))
    assert got == {("info", "controller", "3 device(s) waiting to be adopted")}


def test_no_health_data_adds_nothing():
    assert found(Snapshot(site={}, devices=[], clients=[],
                          legacy_devices=[{"mac": "aa:01", "type": "usw"}])) == set()


# -- settings --------------------------------------------------------------

def test_new_thresholds_load_validate_and_default(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("[thresholds]\nwan_latency_warn_ms = 250.5\nwan_drops_warn = 3\n")
    s = load_settings(path)
    assert (s.wan_latency_warn_ms, s.wan_drops_warn) == (250.5, 3)
    assert (DiagnoseSettings().wan_latency_warn_ms, DiagnoseSettings().wan_drops_warn) == (100, 10)
    for text, message in (("wan_latency_warn_ms = -1", "at least 0"),
                          ('wan_drops_warn = "many"', "must be a number")):
        path.write_text("[thresholds]\n" + text + "\n")
        with pytest.raises(ConfigError, match=re.escape(message)):
            load_settings(path)


# -- snapshot and CLI ------------------------------------------------------

def test_snapshot_collects_health_only_when_asked(fake_client):
    assert collect_snapshot(fake_client, "default").health == []
    health = collect_snapshot(fake_client, "default", Needs(health=True)).health
    assert {h["subsystem"] for h in health} == {"wlan", "lan", "wan", "www", "vpn"}


def test_health_endpoint_failure_degrades_with_a_warning(fake_client, monkeypatch, capsys):
    real = fake_client.legacy_stat

    def flaky(site_ref, resource):
        if resource == "health":
            raise UniFiAPIError("boom")
        return real(site_ref, resource)

    monkeypatch.setattr(fake_client, "legacy_stat", flaky)
    snapshot = collect_snapshot(fake_client, "default", Needs(health=True))
    assert snapshot.health == []
    assert "controller health and WAN checks were skipped" in capsys.readouterr().err
    diagnose(snapshot)  # still works without health data


def test_cli_diagnose_shows_the_explained_wlan_status_and_keeps_the_exit_code(
        fake_client, monkeypatch, capsys):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    code = cli.main(["diagnose", "--no-emoji"])
    out = capsys.readouterr().out
    assert "wlan: wlan subsystem reports warning: 1 device(s) disconnected" in out
    assert "[INFO" in out and code == 1  # fixture still has real warnings; the new line is info only
