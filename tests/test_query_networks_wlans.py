"""`query networks`, `query wlans` and the `query clients` filters --network, --ssid and --ap (issue #138)."""

import csv
import io
import json

import pytest

from unifi_sentinel import cli
from unifi_sentinel.query import (
    NETWORK_COLUMNS,
    WLAN_COLUMNS,
    filter_clients,
    network_rows,
    network_vlan,
    query_rows,
    security_label,
    wlan_rows,
)
from unifi_sentinel.snapshot import Needs, Snapshot, collect_snapshot

SECRET = "pa55-never-print-this-passphrase"


def run(fake_client, monkeypatch, capsys, *argv):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def usage_error(monkeypatch, capsys, *argv):
    """(exit code, stderr) of a command line the parser refuses."""
    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("API_KEY", "key")
    with pytest.raises(SystemExit) as stopped:
        cli.main(list(argv))
    return stopped.value.code, capsys.readouterr().err


def run_json(fake_client, monkeypatch, capsys, *argv):
    code, out, err = run(fake_client, monkeypatch, capsys, *argv, "--json")
    return code, json.loads(out) if out.strip() else None, err


def snapshot(**fields):
    base = {"site": {"id": "site-1"}, "devices": [], "clients": []}
    return Snapshot(**{**base, **fields})


def by_name(rows):
    return {row["Name"]: row for row in rows}


def fail(fake_client, *suffixes):
    get = fake_client.session.get

    def request(url, *args, **kwargs):
        if any(url.endswith(suffix) for suffix in suffixes):
            from conftest import FakeResponse
            return FakeResponse(503, {})
        return get(url, *args, **kwargs)

    fake_client.session.get = request


# -- networks --------------------------------------------------------------------------------------------------

def test_the_fixture_networks_as_rows(fake_client):
    snap = collect_snapshot(fake_client, "default", Needs(networks=True))
    rows = by_name(network_rows(snap))
    assert list(rows) == ["Main", "IoT", "Internet 1", "Remote Access"]          # the controller's order
    assert rows["Main"] == {"Name": "Main", "Purpose": "corporate", "VLAN": 1, "Subnet": "10.0.0.0/24",
                            "Gateway": "10.0.0.1", "DHCP": "Server", "DHCP Range": "10.0.0.100 - 10.0.0.200",
                            "Clients": 1}
    assert rows["IoT"]["VLAN"] == 20 and rows["IoT"]["Clients"] == 0
    assert rows["Internet 1"] == {"Name": "Internet 1", "Purpose": "wan", "VLAN": "", "Subnet": "", "Gateway": "",
                                  "DHCP": "", "DHCP Range": "", "Clients": 0}
    assert rows["Remote Access"]["Subnet"] == "10.0.99.0/24" and rows["Remote Access"]["DHCP"] == ""
    assert all(list(row) == NETWORK_COLUMNS for row in rows.values())


@pytest.mark.parametrize("net, vlan", [
    ({"vlan_enabled": True, "vlan": 30}, 30),
    ({"vlan_enabled": False}, 1),
    ({"vlan_enabled": False, "vlan": 30}, 1),               # a stale tag on a network with VLANs off is not shown
    ({}, ""), ({"vlan": 30}, ""),                            # WAN and VPN networks do not say
    ({"vlan_enabled": True}, ""), ({"vlan_enabled": True, "vlan": "20"}, ""), ({"vlan_enabled": True, "vlan": True}, ""),
    ({"vlan_enabled": "yes", "vlan": 5}, ""),
])
def test_the_vlan_of_a_network(net, vlan):
    assert network_vlan(net) == vlan


@pytest.mark.parametrize("net, expected", [
    ({"ip_subnet": "192.168.5.1/24"}, ("192.168.5.0/24", "192.168.5.1")),
    ({"ip_subnet": " 192.168.5.77/28 "}, ("192.168.5.64/28", "192.168.5.77")),         # the address keeps its host bits
    ({"ip_subnet": "fd00:1::1/64"}, ("fd00:1::/64", "fd00:1::1")),
    ({"ip_subnet": "not an address"}, ("not an address", "")),
    ({"ip_subnet": "300.1.1.1/24"}, ("300.1.1.1/24", "")),
    ({"ip_subnet": ""}, ("", "")), ({"ip_subnet": None}, ("", "")), ({}, ("", "")),
])
def test_the_subnet_and_gateway_of_a_network(net, expected):
    row = network_rows(snapshot(networks=[{"_id": "n", "name": "x", **net}]))[0]
    assert (row["Subnet"], row["Gateway"]) == expected


@pytest.mark.parametrize("net, expected", [
    ({"dhcpd_enabled": True, "dhcpd_start": "10.0.0.2", "dhcpd_stop": "10.0.0.9"}, ("Server", "10.0.0.2 - 10.0.0.9")),
    ({"dhcpd_enabled": True, "dhcp_relay_enabled": True, "dhcpd_start": "10.0.0.2", "dhcpd_stop": "10.0.0.9"},
     ("Relay", "")),
    ({"dhcp_relay_enabled": True}, ("Relay", "")),
    ({"dhcpd_enabled": False, "dhcpd_start": "10.0.0.2", "dhcpd_stop": "10.0.0.9"}, ("Off", "")),
    ({"dhcpd_enabled": True}, ("Server", "")),                                          # on, but no usable range
    ({"dhcpd_enabled": True, "dhcpd_start": "10.0.0.9", "dhcpd_stop": "10.0.0.2"}, ("Server", "")),   # reversed
    ({"dhcpd_enabled": True, "dhcpd_start": "x", "dhcpd_stop": "y"}, ("Server", "")),
    ({"dhcpd_start": "10.0.0.2", "dhcpd_stop": "10.0.0.9"}, ("", "")),                  # a VPN's range is not DHCP
    ({}, ("", "")),
])
def test_who_serves_dhcp_on_a_network(net, expected):
    row = network_rows(snapshot(networks=[{"_id": "n", "name": "x", **net}]))[0]
    assert (row["DHCP"], row["DHCP Range"]) == expected


def test_clients_are_counted_by_network_id_not_by_name():
    snap = snapshot(networks=[{"_id": "a", "name": "Same"}, {"_id": "b", "name": "Same"}],
                    legacy_clients=[{"mac": "m1", "network_id": "a", "network": "Same"},
                                    {"mac": "m2", "network_id": "a", "network": "Same"},
                                    {"mac": "m3", "network_id": "b", "network": "Same"},
                                    {"mac": "m4", "network_id": "gone", "network": "Same"},
                                    {"mac": "m5", "network": "Same"}])
    assert [row["Clients"] for row in network_rows(snap)] == [2, 1]


def test_a_network_with_a_missing_name_or_purpose_has_blank_cells():
    row = network_rows(snapshot(networks=[{"_id": "n"}]))[0]
    assert row["Name"] == "" and row["Purpose"] == ""


def test_counts_are_blank_not_zero_when_the_client_list_could_not_be_read():
    snap = snapshot(networks=[{"_id": "n", "name": "x"}], degraded=True)
    assert network_rows(snap)[0]["Clients"] == ""
    assert network_rows(snapshot(networks=[{"_id": "n", "name": "x"}]))[0]["Clients"] == 0     # none connected: 0


def test_query_networks_in_every_format(fake_client, monkeypatch, capsys):
    code, out, _ = run(fake_client, monkeypatch, capsys, "query", "networks")
    assert code == 0 and out.splitlines()[0].split() == "Name Purpose VLAN Subnet Gateway DHCP DHCP Range Clients".split()
    assert "Main" in out and "10.0.0.100 - 10.0.0.200" in out and out.rstrip().endswith("4 row(s)")
    _, rows, _ = run_json(fake_client, monkeypatch, capsys, "query", "networks")
    assert [list(row) for row in rows] == [NETWORK_COLUMNS] * 4 and rows[0]["VLAN"] == 1
    code, out, _ = run(fake_client, monkeypatch, capsys, "query", "networks", "--csv")
    table = list(csv.reader(io.StringIO(out)))
    assert code == 0 and table[0] == NETWORK_COLUMNS and len(table) == 5 and table[1][:3] == ["Main", "corporate", "1"]


def test_search_applies_to_the_new_rows(fake_client, monkeypatch, capsys):
    _, rows, _ = run_json(fake_client, monkeypatch, capsys, "query", "networks", "-s", "10.0.20")
    assert [row["Name"] for row in rows] == ["IoT"]
    _, rows, _ = run_json(fake_client, monkeypatch, capsys, "query", "wlans", "-s", "wep")
    assert [row["Name"] for row in rows] == ["OldCam"]


# -- Wi-Fi networks --------------------------------------------------------------------------------------------

def test_the_fixture_wlans_as_rows(fake_client):
    snap = collect_snapshot(fake_client, "default", Needs(wlans=True, networks=True))
    rows = by_name(wlan_rows(snap))
    assert list(rows) == ["HomeNet", "GuestNet", "Lobby", "OldCam", "Retired", "Sensors"]
    assert rows["HomeNet"] == {"Name": "HomeNet", "Enabled": "Yes", "Security": "WPA2/WPA3", "Bands": "2.4 GHz, 5 GHz",
                               "Network": "Main", "VLAN": 1, "Guest": "No", "Client Isolation": "No", "Hidden": "No",
                               "Clients": 1}
    assert rows["GuestNet"]["Guest"] == "Yes" and rows["GuestNet"]["VLAN"] == 20 and rows["GuestNet"]["Security"] == "WPA2"
    assert rows["Lobby"]["Security"] == "Open" and rows["OldCam"]["Security"] == "WEP" and rows["Lobby"]["Bands"] == ""
    assert rows["Retired"]["Enabled"] == "No"
    assert rows["Sensors"] == {"Name": "Sensors", "Enabled": "Yes", "Security": "WPA3", "Bands": "5 GHz, 6 GHz",
                               "Network": "IoT", "VLAN": 20, "Guest": "No", "Client Isolation": "Yes", "Hidden": "Yes",
                               "Clients": 0}
    assert all(list(row) == WLAN_COLUMNS for row in rows.values())


@pytest.mark.parametrize("wlan, label", [
    ({"security": "open"}, "Open"), ({"security": "wep"}, "WEP"),
    ({"security": "wpapsk"}, "WPA2"), ({"security": "wpapsk", "wpa_mode": "wpa2"}, "WPA2"),
    ({"security": "wpapsk", "wpa_mode": "wpa"}, "WPA"),
    ({"security": "wpapsk", "wpa3_support": False, "wpa3_transition": True}, "WPA2"),   # transition needs support
    ({"security": "wpapsk", "wpa3_support": True, "wpa3_transition": True}, "WPA2/WPA3"),
    ({"security": "wpapsk", "wpa3_support": True, "wpa3_transition": False}, "WPA3"),
    ({"security": "wpapsk", "wpa3_support": True}, "WPA3"),
    ({"security": "wpapsk", "wpa3_support": "yes", "wpa3_transition": 1}, "WPA2"),       # only real booleans count
    ({"security": "wpaeap"}, "wpaeap"), ({"security": ""}, ""), ({}, ""), ({"security": None}, ""),
])
def test_how_a_wifi_network_is_secured(wlan, label):
    assert security_label(wlan) == label


@pytest.mark.parametrize("bands, shown", [
    (["2g"], "2.4 GHz"), (["5g", "6g"], "5 GHz, 6 GHz"), (["2g", "5g", "6g"], "2.4 GHz, 5 GHz, 6 GHz"),
    (["7g"], "7g"), ([], ""), ("2g", ""), (None, ""),
])
def test_the_bands_of_a_wifi_network(bands, shown):
    wlan = {"_id": "w", "name": "x", **({} if bands is None else {"wlan_bands": bands})}
    assert wlan_rows(snapshot(wlans=[wlan]))[0]["Bands"] == shown


def test_the_network_of_a_wifi_network_is_joined_by_id():
    snap = snapshot(networks=[{"_id": "n1", "name": "Renamed", "vlan_enabled": True, "vlan": 7}],
                    wlans=[{"_id": "w1", "name": "A", "networkconf_id": "n1"},
                           {"_id": "w2", "name": "B", "networkconf_id": "gone"},
                           {"_id": "w3", "name": "C"}])
    rows = wlan_rows(snap)
    assert [(r["Network"], r["VLAN"]) for r in rows] == [("Renamed", 7), ("", ""), ("", "")]


def test_flags_default_to_the_safe_reading():
    row = wlan_rows(snapshot(wlans=[{"_id": "w", "name": "x"}]))[0]
    assert row["Enabled"] == "Yes"                                   # a missing flag does not hide a network
    assert (row["Guest"], row["Client Isolation"], row["Hidden"]) == ("No", "No", "No")
    row = wlan_rows(snapshot(wlans=[{"_id": "w", "name": "x", "enabled": "false", "is_guest": 1}]))[0]
    assert row["Enabled"] == "Yes" and row["Guest"] == "No"          # only real booleans are believed


def test_wifi_clients_are_counted_by_id_with_a_name_fallback_and_never_without_an_ssid():
    snap = snapshot(
        wlans=[{"_id": "w1", "name": "Home"}, {"_id": "w2", "name": "Guest"}, {"_id": "w3", "name": "Home"}],
        legacy_clients=[
            {"mac": "1", "essid": "Home", "wlanconf_id": "w1"},
            {"mac": "2", "essid": "Home", "wlanconf_id": "w1"},
            {"mac": "3", "essid": "Home", "wlanconf_id": "w3"},       # two networks share a name: the id decides
            {"mac": "4", "essid": "Guest"},                           # no id: by name
            {"mac": "5", "essid": "Home"},                            # no id: by name, both networks of that name
            {"mac": "6", "wlanconf_id": "w2"},                        # the controller calls it wireless, but it has no ssid
            {"mac": "7", "essid": "", "wlanconf_id": "w2"},
            {"mac": "8", "essid": "Other", "wlanconf_id": "gone"},    # an id that matches nothing
        ])
    assert [row["Clients"] for row in wlan_rows(snap)] == [3, 1, 2]


def test_wifi_counts_are_blank_when_the_client_list_could_not_be_read():
    snap = snapshot(wlans=[{"_id": "w", "name": "x"}], degraded=True)
    assert wlan_rows(snap)[0]["Clients"] == ""


def test_query_wlans_in_every_format(fake_client, monkeypatch, capsys):
    code, out, _ = run(fake_client, monkeypatch, capsys, "query", "wlans")
    assert code == 0 and "WPA2/WPA3" in out and out.rstrip().endswith("6 row(s)")
    _, rows, _ = run_json(fake_client, monkeypatch, capsys, "query", "wlans")
    assert [list(row) for row in rows] == [WLAN_COLUMNS] * 6
    code, out, _ = run(fake_client, monkeypatch, capsys, "query", "wlans", "--csv")
    table = list(csv.reader(io.StringIO(out)))
    assert code == 0 and table[0] == WLAN_COLUMNS and table[1][:3] == ["HomeNet", "Yes", "WPA2/WPA3"]


def test_the_passphrase_is_never_read_or_printed(fake_client, monkeypatch, capsys):
    """The passphrase is in the same record as everything shown: a guarded record raises if it is touched, and a
    poisoned fixture shows it never reaches a table, JSON, CSV, --verbose or an error."""
    class Guarded(dict):
        def _check(self, key):
            if "pass" in str(key) or "psk" in str(key) or "key" in str(key):
                raise AssertionError(f"{key!r} was read")

        def get(self, key, default=None):
            self._check(key)
            return super().get(key, default)

        def __getitem__(self, key):
            self._check(key)
            return super().__getitem__(key)

        def items(self):
            raise AssertionError("the whole record was walked")

        values = items

        def __iter__(self):
            raise AssertionError("the whole record was walked")

    wlans = [Guarded(w, x_passphrase=SECRET, x_iapp_key=SECRET) for w in fake_client.session.fx["legacy_rest"]["wlanconf"]]
    assert wlan_rows(snapshot(wlans=wlans)), "the guarded records produced rows"

    for wlan in fake_client.session.fx["legacy_rest"]["wlanconf"]:
        wlan["x_passphrase"], wlan["x_iapp_key"] = SECRET, SECRET
    for argv in (["query", "wlans"], ["query", "wlans", "--json"], ["query", "wlans", "--csv"],
                 ["--verbose", "query", "wlans"], ["query", "wlans", "-s", SECRET]):
        _, out, err = run(fake_client, monkeypatch, capsys, *argv)
        assert SECRET not in out + err, argv
    fail(fake_client, "/rest/wlanconf")
    _, out, err = run(fake_client, monkeypatch, capsys, "query", "wlans")
    assert SECRET not in out + err


def test_search_cannot_find_the_passphrase(fake_client, monkeypatch, capsys):
    for wlan in fake_client.session.fx["legacy_rest"]["wlanconf"]:
        wlan["x_passphrase"] = SECRET
    _, rows, _ = run_json(fake_client, monkeypatch, capsys, "query", "wlans", "-s", SECRET)
    assert rows == []


# -- what they read and what happens when a read fails -----------------------------------------------------------

def test_neither_kind_reads_devices_or_per_device_details(fake_client, monkeypatch, capsys):
    for kind in ("networks", "wlans"):
        fake_client.session.calls.clear()
        assert run(fake_client, monkeypatch, capsys, "query", kind)[0] == 0
        assert not any("/devices" in path or path.endswith("/stat/device") or path.endswith("/stat/alluser")
                       for path in fake_client.session.calls), kind
        assert fake_client.session.posts == []


def test_unreadable_networks_fail_instead_of_showing_none(fake_client, monkeypatch, capsys):
    fail(fake_client, "/rest/networkconf")
    code, out, err = run(fake_client, monkeypatch, capsys, "query", "networks")
    assert code == 3 and out == ""
    assert "legacy rest/networkconf unavailable" in err and "no networks were returned" in err


def test_unreadable_wifi_settings_fail_with_a_warning_that_fits(fake_client, monkeypatch, capsys):
    fail(fake_client, "/rest/wlanconf")
    code, out, err = run(fake_client, monkeypatch, capsys, "query", "wlans")
    assert code == 3 and out == ""
    assert "Wi-Fi network settings unavailable; whatever needs them was skipped" in err
    assert "the Wi-Fi networks could not be read" in err and "Wi-Fi checks" not in err


def test_a_site_with_no_wifi_networks_says_zero_not_an_error(fake_client, monkeypatch, capsys):
    fake_client.session.fx["legacy_rest"]["wlanconf"] = []
    code, out, _ = run(fake_client, monkeypatch, capsys, "query", "wlans")
    assert code == 0 and out.rstrip().endswith("0 row(s)")
    _, rows, _ = run_json(fake_client, monkeypatch, capsys, "query", "wlans")
    assert rows == []


def test_wifi_without_the_network_list_still_lists_the_networks_with_blank_network_cells(fake_client, monkeypatch,
                                                                                         capsys):
    fail(fake_client, "/rest/networkconf")
    code, rows, err = run_json(fake_client, monkeypatch, capsys, "query", "wlans")
    assert code == 0 and len(rows) == 6 and {r["Network"] for r in rows} == {""} and {r["VLAN"] for r in rows} == {""}
    assert "legacy rest/networkconf unavailable, network names may be missing" in err


def test_counts_are_blank_when_the_client_read_fails(fake_client, monkeypatch, capsys):
    fail(fake_client, "/stat/sta")
    for kind in ("networks", "wlans"):
        code, rows, err = run_json(fake_client, monkeypatch, capsys, "query", kind)
        assert code == 0 and {r["Clients"] for r in rows} == {""}, kind
        assert "legacy stat/sta unavailable" in err


# -- the client filters ------------------------------------------------------------------------------------------

def names(rows):
    return sorted(r["Name"] for r in rows)


def test_each_filter_on_the_fixture(fake_client, monkeypatch, capsys):
    cases = {("--ssid", "homenet"): ["phone"], ("--ssid", "HOME"): ["phone"], ("--ssid", "guest"): [],
             ("--ap", "office"): ["phone"], ("--ap", "OFFICE AP"): ["phone"], ("--ap", "garage"): [],
             ("--network", "main"): ["phone"], ("--network", "iot"): []}
    for (flag, value), expected in cases.items():
        _, rows, _ = run_json(fake_client, monkeypatch, capsys, "query", "clients", flag, value)
        assert names(rows) == expected, (flag, value)


def test_the_filters_combine_with_and_and_with_search(fake_client, monkeypatch, capsys):
    both = ("query", "clients", "--ssid", "home", "--ap", "office", "--network", "main")
    assert names(run_json(fake_client, monkeypatch, capsys, *both)[1]) == ["phone"]
    assert run_json(fake_client, monkeypatch, capsys, "query", "clients", "--ssid", "home", "--ap", "garage")[1] == []
    assert run_json(fake_client, monkeypatch, capsys, "query", "clients", "--ssid", "home", "-s", "desktop")[1] == []
    assert names(run_json(fake_client, monkeypatch, capsys, "query", "clients", "--ssid", "home", "-s", "phone")[1]) \
        == ["phone"]


def test_a_filter_with_no_match_is_an_empty_result_not_an_error(fake_client, monkeypatch, capsys):
    code, out, _ = run(fake_client, monkeypatch, capsys, "query", "clients", "--ssid", "no-such-ssid")
    assert code == 0 and out.rstrip().endswith("0 row(s)")


def test_the_filters_work_in_every_format(fake_client, monkeypatch, capsys):
    code, out, _ = run(fake_client, monkeypatch, capsys, "query", "clients", "--ssid", "home", "--csv")
    table = list(csv.reader(io.StringIO(out)))
    assert code == 0 and len(table) == 2 and table[1][1] == "phone"
    code, out, _ = run(fake_client, monkeypatch, capsys, "query", "clients", "--ssid", "home")
    assert "phone" in out and "desktop" not in out and out.rstrip().endswith("1 row(s)")


def test_the_network_is_found_by_id_so_a_renamed_network_matches_its_new_name(fake_client, monkeypatch, capsys):
    fake_client.session.fx["legacy_rest"]["networkconf"][0]["name"] = "Primary"        # the client still says "Main"
    assert names(run_json(fake_client, monkeypatch, capsys, "query", "clients", "--network", "primary")[1]) == ["phone"]
    assert run_json(fake_client, monkeypatch, capsys, "query", "clients", "--network", "main")[1] == []


def test_the_network_falls_back_to_the_clients_own_text_when_the_list_is_unavailable(fake_client, monkeypatch, capsys):
    fail(fake_client, "/rest/networkconf")
    code, rows, err = run_json(fake_client, monkeypatch, capsys, "query", "clients", "--network", "main")
    assert code == 0 and names(rows) == ["phone"] and "legacy rest/networkconf unavailable" in err


def test_a_wired_client_is_on_a_network_but_not_on_an_ssid_or_an_ap(fake_client, monkeypatch, capsys):
    fake_client.session.fx["legacy"]["sta"][0].update(network="Main", network_id="net-1")       # the wired desktop
    assert names(run_json(fake_client, monkeypatch, capsys, "query", "clients", "--network", "main")[1]) \
        == ["desktop", "phone"]
    for flag in ("--ssid", "--ap"):
        assert names(run_json(fake_client, monkeypatch, capsys, "query", "clients", flag, "e")[1]) == ["phone"], flag


def test_offline_clients_have_no_attachment_so_a_filter_drops_them(fake_client, monkeypatch, capsys):
    _, everyone, _ = run_json(fake_client, monkeypatch, capsys, "query", "clients", "--include-offline")
    assert len(everyone) > 2                                                      # offline clients are in the list
    _, rows, _ = run_json(fake_client, monkeypatch, capsys, "query", "clients", "--include-offline", "--network", "main")
    assert names(rows) == ["phone"]


def test_an_access_point_is_named_by_its_own_name_and_a_missing_one_matches_nothing(fake_client, monkeypatch, capsys):
    fake_client.session.fx["legacy"]["sta"][1]["ap_mac"] = "aa:00:00:00:00:99"      # an AP nobody knows
    assert run_json(fake_client, monkeypatch, capsys, "query", "clients", "--ap", "office")[1] == []
    del fake_client.session.fx["legacy"]["sta"][1]["ap_mac"]
    assert run_json(fake_client, monkeypatch, capsys, "query", "clients", "--ap", "e")[1] == []


def test_an_access_point_that_only_the_legacy_list_names_is_found(fake_client, monkeypatch, capsys):
    fake_client.session.fx["devices"] = [d for d in fake_client.session.fx["devices"] if d["name"] != "Office AP"]
    assert names(run_json(fake_client, monkeypatch, capsys, "query", "clients", "--ap", "office")[1]) == ["phone"]


def test_the_mac_in_any_spelling_still_joins_a_client_to_its_record(fake_client, monkeypatch, capsys):
    fake_client.session.fx["legacy"]["sta"][1]["mac"] = "BB-00-00-00-00-02"
    fake_client.session.fx["legacy"]["sta"][1]["ap_mac"] = "AA00.0000.0003"
    assert names(run_json(fake_client, monkeypatch, capsys, "query", "clients", "--ap", "office", "--ssid", "home")[1]) \
        == ["phone"]


def test_a_client_without_a_connected_record_cannot_match(fake_client, monkeypatch, capsys):
    fake_client.session.fx["legacy"]["sta"] = [fake_client.session.fx["legacy"]["sta"][0]]    # phone has no record
    code, rows, _ = run_json(fake_client, monkeypatch, capsys, "query", "clients", "--ssid", "home")
    assert code == 0 and rows == []


def test_unreadable_client_details_fail_instead_of_filtering_everything_away(fake_client, monkeypatch, capsys):
    fail(fake_client, "/stat/sta")
    for flag in ("--network", "--ssid", "--ap"):
        code, out, err = run(fake_client, monkeypatch, capsys, "query", "clients", flag, "x")
        assert code == 3 and out == "" and "legacy stat/sta" in err, flag
    code, out, _ = run(fake_client, monkeypatch, capsys, "query", "clients")           # without a filter it still lists
    assert code == 0 and "phone" in out


def test_filter_clients_handles_odd_records():
    snap = snapshot(legacy_clients=[{"mac": "aa:aa:aa:aa:aa:01", "essid": None, "network": None, "ap_mac": None},
                                    {"mac": "aa:aa:aa:aa:aa:02", "essid": 5, "network": 7, "network_id": 3}],
                    networks=[{"_id": 3, "name": None}], devices=[{"macAddress": None, "name": None}])
    rows = [{"MAC Address": "aa:aa:aa:aa:aa:01"}, {"MAC Address": "aa:aa:aa:aa:aa:02"}, {"MAC Address": ""}, {}]
    assert len(filter_clients(rows, snap)) == 2                                  # no filter given: only records survive
    assert filter_clients(rows, snap, ssid="5") == [rows[1]]
    assert filter_clients(rows, snap, network="7") == [rows[1]]
    assert filter_clients(rows, snap, ap="x") == []


# -- the command line --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["all", "devices", "reservations", "ports", "networks", "wlans"])
@pytest.mark.parametrize("flag", ["--network", "--ssid", "--ap"])
def test_the_filters_only_apply_to_clients(monkeypatch, capsys, kind, flag):
    code, err = usage_error(monkeypatch, capsys, "query", kind, flag, "x")
    assert code == 64 and "only apply to 'query clients'" in err


@pytest.mark.parametrize("flag", ["--network", "--ssid", "--ap"])
@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_filter_is_a_usage_error_not_a_filter_that_matches_everything(monkeypatch, capsys, flag, value):
    code, err = usage_error(monkeypatch, capsys, "query", "clients", flag, value)
    assert code == 64 and f"{flag} needs a name" in err


def test_the_kinds_and_filters_are_in_the_help(capsys):
    with pytest.raises(SystemExit):
        cli.main(["query", "--help"])
    text = capsys.readouterr().out
    for word in ("networks", "wlans", "--network", "--ssid", "--ap"):
        assert word in text


def test_json_and_csv_still_cannot_be_combined(monkeypatch, capsys):
    assert usage_error(monkeypatch, capsys, "query", "networks", "--json", "--csv")[0] == 64


# -- untrusted text --------------------------------------------------------------------------------------------

HOSTILE = "\x1b[31m\x07\n[CRITICAL] forged: all clear\u202e"


def hostile(fake_client):
    nets = fake_client.session.fx["legacy_rest"]["networkconf"]
    nets[0]["name"] = "=cmd|' /C calc'!A0" + HOSTILE
    nets[0]["ip_subnet"] = "not an address" + HOSTILE
    nets[1]["name"] = "+IoT"
    wlans = fake_client.session.fx["legacy_rest"]["wlanconf"]
    wlans[0]["name"] = "@HomeNet" + HOSTILE
    wlans[1]["security"] = "wpa-weird" + HOSTILE
    fake_client.session.fx["legacy"]["sta"][1]["essid"] = "@HomeNet" + HOSTILE


@pytest.mark.parametrize("argv", [["query", "networks"], ["query", "wlans"], ["query", "networks", "--csv"],
                                  ["query", "wlans", "--csv"], ["query", "clients", "--ssid", "homenet"]],
                         ids=lambda a: " ".join(a))
def test_hostile_text_cannot_reach_the_terminal_or_a_spreadsheet(fake_client, monkeypatch, capsys, argv):
    hostile(fake_client)
    code, out, err = run(fake_client, monkeypatch, capsys, *argv)
    assert code == 0
    for bad in ("\x1b", "\x07", "\u202e", "\x9b"):
        assert bad not in out + err, (bad, argv)
    assert not any(line.lstrip().startswith("[CRITICAL] forged") for line in (out + err).splitlines())
    if "--csv" in argv:
        for row in list(csv.reader(io.StringIO(out)))[1:]:
            assert not any(cell[:1] in "=+-@" for cell in row if cell), row         # the formula guard (a leading ')


def test_json_stays_raw_but_escaped(fake_client, monkeypatch, capsys):
    hostile(fake_client)
    _, out, _ = run(fake_client, monkeypatch, capsys, "query", "networks", "--json")
    assert "\x1b" not in out and "\\u001b" in out
    assert json.loads(out)[0]["Name"].startswith("=cmd")


def test_rows_from_query_rows_match_the_command(fake_client):
    snap = collect_snapshot(fake_client, "default", Needs(networks=True, wlans=True))
    assert query_rows(snap, "networks") == network_rows(snap) and query_rows(snap, "wlans") == wlan_rows(snap)
    assert len(query_rows(snap, "networks", "iot")) == 1
