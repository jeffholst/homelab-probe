import csv

from unifi_sentinel import cli
from unifi_sentinel.diagnose import diagnose
from unifi_sentinel.export import run_export
from unifi_sentinel.query import query_rows
from unifi_sentinel.snapshot import collect_snapshot


def read(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def test_snapshot_collects_everything(fake_client):
    snap = collect_snapshot(fake_client, "default", include_offline=True)
    assert snap.site["id"] == "site-1"
    assert len(snap.devices) == 4 and len(snap.clients) == 2
    assert set(snap.device_details) == {"gw1", "sw1", "ap1", "ap2"}
    assert set(snap.device_stats) == {"gw1", "sw1", "ap1"}  # offline AP has none
    assert len(snap.legacy_devices) == 4 and len(snap.all_users) == 3


def test_export_writes_inventory_and_switch_csvs(fake_client, tmp_path):
    run_export(collect_snapshot(fake_client, "default", True), tmp_path)
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
    full = collect_snapshot(fake_client, "default", include_offline=True)
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


def test_cli_reports_missing_config(monkeypatch, capsys):
    monkeypatch.delenv("CONTROLLER_URL", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setattr("unifi_sentinel.config.load_dotenv", lambda: None)
    assert cli.main(["info"]) == cli.EXIT_ERROR
    assert "CONTROLLER_URL" in capsys.readouterr().err


def test_reservations_include_offline_and_skip_stale(fake_client):
    from unifi_sentinel.query import render
    from unifi_sentinel.reservations import build_reservations

    snap = collect_snapshot(fake_client, "default", include_reservations=True)
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
