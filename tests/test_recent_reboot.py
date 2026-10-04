"""`diagnose`: `device.recent_reboot`, an online device restarted recently (issue #149)."""

import json
import math

import pytest

from unifi_sentinel import cli
from unifi_sentinel.config import ConfigError
from unifi_sentinel.diagnose import diagnose, needs_for
from unifi_sentinel.diagnose.devices import _recent_reboot_findings
from unifi_sentinel.settings import DiagnoseSettings, load_settings
from unifi_sentinel.snapshot import Snapshot, collect_snapshot

GATEWAY, SWITCH, AP, GARAGE = 0, 1, 2, 3                       # the order of the fixture's devices


def snapshot(*uptimes, state="ONLINE"):
    """One device per uptime, each online unless ``state`` says otherwise."""
    devices = [{"id": f"d{i}", "macAddress": f"aa:bb:cc:00:00:0{i}", "name": f"Device {i}", "state": state}
               for i in range(len(uptimes))]
    legacy = [{"mac": f"aa:bb:cc:00:00:0{i}", "name": f"legacy {i}", "uptime": up} for i, up in enumerate(uptimes)]
    return Snapshot(site={"id": "s"}, devices=devices, clients=[], legacy_devices=legacy)


def found(*uptimes, settings=None, **kwargs):
    return _recent_reboot_findings(snapshot(*uptimes, **kwargs), settings or DiagnoseSettings())


def run(fake_client, monkeypatch, capsys, *argv):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def reboots(fake_client, monkeypatch, capsys, *extra):
    _, out, _ = run(fake_client, monkeypatch, capsys, "diagnose", "--no-events", "--json", *extra)
    return [f for f in json.loads(out)["findings"] if f["code"] == "device.recent_reboot"]


def set_uptime(fake_client, index, value):
    record = fake_client.session.fx["legacy"]["device"][index]
    if value is None:
        record.pop("uptime", None)
    else:
        record["uptime"] = value


# -- the check -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("seconds, shown", [(0, "0s"), (45, "45s"), (300, "5m"), (3599, "59m"), (3540, "59m")])
def test_a_device_up_for_less_than_an_hour_is_an_information_finding(seconds, shown):
    (finding,) = found(seconds)
    assert (finding.severity, finding.subject, finding.message) == ("info", "Device 0", f"restarted {shown} ago")
    assert finding.target_mac == "AA:BB:CC:00:00:00" and finding.code == "device.recent_reboot"


@pytest.mark.parametrize("seconds", [3600, 3601, 7200, 86400, 2_000_000, 3600.0])
def test_a_device_up_for_the_threshold_or_longer_gives_nothing(seconds):
    assert found(seconds) == []                                              # at 60 minutes it is no longer recent


def test_the_boundary_follows_the_setting_in_minutes():
    settings = DiagnoseSettings(recent_reboot_minutes=10)
    assert [f.message for f in found(599, 600, 601, settings=settings)] == ["restarted 9m ago"]
    long = DiagnoseSettings(recent_reboot_minutes=180)
    assert [f.message for f in found(3 * 3600 - 1, 3 * 3600, settings=long)] == ["restarted 2h 59m ago"]
    assert found(1, settings=DiagnoseSettings(recent_reboot_minutes=1)) != []
    assert found(60, settings=DiagnoseSettings(recent_reboot_minutes=1)) == []


@pytest.mark.parametrize("value", [None, "300", "5m", True, False, [], {}, [300], -1, -0.5, math.nan, math.inf,
                                   -math.inf])
def test_an_uptime_that_is_not_a_finite_non_negative_number_gives_nothing(value):
    assert found(value) == []


def test_a_device_without_uptime_is_not_judged():
    snap = snapshot(300)
    del snap.legacy_devices[0]["uptime"]
    assert _recent_reboot_findings(snap, DiagnoseSettings()) == []


@pytest.mark.parametrize("state", ["OFFLINE", "UPDATING", "PENDING_ADOPTION", "ADOPTING", "", None, "online"])
def test_only_an_online_device_counts(state):
    assert found(300, state=state) == []


def test_every_recently_restarted_device_gets_its_own_finding_the_gateway_included():
    snap = snapshot(120, 99999, 600)
    snap.devices[0]["name"] = "Gateway"
    assert [(f.subject, f.message) for f in _recent_reboot_findings(snap, DiagnoseSettings())] == [
        ("Gateway", "restarted 2m ago"), ("Device 2", "restarted 10m ago")]


def test_a_legacy_device_the_integration_api_does_not_list_is_not_reported():
    snap = snapshot(300)
    snap.devices = []
    assert _recent_reboot_findings(snap, DiagnoseSettings()) == []
    snap = snapshot(300)
    snap.legacy_devices[0]["mac"] = "aa:bb:cc:00:00:99"
    assert _recent_reboot_findings(snap, DiagnoseSettings()) == []


def test_the_mac_is_joined_in_any_spelling():
    for spelling in ("AA-BB-CC-00-00-00", "aabb.cc00.0000", "AABBCC000000", " aa:bb:cc:00:00:00 "):
        snap = snapshot(300)
        snap.legacy_devices[0]["mac"] = spelling
        assert len(_recent_reboot_findings(snap, DiagnoseSettings())) == 1, spelling


def test_the_name_is_the_integration_apis_then_the_legacy_one_then_the_mac():
    snap = snapshot(300)
    assert _recent_reboot_findings(snap, DiagnoseSettings())[0].subject == "Device 0"
    snap.devices[0]["name"] = ""
    assert _recent_reboot_findings(snap, DiagnoseSettings())[0].subject == "legacy 0"
    snap.legacy_devices[0]["name"] = None
    assert _recent_reboot_findings(snap, DiagnoseSettings())[0].subject == "AA:BB:CC:00:00:00"


def test_malformed_legacy_records_are_skipped():
    snap = snapshot(300)
    snap.legacy_devices += [{}, {"uptime": 1}, {"mac": None, "uptime": 1}, {"mac": "", "uptime": 1}]
    assert len(_recent_reboot_findings(snap, DiagnoseSettings())) == 1


def test_a_snapshot_that_did_not_read_the_legacy_devices_does_not_crash():
    assert _recent_reboot_findings(Snapshot(site={"id": "s"}, devices=[], clients=[]), DiagnoseSettings()) == []


# -- in the command ---------------------------------------------------------------------------------------------

def test_the_fixture_has_one_recently_restarted_device(fake_client, monkeypatch, capsys):
    (finding,) = reboots(fake_client, monkeypatch, capsys)
    assert (finding["severity"], finding["subject"], finding["mac"]) == ("info", "Office AP", "AA:00:00:00:00:03")
    assert finding["message"] == "restarted 5m ago"


def test_it_is_information_only_so_it_never_changes_the_exit_code(fake_client, monkeypatch, capsys):
    with_it, _, _ = run(fake_client, monkeypatch, capsys, "diagnose", "--no-events", "--json")
    set_uptime(fake_client, AP, 999999)
    without_it, _, _ = run(fake_client, monkeypatch, capsys, "diagnose", "--no-events", "--json")
    assert with_it == without_it                                              # the rest of the fixture decides
    # on its own it passes --fail-on warning and fails --fail-on info
    only = ["diagnose", "--no-events", "--json", "--only", "devices", "--fail-on"]
    for device in (GATEWAY, SWITCH):
        fake_client.session.fx["legacy"]["device"][device]["overheating"] = False
    for entry in fake_client.session.fx["legacy"]["device"][GATEWAY]["storage"]:
        entry["used"] = 1
    fake_client.session.fx["devices"][GARAGE]["state"] = "ONLINE"
    fake_client.session.fx["device_stats"]["sw1"]["cpuUtilizationPct"] = 1
    set_uptime(fake_client, AP, 300)
    assert run(fake_client, monkeypatch, capsys, *only, "warning")[0] == 0
    assert run(fake_client, monkeypatch, capsys, *only, "info")[0] == 1


def test_the_settings_file_changes_the_window(fake_client, monkeypatch, capsys, tmp_path):
    config = tmp_path / "unifi-sentinel.toml"
    config.write_text("[thresholds]\nrecent_reboot_minutes = 4\n")                 # the AP is up for 5 minutes
    assert reboots(fake_client, monkeypatch, capsys, "--config", str(config)) == []
    config.write_text("[thresholds]\nrecent_reboot_minutes = 6\n")
    assert len(reboots(fake_client, monkeypatch, capsys, "--config", str(config))) == 1


def test_it_is_not_judged_for_a_device_that_goes_offline(fake_client, monkeypatch, capsys):
    fake_client.session.fx["devices"][AP]["state"] = "OFFLINE"
    assert reboots(fake_client, monkeypatch, capsys) == []


def test_it_runs_with_the_devices_area_and_only_there(fake_client, monkeypatch, capsys):
    for extra, expected in ((["--only", "devices"], 1), (["--skip", "ports", "--skip", "wifi"], 1),
                            (["--only", "ports"], 0), (["--skip", "devices"], 0), (["--only", "wan,wifi"], 0)):
        assert len(reboots(fake_client, monkeypatch, capsys, *extra)) == expected, extra


def test_reading_only_the_devices_area_gives_the_same_finding_as_a_full_read(fake_client):
    full = [f for f in diagnose(collect_snapshot(fake_client, "default", needs_for(None, 86400)))
            if f.code == "device.recent_reboot"]
    part = [f for f in diagnose(collect_snapshot(fake_client, "default", needs_for(["devices"], 86400)),
                                areas=["devices"]) if f.code == "device.recent_reboot"]
    assert full and part == full


def test_no_extra_request_is_made_for_it(fake_client, monkeypatch, capsys):
    run(fake_client, monkeypatch, capsys, "diagnose", "--no-events", "--only", "devices")
    with_it = sorted(fake_client.session.calls)
    fake_client.session.calls.clear()
    set_uptime(fake_client, AP, 999999)
    run(fake_client, monkeypatch, capsys, "diagnose", "--no-events", "--only", "devices")
    assert sorted(fake_client.session.calls) == with_it


def test_an_ignore_rule_by_code_or_by_name_silences_it(fake_client, monkeypatch, capsys, tmp_path):
    config = tmp_path / "unifi-sentinel.toml"
    for rule in ('code = "device.recent_reboot"', 'subject = "office ap"\nmessage = "restarted"',
                 'code = "device.recent_reboot"\nsubject = "Office*"'):
        config.write_text(f'[[ignore]]\n{rule}\nreason = "it was updated tonight"\n')
        assert reboots(fake_client, monkeypatch, capsys, "--config", str(config)) == [], rule
    config.write_text('[[ignore]]\ncode = "device.recent_reboot"\nsubject = "Garage AP"\nreason = "x"\n')
    assert len(reboots(fake_client, monkeypatch, capsys, "--config", str(config))) == 1


def test_it_is_sent_only_when_the_minimum_severity_is_information(fake_client, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("NOTIFY_NTFY_URL", "https://ntfy.example/topic-for-the-test")
    base = ["diagnose", "--no-events", "--json", "--notify", "--notify-dry-run", "--notify-state",
            str(tmp_path / "state.json")]
    _, _, default = run(fake_client, monkeypatch, capsys, *base)
    assert "restarted" not in default                                          # --notify-min defaults to warning
    _, _, info = run(fake_client, monkeypatch, capsys, *base, "--notify-min", "info")
    assert "[INFO] NEW  Office AP: restarted 5m ago" in info
    _, _, redacted = run(fake_client, monkeypatch, capsys, *base, "--notify-min", "info", "--notify-redact")
    assert "Office AP" not in redacted.split("[INFO]")[-1] and "has been up for less than" in redacted


def test_when_the_legacy_data_is_unreadable_there_is_no_finding_and_the_one_notice_stays(fake_client, monkeypatch,
                                                                                         capsys):
    from unifi_sentinel.client import UniFiAPIError
    legacy_stat = fake_client.legacy_stat

    def fail_device_read(site_ref, resource):
        if resource == "device":
            raise UniFiAPIError("HTTP 500 forced")
        return legacy_stat(site_ref, resource)

    monkeypatch.setattr(fake_client, "legacy_stat", fail_device_read)
    _, out, _ = run(fake_client, monkeypatch, capsys, "diagnose", "--no-events", "--json")
    findings = json.loads(out)["findings"]
    assert not [f for f in findings if f["code"] == "device.recent_reboot"]
    (notice,) = [f for f in findings if f["code"] == "controller.legacy_unavailable"]
    assert notice["message"] == (
        "legacy device data unavailable; port, overheating, storage and recent-reboot checks were skipped"
    )


def test_the_client_view_lists_it_for_a_device_on_the_clients_path(fake_client, monkeypatch, capsys):
    _, out, _ = run(fake_client, monkeypatch, capsys, "client", "phone", "--json", "--no-events")
    assert "device.recent_reboot" in out                                       # the phone is on the restarted AP


# -- the setting ---------------------------------------------------------------------------------------------------

def write(tmp_path, text):
    path = tmp_path / "unifi-sentinel.toml"
    path.write_text(text)
    return path


def test_the_default_and_the_example_file():
    from docs_support import ROOT
    assert DiagnoseSettings().recent_reboot_minutes == 60
    assert load_settings(ROOT / "unifi-sentinel.example.toml").recent_reboot_minutes == 60


@pytest.mark.parametrize("value, minutes", [("1", 1), ("60", 60), ("1440", 1440), ("90.0", 90)])
def test_a_whole_number_of_minutes_loads(tmp_path, value, minutes):
    settings = load_settings(write(tmp_path, f"[thresholds]\nrecent_reboot_minutes = {value}\n"))
    assert settings.recent_reboot_minutes == minutes and isinstance(settings.recent_reboot_minutes, int)


@pytest.mark.parametrize("value, message", [
    ("0", "must be at least 1|at least"), ("-5", "at least"), ("1.5", "must be a whole number"),
    ('"an hour"', "must be a number"), ("true", "must be a number"), ("nan", "must be finite"),
])
def test_a_bad_number_of_minutes_is_a_config_error_naming_the_setting(tmp_path, value, message):
    with pytest.raises(ConfigError, match=f"recent_reboot_minutes.*({message})|({message}).*recent_reboot_minutes"):
        load_settings(write(tmp_path, f"[thresholds]\nrecent_reboot_minutes = {value}\n"))
