"""The diagnose finding for a new device: a client the controller first saw within new_client_window_hours."""

import json
from pathlib import Path

import pytest

from homelab_probe import cli
from homelab_probe.client import UniFiAPIError
from homelab_probe.diagnose import _new_client_findings, diagnose
from homelab_probe.diagnose.areas import needs_for
from homelab_probe.diagnose.model import CODES, INFO
from homelab_probe.notify import plan
from homelab_probe.settings import ConfigError, DiagnoseSettings, load_settings
from homelab_probe.snapshot import Snapshot

NOW = 2_000_000.0
HOUR = 3600


def snap(*users, devices=(), clients=()):
    return Snapshot(site={"id": "s"}, devices=list(devices), clients=list(clients), all_users=list(users))


def user(mac, name, age_hours=None, **kw):
    record = {"mac": mac, "name": name, **kw}
    if age_hours is not None:
        record["first_seen"] = NOW - age_hours * HOUR
    return record


def found(snapshot, **settings):
    return _new_client_findings(snapshot, DiagnoseSettings(**settings), NOW)


def test_a_client_first_seen_within_the_window_is_an_info_finding_named_by_its_mac():
    findings = found(snap(user("bb:00:00:00:00:01", "phone", 2, is_wired=False, last_ip="10.0.0.9"),
                          user("bb:00:00:00:00:02", "old", 30)))
    assert [(f.severity, f.subject, f.code, f.target_mac) for f in findings] == [
        (INFO, "BB:00:00:00:00:01", "client.new_device", "BB:00:00:00:00:01")]
    assert findings[0].message == "new device 'phone' first seen 2h ago (wireless, 10.0.0.9)"
    assert "client.new_device" in CODES


def test_the_window_is_new_client_window_hours_and_zero_turns_the_check_off():
    two = snap(user("bb:00:00:00:00:01", "phone", 2))
    assert found(two, new_client_window_hours=2) != [] and found(two, new_client_window_hours=1.5) == []
    assert found(two, new_client_window_hours=0) == []


def test_a_client_without_a_usable_first_seen_is_never_reported():
    users = [user(f"bb:00:00:00:00:0{i}", "x", None, first_seen=value)
             for i, value in enumerate([0, -1, "1999999", None, True], 1)] + [user("bb:00:00:00:00:09", "none")]
    assert found(snap(*users)) == []


def test_devices_and_a_blank_mac_are_not_clients():
    findings = found(snap(user("aa:00:00:00:00:01", "switch", 1), user("", "blank", 1),
                          devices=[{"macAddress": "AA-00-00-00-00-01"}]))
    assert findings == []


def test_the_message_shows_wired_private_mac_and_a_connected_clients_current_ip():
    live = [{"macAddress": "BE:00:00:00:00:07", "ipAddress": "10.0.0.77"}]
    findings = found(snap(user("be:00:00:00:00:07", None, 1, hostname="pixel", is_wired=True, last_ip="10.0.0.1"),
                          clients=live))
    assert findings[0].message == "new device 'pixel' first seen 1h ago (wired, private MAC, 10.0.0.77)"


def test_renaming_the_device_keeps_the_same_notification_identity():
    before = found(snap(user("bb:00:00:00:00:01", "Unknown-phone", 2)))
    after = found(snap(user("bb:00:00:00:00:01", "Alice's phone", 3)))
    state = plan(before, {"version": 1, "active": {}}, 1.0, "info")[1]
    events, _ = plan(after, state, 2.0, "info")
    assert events == []                                         # the same finding: nothing new to announce
    new, _ = plan(before, {"version": 1, "active": {}}, 1.0, "info")
    assert [e.kind for e in new] == ["new"]


def test_by_default_notify_does_not_announce_information_findings():
    assert plan(found(snap(user("bb:00:00:00:00:01", "phone", 2))), {"version": 1, "active": {}}, 1.0)[0] == []


def test_the_clients_area_reads_the_client_history_and_the_others_do_not():
    assert needs_for(["clients"], 3600).offline is True
    assert needs_for(["ports"], 3600).offline is False and needs_for(["wan"], 3600).offline is False


def test_diagnose_on_the_demo_network_reports_the_guest_phone(fake_client):
    from homelab_probe.snapshot import collect_snapshot
    snapshot = collect_snapshot(fake_client, "default", needs_for(["clients"], 3600))
    findings = [f for f in diagnose(snapshot, DiagnoseSettings(), areas=["clients"]) if f.code == "client.new_device"]
    assert [f.subject for f in findings] == ["BE:00:00:00:00:06"]


def test_an_unreadable_client_history_reports_nothing_and_warns(fake_client, monkeypatch, capsys):
    legacy_stat = fake_client.legacy_stat

    def fail(site_ref, resource):
        if resource == "alluser":
            raise UniFiAPIError("alluser unavailable")
        return legacy_stat(site_ref, resource)

    monkeypatch.setattr(fake_client, "legacy_stat", fail)
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, cfg: fake_client))
    monkeypatch.setenv("UNIFI_URL", "https://controller")
    monkeypatch.setenv("UNIFI_API_KEY", "key")
    cli.main(["diagnose", "--only", "clients", "--json"])
    captured = capsys.readouterr()
    assert "client.new_device" not in captured.out and "stat/alluser unavailable" in captured.err
    assert "new-device check (client.new_device) were skipped" in captured.err      # the warning says what was lost


def test_the_threshold_loads_validates_and_is_in_the_example_file():
    root = Path(__file__).resolve().parent.parent
    assert DiagnoseSettings().new_client_window_hours == 24
    assert load_settings(root / "hlp.example.toml").new_client_window_hours == 24

    def load(tmp_path, text):
        path = tmp_path / "hlp.toml"
        path.write_text(text)
        return load_settings(path)

    import tempfile
    with tempfile.TemporaryDirectory() as folder:
        tmp = Path(folder)
        assert load(tmp, "[thresholds]\nnew_client_window_hours = 6\n").new_client_window_hours == 6
        assert load(tmp, "[thresholds]\nnew_client_window_hours = 0\n").new_client_window_hours == 0
        for bad in ('"soon"', "-1", "true", "1e999", "100000"):
            with pytest.raises(ConfigError, match="new_client_window_hours"):
                load(tmp, f"[thresholds]\nnew_client_window_hours = {bad}\n")


def test_an_ignore_rule_for_the_code_silences_it(fake_client, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, cfg: fake_client))
    monkeypatch.setenv("UNIFI_URL", "https://controller")
    monkeypatch.setenv("UNIFI_API_KEY", "key")
    cfg = tmp_path / "hlp.toml"
    cfg.write_text('[[ignore]]\ncode = "client.new_device"\nreason = "guests are expected"\n')
    cli.main(["diagnose", "--only", "clients", "--json", "--config", str(cfg)])
    assert "client.new_device" not in [f["code"] for f in json.loads(capsys.readouterr().out)["findings"]]
