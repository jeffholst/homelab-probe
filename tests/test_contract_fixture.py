"""The synthetic fixture satisfies the contract, and the contract covers every field the code reads."""

import contextlib
import io
from unittest import mock

import pytest
from contract import CONTRACT, covered, endpoint_of, present, problems, segments
from field_tracking import TrackingSession

from unifi_sentinel import cli
from unifi_sentinel.client import UniFiClient

# every command, so every field the program can read from the fixture is read
COMMANDS = [["info"], ["export"], ["export", "--include-offline"], ["query", "devices"],
            ["query", "clients", "--include-offline"], ["query", "reservations"], ["query", "reservations", "--offline"],
            ["query", "ports"], ["new-clients"], ["topology", "--clients"], ["wifi", "--all"], ["wan"], ["events"],
            ["events", "--since", "14d"], ["events", "--device", "no-such-device"],
            ["events", "--summary"], ["client", "desktop"], ["client", "phone"], ["client", "old-printer"],
            ["diagnose"], ["snapshot"], ["diff"], ["firewall", "--all", "--zones"], ["audit"]]


def fixture_records(name):
    """The fixture's records for an endpoint, as the fake controller serves them."""
    session = TrackingSession()
    fx = session.fx
    table = {
        "integration/sites": fx["sites"], "integration/devices": fx["devices"], "integration/clients": fx["clients"],
        "integration/device": list(fx["device_detail"].values()),
        "integration/device-statistics": list(fx["device_stats"].values()),
        "legacy/stat/device": fx["legacy"]["device"], "legacy/stat/sta": fx["legacy"]["sta"],
        "legacy/stat/alluser": fx["legacy"]["alluser"], "legacy/stat/health": fx["legacy"]["health"],
        "legacy/stat/rogueap": fx["legacy"]["rogueap"], "legacy/rest/networkconf": fx["legacy_rest"]["networkconf"],
        "legacy/v2/network-members-groups": fx["legacy_v2"]["network-members-groups"],
        "legacy/v2/speedtest": fx["legacy_v2"]["speedtest"]["data"], "legacy/system-log": session.events,
        "legacy/v2/firewall-policies": fx["legacy_v2"]["firewall-policies"],
        "legacy/v2/firewall/zone": fx["legacy_v2"]["firewall/zone"],
        "legacy/v2/firewall/zone-matrix": fx["legacy_v2"]["firewall/zone-matrix"],
        "legacy/rest/portforward": fx["legacy_rest"]["portforward"],
        "legacy/rest/wlanconf": fx["legacy_rest"]["wlanconf"],
    }
    return table[name]


@pytest.mark.parametrize("name", sorted(CONTRACT))
def test_the_fixture_has_every_field_of_the_contract(name):
    assert problems(name, fixture_records(name)) == [], f"extend tests/fixtures/controller.json for {name}"


def paths_read(tmp_path, monkeypatch):
    """{endpoint: field paths} the program reads from the fake controller across every command."""
    monkeypatch.chdir(tmp_path)
    seen = {}
    for argv in COMMANDS:
        session = TrackingSession()
        client = UniFiClient("https://controller", "key")
        client.session = session
        args = list(argv) + (["-o", str(tmp_path)] if argv[0] == "export" else [])
        with mock.patch.object(cli.UniFiClient, "from_config", classmethod(lambda cls, c, client=client: client)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
            monkeypatch.setenv("API_KEY", "key")
            if args[0] == "diff":
                cli.main(["snapshot", "--dir", str(tmp_path / "snaps")])
                cli.main(["diff", "--dir", str(tmp_path / "snaps")])
            else:
                cli.main(args)
        for endpoint, paths in session.accessed.items():
            seen.setdefault(endpoint, set()).update(p[3:] if p.startswith("[].") else p for p in paths if p != "[]")

    session = TrackingSession()
    session.events.append({
        "key": "SYNTHETIC_TITLE_ONLY",
        "event": "SYNTHETIC_TITLE_ONLY",
        "timestamp": session.events[0]["timestamp"] + 1,
        "category": "AUDIT",
        "severity": "LOW",
        "title_raw": "Synthetic title only",
    })
    conflict = next(e for e in session.events if e.get("event") == "CLIENT_IP_CONFLICT")
    conflict["parameters"]["CLIENTS"]["clients"][0].pop("name")
    client = UniFiClient("https://controller", "key")
    client.session = session
    with mock.patch.object(cli.UniFiClient, "from_config", classmethod(lambda cls, c: client)), \
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        cli.main(["events", "--since", "14d"])
        cli.main(["diagnose"])
    for endpoint, paths in session.accessed.items():
        seen.setdefault(endpoint, set()).update(p[3:] if p.startswith("[].") else p for p in paths if p != "[]")
    return seen


def test_the_contract_covers_every_field_the_code_reads(tmp_path, monkeypatch):
    seen = paths_read(tmp_path, monkeypatch)
    seen.pop("other//proxy/network/integration/v1/info", None)    # the version string is not part of any record
    assert set(seen) <= set(CONTRACT), f"endpoints read but not in the contract: {sorted(set(seen) - set(CONTRACT))}"
    for endpoint, paths in seen.items():
        unlisted = sorted(p for p in paths if not covered(endpoint, p))
        assert not unlisted, (f"the code reads {unlisted} from {endpoint}, which tests/contract.py does not list; "
                              "add each to always, somewhere or optional")


def test_the_contract_lists_nothing_the_code_never_reads(tmp_path, monkeypatch):
    """The other direction: a declared field that no command reads is a stale entry."""
    seen = paths_read(tmp_path, monkeypatch)
    stale = {}
    for name, spec in CONTRACT.items():
        missing = sorted(d for d in spec.declared() if not any(covered_by(d, r) for r in seen.get(name, ())))
        if missing:
            stale[name] = missing
    assert not stale, f"declared but never read by any command: {stale}"


def covered_by(declared: str, read: str) -> bool:
    """Is the declared field (or something under it) among the paths that were read?"""
    d, r = segments(declared), segments(read)
    return len(r) >= len(d) and all(x == y or x == "*" or y == "*" for x, y in zip(d, r, strict=False))


# -- the helpers themselves ----------------------------------------------------------------------------------------

def test_present_understands_lists_and_wildcards():
    record = {"uplink": {"speed": 1000}, "port_table": [{"up": True}, {"speed": 100}],
              "uptime_stats": {"WAN": {"monitors": [{"target": "x"}]}}, "empty": []}
    assert present(record, "uplink.speed") and not present(record, "uplink.max_speed")
    assert present(record, "port_table[].up") and present(record, "port_table[].speed")
    assert not present(record, "port_table[].full_duplex") and not present(record, "empty[].x")
    assert present(record, "uptime_stats.*.monitors[].target") and not present(record, "uptime_stats.*.availability")
    assert not present({"uplink": 5}, "uplink.speed")


def test_a_missing_field_is_reported_with_the_endpoint_and_the_field():
    records = [{"id": "a", "macAddress": "M", "name": "x", "state": "ONLINE", "model": "m"},
               {"id": "b", "macAddress": "N", "name": "y", "state": "ONLINE"}]
    found = problems("integration/devices", records)
    assert "integration/devices: 'model' is missing from 1 of 2 record(s)" in found
    assert "integration/devices: no record has 'ipAddress'" in found
    assert "integration/devices: no record has 'firmwareUpdatable'" in found


def test_removing_a_field_from_the_fixture_makes_the_check_fail():
    records = [dict(r) for r in fixture_records("legacy/stat/sta")]
    for record in records:
        record.pop("wifi_tx_retries_percentage", None)
    assert problems("legacy/stat/sta", records) == ["legacy/stat/sta: no record has 'wifi_tx_retries_percentage'"]


def test_no_records_is_reported_not_passed():
    assert problems("legacy/v2/speedtest", []) == ["legacy/v2/speedtest: no records to check"]


def test_covered_accepts_containers_and_wildcards_only():
    assert covered("legacy/stat/device", "uplink") and covered("legacy/stat/device", "port_table")
    assert covered("legacy/stat/device", "port_table[].up")
    assert covered("legacy/stat/health", "uptime_stats.WAN.availability")
    assert not covered("legacy/stat/device", "port_table[].brand_new_field")
    assert not covered("integration/clients", "ssid")


def test_every_endpoint_name_the_tracker_makes_is_known():
    assert endpoint_of("/proxy/network/integration/v1/sites") == "integration/sites"
    assert endpoint_of("/proxy/network/integration/v1/sites/s/devices") == "integration/devices"
    assert endpoint_of("/proxy/network/integration/v1/sites/s/devices/d1") == "integration/device"
    assert endpoint_of("/proxy/network/integration/v1/sites/s/devices/d1/statistics/latest") == "integration/device-statistics"
    assert endpoint_of("/proxy/network/api/s/default/stat/sta") == "legacy/stat/sta"
    assert endpoint_of("/proxy/network/v2/api/site/default/speedtest") == "legacy/v2/speedtest"
