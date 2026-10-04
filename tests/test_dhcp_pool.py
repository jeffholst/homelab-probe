"""Reservations inside the dynamic DHCP range of their network."""

import ipaddress
import json
import time

import pytest

from unifi_sentinel import cli
from unifi_sentinel.diagnose import CRITICAL, INFO, WARNING, _pool_findings, apply_ignores, diagnose
from unifi_sentinel.reservations import dhcp_pool
from unifi_sentinel.settings import IgnoreRule
from unifi_sentinel.snapshot import Needs, collect_snapshot


def net(**kw):
    """A network record shaped like the live controller's (Network 10.6.106)."""
    return {"_id": "net-1", "name": "Main", "purpose": "corporate", "ip_subnet": "10.0.0.1/24",
            "dhcpd_enabled": True, "dhcp_relay_enabled": False,
            "dhcpd_start": "10.0.0.100", "dhcpd_stop": "10.0.0.200", **kw}


# -- reading the pool ---------------------------------------------------------------------

def test_an_enabled_network_has_its_range():
    state, pool = dhcp_pool(net())
    assert state == "ok" and pool == (ipaddress.ip_address("10.0.0.100"), ipaddress.ip_address("10.0.0.200"))


@pytest.mark.parametrize("record", [
    {"dhcpd_enabled": False},
    {"dhcpd_enabled": None},
    {"dhcp_relay_enabled": True},
    {"purpose": "remote-user-vpn", "dhcpd_enabled": None},       # a VPN pool has a range but is not client DHCP
    {"purpose": "wan", "dhcpd_enabled": None, "dhcpd_start": None, "dhcpd_stop": None},
    {"dhcpd_enabled": "true"},                                    # only a real boolean true counts
])
def test_networks_where_the_controller_does_not_serve_dhcp_are_off(record):
    assert dhcp_pool(net(**record)) == ("off", None)


def test_the_missing_dhcpd_enabled_key_is_off():
    record = net()
    del record["dhcpd_enabled"]
    assert dhcp_pool(record) == ("off", None)


@pytest.mark.parametrize("record", [
    {"dhcpd_start": None}, {"dhcpd_stop": None}, {"dhcpd_start": "", "dhcpd_stop": ""},
    {"dhcpd_start": "not-an-ip"}, {"dhcpd_start": "10.0.0.300"},
    {"dhcpd_start": "10.0.0.200", "dhcpd_stop": "10.0.0.100"},      # reversed
    {"dhcpd_start": "fd00::10", "dhcpd_stop": "10.0.0.200"},         # mixed families
])
def test_dhcp_on_with_an_unusable_range_is_unknown(record):
    assert dhcp_pool(net(**record)) == ("unknown", None)


def test_the_range_may_be_a_single_address_and_whitespace_is_ignored():
    state, pool = dhcp_pool(net(dhcpd_start=" 10.0.0.150 ", dhcpd_stop="10.0.0.150"))
    assert state == "ok" and pool[0] == pool[1]


# -- the check ------------------------------------------------------------------------------

def snapshot(fake_client, *, reservations=(), network=None, clients=()):
    fx = fake_client.session.fx
    if network is not None:
        fx["legacy_rest"]["networkconf"][0] = {**fx["legacy_rest"]["networkconf"][0], **network}
    for mac, name, ip in reservations:
        fx["legacy"]["alluser"].append({"mac": mac, "name": name, "use_fixedip": True, "fixed_ip": ip,
                                        "last_connection_network_id": "net-1", "last_seen": int(time.time())})
    for mac, name, ip in clients:
        fx["clients"].append({"id": f"c-{name}", "name": name, "type": "WIRELESS", "macAddress": mac,
                              "ipAddress": ip, "connectedAt": "2026-01-01T09:00:00Z", "uplinkDeviceId": "ap1"})
    return collect_snapshot(fake_client, "default", Needs(reservations=True))


def test_the_fixture_has_pools_and_no_reservation_inside_one(fake_client):
    snap = collect_snapshot(fake_client, "default", Needs(reservations=True))
    assert [dhcp_pool(n)[0] for n in snap.networks] == ["ok", "ok", "off", "off"]
    assert _pool_findings(snap) == []                                  # reservations are .10 and .50, pool is .100-.200


def test_a_reservation_inside_the_pool_is_a_warning(fake_client):
    snap = snapshot(fake_client, reservations=[("cc:00:00:00:00:01", "media-box", "10.0.0.150")])
    (f,) = _pool_findings(snap)
    assert (f.severity, f.subject, f.code) == (WARNING, "media-box", "reservation.in_dhcp_pool")
    assert f.message == "reserved IP 10.0.0.150 is inside the DHCP pool 10.0.0.100-10.0.0.200 of Main"


@pytest.mark.parametrize("ip, inside", [("10.0.0.99", False), ("10.0.0.100", True), ("10.0.0.101", True),
                                        ("10.0.0.199", True), ("10.0.0.200", True), ("10.0.0.201", False)])
def test_both_ends_of_the_range_are_inside(fake_client, ip, inside):
    snap = snapshot(fake_client, reservations=[("cc:00:00:00:00:01", "box", ip)])
    assert bool(_pool_findings(snap)) is inside


def test_another_client_using_the_address_makes_it_critical(fake_client):
    snap = snapshot(fake_client, reservations=[("cc:00:00:00:00:01", "media-box", "10.0.0.150")],
                    clients=[("cc:00:00:00:00:09", "guest-phone", "10.0.0.150")])
    (f,) = _pool_findings(snap)
    assert f.severity == CRITICAL and "guest-phone" in f.message and f.message.count("also in use by") == 1


def test_the_reserved_client_itself_using_the_address_is_not_a_conflict(fake_client):
    snap = snapshot(fake_client, reservations=[("cc:00:00:00:00:01", "media-box", "10.0.0.150")],
                    clients=[("cc:00:00:00:00:01", "media-box", "10.0.0.150")])
    (f,) = _pool_findings(snap)
    assert f.severity == WARNING


def test_an_online_unifi_device_using_the_address_counts_as_another_holder(fake_client):
    snap = snapshot(fake_client, reservations=[("cc:00:00:00:00:01", "media-box", "10.0.0.150")])
    snap.devices[0] = {**snap.devices[0], "ipAddress": "10.0.0.150", "state": "ONLINE"}
    (f,) = _pool_findings(snap)
    assert f.severity == CRITICAL and "UniFi device" in f.message


@pytest.mark.parametrize("network", [{"dhcpd_enabled": False}, {"dhcp_relay_enabled": True}])
def test_no_finding_when_the_controller_is_not_the_dhcp_server(fake_client, network):
    snap = snapshot(fake_client, reservations=[("cc:00:00:00:00:01", "box", "10.0.0.150")], network=network)
    assert _pool_findings(snap) == []


def test_missing_range_is_one_info_per_network_not_a_guess(fake_client):
    snap = snapshot(fake_client, reservations=[("cc:00:00:00:00:01", "a", "10.0.0.150"),
                                               ("cc:00:00:00:00:02", "b", "10.0.0.151")],
                    network={"dhcpd_start": None, "dhcpd_stop": None})
    got = _pool_findings(snap)
    assert [(f.severity, f.subject, f.code) for f in got] == [(INFO, "Main", "reservation.pool_unknown")]
    assert "missing or invalid" in got[0].message


def test_a_network_without_reservations_is_not_reported_even_if_its_range_is_bad(fake_client):
    snap = snapshot(fake_client, network={"dhcpd_start": None})
    fx_nets = [dict(n, name=n["name"]) for n in snap.networks]
    assert fx_nets[0]["dhcpd_start"] is None
    snap.all_users = [u for u in snap.all_users if not u.get("use_fixedip")]
    assert _pool_findings(snap) == []


def test_unresolved_networks_and_odd_addresses_are_skipped_safely(fake_client):
    snap = snapshot(fake_client, reservations=[("cc:00:00:00:00:01", "a", "not-an-ip"),
                                               ("cc:00:00:00:00:02", "b", "fd00::150")])
    snap.networks = []                                                # networks could not be read
    assert _pool_findings(snap) == []
    snap = snapshot(fake_client, reservations=[("cc:00:00:00:00:03", "c", "fd00::150")])
    assert [f for f in _pool_findings(snap) if f.subject == "c"] == []        # IPv6 never matches an IPv4 pool


def test_the_subject_falls_back_to_hostname_then_mac(fake_client):
    fx = fake_client.session.fx
    fx["legacy"]["alluser"] += [
        {"mac": "cc:00:00:00:00:01", "hostname": "nas", "use_fixedip": True, "fixed_ip": "10.0.0.150",
         "last_connection_network_id": "net-1"},
        {"mac": "cc:00:00:00:00:02", "use_fixedip": True, "fixed_ip": "10.0.0.151",
         "last_connection_network_id": "net-1"}]
    snap = collect_snapshot(fake_client, "default", Needs(reservations=True))
    assert sorted(f.subject for f in _pool_findings(snap)) == ["CC:00:00:00:00:02", "nas"]


def test_the_ignore_list_can_silence_a_deliberate_in_pool_reservation(fake_client):
    snap = snapshot(fake_client, reservations=[("cc:00:00:00:00:01", "media-box", "10.0.0.150"),
                                               ("cc:00:00:00:00:02", "nas", "10.0.0.151")])
    kept, ignored = apply_ignores(_pool_findings(snap),
                                  (IgnoreRule(subject="media-box", message="DHCP pool", reason="on purpose"),))
    assert [f.subject for f in kept] == ["nas"] and [f.subject for f, _ in ignored] == ["media-box"]


def test_diagnose_includes_the_check(fake_client):
    snap = snapshot(fake_client, reservations=[("cc:00:00:00:00:01", "media-box", "10.0.0.150")])
    assert "reservation.in_dhcp_pool" in {f.code for f in diagnose(snap)}


# -- the command line -------------------------------------------------------------------------------

def test_diagnose_json_end_to_end(fake_client, monkeypatch, capsys):
    fake_client.session.fx["legacy"]["device"][0]["overheating"] = False
    snapshot(fake_client, reservations=[("cc:00:00:00:00:01", "media-box", "10.0.0.150")])
    monkeypatch.setenv("CONTROLLER_URL", "https://controller")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    assert cli.main(["diagnose", "--json", "--no-events"]) == 1
    doc = json.loads(capsys.readouterr().out)
    (f,) = [f for f in doc["findings"] if f["code"] == "reservation.in_dhcp_pool"]
    assert f["severity"] == "warning" and "10.0.0.100-10.0.0.200 of Main" in f["message"]
