import re

import pytest

from unifi_sentinel.config import ConfigError
from unifi_sentinel.diagnose import diagnose
from unifi_sentinel.settings import DiagnoseSettings, load_settings
from unifi_sentinel.snapshot import Snapshot


def port(idx, **kw):
    base = {"port_idx": idx, "up": True, "speed": 1000, "full_duplex": True}
    base.update(kw)
    return base


def switch(*ports, **kw):
    return {"mac": "aa:01", "type": "usw", "name": "SW", "uptime": 10 * 86400,
            "port_table": list(ports), **kw}


def found(*legacy_devices, devices=(), details=None, settings=None):
    snap = Snapshot(site={"id": "s"}, devices=list(devices), clients=[],
                    legacy_devices=list(legacy_devices), device_details=details or {})
    return {(f.severity, f.subject, f.message) for f in diagnose(snap, settings)}


def test_healthy_ports_add_nothing():
    got = found(switch(port(1, link_down_count=1, rx_packets=10**6, tx_packets=10**6,
                            rx_dropped=0, tx_dropped=0, stp_state="forwarding"),
                       port(2, up=False, stp_state="disabled"),
                       total_max_power=52, total_used_power=8))
    assert got == set()


# -- flapping links --------------------------------------------------------

def test_link_flaps_at_or_above_the_threshold_include_switch_uptime():
    got = found(switch(port(1, link_down_count=4), port(2, link_down_count=5),
                       port(3, up=False, link_down_count=9)))
    assert ("warning", "SW port 2", "link has gone down 5 times since boot, switch up 10d 0h") in got
    assert ("warning", "SW port 3", "link has gone down 9 times since boot, switch up 10d 0h") in got
    assert not any(s == "SW port 1" for _, s, _ in got)                 # 4 is below the default 5


def test_link_flap_threshold_is_configurable_and_tolerates_bad_values():
    assert any("gone down 3 times" in m for _, _, m in
               found(switch(port(1, link_down_count=3)), settings=DiagnoseSettings(link_flap_count=3)))
    assert found(switch(port(1, link_down_count="many"), port(2, link_down_count=None))) == set()
    no_uptime = found(switch(port(1, link_down_count=5), uptime=None))
    assert ("warning", "SW port 1", "link has gone down 5 times since boot") in no_uptime


# -- dropped packets (real counters from a live controller) ----------------

def test_drops_are_judged_as_a_percentage_of_packets():
    busy_port = port(3, tx_packets=146_040_985, tx_dropped=3633, rx_packets=25_075_151, rx_dropped=0)
    bad_link = port(2, rx_packets=18_091, rx_dropped=133, tx_packets=6_927_428, tx_dropped=0)
    assert found(switch(busy_port)) == set()                             # 0.0025%: not a problem
    got = found(switch(bad_link))
    assert ("warning", "SW port 2", "dropping 0.74% of rx packets (133 of 18091)") in got  # 0.735%


def test_tiny_drop_rates_stay_readable():
    busy_port = port(3, tx_packets=146_040_985, tx_dropped=3633)
    got = found(switch(busy_port), settings=DiagnoseSettings(port_drop_pct=0.001))
    assert ("warning", "SW port 3", "dropping 0.0025% of tx packets (3633 of 146040985)") in got


def test_drop_threshold_minimum_sample_and_down_ports():
    tiny = port(1, rx_packets=500, rx_dropped=400)                       # too few packets to judge
    assert found(switch(tiny)) == set()
    stale = port(2, up=False, rx_packets=10**6, rx_dropped=10**5)        # down ports are ignored
    assert found(switch(stale)) == set()
    one_pct = port(3, tx_packets=10_000, tx_dropped=100)
    assert found(switch(one_pct), settings=DiagnoseSettings(port_drop_pct=5)) == set()
    assert len(found(switch(one_pct), settings=DiagnoseSettings(port_drop_pct=1))) == 1  # at or above


# -- STP -------------------------------------------------------------------

def test_up_port_not_forwarding_is_flagged_but_down_and_unknown_are_not():
    got = found(switch(port(1, stp_state="blocking"), port(2, stp_state="forwarding"),
                       port(3, stp_state=None), port(4, up=False, stp_state="disabled")))
    assert got == {("warning", "SW port 1", "STP state is blocking, not forwarding")}


# -- PoE budget ------------------------------------------------------------

def test_poe_budget_thresholds_and_missing_budgets():
    assert found(switch(total_max_power=52, total_used_power=41.5)) == set()          # 79.8%
    assert ("warning", "SW", "PoE budget 41.6 W of 52 W used (80%)") in found(
        switch(total_max_power=52, total_used_power=41.6))
    assert ("critical", "SW", "PoE budget 50.0 W of 52 W used (96%)") in found(
        switch(total_max_power=52, total_used_power=50))
    assert found(switch(total_max_power=None, total_used_power=None)) == set()        # no PoE
    assert found(switch(total_max_power=0, total_used_power=5)) == set()              # no divide by zero
    tuned = DiagnoseSettings(poe_warn_pct=10, poe_critical_pct=20)
    assert any(s == "critical" for s, _, _ in found(switch(total_max_power=50, total_used_power=11), settings=tuned))


def test_poe_good_is_not_used_so_unpowered_ports_never_alarm():
    # live data: PoE-capable ports with no PoE device report poe_good False
    assert found(switch(port(1, port_poe=True, poe_enable=True, poe_good=False, poe_power="0.00"))) == set()


# -- uplink speed bottleneck -----------------------------------------------

def link(speed, child_max, parent_max, up=True, parent_port=3):
    parent = {"id": "gw", "macAddress": "aa:00", "name": "Gateway", "state": "ONLINE"}
    child = {"mac": "aa:01", "type": "usw", "name": "SW", "port_table": [],
             "uplink": {"uplink_mac": "aa:00", "uplink_remote_port": parent_port, "up": up,
                        "speed": speed, "max_speed": child_max}}
    details = {"gw": {"interfaces": {"ports": [{"idx": parent_port, "maxSpeedMbps": parent_max}]}}}
    return found(child, {"mac": "aa:00", "type": "udm", "name": "Gateway"},
                 devices=[parent], details=details)


def test_uplink_below_what_both_ends_support_is_flagged():
    assert ("warning", "SW", "uplink to Gateway negotiated at 100 Mbps but both ends support 1000 Mbps") \
        in link(100, 1000, 2500)
    # parent port is the limit (1000) even though the child could do 2500
    assert any("both ends support 1000 Mbps" in m for _, _, m in link(100, 2500, 1000))


def test_uplink_at_capability_or_with_unknowns_is_not_flagged():
    assert link(1000, 1000, 2500) == set()      # gigabit switch on a 2.5G port: at its own maximum
    assert link(2500, 2500, 2500) == set()
    assert link(1000, 1000, 2500, up=False) == set()
    assert link(None, 1000, 2500) == set()      # offline device: no negotiated speed
    assert link(100, 0, 0) == set()             # unknown capability: nothing to compare against


def test_uplink_without_parent_detail_uses_the_childs_maximum():
    child = {"mac": "aa:01", "type": "usw", "name": "SW", "port_table": [],
             "uplink": {"uplink_mac": "zz:99", "up": True, "speed": 100, "max_speed": 1000}}
    assert ("warning", "SW", "uplink to ZZ:99 negotiated at 100 Mbps but both ends support 1000 Mbps") \
        in found(child)


# -- settings --------------------------------------------------------------

def test_new_thresholds_load_default_and_validate(tmp_path):
    d = DiagnoseSettings()
    assert (d.link_flap_count, d.port_drop_pct, d.poe_warn_pct, d.poe_critical_pct) == (5, 0.1, 80, 95)
    cfg = tmp_path / "c.toml"
    cfg.write_text("[thresholds]\nlink_flap_count = 2\nport_drop_pct = 0.5\npoe_warn_pct = 60\npoe_critical_pct = 90\n")
    s = load_settings(cfg)
    assert (s.link_flap_count, s.port_drop_pct, s.poe_warn_pct, s.poe_critical_pct) == (2, 0.5, 60, 90)
    for text, message in (("link_flap_count = 0", "at least 1"),
                          ("port_drop_pct = 101", "between 0 and 100"),
                          ("poe_warn_pct = 99\npoe_critical_pct = 50", "poe_warn_pct must not exceed"),
                          ('link_flap_count = "x"', "must be a number")):
        cfg.write_text("[thresholds]\n" + text + "\n")
        with pytest.raises(ConfigError, match=re.escape(message)):
            load_settings(cfg)
