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
