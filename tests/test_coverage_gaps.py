"""Small branches that nothing else exercised: failure paths, odd values and rarely used inputs.

Each test names the behavior it pins, so a gap closed here is a documented decision, not a line
touched for the coverage number.
"""

import json
import runpy
import sys

import pytest

from unifi_sentinel import cli
from unifi_sentinel import wan as wan_module
from unifi_sentinel.client import UniFiAPIError
from unifi_sentinel.client_view import _link_text, build_client_detail, find_clients, known_clients, render_detail
from unifi_sentinel.config import ConfigError
from unifi_sentinel.diagnose import _event_findings
from unifi_sentinel.events import local_time
from unifi_sentinel.export import device_type_label
from unifi_sentinel.notify import Event, _priority, empty_state, plan
from unifi_sentinel.reservations import build_reservations
from unifi_sentinel.settings import DiagnoseSettings, load_settings
from unifi_sentinel.snapshot import collect_snapshot
from unifi_sentinel.util import format_time
from unifi_sentinel.wifi import span_mhz


def run(fake_client, monkeypatch, argv):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    return cli.main(argv)


# -- cli -------------------------------------------------------------------------------------------

@pytest.mark.filterwarnings("ignore:'unifi_sentinel.cli' found in sys.modules:RuntimeWarning")
def test_the_module_can_be_run_as_a_script(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["unifi-sentinel", "--version"])
    with pytest.raises(SystemExit) as caught:
        runpy.run_module("unifi_sentinel.cli", run_name="__main__")
    assert caught.value.code == 0 and capsys.readouterr().out.strip()


def test_a_snapshot_is_still_saved_when_the_controller_version_cannot_be_read(fake_client, monkeypatch, tmp_path):
    def broken_info():
        raise UniFiAPIError("info unavailable")

    monkeypatch.setattr(fake_client, "info", broken_info)
    folder = tmp_path / "snaps"
    assert run(fake_client, monkeypatch, ["snapshot", "--dir", str(folder)]) == 0
    (saved,) = list(folder.glob("snapshot-*.json"))
    assert json.loads(saved.read_text())["controller"] == {"application_version": ""} or \
        json.loads(saved.read_text())["controller"].get("application_version", "") == ""


def test_a_baseline_that_cannot_be_saved_is_a_config_error(fake_client, monkeypatch, capsys, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    monkeypatch.setenv("NOTIFY_NTFY_URL", "https://ntfy.example/topic")
    argv = ["diagnose", "--no-events", "--notify", "--notify-baseline", "--notify-state", str(blocker / "s.json")]
    assert run(fake_client, monkeypatch, argv) == cli.EXIT_ERROR
    assert "the notification baseline could not be saved" in capsys.readouterr().err


def test_a_quiet_run_whose_state_cannot_be_updated_is_a_config_error(fake_client, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("NOTIFY_NTFY_URL", "https://ntfy.example/topic")
    state = tmp_path / "state.json"
    base = ["diagnose", "--no-events", "--notify", "--notify-state", str(state)]
    assert run(fake_client, monkeypatch, [*base, "--notify-baseline"]) in (0, 1, 2)
    saved = json.loads(state.read_text())
    for key in saved["active"]:
        if key.startswith("device.offline|"):
            saved["active"][key]["severity"] = "critical"          # it improved since: nothing to say, but the level changes
    state.write_text(json.dumps(saved))

    def refuse(path, new_state):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(cli, "save_state", refuse)
    capsys.readouterr()
    assert run(fake_client, monkeypatch, base) == cli.EXIT_ERROR
    assert "the notification state could not be saved" in capsys.readouterr().err


# -- events, wan, export, reservations: odd values --------------------------------------------------

@pytest.mark.parametrize("value", [None, "soon", "", [], 10 ** 30, float("inf"), -(10 ** 30)])
def test_unreadable_timestamps_render_as_blank(value):
    assert local_time(value) == ""
    assert wan_module._when(value) == ""


def test_an_unparsable_connection_time_is_shown_as_it_came():
    assert format_time("not a date") == "not a date" and format_time(None) == "" and format_time("") == ""


def test_a_device_with_an_unknown_model_is_typed_by_its_features_then_its_legacy_type():
    assert device_type_label({"model": "ZZZ-NEW", "features": ["switching"]}, "usw") == "Switch"
    assert device_type_label({"model": "ZZZ-NEW", "features": ["nothing-known"]}, "zzz") == "ZZZ"
    assert device_type_label({"model": "ZZZ-NEW"}, "") == "Unknown"


def test_a_wired_client_without_a_switch_port_has_a_blank_port(fake_client):
    from unifi_sentinel.export import build_inventory
    snap = collect_snapshot(fake_client, "default")
    for sta in snap.legacy_clients:
        if sta.get("mac") == "bb:00:00:00:00:01":
            sta["sw_port"] = None
    rows = {r["Name"]: r for r in build_inventory(snap.devices, snap.clients, snap.legacy_devices,
                                                   snap.legacy_clients, snap.device_details, snap.device_stats)}
    assert rows["desktop"]["Port"] == "" and rows["desktop"]["Switch"] == "Office Switch"


def test_reservations_sort_a_value_that_is_not_an_ip_after_the_real_ones(fake_client):
    fx = fake_client.session.fx["legacy"]["alluser"]
    fx.append({"mac": "cc:00:00:00:00:01", "name": "typo", "use_fixedip": True, "fixed_ip": "not-an-ip",
               "last_connection_network_id": "net-1"})
    snap = collect_snapshot(fake_client, "default", include_reservations=True)
    ips = [r["Reserved IP"] for r in build_reservations(snap)]
    assert ips[-1] == "not-an-ip" and ips[:-1] == sorted(ips[:-1], key=lambda ip: tuple(map(int, ip.split("."))))


def test_a_reservation_with_no_last_seen_has_a_blank_last_seen(fake_client):
    fake_client.session.fx["legacy"]["alluser"].append(
        {"mac": "cc:00:00:00:00:02", "name": "never", "use_fixedip": True, "fixed_ip": "10.0.0.88",
         "last_connection_network_id": "net-1"})
    snap = collect_snapshot(fake_client, "default", include_reservations=True)
    assert {r["Name"]: r["Last Seen"] for r in build_reservations(snap)}["never"] == ""


# -- settings, snapshot, notify --------------------------------------------------------------------------

def test_thresholds_must_be_a_table(tmp_path):
    path = tmp_path / "s.toml"
    path.write_text("thresholds = 5\n")
    with pytest.raises(ConfigError, match=r"\[thresholds\] must be a table"):
        load_settings(path)


def test_a_failed_network_config_read_warns_and_carries_on(fake_client, monkeypatch, capsys):
    def broken(site_ref, resource):
        raise UniFiAPIError("rest unavailable")

    monkeypatch.setattr(fake_client, "legacy_rest", broken)
    snap = collect_snapshot(fake_client, "default", include_reservations=True)
    assert snap.networks == [] and "legacy rest/networkconf unavailable" in capsys.readouterr().err


def test_ntfy_priority_for_an_info_only_message(fake_client):
    from unifi_sentinel.diagnose import INFO, Finding
    events, _ = plan([Finding(INFO, "Office Switch port 2", "negotiated at 100 Mbps", code="port.slow_link")],
                     empty_state(), 0, min_severity=INFO)
    assert _priority(events) == ("3", "information_source")
    assert _priority([Event("recovered", "warning", "port.errors", "x")]) == ("3", "white_check_mark")


# -- diagnose: a device event that names only an id -----------------------------------------------------------

def test_an_unreachable_event_for_an_unknown_device_is_still_counted_by_its_id(fake_client):
    snap = collect_snapshot(fake_client, "default")
    snap.devices = []                                              # nothing to match the event against
    snap.event_window_seconds = 86400
    snap.events = [{"event": "DEVICE_UNREACHABLE", "timestamp": 1,
                    "parameters": {"DEVICE": {"id": "ghost-id", "name": "Ghost"}}}
                   for _ in range(10)]
    found = _event_findings(snap, DiagnoseSettings(event_flap_count=10))
    assert [(f.subject, f.code) for f in found] == [("Ghost", "event.device_unreachable")]


# -- wifi spans -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("args, expected", [
    (("ng", 6, 40, 0), None),                       # a zero centre frequency is not a measurement
    (("ng", 6, 40, -5), None),
    (("ng", 6, 40, None, 8), (2427.0, 2467.0)),     # the centre channel gives the centre
    (("ng", 6, 40, None, None, "above"), (2427.0, 2467.0)),
    (("ng", 6, 40, None, None, "below"), (2407.0, 2447.0)),
    (("ng", 6, 40, None, None, "none"), None),      # an extension of "none" says nothing about the centre
    (("ng", 6, 40), None),
    (("ng", 6, 20, 2437), (2426.0, 2448.0)),
])
def test_2_4_ghz_wide_channels_use_the_best_centre_information(args, expected):
    assert span_mhz(*args) == expected


# -- the client view -------------------------------------------------------------------------------------------------

def view(snap, query):
    return build_client_detail(snap, find_clients(snap, query)[0])


def test_wifi_link_text_lists_each_quality_measure_that_exists():
    link = {"kind": "wireless", "signal_dbm": -61.0, "noise_dbm": -95.0, "tx_rate_mbps": 866.0, "rx_rate_mbps": 650.0,
            "retries_pct": 12.0, "satisfaction": 87.0}
    assert _link_text(link) == ("signal -61 dBm, noise -95 dBm, 866/650 Mbps tx/rx, 12% retried, "
                                "satisfaction 87%")
    empty = {"kind": "wireless", "signal_dbm": None, "noise_dbm": None, "tx_rate_mbps": None, "rx_rate_mbps": None,
             "retries_pct": None, "satisfaction": None}
    assert _link_text(empty) == "no Wi-Fi quality data reported"
    wired = {"kind": "wired", "full_duplex": None, "speed_mbps": None, "rx_errors": 0, "tx_errors": 0,
             "rx_dropped": 0, "tx_dropped": 0}
    assert _link_text(wired) == "speed unknown, duplex unknown, 0 errors, 0 dropped packets on its port"


def test_group_ids_are_shown_unresolved_when_the_definitions_could_not_be_read(fake_client):
    snap = collect_snapshot(fake_client, "default", include_reservations=True, include_groups=True)
    assert "Groups:     Desktops" in render_detail(view(snap, "desktop"), emoji=False)
    snap.client_groups = None                                       # the group definitions were unavailable
    text = render_detail(view(snap, "desktop"), emoji=False)
    assert "Groups:     1 (names unavailable)" in text
    for user in snap.all_users:
        user["network_members_group_ids"] = ["deleted-group"]
    snap.client_groups = [{"id": "other", "name": "Other"}]          # defined, but not that one: it no longer exists
    assert "none (not in any client group)" in render_detail(view(snap, "desktop"), emoji=False)


def test_a_wireless_client_without_an_ap_mac_uses_the_integration_uplink(fake_client):
    for sta in fake_client.session.fx["legacy"]["sta"]:
        if sta["mac"] == "bb:00:00:00:00:02":
            sta.pop("ap_mac", None)
    snap = collect_snapshot(fake_client, "default", include_reservations=True)
    detail = view(snap, "phone")
    assert [hop["device"] for hop in detail["attachment"]][:1] == ["Office AP"]


def test_a_wireless_client_with_no_known_access_point_has_no_attachment(fake_client):
    for sta in fake_client.session.fx["legacy"]["sta"]:
        if sta["mac"] == "bb:00:00:00:00:02":
            sta.pop("ap_mac", None)
    for client in fake_client.session.fx["clients"]:
        if client["name"] == "phone":
            client["uplinkDeviceId"] = "nobody"
    snap = collect_snapshot(fake_client, "default", include_reservations=True)
    assert view(snap, "phone")["attachment"] == []


def test_a_wired_client_without_a_port_number_still_names_its_switch(fake_client):
    for sta in fake_client.session.fx["legacy"]["sta"]:
        if sta["mac"] == "bb:00:00:00:00:01":
            sta["sw_port"] = None
    snap = collect_snapshot(fake_client, "default", include_reservations=True)
    hops = view(snap, "desktop")["attachment"]
    assert hops[0]["device"] == "Office Switch" and hops[0]["port"] is None


def test_the_search_falls_through_when_a_mac_or_an_ip_matches_nobody(fake_client):
    snap = collect_snapshot(fake_client, "default", include_reservations=True)
    assert find_clients(snap, "aa:bb:cc:dd:ee:ff") == [] and find_clients(snap, "10.9.9.9") == []
    assert [r["name"] for r in find_clients(snap, "10.0.0.10")] == ["desktop"]


def test_records_without_a_mac_are_not_clients(fake_client):
    snap = collect_snapshot(fake_client, "default", include_reservations=True)
    snap.all_users = list(snap.all_users) + [{"name": "no-mac"}, {"mac": "", "name": "blank"}]
    assert {r["name"] for r in known_clients(snap)} >= {"desktop", "phone"}
    assert "no-mac" not in {r["name"] for r in known_clients(snap)}



# -- the last partial branches ---------------------------------------------------------------------------------

def test_an_unreachable_event_with_only_an_address_is_counted_by_that_address(fake_client):
    snap = collect_snapshot(fake_client, "default")
    snap.devices = []
    snap.event_window_seconds = 86400
    snap.events = [{"event": "DEVICE_UNREACHABLE", "timestamp": 1,
                    "parameters": {"DEVICE": {"ip": "10.9.9.9", "name": "Far Switch"}}} for _ in range(10)]
    found = _event_findings(snap, DiagnoseSettings(event_flap_count=10))
    assert [(f.subject, f.code) for f in found] == [("Far Switch", "event.device_unreachable")]


def test_a_milder_finding_listed_after_a_worse_one_with_the_same_identity_changes_nothing():
    from unifi_sentinel.diagnose import CRITICAL, WARNING, Finding
    found = [Finding(CRITICAL, "GW", "down", code="device.offline"), Finding(WARNING, "GW", "slow", code="device.offline")]
    events, state = plan(found, empty_state(), 0)
    assert [(e.severity, e.message) for e in events] == [(CRITICAL, "down")] and len(state["active"]) == 1


def test_device_details_skip_rows_that_are_not_devices_or_not_known(fake_client):
    from unifi_sentinel.query import _add_device_details
    snap = collect_snapshot(fake_client, "default")
    rows = [{"Type": "Client", "MAC Address": "BB:00:00:00:00:01"},
            {"Type": "Device - Switch", "MAC Address": "FF:FF:FF:FF:FF:01"}]
    _add_device_details(rows, snap)
    assert all("Firmware" not in r for r in rows)


def test_wireless_clients_without_an_access_point_are_not_counted_on_one(fake_client):
    from unifi_sentinel.topology import build_topology
    snap = collect_snapshot(fake_client, "default")

    def ap_counts(tree):
        nodes, stack = [], list(tree["roots"])
        while stack:
            node = stack.pop()
            nodes.append(node)
            stack.extend(node["children"])
        return {n["name"]: n["clients"] for n in nodes if n["clients"] is not None}

    before = ap_counts(build_topology(snap))
    snap.legacy_clients = list(snap.legacy_clients) + [{"mac": "cc:00:00:00:00:09", "is_wired": False}]
    assert ap_counts(build_topology(snap)) == before


def test_a_link_with_a_speed_but_no_known_capability_shows_only_the_speed(fake_client):
    from unifi_sentinel.topology import build_topology, render_text
    tree = build_topology(collect_snapshot(fake_client, "default"))

    def visit(nodes):
        for n in nodes:
            if n["speed_mbps"]:
                n["supports_mbps"] = None
            visit(n["children"])

    visit(tree["roots"])
    text = render_text(tree, emoji=False)
    assert "(100 Mbps)" in text and "supports" not in text


def test_speedtest_statistics_that_are_missing_are_left_out(fake_client):
    from unifi_sentinel.wan import build_wan, render_text
    for test in fake_client.session.fx["legacy_v2"]["speedtest"]["data"]:
        test.pop("latency_ms", None)
    snap = collect_snapshot(fake_client, "default", include_health=True, include_speedtests=True)
    text = render_text(build_wan(snap))
    assert "  Download: min" in text and "  Upload: min" in text and "  Latency: min" not in text


def test_an_uplink_known_only_by_its_own_port_names_that_port(fake_client):
    from unifi_sentinel.client_view import DeviceIndex, _uplink_chain
    snap = collect_snapshot(fake_client, "default")
    for device in snap.legacy_devices:
        if device["mac"] == "aa:00:00:00:00:03":
            device["uplink"] = {"uplink_mac": "aa:00:00:00:00:02", "port_idx": 1}      # no remote port number
    subjects: set = set()
    hops = _uplink_chain(DeviceIndex(snap), "AA:00:00:00:00:03", subjects)
    assert hops[0]["device"] == "Office Switch" and "Office AP port 1" in subjects
    assert not any("port None" in s for s in subjects) and "Office Switch" in subjects
