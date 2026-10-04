"""`Needs` and `EventQuery`: each command declares what it reads, and nothing else is read."""

import dataclasses

import pytest

from unifi_sentinel import cli
from unifi_sentinel.client import UniFiAPIError
from unifi_sentinel.snapshot import EventQuery, Needs, collect_event_snapshot, collect_snapshot

ALWAYS = {"/proxy/network/integration/v1/sites", "/proxy/network/api/s/default/stat/device",
          "/proxy/network/api/s/default/stat/sta"}


def reads(fake_client):
    """The kinds of data the fake controller was asked for: the last path segment(s) of each GET, and POSTs."""
    kinds = set()
    for path in fake_client.session.calls:
        if "/sites/site-1/devices" in path:
            kinds.add("devices")
        elif path.endswith("/sites/site-1/clients"):
            kinds.add("clients")
        elif path.endswith("/stat/alluser"):
            kinds.add("alluser")
        elif path.endswith("/rest/networkconf"):
            kinds.add("networkconf")
        elif path.endswith("/stat/health"):
            kinds.add("health")
        elif path.endswith("/speedtest"):
            kinds.add("speedtests")
        elif path.endswith("/stat/rogueap"):
            kinds.add("neighbors")
        elif path.endswith("/network-members-groups"):
            kinds.add("groups")
        elif path.endswith(("/firewall-policies", "/firewall/zone", "/firewall/zone-matrix", "/rest/portforward")):
            kinds.add("firewall")
        elif path.endswith("/rest/wlanconf"):
            kinds.add("wlans")
        elif path.endswith("/stat/device"):
            kinds.add("legacy-devices")
        elif path.endswith("/stat/sta"):
            kinds.add("legacy-clients")
    if fake_client.session.posts:
        kinds.add("events")
    return kinds


BASE = {"devices", "clients", "legacy-devices", "legacy-clients"}


@pytest.mark.parametrize("needs, extra", [
    (Needs(), set()),
    (Needs(offline=True), {"alluser"}),
    (Needs(reservations=True), {"alluser", "networkconf"}),
    (Needs(groups=True), {"alluser", "groups"}),
    (Needs(health=True), {"health"}),
    (Needs(speedtests=True), {"speedtests"}),
    (Needs(neighbors=True), {"neighbors"}),
    (Needs(firewall=True), {"firewall"}),
    (Needs(wlans=True), {"wlans"}),
    (Needs(networks=True), {"networkconf"}),
    (Needs(events=EventQuery()), {"events"}),
    (Needs(reservations=True, groups=True, health=True, speedtests=True, neighbors=True, firewall=True,
           events=EventQuery()),
     {"alluser", "networkconf", "groups", "health", "speedtests", "neighbors", "firewall", "events"}),
])
def test_a_snapshot_reads_the_base_data_plus_exactly_what_was_asked_for(fake_client, needs, extra):
    collect_snapshot(fake_client, "default", needs)
    assert reads(fake_client) == BASE | extra


def test_required_users_alone_reads_nothing_extra(fake_client):
    collect_snapshot(fake_client, "default", Needs(users_required=True))
    assert reads(fake_client) == BASE


def test_the_default_is_the_empty_need(fake_client):
    collect_snapshot(fake_client, "default")
    assert reads(fake_client) == BASE and Needs() == Needs(offline=False, events=None)


def test_needs_are_frozen_and_comparable():
    needs = Needs(reservations=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        needs.groups = True
    assert needs == Needs(reservations=True) and needs != Needs(groups=True) and hash(needs) == hash(Needs(reservations=True))
    assert EventQuery(3600) == EventQuery(since_seconds=3600) and EventQuery().since_seconds == 86400


def test_the_event_window_and_filters_reach_the_query(fake_client):
    snap = collect_snapshot(fake_client, "default", Needs(events=EventQuery(7200, ("audit",), ("high",), "roamed")),
                            now_ms=2_000_000_000_000)
    (path, body), = fake_client.session.posts
    assert body["timestampFrom"] == 2_000_000_000_000 - 7200 * 1000 and body["timestampTo"] == 2_000_000_000_000
    assert body["categories"] == ["AUDIT"] and body["severities"] == ["HIGH"] and body["searchText"] == "roamed"
    assert snap.event_window_seconds == 7200 and snap.events_available


def test_no_event_query_means_no_window_and_no_request(fake_client):
    snap = collect_snapshot(fake_client, "default", Needs())
    assert snap.event_window_seconds == 0 and not snap.events_available and fake_client.session.posts == []


def test_the_events_command_reads_only_the_log(fake_client):
    snap = collect_event_snapshot(fake_client, "default", EventQuery(3600))
    assert reads(fake_client) == {"events"} and snap.devices == [] and snap.event_window_seconds == 3600


def test_a_required_client_history_fails_and_an_optional_one_degrades(fake_client, monkeypatch, capsys):
    legacy_stat = fake_client.legacy_stat

    def broken(site_ref, resource):
        if resource == "alluser":
            raise UniFiAPIError("unavailable")
        return legacy_stat(site_ref, resource)

    monkeypatch.setattr(fake_client, "legacy_stat", broken)
    with pytest.raises(UniFiAPIError):
        collect_snapshot(fake_client, "default", Needs(groups=True, users_required=True))
    assert collect_snapshot(fake_client, "default", Needs(groups=True)).all_users == []
    assert "legacy stat/alluser unavailable" in capsys.readouterr().err


# -- what each command reads ---------------------------------------------------------------------------------

def run(fake_client, monkeypatch, argv):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    return cli.main(argv)


COMMANDS = {
    "info": (["info"], set()),
    "export": (["export"], BASE),
    "export offline": (["export", "--include-offline"], BASE | {"alluser"}),
    "export json": (["export", "--format", "json"], BASE),
    "export json offline": (["export", "--format", "json", "--include-offline"], BASE | {"alluser"}),
    "query devices": (["query", "devices"], BASE),
    "query clients offline": (["query", "clients", "--include-offline"], BASE | {"alluser"}),
    "query reservations": (["query", "reservations"], BASE | {"alluser", "networkconf"}),
    "query ports": (["query", "ports"], BASE),
    "query networks": (["query", "networks"], {"legacy-clients", "networkconf"}),
    "query wlans": (["query", "wlans"], {"legacy-clients", "networkconf", "wlans"}),
    "query clients by ssid": (["query", "clients", "--ssid", "home"], BASE),
    "query clients by ap": (["query", "clients", "--ap", "office"], BASE),
    "query clients by network": (["query", "clients", "--network", "main"], BASE | {"networkconf"}),
    "new-clients": (["new-clients"], BASE | {"alluser", "groups"}),
    "topology": (["topology"], BASE),
    "wifi": (["wifi"], BASE | {"neighbors"}),
    "wan": (["wan"], BASE | {"health", "speedtests"}),
    "firewall": (["firewall"], BASE | {"alluser", "networkconf", "firewall"}),
    "audit": (["audit"], (BASE - {"legacy-devices"}) | {"alluser", "wlans"}),
    "events": (["events"], {"events"}),
    "client without events": (["client", "desktop", "--no-events"], BASE | {"alluser", "networkconf", "groups"}),
    "client": (["client", "desktop"], BASE | {"alluser", "networkconf", "groups", "events"}),
    "client no match": (["client", "nobody-has-this-name"], BASE - {"legacy-devices"} | {"alluser"}),
    "diagnose without events": (["diagnose", "--no-events"],
                                BASE | {"alluser", "networkconf", "health", "speedtests"}),
    "diagnose": (["diagnose"], BASE | {"alluser", "networkconf", "health", "speedtests", "events"}),
    "snapshot": (["snapshot"], BASE | {"alluser", "networkconf", "groups"}),
}


@pytest.mark.parametrize("name", sorted(COMMANDS))
def test_each_command_reads_exactly_what_it_declares(fake_client, monkeypatch, tmp_path, name):
    argv, expected = COMMANDS[name]
    if argv[0] == "snapshot":
        argv = ["snapshot", "--dir", str(tmp_path / "snaps")]
    if argv[0] == "export":
        argv = [*argv, "-o", str(tmp_path)]
    assert run(fake_client, monkeypatch, argv) in (0, 1, 2, 4)
    kinds = reads(fake_client)
    if argv[0] == "info":
        assert kinds == set() and fake_client.session.posts == []
    else:
        assert kinds == expected, f"{name} read {sorted(kinds ^ expected)} differently from its declaration"


def test_client_defers_device_reads_until_a_unique_match(fake_client, monkeypatch, capsys):
    assert run(fake_client, monkeypatch, ["client", "nobody-has-this-name"]) == 4
    capsys.readouterr()
    assert not any(path.endswith("/stat/device") or
                   ("/devices/" in path and not path.endswith("/devices")) for path in fake_client.session.calls)

    fake_client.session.calls.clear()
    assert run(fake_client, monkeypatch, ["client", "desktop", "--no-events"]) == 0
    assert any(path.endswith("/stat/device") for path in fake_client.session.calls)
    assert len([path for path in fake_client.session.calls
                if "/devices/" in path and not path.endswith("/devices")]) == 8
