import re

import pytest

from unifi_sentinel import cli
from unifi_sentinel.client import UniFiAPIError
from unifi_sentinel.config import ConfigError
from unifi_sentinel.diagnose import diagnose, exit_code
from unifi_sentinel.events import describe_duration
from unifi_sentinel.settings import DiagnoseSettings, load_settings
from unifi_sentinel.snapshot import Snapshot

HOUR = 3600
STAMP = 1_790_000_000_000      # ms


def event(kind, ts=STAMP, **params):
    return {"event": kind, "key": kind + "_2", "timestamp": ts, "parameters": params}


def client_event(kind, name="phone", mac="bb:01", ts=STAMP):
    return event(kind, ts, CLIENT={"id": mac, "name": name})


def snap(events, devices=(), window=86400, truncated=False):
    return Snapshot(site={}, devices=list(devices), clients=[], events=list(events),
                    event_window_seconds=window, events_truncated=truncated,
                    legacy_devices=[{"mac": "aa:01", "type": "usw"}])    # keeps the unrelated legacy info out


def found(events, settings=None, **kw):
    return {(f.severity, f.subject, f.message) for f in diagnose(snap(events, **kw), settings)}


def messages(events, settings=None, **kw):
    return {(s, m) for s, _subject, m in found(events, settings, **kw)}


# -- nothing to report -------------------------------------------------------

def test_no_events_and_irrelevant_events_add_nothing():
    assert found([]) == set()
    boring = [client_event("CLIENT_CONNECTED_WIRELESS"), event("ADMIN_ACCESS"), event("MADE_CHANGES"),
              client_event("CLIENT_CONNECTED_WIRED"), event("REMOVED_ENTITY")]
    assert found(boring) == set()


# -- IP conflicts ------------------------------------------------------------

def test_ip_conflicts_are_warnings_grouped_by_ip_with_a_count_and_latest_time():
    events = [event("CLIENT_IP_CONFLICT", STAMP + i * 1000, IP={"name": "10.0.0.5"}) for i in range(4)]
    events += [event("CLIENT_IP_CONFLICT", STAMP, IP={"name": "10.0.0.9"})]
    got = found(events)
    assert {(s, subj) for s, subj, _m in got} == {("warning", "10.0.0.5"), ("warning", "10.0.0.9")}
    four = next(m for _s, subj, m in got if subj == "10.0.0.5")
    assert four.startswith("IP conflict reported 4 times in the last 24h (most recent ")
    assert re.search(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\)$", four)
    assert next(m for _s, subj, m in got if subj == "10.0.0.9").startswith("IP conflict reported 1 time in")


def test_an_ip_conflict_without_an_address_is_still_reported():
    assert ("warning", "unknown IP") in {(s, subj) for s, subj, _m in found([event("CLIENT_IP_CONFLICT")])}


# -- flapping --------------------------------------------------------------

def test_repeated_disconnects_warn_at_the_threshold_wired_and_wireless_together():
    events = ([client_event("CLIENT_DISCONNECTED_WIRELESS", "speaker", "bb:09")] * 6
              + [client_event("CLIENT_DISCONNECTED_WIRED", "speaker", "bb:09")] * 4)
    assert ("warning", "speaker", "disconnected 10 times in the last 24h") in found(events)
    assert found(events[:9]) == set()                                           # 9 is below the default 10


def test_disconnects_are_counted_per_client_not_per_name():
    events = ([client_event("CLIENT_DISCONNECTED_WIRELESS", "iPhone", "bb:01")] * 6
              + [client_event("CLIENT_DISCONNECTED_WIRELESS", "iPhone", "bb:02")] * 6)   # same name, two devices
    assert found(events) == set()
    tight = DiagnoseSettings(event_flap_count=6)
    two = [f for f in diagnose(snap(events), tight) if f.subject == "iPhone"]       # a list: a set would merge them
    assert len(two) == 2 and {f.message for f in two} == {"disconnected 6 times in the last 24h"}


def test_flap_threshold_is_configurable_and_odd_events_are_safe():
    events = [client_event("CLIENT_DISCONNECTED_WIRELESS")] * 3
    assert found(events) == set()
    assert ("warning", "phone", "disconnected 3 times in the last 24h") in found(
        events, DiagnoseSettings(event_flap_count=3))
    odd = [event("CLIENT_DISCONNECTED_WIRELESS"), event("CLIENT_DISCONNECTED_WIRELESS", CLIENT={}),
           {"event": "CLIENT_DISCONNECTED_WIRELESS", "parameters": None}, {"parameters": {}}, {}]
    assert found(odd * 5, DiagnoseSettings(event_flap_count=1)) == set()      # no client to blame: skipped


def test_roaming_is_only_ever_informational():
    events = [client_event("CLIENT_ROAMED", "iPhone", "bb:03")] * 30
    got = found(events)
    assert got == {("info", "iPhone", "roamed 30 times in the last 24h (normal for a mobile device)")}
    assert found(events[:9]) == set()
    assert exit_code(diagnose(snap(events))) == 0                               # never fails a cron job


# -- devices and the internet -----------------------------------------------

AP_ONLINE = {"id": "1", "macAddress": "aa:03", "name": "Office AP", "state": "ONLINE"}
AP_OFFLINE = {"id": "2", "macAddress": "aa:04", "name": "Garage AP", "state": "OFFLINE"}


def unreachable(name, n=1):
    return [event("DEVICE_UNREACHABLE", DEVICE={"name": name})] * n


def test_a_device_unreachable_earlier_but_online_now_is_info():
    assert ("info", "Office AP", "was unreachable 1 time in the last 24h; online now") in found(
        unreachable("Office AP"), devices=[AP_ONLINE])
    assert ("info", "Office AP", "was unreachable 3 times in the last 24h; online now") in found(
        unreachable("office ap", 3), devices=[AP_ONLINE])                     # name match ignores case


def test_a_device_that_is_offline_now_is_left_to_the_offline_finding():
    got = found(unreachable("Garage AP", 4), devices=[AP_OFFLINE])
    assert not [f for f in got if "unreachable" in f[2]]
    assert any(f[1] == "Garage AP" and f[2] == "device is offline" for f in got)   # the existing finding


def test_a_device_that_keeps_dropping_is_a_warning_and_unknown_devices_still_report():
    assert ("warning", "Office AP", "was unreachable 10 times in the last 24h") in found(
        unreachable("Office AP", 10), devices=[AP_ONLINE])
    assert ("info", "Old AP", "was unreachable 1 time in the last 24h; online now") in found(unreachable("Old AP"))


def test_isp_latency_events_are_counted_as_info():
    assert ("info", "internet", "high latency was reported 3 times in the last 24h") in found(
        [event("ISP_HIGH_LATENCY")] * 3)
    assert ("info", "internet", "high latency was reported 1 time in the last 24h") in found(
        [event("ISP_HIGH_LATENCY")])


# -- window, cap and wording --------------------------------------------------

@pytest.mark.parametrize("seconds, text", [(86400, "24h"), (3 * HOUR, "3h"), (12 * HOUR, "12h"),
                                           (2 * 86400, "2d"), (7 * 86400, "7d"), (36 * HOUR, "36h"),
                                           (90 * 60, "90m"), (30, "1m")])
def test_describe_duration(seconds, text):
    assert describe_duration(seconds) == text


def test_messages_use_the_snapshot_window_and_default_to_24h():
    conflict = [event("CLIENT_IP_CONFLICT", IP={"name": "10.0.0.5"})]
    assert "in the last 7d" in next(iter(found(conflict, window=7 * 86400)))[2]
    assert "in the last 90m" in next(iter(found(conflict, window=90 * 60)))[2]
    assert "in the last 24h" in next(iter(found(conflict, window=0)))[2]


def test_a_capped_event_read_says_counts_may_be_low():
    got = found([event("ISP_HIGH_LATENCY")], truncated=True)
    assert ("info", "controller", "the event log read was cut off at its cap; event counts may be low") in got


def test_event_findings_set_the_exit_code_through_their_severity():
    conflict = diagnose(snap([event("CLIENT_IP_CONFLICT", IP={"name": "10.0.0.5"})]))
    assert exit_code(conflict) == 1 and exit_code(conflict, "critical") == 0       # a warning, never critical
    assert exit_code(diagnose(snap([event("ISP_HIGH_LATENCY")]))) == 0


# -- settings ----------------------------------------------------------------

def test_event_flap_count_loads_defaults_and_validates(tmp_path):
    assert DiagnoseSettings().event_flap_count == 10
    cfg = tmp_path / "c.toml"
    cfg.write_text("[thresholds]\nevent_flap_count = 4\n")
    assert load_settings(cfg).event_flap_count == 4
    for text, message in (("event_flap_count = 0", "at least 1"), ("event_flap_count = 2.5", "whole number"),
                          ('event_flap_count = "x"', "must be a number")):
        cfg.write_text("[thresholds]\n" + text + "\n")
        with pytest.raises(ConfigError, match=re.escape(message)):
            load_settings(cfg)


# -- command line ------------------------------------------------------------

def _run(fake_client, monkeypatch, argv):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    return cli.main(argv)


def test_cli_checks_events_by_default_and_names_the_window(fake_client, monkeypatch, capsys):
    assert _run(fake_client, monkeypatch, ["diagnose", "--no-emoji"]) == 1
    out = capsys.readouterr().out
    assert "[WARNING ] 10.0.0.50: IP conflict reported 1 time in the last 24h" in out
    assert "Garage AP: was unreachable" not in out                                # offline: the other finding covers it
    assert fake_client.session.posts and all(
        path.endswith("/system-log/all") for path, _ in fake_client.session.posts)     # the one approved POST


def test_cli_no_events_skips_the_log_and_never_posts(fake_client, monkeypatch, capsys):
    _run(fake_client, monkeypatch, ["diagnose", "--no-emoji", "--no-events"])
    assert "IP conflict" not in capsys.readouterr().out
    assert fake_client.session.posts == []


def test_cli_since_narrows_the_event_window(fake_client, monkeypatch, capsys):
    _run(fake_client, monkeypatch, ["diagnose", "--no-emoji", "--since", "1h"])        # the conflict is 2h old
    assert "IP conflict" not in capsys.readouterr().out
    _run(fake_client, monkeypatch, ["diagnose", "--no-emoji", "--since", "3h"])
    assert "IP conflict reported 1 time in the last 3h" in capsys.readouterr().out
    body = fake_client.session.posts[-1][1]
    assert body["timestampTo"] - body["timestampFrom"] == 3 * HOUR * 1000
    with pytest.raises(SystemExit) as exc:
        _run(fake_client, monkeypatch, ["diagnose", "--since", "5x"])
    assert exc.value.code == cli.EXIT_USAGE


def test_cli_flap_threshold_from_the_settings_file(fake_client, monkeypatch, capsys, tmp_path):
    _run(fake_client, monkeypatch, ["diagnose", "--no-emoji"])
    assert "phone: disconnected" not in capsys.readouterr().out                    # the phone dropped only 3 times
    cfg = tmp_path / "c.toml"
    cfg.write_text("[thresholds]\nevent_flap_count = 3\n")
    _run(fake_client, monkeypatch, ["diagnose", "--no-emoji", "--config", str(cfg)])
    assert "[WARNING ] phone: disconnected 3 times in the last 24h" in capsys.readouterr().out
    cfg.write_text('[[ignore]]\nsubject = "10.0.0.50"\nmessage = "IP conflict"\nreason = "known"\n')
    _run(fake_client, monkeypatch, ["diagnose", "--no-emoji", "--config", str(cfg), "--show-ignored"])
    out = capsys.readouterr().out
    assert "IP conflict reported" in out.split("Ignored")[-1] and "[WARNING ] 10.0.0.50" not in out


def test_cli_unreadable_event_log_warns_and_carries_on(fake_client, monkeypatch, capsys):
    def boom(site_ref, query):
        raise UniFiAPIError("log down")

    monkeypatch.setattr(fake_client, "system_log", boom)
    code = _run(fake_client, monkeypatch, ["diagnose", "--no-emoji"])
    captured = capsys.readouterr()
    assert code == 1                                                              # the fixture's other warnings
    assert "event log unavailable; event history was skipped: log down" in captured.err
    assert "IP conflict" not in captured.out and "Garage AP: device is offline" in captured.out
