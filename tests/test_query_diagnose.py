from unifi_sentinel.diagnose import diagnose
from unifi_sentinel.query import query_rows
from unifi_sentinel.snapshot import Snapshot


def make_snapshot():
    return Snapshot(
        site={"id": "s"},
        devices=[
            {"id": "d1", "macAddress": "aa:aa", "name": "SW", "model": "USW Ultra",
             "state": "ONLINE", "ipAddress": "10.0.0.2"},
            {"id": "d2", "macAddress": "bb:bb", "name": "AP", "model": "U7 Pro",
             "state": "OFFLINE", "ipAddress": "10.0.0.3"},
        ],
        clients=[{"macAddress": "cc:cc", "name": "laptop", "type": "WIRELESS",
                  "ipAddress": "10.0.0.9"}],
        legacy_devices=[{"mac": "aa:aa", "type": "usw", "name": "SW", "port_table": [
            {"port_idx": 1, "up": True, "speed": 1000, "full_duplex": True},
            {"port_idx": 2, "up": True, "speed": 100, "full_duplex": False, "rx_errors": 3},
            {"port_idx": 3, "up": False, "rx_errors": 99},
        ]}],
    )


def test_query_kind_and_search():
    snap = make_snapshot()
    assert len(query_rows(snap, "devices")) == 2
    assert [r["Name"] for r in query_rows(snap, "clients")] == ["laptop"]
    assert [r["Name"] for r in query_rows(snap, "all", search="u7")] == ["AP"]


def test_diagnose_findings():
    findings = diagnose(make_snapshot())
    messages = {(f.severity, f.subject, f.message) for f in findings}
    assert ("warning", "AP", "device is offline") in messages
    assert ("warning", "SW port 2", "3 rx/tx errors") in messages
    assert ("warning", "SW port 2", "link is half duplex") in messages
    assert ("info", "SW port 2", "negotiated at 100 Mbps") in messages
    # down ports are ignored
    assert not any(f.subject == "SW port 3" for f in findings)
    # warnings sort before info
    assert findings[0].severity == "warning"


def test_diagnose_high_resource_use():
    snap = make_snapshot()
    snap.device_stats = {"d1": {"cpuUtilizationPct": 95.0, "memoryUtilizationPct": 50.0}}
    msgs = [(f.subject, f.message) for f in diagnose(snap)]
    assert ("SW", "CPU utilization 95%") in msgs
    assert not any("memory" in m for _, m in msgs)


def test_inventory_uses_integration_uplink_and_heartbeat():
    from unifi_sentinel.export import build_inventory
    devices = [{"id": "gw", "macAddress": "aa:aa", "name": "GW", "model": "UCG Max"},
               {"id": "sw", "macAddress": "bb:bb", "name": "SW", "model": "USW Ultra",
                "state": "ONLINE"}]
    rows = build_inventory(
        devices, [], [], [],
        device_details={"sw": {"uplink": {"deviceId": "gw"}}},
        device_stats={"sw": {"lastHeartbeatAt": "2026-01-01T10:00:00Z"}},
    )
    sw = next(r for r in rows if r["Name"] == "SW")
    assert sw["Switch"] == "GW"
    assert sw["Last Seen"].startswith("2026-01-0")


def test_switch_uplink_port_names_upstream_device():
    from unifi_sentinel.export import build_switch_ports
    legacy = [
        {"mac": "aa:aa", "type": "udm", "name": "GW", "model": "UCG Max"},
        {"mac": "bb:bb", "type": "usw", "name": "SW",
         "uplink": {"uplink_mac": "aa:aa", "uplink_remote_port": 2},
         "port_table": [{"port_idx": 8, "up": True, "is_uplink": True}]},
    ]
    row = build_switch_ports(legacy, [])["SW"][0]
    assert row["Connected Name"] == "GW"
    assert row["Connected Type"] == "Device - Dream Machine"


def test_ssl_error_message_is_actionable(monkeypatch):
    import pytest
    import requests
    from unifi_sentinel.client import UniFiAPIError, UniFiClient

    client = UniFiClient("https://x", "key", verify_ssl=True)

    def boom(*a, **k):
        raise requests.exceptions.SSLError("bad cert")

    monkeypatch.setattr(client.session, "get", boom)
    with pytest.raises(UniFiAPIError, match="VERIFY_SSL=false"):
        client.info()
