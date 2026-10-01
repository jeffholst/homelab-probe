import json

import pytest

from unifi_sentinel import cli
from unifi_sentinel.client_view import (build_client_detail, candidate_rows, find_clients,
                                         known_clients, render_candidates, render_detail, to_json)
from unifi_sentinel.settings import DiagnoseSettings, IgnoreRule
from unifi_sentinel.snapshot import Snapshot, collect_snapshot


@pytest.fixture
def snap(fake_client):
    return collect_snapshot(fake_client, "default", include_reservations=True, include_groups=True)


def detail_for(snap, query, settings=None):
    (match,) = find_clients(snap, query)
    return build_client_detail(snap, match, settings)


# -- matching --------------------------------------------------------------

def test_match_by_name_mac_ip_in_any_format(snap):
    for query in ("desktop", "DESKTOP", "bb:00:00:00:00:01", "BB-00-00-00-00-01", "bb0000000001",
                  "bb.00.00.00.00.01", "10.0.0.10"):
        (match,) = find_clients(snap, query)
        assert match["name"] == "desktop", query


def test_substring_name_and_mac_fragment_matching(snap):
    assert [m["name"] for m in find_clients(snap, "print")] == ["old-printer"]
    assert find_clients(snap, "bb:00") == []                                   # fragment under 6 hex digits
    assert {m["name"] for m in find_clients(snap, "bb0000")} == {
        "desktop", "phone", "old-printer", "old-tablet"}                       # MAC fragment, ambiguous
    assert find_clients(snap, "nope") == []
    assert find_clients(snap, "  ") == []


def test_exact_matches_win_over_substrings():
    snap = Snapshot(site={}, devices=[], clients=[], all_users=[
        {"mac": "aa:01", "name": "nas"}, {"mac": "aa:02", "name": "nas-backup"},
        {"mac": "aa:03", "name": "nas2", "last_ip": "10.0.0.5"}, {"mac": "aa:04", "name": "x", "last_ip": "10.0.0.50"}])
    assert [m["mac"] for m in find_clients(snap, "nas")] == ["AA:01"]          # exact name beats substring
    assert [m["mac"] for m in find_clients(snap, "10.0.0.5")] == ["AA:03"]     # exact IP, not 10.0.0.50
    assert {m["mac"] for m in find_clients(snap, "na")} == {"AA:01", "AA:02", "AA:03"}  # ambiguous


def test_unifi_devices_are_not_clients(snap):
    assert find_clients(snap, "Office Switch") == []
    assert all(r["mac"] != "AA:00:00:00:00:02" for r in known_clients(snap))


def test_known_clients_merge_connected_and_historical_records(snap):
    by_name = {r["name"]: r for r in known_clients(snap)}
    desktop, phone, tablet = by_name["desktop"], by_name["phone"], by_name["old-tablet"]
    assert desktop["online"] and desktop["wired"] and desktop["live"] and desktop["user"]
    assert phone["online"] and not phone["wired"] and phone["user"] is None   # in the controller list only
    assert not tablet["online"] and tablet["ip"] == "10.0.0.51"


# -- the view --------------------------------------------------------------

def test_wired_online_client(snap):
    d = detail_for(snap, "desktop")
    assert d["identity"]["status"] == "Online" and d["identity"]["connection"] == "Wired"
    assert d["identity"]["connected_since"] != "" and d["identity"]["last_seen"] == "connected now"
    assert d["addressing"]["reservation"] == {"reserved_ip": "10.0.0.10", "network": "Main",
                                              "matches_current": True}
    assert d["addressing"]["groups"] == ["Desktops"] and not d["addressing"]["ungrouped"]
    hops = d["attachment"]
    assert (hops[0]["device"], hops[0]["port"]) == ("Office Switch", 3)
    assert (hops[1]["device"], hops[1]["port"]) == ("Gateway", 2)             # the switch's uplink
    assert d["link"]["kind"] == "wired" and d["link"]["speed_mbps"] == 1000 and d["link"]["full_duplex"]


def test_wireless_client_chain_and_quality(snap):
    snap.legacy_clients[1].update(radio="na", channel=36, essid="Home", signal=-62, noise=-95,
                                  tx_rate=866000, rx_rate=650000, satisfaction=97,
                                  wifi_tx_retries_percentage=4.0, wifi_tx_attempts=9000)
    d = detail_for(snap, "phone")
    assert d["identity"]["connection"] == "Wireless"
    assert d["attachment"][0]["device"] == "Office AP"
    assert d["attachment"][0]["detail"] == "5 GHz, channel 36, SSID Home"
    assert [h["device"] for h in d["attachment"][1:]] == ["Office Switch", "Gateway"]
    link = d["link"]
    assert (link["signal_dbm"], link["noise_dbm"], link["tx_rate_mbps"], link["rx_rate_mbps"]) == (-62, -95, 866, 650)
    assert (link["retries_pct"], link["satisfaction"], link["band"]) == (4, 97, "5 GHz")
    assert d["addressing"]["reservation"] is None and d["addressing"]["ungrouped"]


def test_offline_client_uses_its_last_uplink_and_has_no_link_quality(snap):
    d = detail_for(snap, "old-printer")
    assert d["identity"]["status"] == "Offline" and d["identity"]["last_seen"] != "connected now"
    assert d["identity"]["first_seen"] != "" and d["identity"]["connected_since"] == ""
    assert (d["attachment"][0]["device"], d["attachment"][0]["port"]) == ("Office Switch", 6)
    assert d["attachment"][0]["detail"] == "last seen here" and d["attachment"][1]["device"] == "Gateway"
    assert d["link"] is None
    res = d["addressing"]["reservation"]
    assert (res["reserved_ip"], res["network"], res["matches_current"]) == ("10.0.0.50", "IoT", None)
    assert d["addressing"]["vlan"] == 20 and d["addressing"]["ungrouped"]


def test_offline_wireless_client_without_an_uplink_has_no_attachment(snap):
    d = detail_for(snap, "old-tablet")
    assert d["attachment"] == [] and d["link"] is None
    assert "unknown (no uplink data)" in render_detail(d, emoji=False)


def test_reservation_that_differs_from_the_current_ip(snap):
    snap.clients[0]["ipAddress"] = "10.0.0.99"
    d = detail_for(snap, "desktop")
    assert d["addressing"]["reservation"]["matches_current"] is False
    assert "DIFFERS from the current IP" in render_detail(d, emoji=False)


def test_chain_marks_offline_devices_and_survives_loops():
    devices = [{"id": "a", "macAddress": "aa:01", "name": "A", "state": "OFFLINE"},
               {"id": "b", "macAddress": "aa:02", "name": "B", "state": "ONLINE"}]
    legacy = [{"mac": "aa:01", "type": "usw", "name": "A", "port_table": [{"port_idx": 1, "speed": 100}],
               "uplink": {"uplink_mac": "aa:02", "uplink_remote_port": 4, "speed": 100}},
              {"mac": "aa:02", "type": "usw", "name": "B",
               "uplink": {"uplink_mac": "aa:01", "uplink_remote_port": 9}}]      # a cycle
    snap = Snapshot(site={}, devices=devices, clients=[], legacy_devices=legacy,
                    legacy_clients=[{"mac": "cc:01", "name": "pc", "is_wired": True,
                                     "sw_mac": "aa:01", "sw_port": 1}],
                    all_users=[{"mac": "cc:01", "name": "pc"}])
    d = detail_for(snap, "pc")
    assert [h["device"] for h in d["attachment"]] == ["A", "B"]                # stops at the repeat
    assert d["attachment"][0]["offline"] and not d["attachment"][1]["offline"]
    assert "A port 1 (100 Mbps, OFFLINE) -> B port 4 (100 Mbps)" in render_detail(d, emoji=False)


# -- related findings ------------------------------------------------------

def test_related_findings_include_the_clients_dependencies_only(snap):
    snap.legacy_devices[1]["port_table"][1].update(rx_errors=4)               # port 2 errors: unrelated
    snap.legacy_devices[1]["port_table"][2].update(rx_errors=7)               # port 3: the client's port
    d = detail_for(snap, "desktop")
    got = {(f["subject"], f["message"]) for f in d["findings"]}
    assert ("Office Switch port 3", "7 rx/tx errors") in got
    assert not any(s == "Office Switch port 2" for s, _ in got)               # another port, not related
    assert ("Office Switch", "CPU utilization 95%") in got                     # a device on its path
    assert not any(s == "Garage AP" for s, _ in got)                          # offline AP, unrelated


def test_findings_naming_the_client_or_its_ip_are_related():
    snap = Snapshot(
        site={}, devices=[], clients=[
            {"macAddress": "cc:01", "name": "owner", "type": "WIRED", "ipAddress": "10.0.0.7"},
            {"macAddress": "cc:02", "name": "intruder", "type": "WIRED", "ipAddress": "10.0.0.7"}],
        all_users=[{"mac": "cc:01", "name": "owner", "use_fixedip": True, "fixed_ip": "10.0.0.7",
                    "last_connection_network_id": "n"}],
        legacy_devices=[{"mac": "aa:01", "type": "usw"}], networks=[{"_id": "n", "name": "Main"}])
    findings = {(f["subject"], f["message"]) for f in detail_for(snap, "intruder")["findings"]}
    assert any(s == "10.0.0.7" and "in use by" in m for s, m in findings)      # duplicate IP, by IP
    other = {(f["subject"], f["message"]) for f in detail_for(snap, "owner")["findings"]}
    assert any(s == "10.0.0.7" for s, _ in other)


def test_ignore_rules_apply_to_related_findings(snap):
    settings = DiagnoseSettings(ignore=(IgnoreRule(subject="Office Switch", message="CPU", reason="known"),))
    d = detail_for(snap, "desktop", settings)
    assert not any("CPU" in f["message"] for f in d["findings"])


# -- rendering and CLI -----------------------------------------------------

def test_text_rendering_has_the_issue_example_shape(snap):
    text = render_detail(detail_for(snap, "desktop"), emoji=False)
    assert "desktop -> Office Switch port 3 (1000 Mbps) -> Gateway port 2" in text
    assert "Groups:     Desktops" in text and "(reserved 10.0.0.10, matches)" in text
    assert "Network:    Main" in text
    assert "1000 Mbps, full duplex, 0 errors, 60 dropped packets on its port" in text   # fixture port 3
    assert "Gateway port 2 (100 Mbps)" in text                                          # fixture uplink speed
    assert "Related findings:" in text and "[WARNING ]" in text or "[INFO" in text
    ungrouped = render_detail(detail_for(snap, "old-tablet"), emoji=False)
    assert "none (not in any client group)" in ungrouped


def test_json_shape_and_candidates(snap):
    parsed = json.loads(to_json(detail_for(snap, "desktop")))
    assert set(parsed) == {"identity", "addressing", "attachment", "link", "findings"}
    assert parsed["identity"]["mac"] == "BB:00:00:00:00:01"
    rows = candidate_rows(find_clients(snap, "bb0000"))
    assert {r["Status"] for r in rows} == {"Online", "Offline"}
    assert "4 clients match" in render_candidates("bb0000", find_clients(snap, "bb0000"))
    assert "No client matches 'zzz'" in render_candidates("zzz", [])


def test_candidates_are_sorted_and_capped():
    users = [{"mac": f"aa:{i:02x}", "name": f"Speaker {i:02d}"} for i in range(30, 0, -1)]
    snap = Snapshot(site={}, devices=[], clients=[], all_users=users)
    matches = find_clients(snap, "speaker")
    assert [m["name"] for m in matches] == sorted(m["name"] for m in matches)    # by name
    text = render_candidates("speaker", matches)
    assert "30 clients match 'speaker'" in text and "... and 10 more" in text
    assert "Speaker 01" in text and "Speaker 21" not in text                    # first 20 only
    assert "... and" not in render_candidates("speaker", matches[:20])         # exactly 20: no marker


def _run(fake_client, monkeypatch, argv):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    return cli.main(argv)


def test_cli_found_ambiguous_and_missing(fake_client, monkeypatch, capsys):
    assert _run(fake_client, monkeypatch, ["client", "desktop", "--no-emoji"]) == 0
    out = capsys.readouterr().out
    assert "desktop -> Office Switch port 3" in out

    assert _run(fake_client, monkeypatch, ["client", "bb0000"]) == cli.EXIT_NO_MATCH == 4
    err = capsys.readouterr().err
    assert "4 clients match" in err and "old-printer" in err

    assert _run(fake_client, monkeypatch, ["client", "nonexistent"]) == 4
    assert "No client matches" in capsys.readouterr().err

    assert _run(fake_client, monkeypatch, ["client", "10.0.0.10", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["identity"]["name"] == "desktop"


def test_cli_client_uses_the_settings_file_and_reports_a_bad_one(fake_client, monkeypatch, capsys, tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[[ignore]]\nsubject = "Office Switch"\nmessage = "CPU"\nreason = "known"\n')
    assert _run(fake_client, monkeypatch, ["client", "desktop", "--no-emoji", "--config", str(cfg)]) == 0
    assert "CPU utilization" not in capsys.readouterr().out

    cfg.write_text("[thresholds]\nbogus = 1\n")
    assert _run(fake_client, monkeypatch, ["client", "desktop", "--config", str(cfg)]) == cli.EXIT_ERROR
    assert "unknown [thresholds] key" in capsys.readouterr().err
