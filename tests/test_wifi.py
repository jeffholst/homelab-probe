import re

import pytest

from unifi_sentinel.config import ConfigError
from unifi_sentinel.diagnose import diagnose
from unifi_sentinel.settings import DiagnoseSettings, load_settings
from unifi_sentinel.snapshot import Snapshot

AP = {"mac": "aa:03", "type": "uap", "name": "Upper AP", "port_table": []}


def client(name="phone", **kw):
    base = {"mac": "cc:01", "name": name, "is_wired": False, "ap_mac": "aa:03", "radio": "na",
            "signal": -55, "satisfaction": 100, "wifi_tx_retries_percentage": 2.0,
            "wifi_tx_attempts": 50_000}
    base.update(kw)
    return base


def radio(**kw):
    base = {"radio": "ng", "channel": 6, "cu_total": 20, "tx_retries_pct": 5.0, "satisfaction": 99}
    base.update(kw)
    return base


def found(clients=(), radios=(), settings=None):
    ap = dict(AP, radio_table_stats=list(radios))
    snap = Snapshot(site={"id": "s"}, devices=[], clients=[], legacy_clients=list(clients),
                    legacy_devices=[ap])
    return {(f.severity, f.subject, f.message) for f in diagnose(snap, settings)}


def test_healthy_wifi_adds_nothing():
    assert found([client()], [radio(), radio(radio="na", channel=36)]) == set()


# -- client signal ---------------------------------------------------------

def test_weak_signal_uses_live_values_and_names_band_and_ap():
    got = found([client("far", signal=-81), client("edge", signal=-75, mac="cc:02"),
                 client("ok", signal=-73, mac="cc:03")])
    assert ("warning", "far", "weak Wi-Fi signal -81 dBm (5 GHz, on Upper AP)") in got
    assert any(s == "edge" for _, s, _ in got)          # "at or below" the threshold
    assert not any(s == "ok" for _, s, _ in got)
    assert any("-90 dBm" in m for _, _, m in found([client("x", signal=-90, radio="ng")]))
    assert any("2.4 GHz" in m for _, _, m in found([client("x", signal=-90, radio="ng")]))


def test_missing_signal_wired_clients_and_unknown_ap_are_handled():
    assert found([client(signal=None), client(signal="n/a", mac="cc:02"),
                  client(signal=0, mac="cc:03")]) == set()                  # no data is not weak
    assert found([client(is_wired=True, signal=-90)]) == set()              # wired clients skipped
    got = found([client(signal=-90, ap_mac="zz:99", radio=None)])
    assert ("warning", "phone", "weak Wi-Fi signal -90 dBm") in got         # no band/AP to name


def test_weak_signal_threshold_is_configurable():
    assert found([client(signal=-72)], settings=DiagnoseSettings(wifi_weak_signal_dbm=-70)) != set()
    assert found([client(signal=-85)], settings=DiagnoseSettings(wifi_weak_signal_dbm=-90)) == set()


# -- client retries --------------------------------------------------------

def test_retries_use_the_live_distribution():
    # real clients: 42.9% and 33.3% are flagged at the default 30%; 22.2% and 24.5% are not
    for pct, flagged in ((42.9, True), (33.3, True), (30.0, True), (24.5, False), (22.2, False)):
        got = found([client(wifi_tx_retries_percentage=pct, radio="ng")])
        assert bool(got) == flagged, pct
    assert ("warning", "phone", "43% of Wi-Fi transmissions retried (2.4 GHz, on Upper AP)") in found(
        [client(wifi_tx_retries_percentage=42.9, radio="ng")])


def test_retries_need_enough_attempts_and_are_configurable():
    assert found([client(wifi_tx_retries_percentage=90, wifi_tx_attempts=999)]) == set()
    assert found([client(wifi_tx_retries_percentage=90, wifi_tx_attempts=1000)]) != set()
    assert found([client(wifi_tx_retries_percentage=90, wifi_tx_attempts=50)],
                 settings=DiagnoseSettings(wifi_min_attempts=10)) != set()
    assert found([client(wifi_tx_retries_percentage=22, radio="ng")],
                 settings=DiagnoseSettings(wifi_retry_pct=20)) != set()
    assert found([client(wifi_tx_retries_percentage=None, wifi_tx_attempts=None)]) == set()


# -- satisfaction ----------------------------------------------------------

def test_low_satisfaction_flags_but_unknown_values_do_not():
    assert ("warning", "phone", "Wi-Fi satisfaction 40% (5 GHz, on Upper AP)") in found(
        [client(satisfaction=40)])
    assert found([client(satisfaction=50)]) == set()                         # below, not at
    assert found([client(satisfaction=None), client(satisfaction=-1, mac="cc:02")]) == set()
    assert found([client(satisfaction=0)]) != set()                          # 0 is a real score
    assert found([client(satisfaction=60)], settings=DiagnoseSettings(wifi_satisfaction_warn=70)) != set()


# -- AP radios -------------------------------------------------------------

def test_radio_channel_utilization_thresholds():
    assert found(radios=[radio(cu_total=41)]) == set()                      # busiest live radio
    assert ("warning", "Upper AP 2.4 GHz radio", "channel utilization 70% (channel 6)") in found(
        radios=[radio(cu_total=70)])
    assert ("critical", "Upper AP 5 GHz radio", "channel utilization 95% (channel 100)") in found(
        radios=[radio(radio="na", channel=100, cu_total=95)])
    tuned = DiagnoseSettings(radio_util_warn_pct=30, radio_util_critical_pct=40)
    assert any(s == "critical" for s, _, _ in found(radios=[radio(cu_total=41)], settings=tuned))


def test_radio_retries_and_satisfaction_with_unknown_values():
    assert ("warning", "Upper AP 2.4 GHz radio", "35% of transmissions retried (channel 1)") in found(
        radios=[radio(channel=1, tx_retries_pct=35)])
    assert found(radios=[radio(tx_retries_pct=21.4)]) == set()               # live radio, below 30
    assert found(radios=[radio(satisfaction=-1)]) == set()                  # -1 means unknown (live U7s)
    assert found(radios=[radio(satisfaction=None)]) == set()
    assert ("warning", "Upper AP 6 GHz radio", "satisfaction 30%") in found(
        radios=[radio(radio="6e", channel=None, satisfaction=30)])
    assert found(radios=[radio(cu_total=None, tx_retries_pct="x")]) == set()


def test_non_ap_devices_and_missing_radio_tables_are_ignored():
    snap = Snapshot(site={}, devices=[], clients=[], legacy_clients=[],
                    legacy_devices=[{"mac": "aa:01", "type": "usw", "name": "SW",
                                     "radio_table_stats": [radio(cu_total=99)]},
                                    {"mac": "aa:03", "type": "uap", "name": "AP2"}])
    assert [f for f in diagnose(snap) if "radio" in f.subject] == []


# -- settings --------------------------------------------------------------

def test_new_thresholds_load_default_and_validate(tmp_path):
    d = DiagnoseSettings()
    assert (d.wifi_weak_signal_dbm, d.wifi_retry_pct, d.wifi_min_attempts, d.wifi_satisfaction_warn,
            d.radio_util_warn_pct, d.radio_util_critical_pct) == (-75, 30, 1000, 50, 70, 90)
    cfg = tmp_path / "c.toml"
    cfg.write_text("[thresholds]\nwifi_weak_signal_dbm = -80\nwifi_retry_pct = 25\nwifi_min_attempts = 500\n"
                   "wifi_satisfaction_warn = 60\nradio_util_warn_pct = 50\nradio_util_critical_pct = 80\n")
    s = load_settings(cfg)
    assert (s.wifi_weak_signal_dbm, s.wifi_retry_pct, s.wifi_min_attempts, s.wifi_satisfaction_warn,
            s.radio_util_warn_pct, s.radio_util_critical_pct) == (-80, 25, 500, 60, 50, 80)
    for text, message in (("wifi_weak_signal_dbm = 5", "between -120 and 0"),
                          ("wifi_retry_pct = 120", "between 0 and 100"),
                          ("wifi_min_attempts = 0", "at least 1"),
                          ("wifi_min_attempts = 2.5", "must be a whole number"),
                          ("radio_util_warn_pct = 95\nradio_util_critical_pct = 80", "must not exceed"),
                          ('wifi_satisfaction_warn = "x"', "must be a number")):
        cfg.write_text("[thresholds]\n" + text + "\n")
        with pytest.raises(ConfigError, match=re.escape(message)):
            load_settings(cfg)
