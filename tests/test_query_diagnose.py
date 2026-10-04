import pytest

from homelab_probe.diagnose import diagnose
from homelab_probe.query import query_rows
from homelab_probe.snapshot import Snapshot


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
    from homelab_probe.export import build_inventory
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
    from homelab_probe.export import build_switch_ports
    legacy = [
        {"mac": "aa:aa", "type": "udm", "name": "GW", "model": "UCG Max"},
        {"mac": "bb:bb", "type": "usw", "name": "SW",
         "uplink": {"uplink_mac": "aa:aa", "uplink_remote_port": 2},
         "port_table": [{"port_idx": 8, "up": True, "is_uplink": True}]},
    ]
    row = build_switch_ports(legacy, [])["BB:BB"][1][0]
    assert row["Connected Name"] == "GW"
    assert row["Connected Type"] == "Device - Dream Machine"


def test_ssl_error_message_is_actionable(monkeypatch):
    import pytest
    import requests

    from homelab_probe.client import UniFiAPIError, UniFiClient

    client = UniFiClient("https://x", "key", verify_ssl=True)

    def boom(*a, **k):
        raise requests.exceptions.SSLError("bad cert")

    monkeypatch.setattr(client.session, "get", boom)
    with pytest.raises(UniFiAPIError, match="UNIFI_VERIFY_SSL=false"):
        client.info()


def _fleet(gw_state="ONLINE", sw_state="ONLINE"):
    return Snapshot(
        site={"id": "s"},
        devices=[
            {"id": "gw", "macAddress": "aa:01", "name": "GW", "model": "UCG Max", "state": gw_state},
            {"id": "sw", "macAddress": "aa:02", "name": "SW", "model": "USW Ultra", "state": sw_state},
            {"id": "ap", "macAddress": "aa:03", "name": "AP", "model": "U7 Pro", "state": "OFFLINE"},
        ],
        clients=[],
        device_details={"sw": {"uplink": {"deviceId": "gw"}}, "ap": {"uplink": {"deviceId": "sw"}}},
        legacy_devices=[{"mac": "aa:01", "type": "udm"}],
    )


def test_offline_gateway_is_critical_and_sorts_first():
    findings = diagnose(_fleet(gw_state="OFFLINE"))
    assert findings[0].severity == "critical"
    assert (findings[0].subject, findings[0].message) == ("GW", "device is offline (gateway)")
    assert [f.severity for f in findings] == sorted(
        (f.severity for f in findings), key=["critical", "warning", "info"].index)


def test_offline_uplink_switch_is_critical_but_leaf_is_warning():
    findings = {(f.severity, f.subject): f.message for f in diagnose(_fleet(sw_state="OFFLINE"))}
    assert findings[("critical", "SW")] == "device is offline (1 device(s) uplink through it)"
    assert ("warning", "AP") in findings  # AP has no downstream devices


def test_resource_use_thresholds():
    snap = _fleet()
    snap.device_stats = {"gw": {"cpuUtilizationPct": 99.0, "memoryUtilizationPct": 92.0}}
    got = {(f.severity, f.message) for f in diagnose(snap) if f.subject == "GW"}
    assert got == {("critical", "CPU utilization 99%"), ("warning", "memory utilization 92%")}


def test_format_findings_emoji_and_text():
    from homelab_probe.diagnose import Finding, format_findings, stream_supports_emoji

    findings = [Finding("critical", "GW", "down"), Finding("warning", "A", "x"),
                Finding("warning", "B", "y"), Finding("info", "C", "z")]
    emoji = format_findings(findings)
    assert emoji.splitlines()[0].startswith("\U0001F6D1 GW: down")
    assert emoji.endswith("\U0001F6D1 1 critical, ⚠️ 2 warnings, ℹ️ 1 info")

    text = format_findings(findings, emoji=False)
    assert text.splitlines()[0] == "[CRITICAL] GW: down"
    assert text.endswith("1 critical, 2 warnings, 1 info")
    # zero counts are omitted
    assert format_findings([Finding("info", "C", "z")], emoji=False).endswith("1 info")
    assert format_findings([]) == "No issues found."

    class Tty:
        encoding = "UTF-8"
        def isatty(self): return True
    class Pipe(Tty):
        def isatty(self): return False
    assert stream_supports_emoji(Tty()) and not stream_supports_emoji(Pipe())


def test_exit_code_by_severity_and_threshold():
    from homelab_probe.diagnose import Finding, exit_code

    crit, warn, info = Finding("critical", "a", "x"), Finding("warning", "b", "y"), Finding("info", "c", "z")
    assert exit_code([]) == 0
    assert exit_code([info]) == 0
    assert exit_code([info, warn]) == 1
    assert exit_code([warn, crit]) == 2
    # --fail-on threshold
    assert exit_code([info], "info") == 1
    assert exit_code([warn], "critical") == 0
    assert exit_code([warn, info], "critical") == 0
    assert exit_code([crit], "critical") == 2
    assert exit_code([crit, warn], "info") == 2


def _run_cli(fake_client, monkeypatch, argv):
    from homelab_probe import cli
    fake_client.session.fx["legacy"]["device"][0]["overheating"] = False
    monkeypatch.setenv("UNIFI_URL", "https://controller")
    monkeypatch.setenv("UNIFI_API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, cfg: fake_client))
    return cli.main(argv)


def test_cli_diagnose_exit_codes(fake_client, monkeypatch):
    assert _run_cli(fake_client, monkeypatch, ["diagnose"]) == 1
    assert _run_cli(fake_client, monkeypatch, ["diagnose", "--fail-on", "critical"]) == 0
    assert _run_cli(fake_client, monkeypatch, ["diagnose", "--fail-on", "info"]) == 1


def test_cli_critical_exits_2(fake_client, monkeypatch):
    fake_client.session.fx = {**fake_client.session.fx, "devices": [
        {**d, "state": "OFFLINE"} if d["id"] == "gw1" else d
        for d in fake_client.session.fx["devices"]]}
    assert _run_cli(fake_client, monkeypatch, ["diagnose"]) == 2
    assert _run_cli(fake_client, monkeypatch, ["diagnose", "--fail-on", "critical"]) == 2


def test_cli_errors_and_usage_use_distinct_codes(fake_client, monkeypatch, capsys):
    from homelab_probe import cli
    fake_client.session.status = 500
    assert _run_cli(fake_client, monkeypatch, ["diagnose"]) == cli.EXIT_ERROR == 3
    assert _run_cli(fake_client, monkeypatch, ["info"]) == 3
    with pytest.raises(SystemExit) as exc:
        cli.main(["diagnose", "--fail-on", "bogus"])
    assert exc.value.code == cli.EXIT_USAGE == 64  # not 2, which means critical
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0


def test_null_uplink_in_device_detail_does_not_crash():
    from homelab_probe.export import build_inventory
    devices = [{"id": "sw", "macAddress": "bb:bb", "name": "SW", "model": "USW Ultra"}]
    rows = build_inventory(devices, [], [], [], device_details={"sw": {"uplink": None}})
    assert rows[0]["Switch"] == ""
    snap = Snapshot(site={}, devices=devices, clients=[], device_details={"sw": {"uplink": None}})
    assert isinstance(diagnose(snap), list)


def _client_snapshot(clients, legacy_clients=()):
    return Snapshot(
        site={"id": "s"},
        devices=[{"id": "sw", "macAddress": "aa:02", "name": "Office Switch", "state": "ONLINE"},
                 {"id": "ap", "macAddress": "aa:03", "name": "Office AP", "state": "ONLINE"}],
        clients=list(clients),
        legacy_clients=list(legacy_clients),
        legacy_devices=[{"mac": "aa:02", "type": "usw"}],
    )


def test_client_without_ip_is_flagged_with_location():
    snap = _client_snapshot(
        [{"macAddress": "cc:01", "name": "desktop", "type": "WIRED", "uplinkDeviceId": "sw"},
         {"macAddress": "cc:02", "name": "phone", "type": "WIRELESS", "uplinkDeviceId": "ap",
          "ipAddress": ""},
         {"macAddress": "cc:03", "type": "WIRELESS"},  # unnamed, unknown AP
         {"macAddress": "cc:04", "name": "healthy", "type": "WIRED", "ipAddress": "10.0.0.5"}],
        [{"mac": "cc:01", "sw_mac": "aa:02", "sw_port": 3}],
    )
    got = {(f.severity, f.subject): f.message for f in diagnose(snap)}
    assert got[("warning", "desktop")] == "no IP address (Wired, Office Switch port 3)"
    assert got[("warning", "phone")] == "no IP address (Wireless, via Office AP)"
    assert got[("warning", "cc:03")] == "no IP address (Wireless)"
    assert not any(subject == "healthy" for _, subject in got)


def test_link_local_client_is_flagged():
    snap = _client_snapshot([{"macAddress": "cc:01", "name": "printer", "type": "WIRED",
                              "ipAddress": "169.254.10.20", "uplinkDeviceId": "sw"}])
    (finding,) = [f for f in diagnose(snap) if f.subject == "printer"]
    assert finding.severity == "warning"
    assert finding.message == "link-local address 169.254.10.20, DHCP probably failed (Wired, Office Switch)"


def test_clients_with_valid_ips_add_no_findings():
    snap = _client_snapshot([{"macAddress": "cc:01", "name": "ok", "type": "WIRED",
                              "ipAddress": "10.0.0.9"}])
    assert diagnose(snap) == []


def _reservation_snapshot(users, clients=(), networks=None):
    nets = networks if networks is not None else [
        {"_id": "n1", "name": "Main", "ip_subnet": "10.0.0.1/24"},
        {"_id": "n2", "name": "IoT", "ip_subnet": "10.0.20.1/24"}]
    return Snapshot(site={"id": "s"}, devices=[], clients=list(clients),
                    all_users=list(users), networks=nets,
                    legacy_devices=[{"mac": "aa:02", "type": "usw"}])


def _user(mac, name, ip, net="n1", **kw):
    return {"mac": mac, "name": name, "use_fixedip": True, "fixed_ip": ip,
            "last_connection_network_id": net, **kw}


def _reservation_messages(snap):
    return {(f.subject, f.message) for f in diagnose(snap) if f.severity == "warning"}


def test_reservation_ip_mismatch_is_flagged_for_online_clients_only():
    snap = _reservation_snapshot(
        [_user("cc:01", "nas", "10.0.0.5"), _user("cc:02", "ok", "10.0.0.6"),
         _user("cc:03", "away", "10.0.0.7")],
        clients=[{"macAddress": "cc:01", "name": "nas", "type": "WIRED", "ipAddress": "10.0.0.99"},
                 {"macAddress": "cc:02", "name": "ok", "type": "WIRED", "ipAddress": "10.0.0.6"}])
    assert _reservation_messages(snap) == {
        ("nas", "current IP 10.0.0.99 differs from its reservation 10.0.0.5")}


def test_duplicate_reserved_ips_are_flagged_once_per_ip():
    snap = _reservation_snapshot([_user("cc:01", "b", "10.0.0.5"), _user("cc:02", "a", "10.0.0.5"),
                                  _user("cc:03", "c", "10.0.0.6")])
    assert _reservation_messages(snap) == {("10.0.0.5", "reserved for 2 clients: a, b")}


def test_reserved_ip_outside_network_subnet_is_flagged():
    snap = _reservation_snapshot([
        _user("cc:01", "sensor", "10.0.0.50", net="n2"),       # IoT is 10.0.20.0/24
        _user("cc:02", "fine", "10.0.20.50", net="n2"),
        _user("cc:03", "lost", "172.16.0.5", net="unknown"),   # network unresolved: skipped
    ])
    assert _reservation_messages(snap) == {
        ("sensor", "reserved IP 10.0.0.50 is outside network IoT (10.0.20.1/24)")}


def test_disabled_and_invalid_reservations_are_ignored():
    stale = {**_user("cc:01", "stale", "10.9.9.9"), "use_fixedip": False}
    snap = _reservation_snapshot(
        [stale, _user("cc:02", "weird", "not-an-ip")],
        networks=[{"_id": "n1", "name": "Main", "ip_subnet": "10.0.0.1/24"}])
    assert _reservation_messages(snap) == set()  # unparsable IP is skipped, not a crash
    assert diagnose(_reservation_snapshot([])) == []


def _ip_snapshot(clients=(), devices=(), users=(), legacy_clients=()):
    return Snapshot(
        site={"id": "s"}, devices=list(devices), clients=list(clients), all_users=list(users),
        legacy_clients=list(legacy_clients), legacy_devices=[{"mac": "aa:02", "type": "usw"}],
        networks=[{"_id": "n1", "name": "Main", "ip_subnet": "10.0.0.1/24"}])


def _client(mac, name, ip, kind="WIRED", **kw):
    return {"macAddress": mac, "name": name, "type": kind, "ipAddress": ip, **kw}


def _ip_findings(snap):
    return {(f.subject, f.message) for f in diagnose(snap)
            if "in use by" in f.message}


def test_two_clients_sharing_an_ip_are_flagged_with_locations():
    snap = _ip_snapshot(
        clients=[_client("cc:01", "desktop", "10.0.0.50", uplinkDeviceId="sw"),
                 _client("cc:02", "printer", "10.0.0.50", kind="WIRELESS", uplinkDeviceId="ap"),
                 _client("cc:03", "unique", "10.0.0.51")],
        devices=[{"id": "sw", "macAddress": "aa:02", "name": "Office Switch", "ipAddress": "10.0.0.2"},
                 {"id": "ap", "macAddress": "aa:03", "name": "Office AP", "ipAddress": "10.0.0.3"}],
        legacy_clients=[{"mac": "cc:01", "sw_mac": "aa:02", "sw_port": 3}])
    assert _ip_findings(snap) == {(
        "10.0.0.50", "in use by desktop (Wired, Office Switch port 3), "
                     "printer (Wireless, via Office AP)")}


def test_client_colliding_with_a_unifi_device_is_flagged():
    snap = _ip_snapshot(
        clients=[_client("cc:01", "laptop", "10.0.0.2")],
        devices=[{"id": "sw", "macAddress": "aa:02", "name": "Office Switch",
                  "state": "ONLINE", "ipAddress": "10.0.0.2"}])
    ((subject, message),) = _ip_findings(snap)
    assert subject == "10.0.0.2"
    assert "Office Switch (UniFi device)" in message and "laptop (Wired)" in message


def test_offline_device_ip_is_not_counted_as_a_holder():
    snap = _ip_snapshot(
        devices=[{"id": "ap", "macAddress": "aa:03", "name": "Office AP",
                  "state": "OFFLINE", "ipAddress": "10.0.0.7"}],
        users=[_user("cc:01", "owner", "10.0.0.7")])
    assert _ip_findings(snap) == set()


def test_reservation_whose_ip_is_used_by_another_client_is_flagged():
    snap = _ip_snapshot(
        clients=[_client("cc:09", "intruder", "10.0.0.7")],
        users=[_user("cc:01", "owner", "10.0.0.7")])  # owner offline / elsewhere
    assert _ip_findings(snap) == {
        ("owner", "reserved IP 10.0.0.7 is in use by intruder (Wired)")}


def test_owner_using_its_own_reserved_ip_is_not_flagged():
    snap = _ip_snapshot(clients=[_client("cc:01", "owner", "10.0.0.7")],
                        users=[_user("cc:01", "owner", "10.0.0.7")])
    assert _ip_findings(snap) == set()


def test_owner_and_intruder_share_the_ip_reported_once_as_duplicate():
    snap = _ip_snapshot(
        clients=[_client("cc:01", "owner", "10.0.0.7"), _client("cc:09", "intruder", "10.0.0.7")],
        users=[_user("cc:01", "owner", "10.0.0.7")])
    found = _ip_findings(snap)
    assert [s for s, _ in found] == ["10.0.0.7"]  # duplicate finding only, no reservation finding


def test_empty_invalid_and_equivalent_ips():
    snap = _ip_snapshot(clients=[
        _client("cc:01", "a", ""), _client("cc:02", "b", ""),
        _client("cc:03", "c", "not-an-ip"), _client("cc:04", "d", "not-an-ip"),
        _client("cc:05", "e", "10.0.0.9"), _client("cc:06", "f", " 10.0.0.9 ")])
    assert [s for s, _ in _ip_findings(snap)] == ["10.0.0.9"]  # only the real, normalized duplicate
