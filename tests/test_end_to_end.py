import csv

from unifi_sentinel import cli
from unifi_sentinel.diagnose import diagnose
from unifi_sentinel.export import run_export
from unifi_sentinel.query import query_rows
from unifi_sentinel.snapshot import Needs, collect_snapshot


def read(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def test_snapshot_collects_everything(fake_client):
    snap = collect_snapshot(fake_client, "default", Needs(offline=True))
    assert snap.site["id"] == "site-1"
    assert len(snap.devices) == 4 and len(snap.clients) == 2
    assert set(snap.device_details) == {"gw1", "sw1", "ap1", "ap2"}
    assert set(snap.device_stats) == {"gw1", "sw1", "ap1"}  # offline AP has none
    assert len(snap.legacy_devices) == 4 and len(snap.all_users) == 3


def test_export_writes_inventory_and_switch_csvs(fake_client, tmp_path):
    run_export(collect_snapshot(fake_client, "default", Needs(offline=True)), tmp_path)
    rows = {r["Name"]: r for r in read(tmp_path / "unifi_clients.csv")}

    assert rows["desktop"]["Switch"] == "Office Switch" and rows["desktop"]["Port"] == "3"
    assert rows["phone"]["Connection Type"] == "Wireless"
    assert rows["Gateway"]["Type"] == "Device - Dream Machine"
    assert rows["Office AP"]["Switch"] == "Office Switch" and rows["Office AP"]["Port"] == "2"
    assert rows["Garage AP"]["Status"] == "Offline"
    assert rows["old-printer"]["Status"] == "Offline"
    assert rows["old-printer"]["Switch"] == "Office Switch" and rows["old-printer"]["Port"] == "6"
    assert rows["old-tablet"]["Connection Type"] == "Wireless"
    # the connected desktop is not duplicated as an offline client
    assert sum(1 for r in read(tmp_path / "unifi_clients.csv") if r["Name"] == "desktop") == 1

    ports = {p["Port Index"]: p for p in read(tmp_path / "switch_Office Switch.csv")}
    assert ports["1"]["Connected Name"] == "Gateway"      # uplink port
    assert ports["2"]["Connected Name"] == "Office AP"    # device via uplink
    assert ports["3"]["Connected Name"] == "desktop"      # client
    assert ports["4"]["Status"] == "Down" and ports["4"]["Connected Name"] == ""


def test_query_filters_and_json(fake_client):
    snap = collect_snapshot(fake_client, "default")
    assert {r["Name"] for r in query_rows(snap, "devices")} == {
        "Gateway", "Office Switch", "Office AP", "Garage AP"}
    assert [r["Name"] for r in query_rows(snap, "clients", search="PHONE")] == ["phone"]
    full = collect_snapshot(fake_client, "default", Needs(offline=True))
    assert len(query_rows(full, "clients", include_offline=True)) == 4


def test_diagnose_reports_fixture_problems(fake_client):
    msgs = {(f.severity, f.subject, f.message) for f in diagnose(collect_snapshot(fake_client, "default"))}
    assert ("warning", "Garage AP", "device is offline") in msgs
    assert ("warning", "Office Switch", "CPU utilization 95%") in msgs
    assert ("warning", "Office Switch port 2", "4 rx/tx errors") in msgs
    assert ("warning", "Office Switch port 2", "link is half duplex") in msgs
    assert ("info", "Office Switch port 2", "negotiated at 100 Mbps") in msgs


def test_cli_end_to_end(fake_client, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, cfg: fake_client))

    assert cli.main(["info"]) == 0
    assert "Site: Default" in capsys.readouterr().out
    assert cli.main(["diagnose"]) == 1  # the fixture has warnings
    assert "Garage AP" in capsys.readouterr().out
    assert cli.main(["export", "-o", str(tmp_path)]) == 0
    assert (tmp_path / "unifi_clients.csv").exists()


def test_cli_reports_missing_config(capsys):
    # no patching needed: the autouse fixture runs every test in an empty directory
    assert cli.main(["info"]) == cli.EXIT_ERROR
    assert "CONTROLLER_URL" in capsys.readouterr().err


def test_reservations_include_offline_and_skip_stale(fake_client):
    from unifi_sentinel.query import render
    from unifi_sentinel.reservations import build_reservations

    snap = collect_snapshot(fake_client, "default", Needs(reservations=True))
    rows = build_reservations(snap)

    assert [r["Reserved IP"] for r in rows] == ["10.0.0.10", "10.0.0.50"]  # sorted, no stale .99
    desktop, printer = rows
    assert (desktop["Name"], desktop["Status"], desktop["Current IP"]) == ("desktop", "Online", "10.0.0.10")
    assert (desktop["Network"], desktop["VLAN"]) == ("Main", 1)
    # offline reservation, network from the override, not the last connection
    assert (printer["Name"], printer["Status"], printer["Current IP"]) == ("old-printer", "Offline", "")
    assert (printer["Network"], printer["VLAN"]) == ("IoT", 20)
    assert printer["Last Seen"] != ""

    assert [r["Name"] for r in query_rows(snap, "reservations", search="iot")] == ["old-printer"]
    assert "Reserved IP" in render(rows, False, "reservations")
    assert '"Reserved IP": "10.0.0.50"' in render(rows, True, "reservations")


def test_query_reservations_via_cli(fake_client, monkeypatch, capsys):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, cfg: fake_client))
    assert cli.main(["query", "reservations"]) == 0
    out = capsys.readouterr().out
    assert "old-printer" in out and "10.0.0.99" not in out and "2 row(s)" in out


def test_query_ports_rows_and_filters(fake_client):
    from unifi_sentinel.query import render

    snap = collect_snapshot(fake_client, "default")
    ports = query_rows(snap, "ports")
    assert [(r["Switch"], r["Port Index"]) for r in ports] == [("Office Switch", i) for i in (1, 2, 3, 4)]
    assert next(r for r in ports if r["Port Index"] == 3)["Connected Name"] == "desktop"

    assert [r["Port Index"] for r in query_rows(snap, "ports", down=True)] == [4]
    assert [r["Port Index"] for r in query_rows(snap, "ports", errors=True)] == [2]
    assert len(query_rows(snap, "ports", switch="OFFICE")) == 4
    assert query_rows(snap, "ports", switch="nope") == []
    assert [r["Port Index"] for r in query_rows(snap, "ports", search="desktop")] == [3]
    assert query_rows(snap, "ports", down=True, errors=True) == []  # filters combine with AND

    table = render(ports, False, "ports")
    assert "Connected Name" in table and "4 row(s)" in table
    assert '"RX Bytes"' in render(ports, True, "ports")  # JSON has every column


def test_duplicate_switch_names_keep_all_ports_and_csvs(fake_client, tmp_path):
    snap = collect_snapshot(fake_client, "default")
    snap.legacy_devices = [*snap.legacy_devices, {
        "mac": "aa:00:00:00:00:09", "type": "usw", "name": "Office Switch",
        "port_table": [{"port_idx": 9, "up": True}],
    }]

    ports = query_rows(snap, "ports")
    assert [(row["Switch"], row["Port Index"]) for row in ports][-1] == ("Office Switch", 9)
    assert sum(row["Switch"] == "Office Switch" for row in ports) == 5

    run_export(snap, tmp_path)
    switch_csvs = sorted(tmp_path.glob("switch_Office Switch_*.csv"))
    assert len(switch_csvs) == 2
    assert sorted(row["Port Index"] for path in switch_csvs for row in read(path)) == [
        "1", "2", "3", "4", "9"
    ]


def test_query_ports_cli_and_flag_validation(fake_client, monkeypatch, capsys):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, cfg: fake_client))

    assert cli.main(["query", "ports", "--errors", "--switch", "office"]) == 0
    assert "1 row(s)" in capsys.readouterr().out

    assert cli.main(["query", "ports", "--switch", ""]) == 0
    assert "4 row(s)" in capsys.readouterr().out

    import pytest
    with pytest.raises(SystemExit) as exc:
        cli.main(["query", "clients", "--down"])
    assert exc.value.code != 0
    assert "only apply to 'query ports'" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        cli.main(["query", "clients", "--switch", ""])
    assert exc.value.code != 0
    assert "only apply to 'query ports'" in capsys.readouterr().err


def test_query_devices_shows_firmware_update_and_uptime(fake_client):
    from unifi_sentinel.query import format_uptime, render

    snap = collect_snapshot(fake_client, "default")
    rows = {r["Name"]: r for r in query_rows(snap, "devices")}

    assert rows["Gateway"]["Firmware"] == "5.0.0"
    assert (rows["Gateway"]["Update Available"], rows["Gateway"]["Uptime"]) == ("No", "2d 7h")
    assert (rows["Office Switch"]["Update Available"], rows["Office Switch"]["Uptime"]) == ("Yes", "3h 12m")
    assert rows["Office AP"]["Uptime"] == "5m"
    # offline device has no statistics; update flag unknown
    assert (rows["Garage AP"]["Uptime"], rows["Garage AP"]["Uptime (s)"]) == ("", "")
    assert rows["Garage AP"]["Update Available"] == ""

    table = render(list(rows.values()), False, "devices")
    assert "Firmware" in table and "Update Available" in table and "2d 7h" in table
    assert "Uptime (s)" not in table  # numeric column is JSON-only
    js = render(list(rows.values()), True, "devices")
    assert '"Uptime (s)": 200000' in js and '"Firmware": "7.0.0"' in js

    # clients and the mixed view are unchanged
    assert "Firmware" not in render(query_rows(snap, "all"), False, "all")
    assert "Firmware" not in render(query_rows(snap, "clients"), True, "clients")
    assert [format_uptime(x) for x in (None, -1, 45, 59, 60, 3599, 3600, 90000)] == [
        "", "", "45s", "59s", "1m", "59m", "1h 0m", "1d 1h"]


def test_export_csv_columns_unchanged(fake_client, tmp_path):
    run_export(collect_snapshot(fake_client, "default"), tmp_path)
    assert list(read(tmp_path / "unifi_clients.csv")[0]) == [
        "Type", "Name", "MAC Address", "IP Address", "Model", "Connection Type",
        "Switch", "Port", "Last Seen", "Status"]


def test_diagnose_flags_fixture_reservation_outside_subnet(fake_client, monkeypatch, capsys):
    snap = collect_snapshot(fake_client, "default", Needs(reservations=True))
    msgs = {(f.subject, f.message) for f in diagnose(snap)}
    # old-printer is reserved 10.0.0.50 but overridden onto IoT (10.0.20.0/24)
    assert ("old-printer", "reserved IP 10.0.0.50 is outside network IoT (10.0.20.1/24)") in msgs
    # the connected desktop matches its reservation: no mismatch finding
    assert not any(s == "desktop" for s, _ in msgs)

    monkeypatch.setenv("CONTROLLER_URL", "https://controller")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    assert cli.main(["diagnose", "--no-emoji"]) == 1
    assert "old-printer: reserved IP 10.0.0.50 is outside network IoT" in capsys.readouterr().out
