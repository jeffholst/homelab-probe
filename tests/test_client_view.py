import json

import pytest

from unifi_sentinel import cli
from unifi_sentinel.client import UniFiAPIError
from unifi_sentinel.client_view import (
    MAX_CLIENT_EVENTS,
    MAX_DEVICE_EVENTS,
    _related,
    build_client_detail,
    candidate_rows,
    find_clients,
    known_clients,
    render_candidates,
    render_detail,
    to_json,
)
from unifi_sentinel.diagnose import Finding
from unifi_sentinel.settings import DiagnoseSettings, IgnoreRule
from unifi_sentinel.snapshot import EventQuery, Needs, Snapshot, collect_snapshot


@pytest.fixture
def snap(fake_client):
    return collect_snapshot(fake_client, "default", Needs(reservations=True, groups=True))


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


def test_negative_wireless_satisfaction_is_unknown(snap):
    snap.legacy_clients[1]["satisfaction"] = -1
    d = detail_for(snap, "phone")
    assert d["link"]["satisfaction"] is None
    assert "satisfaction" not in render_detail(d, emoji=False)


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


def test_related_findings_match_client_aliases_and_mac_case():
    rec = {"name": "integration-name", "names": ["integration-name", "legacy-name", "legacy-host"],
           "mac": "CC:01", "ips": set()}
    findings = [
        Finding("warning", "legacy-name", "legacy client finding"),
        Finding("warning", "cc:01", "MAC finding"),
        Finding("warning", "other", "finding mentions legacy-host"),
        Finding("warning", "other", "unrelated finding"),
    ]
    related = _related(findings, rec, set())
    assert related == findings[:3]


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
    assert set(parsed) == {"identity", "addressing", "attachment", "link", "findings", "events_available",
                           "events_window", "events_truncated", "events", "device_events", "events_omitted"}
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


# -- recent events -----------------------------------------------------------

def ev_snap(fake_client, since=86400):
    return collect_snapshot(fake_client, "default", Needs(reservations=True, groups=True, events=EventQuery(since)))


def test_a_clients_recent_events_are_listed_newest_first_with_the_snapshot_window(fake_client):
    snap = ev_snap(fake_client)
    d = detail_for(snap, "phone")
    assert d["events_available"] is True and d["events_window"] == "24h"
    assert [e["Event"] for e in d["events"]] == [
        "CLIENT_DISCONNECTED_WIRELESS", "CLIENT_CONNECTED_WIRELESS", "CLIENT_DISCONNECTED_WIRELESS",
        "CLIENT_ROAMED", "CLIENT_DISCONNECTED_WIRELESS"]
    assert d["events"][0]["Message"] == "phone disconnected from Home. Time Connected: 25s."
    assert d["events_omitted"] == {"client": 0, "devices": 0}
    assert [e["Event"] for e in detail_for(snap, "desktop")["events"]] == ["CLIENT_CONNECTED_WIRED"]
    text = render_detail(d, emoji=False)
    assert "Recent events (last 24h, newest first):" in text
    assert "CLIENT_ROAMED: phone roamed from Garage AP to Office AP." in text
    assert text.index("Recent events") < text.index("Related findings")


def test_a_client_with_no_events_says_so_and_a_longer_window_finds_older_ones(fake_client):
    tablet = detail_for(ev_snap(fake_client), "old-tablet")
    assert tablet["events"] == [] and tablet["events_available"] is True
    assert "none about this client in the last 24h" in render_detail(tablet, emoji=False)
    wide = detail_for(ev_snap(fake_client, 14 * 86400), "desktop")           # the 10-day-old roam appears
    assert [e["Event"] for e in wide["events"]] == ["CLIENT_CONNECTED_WIRED", "CLIENT_ROAMED"]
    assert wide["events_window"] == "14d" and "last 14d" in render_detail(wide, emoji=False)


def _clients(*clients):
    return [{"mac": mac, "name": name, "is_wired": False, "ap_mac": "AA:03"} for mac, name in clients]


def ev(kind, ts, category="CLIENT_DEVICES", **params):
    return {"event": kind, "key": kind + "_2", "timestamp": ts, "id": f"{kind}{ts}",
            "category": category, "parameters": params}


def snap_with(events, window=86400, available=True, extra_clients=(), truncated=False):
    users = [{"mac": "cc:01", "name": "pc"}, *extra_clients]
    return Snapshot(
        site={}, all_users=users, clients=[],
        devices=[{"id": "ap", "macAddress": "aa:03", "name": "Office AP", "state": "ONLINE"},
                 {"id": "duplicate-ap", "macAddress": "aa:04", "name": "Office AP", "state": "ONLINE"},
                 {"id": "sw", "macAddress": "aa:02", "name": "Other AP", "state": "ONLINE"}],
        legacy_devices=[{"mac": "AA:03", "type": "uap", "name": "Office AP", "port_table": []}],
        legacy_clients=[{"mac": "cc:01", "name": "pc", "is_wired": False, "ap_mac": "aa:03"}],
        events=events, event_window_seconds=window, events_available=available, events_truncated=truncated)


def test_events_are_matched_by_mac_not_by_a_similar_name():
    mine = {"id": "cc:01", "name": "pc"}
    other = {"id": "cc:99", "name": "pc"}                                       # same name, another device
    similar = {"id": "cc:98", "name": "pc-backup"}
    events = [ev("CLIENT_DISCONNECTED_WIRELESS", 3000, CLIENT=mine),
              ev("CLIENT_DISCONNECTED_WIRELESS", 2000, CLIENT=other),
              ev("CLIENT_DISCONNECTED_WIRELESS", 1000, CLIENT=similar)]
    d = detail_for(snap_with(events), "pc")
    assert [e["timestamp"] for e in d["events"]] == [3000]


def test_the_client_list_is_capped_and_says_how_to_see_the_rest():
    mine = {"id": "cc:01", "name": "pc"}
    events = [ev("CLIENT_CONNECTED_WIRELESS", 10_000 - i, CLIENT=mine) for i in range(MAX_CLIENT_EVENTS + 3)]
    d = detail_for(snap_with(events, window=7 * 86400), "pc")
    assert len(d["events"]) == MAX_CLIENT_EVENTS and d["events_omitted"]["client"] == 3
    text = render_detail(d, emoji=False)
    assert "... and 3 more; run: unifi-sentinel events --client CC:01 --since 7d" in text
    assert [e["timestamp"] for e in d["events"]] == sorted((e["timestamp"] for e in d["events"]), reverse=True)


def test_events_about_the_devices_it_depends_on_are_separate_and_exact():
    mine = {"id": "cc:01", "name": "pc"}
    events = [
        ev("DEVICE_UNREACHABLE", 9000, "UNIFI_DEVICES", DEVICE={"name": "Office AP"}),   # the AP itself: shown
        ev("DEVICE_UNREACHABLE", 8000, "UNIFI_DEVICES", DEVICE={"name": "office ap"}),   # name case is ignored
        ev("DEVICE_UNREACHABLE", 7500, "UNIFI_DEVICES",
           DEVICE={"id": "ap", "name": "Office AP"}),                                  # stable ID matches
        ev("DEVICE_UNREACHABLE", 7400, "UNIFI_DEVICES",
           DEVICE={"id": "duplicate-ap", "name": "Office AP"}),                       # same name, other device
        ev("ISP_HIGH_LATENCY", 7300, "INTERNET_AND_WAN", DEVICE={"name": "Office AP"}),  # internet, not why a client dropped
        ev("CLIENT_CONNECTED_WIRELESS", 7000, CLIENT={"id": "cc:77", "name": "tv"},
           DEVICE={"name": "Office AP"}),                                        # someone else on the AP: not shown
        ev("CLIENT_ROAMED", 6000, CLIENT=mine, DEVICE_FROM={"name": "Other AP"},
           DEVICE_TO={"name": "Office AP"}),                                     # the client's own event
        ev("DEVICE_UNREACHABLE", 5000, "UNIFI_DEVICES", DEVICE={"name": "Other AP"}),    # not on its path
        ev("DEVICE_UNREACHABLE", 4000, "UNIFI_DEVICES", DEVICE={"name": "Office AP 2"}), # similar name: not shown
        ev("MADE_CHANGES", 3000, "AUDIT", OBJECT={"name": "Office AP"}),                 # an admin change: not shown
    ]
    d = detail_for(snap_with(events), "pc")
    assert [e["timestamp"] for e in d["device_events"]] == [9000, 8000, 7500]
    assert [e["timestamp"] for e in d["events"]] == [6000]
    text = render_detail(d, emoji=False)
    assert "Events about the devices it depends on:" in text
    assert text.index("Recent events") < text.index("Events about the devices")
    section = text.split("Events about the devices it depends on:")[1].split("Related findings")[0]
    assert section.count("DEVICE_UNREACHABLE") == 3                                  # matching ID and name fallback only
    assert "CLIENT_CONNECTED_WIRELESS" not in section and "CLIENT_ROAMED" not in section
    assert "ISP_HIGH_LATENCY" not in section and "MADE_CHANGES" not in section


def test_device_events_are_capped():
    events = [ev("DEVICE_UNREACHABLE", 9000 - i, "UNIFI_DEVICES", DEVICE={"name": "Office AP"})
              for i in range(MAX_DEVICE_EVENTS + 2)]
    d = detail_for(snap_with(events), "pc")
    assert len(d["device_events"]) == MAX_DEVICE_EVENTS and d["events_omitted"]["devices"] == 2
    assert "... and 2 more" in render_detail(d, emoji=False)


def test_truncated_event_log_marks_omission_counts_incomplete():
    mine = {"id": "cc:01", "name": "pc"}
    events = [ev("CLIENT_CONNECTED_WIRELESS", 10_000 - i, CLIENT=mine)
              for i in range(MAX_CLIENT_EVENTS + 2)]
    detail = detail_for(snap_with(events, truncated=True), "pc")

    assert detail["events_truncated"] is True
    assert detail["events_omitted"]["client"] == 2
    assert json.loads(to_json(detail))["events_truncated"] is True
    text = render_detail(detail, emoji=False)
    assert "... and at least 2 more" in text
    assert "omission counts are incomplete" in text

    empty = detail_for(snap_with([], truncated=True), "pc")
    text = render_detail(empty, emoji=False)
    assert "no matching events found before the 20,000-event read cap" in text
    assert "none about this client" not in text


def test_skipped_and_unavailable_events_are_different():
    skipped = detail_for(snap_with([], window=0, available=False), "pc")        # --no-events
    assert skipped["events_available"] is None and skipped["events_truncated"] is None
    assert skipped["events"] == [] and skipped["events_window"] == ""
    text = render_detail(skipped, emoji=False)
    assert "Recent events" not in text and "unavailable" not in text

    unreadable = detail_for(snap_with([], window=86400, available=False), "pc")  # the log could not be read
    assert unreadable["events_available"] is False and unreadable["events_truncated"] is None
    text = render_detail(unreadable, emoji=False)
    assert "Recent events: unavailable (the event log could not be read)" in text
    assert "Attached:" in text                                                       # the rest of the view remains
    assert "Related findings:" in text or "No related findings." in text


def test_the_snapshot_says_whether_the_event_log_was_read(fake_client, monkeypatch, capsys):
    assert collect_snapshot(fake_client, "default").events_available is False       # not requested
    assert collect_snapshot(fake_client, "default").event_window_seconds == 0
    snap = ev_snap(fake_client)
    assert snap.events_available is True and snap.event_window_seconds == 86400

    def boom(site_ref, query):
        raise UniFiAPIError("log down")

    monkeypatch.setattr(fake_client, "system_log", boom)
    snap = ev_snap(fake_client)
    assert snap.events_available is False and snap.events == [] and snap.event_window_seconds == 86400
    assert "event log unavailable" in capsys.readouterr().err


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


def test_cli_client_shows_recent_events_by_default_and_honours_the_flags(fake_client, monkeypatch, capsys):
    assert _run(fake_client, monkeypatch, ["client", "phone", "--no-emoji"]) == 0
    out = capsys.readouterr().out
    assert "Recent events (last 24h, newest first):" in out and "CLIENT_ROAMED" in out
    assert fake_client.session.posts and all(
        path.endswith("/system-log/all") for path, _ in fake_client.session.posts)    # the one approved POST

    fake_client.session.posts.clear()
    assert _run(fake_client, monkeypatch, ["client", "phone", "--no-emoji", "--no-events"]) == 0
    out = capsys.readouterr().out
    assert "Recent events" not in out and fake_client.session.posts == []             # nothing is sent

    assert _run(fake_client, monkeypatch, ["client", "phone", "--no-emoji", "--since", "30m"]) == 0
    out = capsys.readouterr().out
    assert "Recent events (last 30m" in out and out.count("CLIENT_DISCONNECTED_WIRELESS") == 1
    body = fake_client.session.posts[-1][1]
    assert body["timestampTo"] - body["timestampFrom"] == 30 * 60 * 1000
    with pytest.raises(SystemExit) as exc:
        _run(fake_client, monkeypatch, ["client", "phone", "--since", "soon"])
    assert exc.value.code == cli.EXIT_USAGE


def test_cli_client_json_has_the_events(fake_client, monkeypatch, capsys):
    assert _run(fake_client, monkeypatch, ["client", "phone", "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["events_available"] is True and parsed["events_window"] == "24h"
    assert len(parsed["events"]) == 5 and parsed["events"][0]["Event"] == "CLIENT_DISCONNECTED_WIRELESS"
    assert {"Time", "Severity", "Category", "Event", "Message", "timestamp", "key", "client", "device"} <= set(parsed["events"][0])
    assert parsed["events"][0]["client"]["name"] == "phone"

    assert _run(fake_client, monkeypatch, ["client", "phone", "--json", "--no-events"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["events_available"] is None and parsed["events"] == [] and parsed["device_events"] == []


def test_cli_client_carries_on_when_the_event_log_is_unreadable(fake_client, monkeypatch, capsys):
    def boom(site_ref, query):
        raise UniFiAPIError("log down")

    monkeypatch.setattr(fake_client, "system_log", boom)
    assert _run(fake_client, monkeypatch, ["client", "phone", "--no-emoji"]) == 0
    captured = capsys.readouterr()
    assert "event log unavailable; event history was skipped: log down" in captured.err
    assert "Recent events: unavailable (the event log could not be read)" in captured.out
    assert "Attached: phone -> Office AP" in captured.out                          # the rest of the view is intact

    assert _run(fake_client, monkeypatch, ["client", "phone", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["events_available"] is False
