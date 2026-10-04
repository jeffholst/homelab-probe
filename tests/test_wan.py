import json
import re

import pytest

from unifi_sentinel import cli
from unifi_sentinel.client import UniFiAPIError
from unifi_sentinel.config import ConfigError
from unifi_sentinel.diagnose import diagnose
from unifi_sentinel.settings import DiagnoseSettings, load_settings
from unifi_sentinel.snapshot import Needs, Snapshot, collect_snapshot
from unifi_sentinel.wan import (
    MIN_SPEEDTESTS,
    build_wan,
    describe_age,
    median_download,
    render_text,
    speedtests_since,
    to_json,
)

NOW = 1_790_000_000_000
DAY_MS = 86_400_000


def ts(days_ago):
    return NOW - int(days_ago * DAY_MS)


def run(days_ago, download=900, upload=40, latency=24):
    return {"time": ts(days_ago), "download_mbps": download, "upload_mbps": upload,
            "latency_ms": latency, "interface_name": "eth4"}


def health(**wan):
    return [{"subsystem": "wan", "status": "ok", "isp_name": "Example ISP", "wan_ip": "192.0.2.10",
             "gw_name": "GW", **wan}, {"subsystem": "www", "status": "ok", "latency": 21, "drops": 2}]


def stats(**kw):
    base = {"availability": 100.0, "latency_average": 23, "time_period": 86400, "monitors": [],
            "alerting_monitors": []}
    return {"WAN": {**base, **kw}}


def snap(tests=(), h=None, devices=()):
    return Snapshot(site={}, devices=[], clients=[], speedtests=list(tests), health=h if h is not None else [],
                    legacy_devices=list(devices) or [{"mac": "AA:01", "type": "usw"}])


def findings(snapshot, settings=None):
    return {(f.severity, f.subject, f.message) for f in diagnose(snapshot, settings)
            if f.subject == "wan"}


# -- helpers -----------------------------------------------------------------

@pytest.mark.parametrize("seconds, text", [(10, "1m"), (59 * 60, "59m"), (3600, "1h"), (47 * 3600, "47h"),
                                           (2 * 86400, "2d"), (40 * 86400, "40d")])
def test_describe_age(seconds, text):
    assert describe_age(seconds) == text


def test_speedtests_since_and_median_need_enough_runs():
    tests = [run(1), run(5), run(40), {"time": None, "download_mbps": 1}, {"download_mbps": 1}]
    assert len(speedtests_since(tests, 30, NOW)) == 2
    assert median_download([run(1, d) for d in (100, 200, 300, 400)]) is None          # fewer than MIN_SPEEDTESTS
    assert MIN_SPEEDTESTS == 5
    assert median_download([run(1, d) for d in (100, 900, 300, 200, 400)]) == 300
    assert median_download([run(1, None), *[run(1, 500) for _ in range(5)], {"download_mbps": "fast"}]) == 500


def test_speedtest_baseline_does_not_mix_interfaces():
    tests = [run(1, 900), run(2, 900), {**run(3, 500), "interface_name": "eth5"},
             {**run(4, 500), "interface_name": "eth5"}, {**run(5, 900), "interface_name": "eth4"}]
    report = build_wan(snap(tests), 30, now_ms=NOW)
    assert report["speedtests"]["download"] == {"min": 900, "median": 900, "max": 900}


def test_zero_speed_is_a_slow_run():
    tests = [run(1, 0), *[run(days, 900) for days in range(2, 7)]]
    report = build_wan(snap(tests), 30, now_ms=NOW)
    assert report["speedtests"]["slow_runs"][0]["download_mbps"] == 0


# -- the report from the fixture ---------------------------------------------

@pytest.fixture
def fixture_wan(fake_client):
    snapshot = collect_snapshot(fake_client, "default", Needs(health=True, speedtests=True))
    return build_wan(snapshot, 30)


def test_current_state_link_and_monitoring(fixture_wan):
    assert fixture_wan["now"] == {"status": "ok", "isp": "Example ISP", "wan_ip": "192.0.2.10",
                                  "gateway": "Gateway", "internet_status": "ok", "latency_ms": 20.0,
                                  "drops": 0.0, "speedtest_status": "Success"}
    (link,) = fixture_wan["links"]
    assert (link["port"], link["interface"], link["up"], link["speed_mbps"], link["max_speed_mbps"]) == (
        "wan1", "eth4", True, 1000, 2500)
    assert (link["tx_mbps"], link["rx_mbps"]) == (1.0, 2.0)                           # bytes per second to Mbps
    (mon,) = fixture_wan["monitoring"]
    assert (mon["name"], mon["availability"], mon["period_s"]) == ("WAN", 100.0, 86400)
    assert [(t["target"], t["alerts"]) for t in mon["targets"]] == [
        ("192.0.2.53", True), ("example.com", False), ("example.org", False)]


def test_speedtest_summary_follows_the_window(fixture_wan, fake_client):
    s = fixture_wan["speedtests"]
    assert (s["days"], s["count"], s["total_stored"]) == (30, 11, 12)                 # the 40-day-old run is outside
    assert s["last"]["download_mbps"] == 880 and s["last"]["age"] == "6h"
    assert s["download"] == {"min": 500, "median": 925, "max": 940}
    assert [r["download_mbps"] for r in s["slow_runs"]] == [500]                      # below 70% of 925
    snapshot = collect_snapshot(fake_client, "default", Needs(health=True, speedtests=True))
    assert build_wan(snapshot, 7)["speedtests"]["slow_runs"] == []                    # the slow run is 8 days old
    wide = build_wan(snapshot, 90)["speedtests"]
    assert wide["count"] == 12 and wide["download"]["max"] == 940


def test_slow_runs_follow_the_threshold_setting(fake_client):
    snapshot = collect_snapshot(fake_client, "default", Needs(health=True, speedtests=True))
    strict = build_wan(snapshot, 30, DiagnoseSettings(wan_speed_drop_pct=96))
    assert len(strict["speedtests"]["slow_runs"]) > 1 and strict["speedtests"]["slow_threshold_pct"] == 96
    assert strict["speedtests"]["slow_runs"][0]["download_mbps"] == 880              # newest first


def test_text_report(fixture_wan):
    text = render_text(fixture_wan)
    assert text.startswith("Internet: ok (Example ISP)")
    for line in ("  WAN IP: 192.0.2.10", "  Gateway: Gateway", "  Now: latency 20 ms, 0 drops, status ok",
                 "  Link wan1: eth4 up, 1000 Mbps full duplex (port supports 2500 Mbps)",
                 "    live: 1.0 Mbps up, 2.0 Mbps down",
                 "Last 24h (controller monitoring, WAN): availability 100.0%, average latency 23 ms",
                 "Speedtests, last 30 days (11 runs), 12 stored",
                 "  Download: min 500 Mbps, median 925 Mbps, max 940 Mbps",
                 "  Download below 70% of the median (1):"):
        assert line in text, line
    assert re.search(r"192\.0\.2\.53\s+dns\s+100\.0%\s+22 ms\s+yes", text)                 # the alerts column
    assert re.search(r"example\.com\s+icmp\s+100\.0%\s+20 ms\s*$", text, re.M)               # not configured to alert
    assert json.loads(to_json(fixture_wan))["speedtests"]["count"] == 11


# -- missing and odd data -------------------------------------------------------

def test_a_controller_with_no_wan_data_still_reports():
    wan = build_wan(snap(), 30, now_ms=NOW)
    assert wan["now"]["status"] == "unknown" and wan["links"] == [] and wan["monitoring"] == []
    assert wan["speedtests"]["last"] is None and wan["speedtests"]["download"] is None
    text = render_text(wan)
    assert "Controller monitoring: not available" in text
    assert "none stored (the controller has not run a speedtest)" in text


def test_odd_values_never_crash_the_report_or_diagnose():
    odd_health = health(uptime_stats={"WAN": {"availability": "high", "monitors": "none", "time_period": None,
                                              "alerting_monitors": [None, {"target": None}]},
                                      "BAD": 5, "LTE": {"monitors": [{"target": "x", "availability": None}]}})
    gateway = {"mac": "AA:01", "type": "udm", "wan1": "not a dict", "wan2": {"up": False, "speed": None},
               "wan": {"up": True}, "wanx": {}}
    tests = [{"time": NOW, "download_mbps": None}, "junk", {"time": "yesterday"}, run(1, "fast"), run(2)]
    s = snap(tests, odd_health, [gateway])
    wan = build_wan(s, 30, now_ms=NOW)
    assert [link["port"] for link in wan["links"]] == ["wan1", "wan2"]
    assert wan["links"][1]["up"] is False
    render_text(wan)
    json.loads(to_json(wan))
    diagnose(s)                                                                         # must not raise


def test_a_down_link_and_multiple_wans_are_shown():
    gateway = {"mac": "AA:01", "type": "udm", "wan1": {"name": "eth4", "up": True, "speed": 1000},
               "wan2": {"name": "eth5", "up": False}}
    h = health(uptime_stats={"WAN": {"availability": 100, "time_period": 86400},
                             "WAN2": {"availability": 80.0, "latency_average": 90, "time_period": 3600}})
    text = render_text(build_wan(snap([], h, [gateway]), 30, now_ms=NOW))
    assert "Link wan2: eth5 DOWN" in text
    assert "Last 24h (controller monitoring, WAN)" in text
    assert "Last 1h (controller monitoring, WAN2): availability 80.0%, average latency 90 ms" in text


# -- diagnose ---------------------------------------------------------------------

def test_availability_below_the_threshold_is_a_warning_for_the_total_and_each_monitor():
    bad = [{"target": "example.com", "type": "icmp", "availability": 93.5, "latency_average": 120},
           {"target": "192.0.2.53", "type": "dns", "availability": 100.0, "latency_average": 22}]
    got = findings(snap([], health(uptime_stats=stats(availability=97.5, monitors=bad))))
    assert ("warning", "wan", "internet availability 97.5% over the last 24h") in got
    assert ("warning", "wan", "monitor example.com (icmp) availability 93.5% over the last 24h, "
                              "latency 120 ms") in got
    assert not any("192.0.2.53" in m for _s, _sub, m in got)                           # a healthy monitor is silent


def test_alerting_monitors_at_full_availability_are_not_findings():
    healthy = [{"target": "1.1.1.1", "type": "dns", "availability": 100.0, "latency_average": 27}]
    assert findings(snap([], health(uptime_stats=stats(monitors=healthy, alerting_monitors=healthy)))) == set()


def test_availability_threshold_boundary_and_setting():
    at = health(uptime_stats=stats(availability=99.0))
    assert findings(snap([], at)) == set()                                              # at the threshold is fine
    assert findings(snap([], health(uptime_stats=stats(availability=98.9)))) != set()
    assert findings(snap([], at), DiagnoseSettings(wan_availability_warn_pct=99.5)) != set()
    assert findings(snap([], health(uptime_stats=stats(availability=98.9))),
                    DiagnoseSettings(wan_availability_warn_pct=95)) == set()


def test_multiple_wans_name_the_one_that_is_down():
    h = health(uptime_stats={"WAN": {"availability": 100.0, "time_period": 86400},
                             "WAN2": {"availability": 50.0, "time_period": 86400}})
    assert findings(snap([], h)) == {("warning", "wan", "internet availability (WAN2) 50.0% over the last 24h")}


def baseline(last_download, count=6, last_days=1):
    return [run(10 + i, 900) for i in range(count)] + [run(last_days, last_download)]


def test_a_last_speedtest_far_below_the_median_is_a_warning():
    # diagnose reads the real clock, so build the history relative to it
    import time as _time
    now = int(_time.time() * 1000)
    tests = [{"time": now - (10 + i) * DAY_MS, "download_mbps": 900} for i in range(6)]
    tests.append({"time": now - 5 * 3600 * 1000, "download_mbps": 450})
    got = findings(snap(tests, []))
    assert got == {("warning", "wan", "last speedtest download 450 Mbps (5h ago) is 50% of the 30-day median (900 Mbps)")}
    tests[-1]["download_mbps"] = 630                                                    # exactly 70%
    assert findings(snap(tests, [])) == set()
    assert findings(snap(tests, []), DiagnoseSettings(wan_speed_drop_pct=75)) != set()


def test_the_speedtest_check_needs_a_baseline_and_a_recent_last_run():
    import time as _time
    now = int(_time.time() * 1000)
    few = [{"time": now - (10 + i) * DAY_MS, "download_mbps": 900} for i in range(3)] + [
        {"time": now - 3600_000, "download_mbps": 100}]
    assert findings(snap(few, [])) == set()                                             # too few runs for a median
    stale = [{"time": now - (60 + i) * DAY_MS, "download_mbps": 900} for i in range(6)] + [
        {"time": now - 45 * DAY_MS, "download_mbps": 100}]
    assert findings(snap(stale, [])) == set()                                           # nothing within the baseline
    assert findings(snap([], [])) == set()


# -- settings and snapshot ---------------------------------------------------------

def test_new_thresholds_default_load_and_validate(tmp_path):
    d = DiagnoseSettings()
    assert (d.wan_availability_warn_pct, d.wan_speed_drop_pct) == (99, 70)
    cfg = tmp_path / "c.toml"
    cfg.write_text("[thresholds]\nwan_availability_warn_pct = 95.5\nwan_speed_drop_pct = 50\n")
    s = load_settings(cfg)
    assert (s.wan_availability_warn_pct, s.wan_speed_drop_pct) == (95.5, 50)
    for text, message in (("wan_availability_warn_pct = 101", "between 0 and 100"),
                          ("wan_speed_drop_pct = -1", "between 0 and 100"),
                          ('wan_speed_drop_pct = "low"', "must be a number")):
        cfg.write_text("[thresholds]\n" + text + "\n")
        with pytest.raises(ConfigError, match=re.escape(message)):
            load_settings(cfg)


def test_speedtests_are_collected_oldest_first_and_only_when_asked(fake_client, monkeypatch, capsys):
    assert collect_snapshot(fake_client, "default").speedtests == []
    tests = collect_snapshot(fake_client, "default", Needs(speedtests=True)).speedtests
    assert len(tests) == 12 and [t["time"] for t in tests] == sorted(t["time"] for t in tests)
    assert "age_s" not in tests[0] and tests[0]["download_mbps"] == 930                # the 40-day-old run

    def boom(site_ref, resource):
        raise UniFiAPIError("nope")

    monkeypatch.setattr(fake_client, "legacy_v2", boom)
    assert collect_snapshot(fake_client, "default", Needs(speedtests=True)).speedtests == []
    assert "speedtest history unavailable" in capsys.readouterr().err


def test_malformed_speedtest_time_is_sorted_last_without_crashing(fake_client, monkeypatch):
    monkeypatch.setattr(fake_client, "legacy_v2", lambda site_ref, resource: [
        {"time": "bad", "download_mbps": 1}, {"time": 2, "download_mbps": 2}])
    tests = collect_snapshot(fake_client, "default", Needs(speedtests=True)).speedtests
    assert [test["time"] for test in tests] == ["bad", 2]


def test_unknown_link_status_is_not_rendered_as_down():
    report = build_wan(snap(devices=[{"mac": "AA:01", "type": "udm", "wan1": {}}]), 30, now_ms=NOW)
    assert "Link wan1: ? unknown" in render_text(report)


# -- command line -------------------------------------------------------------------

def _run(fake_client, monkeypatch, argv):
    fake_client.session.fx["legacy"]["device"][0]["overheating"] = False
    monkeypatch.setenv("CONTROLLER_URL", "https://controller")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    return cli.main(argv)


def test_cli_wan_text_days_json_and_no_posts(fake_client, monkeypatch, capsys):
    assert _run(fake_client, monkeypatch, ["wan"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Internet: ok (Example ISP)") and "Speedtests, last 30 days (11 runs), 12 stored" in out

    assert _run(fake_client, monkeypatch, ["wan", "--days", "7"]) == 0
    assert "Speedtests, last 7 days (7 runs), 12 stored" in capsys.readouterr().out

    assert _run(fake_client, monkeypatch, ["wan", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["speedtests"]["last"]["download_mbps"] == 880
    assert fake_client.session.posts == []                                              # wan only reads (GET)

    for bad in (["wan", "--days", "0"], ["wan", "--days", "many"]):
        with pytest.raises(SystemExit) as exc:
            _run(fake_client, monkeypatch, bad)
        assert exc.value.code == cli.EXIT_USAGE
        capsys.readouterr()


def test_cli_wan_uses_the_settings_file_and_degrades_without_speedtests(fake_client, monkeypatch, capsys, tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text("[thresholds]\nwan_speed_drop_pct = 96\n")
    assert _run(fake_client, monkeypatch, ["wan", "--config", str(cfg)]) == 0
    assert "Download below 96% of the median" in capsys.readouterr().out
    cfg.write_text("[thresholds]\nbogus = 1\n")
    assert _run(fake_client, monkeypatch, ["wan", "--config", str(cfg)]) == cli.EXIT_ERROR
    capsys.readouterr()

    def boom(site_ref, resource):
        raise UniFiAPIError("nope")

    monkeypatch.setattr(fake_client, "legacy_v2", boom)
    assert _run(fake_client, monkeypatch, ["wan"]) == 0
    captured = capsys.readouterr()
    assert "none stored (the controller has not run a speedtest)" in captured.out
    assert "speedtest history unavailable" in captured.err and "Internet: ok" in captured.out


def test_cli_diagnose_reports_a_degraded_speedtest_from_the_fixture(fake_client, monkeypatch, capsys):
    assert _run(fake_client, monkeypatch, ["diagnose", "--no-emoji", "--no-events"]) == 1
    assert "last speedtest" not in capsys.readouterr().out                              # the fixture's last run is normal
    fake_client.session.fx["legacy_v2"]["speedtest"]["data"][0]["download_mbps"] = 300   # age 6h: the newest run
    _run(fake_client, monkeypatch, ["diagnose", "--no-emoji", "--no-events"])
    out = capsys.readouterr().out
    assert "[WARNING ] wan: last speedtest download 300 Mbps (6h ago) is 32% of the 30-day median" in out
