"""`diagnose`: `device.storage`, a device's storage is nearly full (issue #148)."""

import json
import math

import pytest

from unifi_sentinel import cli
from unifi_sentinel.client import UniFiAPIError
from unifi_sentinel.config import ConfigError
from unifi_sentinel.diagnose import diagnose, needs_for
from unifi_sentinel.diagnose.devices import _storage_findings
from unifi_sentinel.settings import DiagnoseSettings, load_settings
from unifi_sentinel.snapshot import Snapshot, collect_snapshot

GATEWAY = 0


def snapshot(*entries, name="Gateway", legacy_name="legacy gateway", mac="aa:bb:cc:00:00:01"):
    devices = [{"id": "d1", "macAddress": mac, "name": name, "state": "ONLINE"}] if name is not None else []
    return Snapshot(site={"id": "s"}, devices=devices, clients=[],
                    legacy_devices=[{"mac": mac, "name": legacy_name, "storage": list(entries)}])


def entry(used, size=100, **extra):
    return {"name": "Backup", "mount_point": "/data", "size": size, "type": "eMMC", "used": used, **extra}


def found(*entries, settings=None, **kwargs):
    return _storage_findings(snapshot(*entries, **kwargs), settings or DiagnoseSettings())


def run(fake_client, monkeypatch, capsys, *argv):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def storage_findings(fake_client, monkeypatch, capsys, *extra):
    _, out, _ = run(fake_client, monkeypatch, capsys, "diagnose", "--no-events", "--json", *extra)
    return [f for f in json.loads(out)["findings"] if f["code"] == "device.storage"]


def fill(fake_client, index, used, size=4000000000):
    fake_client.session.fx["legacy"]["device"][GATEWAY]["storage"][index].update(used=used, size=size)


# -- the percentage and its thresholds ---------------------------------------------------------------------------

@pytest.mark.parametrize("used, severity", [
    (89, None), (89.99, None), (90, "warning"), (90.01, "warning"), (97, "warning"), (97.99, "warning"),
    (98, "critical"), (99, "critical"), (100, "critical"),
])
def test_the_boundaries_are_inclusive(used, severity):
    result = found(entry(used))
    assert [f.severity for f in result] == ([severity] if severity else [])


def test_the_message_names_the_percentage_and_the_finding_the_device_and_the_entry():
    (finding,) = found(entry(95))
    assert (finding.severity, finding.subject, finding.message) == ("warning", "Gateway Backup", "storage 95% used")
    assert finding.code == "device.storage" and finding.target_mac == "AA:BB:CC:00:00:01"


@pytest.mark.parametrize("used, shown", [(95, "95"), (97.5, "97.5"), (97.96, "97.9"), (90, "90"), (90.04, "90"),
                                         (90.06, "90"), (91.234, "91.2"), (100, "100"), (99.99, "99.9")])
def test_the_percentage_shows_one_decimal_cut_off_not_rounded(used, shown):
    settings = DiagnoseSettings(storage_warn_pct=90, storage_critical_pct=100)
    (finding,) = found(entry(used), settings=settings)
    assert finding.message == f"storage {shown}% used"


def test_a_large_storage_entry_one_byte_below_critical_stays_below_it():
    size = 4_000_000_000_000
    used = 3_919_999_999_999
    (finding,) = found(entry(used, size=size))
    assert finding.severity == "warning" and finding.message == "storage 97.9% used"


def test_a_figure_below_a_threshold_never_reads_as_that_threshold():
    (finding,) = found(entry(97.96))                                   # a warning: the critical level is 98
    assert finding.severity == "warning" and finding.message == "storage 97.9% used"


def test_the_unit_does_not_matter_only_the_ratio():
    for scale in (1, 1000, 1024 ** 3, 0.001):
        assert [f.severity for f in found(entry(95 * scale, size=100 * scale))] == ["warning"], scale
        assert found(entry(50 * scale, size=100 * scale)) == [], scale


def test_the_thresholds_come_from_the_settings():
    settings = DiagnoseSettings(storage_warn_pct=50, storage_critical_pct=60)
    assert [f.severity for f in found(entry(49), settings=settings)] == []
    assert [f.severity for f in found(entry(50), settings=settings)] == ["warning"]
    assert [f.severity for f in found(entry(60), settings=settings)] == ["critical"]
    assert [f.severity for f in found(entry(95), settings=DiagnoseSettings())] == ["warning"]       # the defaults


def test_equal_thresholds_make_the_warning_level_critical():
    settings = DiagnoseSettings(storage_warn_pct=80, storage_critical_pct=80)
    assert [f.severity for f in found(entry(80), settings=settings)] == ["critical"]


# -- entries whose numbers cannot be trusted ---------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    entry(95, size=0), entry(95, size=-100), entry(-1), entry(101), entry(100.0001),     # not sane
    {"name": "x", "used": 95}, {"name": "x", "size": 100},                              # missing
    entry(None), entry(95, size=None), entry("95"), entry(95, size="100"), entry(True), entry(95, size=True),
    entry(95, size=[100]), entry({"v": 95}), entry(math.nan), entry(95, size=math.nan), entry(math.inf),
    entry(95, size=math.inf), entry(math.inf, size=math.inf),
    {}, None, 5, "storage", [entry(95)], True,                                           # not even an entry
])
def test_an_entry_that_cannot_be_trusted_gives_no_finding_and_no_crash(bad):
    assert found(bad) == []


@pytest.mark.parametrize("storage", [None, [], {}, "full", 5, True, {"size": 100, "used": 99}])
def test_storage_that_is_not_a_list_of_entries_is_nothing_to_judge(storage):
    snap = snapshot()
    snap.legacy_devices[0]["storage"] = storage
    assert _storage_findings(snap, DiagnoseSettings()) == []


def test_a_device_without_storage_or_a_malformed_legacy_record_is_skipped():
    snap = snapshot()
    del snap.legacy_devices[0]["storage"]
    snap.legacy_devices += [{}, {"mac": None}, {"storage": [entry(99)]}]
    assert [f.subject for f in _storage_findings(snap, DiagnoseSettings())] == ["? Backup"]    # no name, no mac: a mark


def test_a_trusted_entry_is_still_judged_next_to_a_bad_one():
    result = found(entry(95, size=0), entry(99), {"junk": True}, entry(10))
    assert [(f.severity, f.message) for f in result] == [("critical", "storage 99% used")]


# -- several entries, names ----------------------------------------------------------------------------------------

def test_every_entry_that_crosses_a_threshold_gets_its_own_finding_in_order():
    result = found(entry(95, name="Backup"), entry(10, name="Idle"), entry(99, name="Temporary"))
    assert [(f.subject, f.severity) for f in result] == [("Gateway Backup", "warning"), ("Gateway Temporary", "critical")]


@pytest.mark.parametrize("label, expected", [
    ({"name": "Backup", "mount_point": "/data"}, "Backup"), ({"name": "", "mount_point": "/data"}, "/data"),
    ({"name": None, "mount_point": "/data"}, "/data"), ({"mount_point": "/data"}, "/data"),
    ({"name": "", "mount_point": ""}, "storage"), ({"name": None, "mount_point": None}, "storage"), ({}, "storage"),
    ({"name": 7}, "7"),
])
def test_the_entry_is_named_by_its_name_then_its_mount_point(label, expected):
    record = {"size": 100, "used": 95}
    record.update(label)
    assert found(record)[0].subject == f"Gateway {expected}"


def test_the_device_is_named_by_the_integration_api_then_the_legacy_record_then_the_mac():
    assert found(entry(95))[0].subject == "Gateway Backup"
    assert found(entry(95), name="")[0].subject == "legacy gateway Backup"
    assert found(entry(95), name=None)[0].subject == "legacy gateway Backup"
    assert found(entry(95), name=None, legacy_name=None)[0].subject == "AA:BB:CC:00:00:01 Backup"


def test_the_mac_is_joined_in_any_spelling():
    snap = snapshot(entry(95))
    snap.legacy_devices[0]["mac"] = "AA-BB-CC-00-00-01"
    assert _storage_findings(snap, DiagnoseSettings())[0].subject == "Gateway Backup"


def test_a_devices_storage_is_judged_whatever_its_state():
    """Unlike a flag, a disk does not empty itself while the device is offline, so its last numbers still count."""
    snap = snapshot(entry(95))
    snap.devices[0]["state"] = "OFFLINE"
    assert len(_storage_findings(snap, DiagnoseSettings())) == 1


# -- in the command ---------------------------------------------------------------------------------------------

def test_the_fixture_gateway_demonstrates_a_nearly_full_storage_entry(fake_client, monkeypatch, capsys):
    (finding,) = storage_findings(fake_client, monkeypatch, capsys)
    assert (finding["severity"], finding["subject"], finding["message"]) == (
        "warning", "Gateway Backup", "storage 97.5% used")
    entries = fake_client.session.fx["legacy"]["device"][GATEWAY]["storage"]
    assert [e.get("name") for e in entries] == ["Backup", "Temporary", None]


def test_a_nearly_full_entry_is_a_finding_in_json_and_text_with_the_exit_code(fake_client, monkeypatch, capsys):
    fill(fake_client, 0, 3800000000)                                   # 95%
    (finding,) = storage_findings(fake_client, monkeypatch, capsys)
    assert (finding["severity"], finding["subject"], finding["message"], finding["mac"]) == (
        "warning", "Gateway Backup", "storage 95% used", "AA:00:00:00:00:01")
    code, out, _ = run(fake_client, monkeypatch, capsys, "diagnose", "--no-events", "--no-emoji", "--only", "devices")
    assert "[WARNING ] Gateway Backup: storage 95% used" in out and code != 0


def test_a_critical_entry_and_the_nameless_entry(fake_client, monkeypatch, capsys):
    fill(fake_client, 1, 1990000000, 2000000000)                       # Temporary at 99.5%
    fill(fake_client, 2, 960000000, 1000000000)                        # the entry without a name: its mount point
    result = {f["subject"]: f["severity"] for f in storage_findings(fake_client, monkeypatch, capsys)}
    assert result == {
        "Gateway Backup": "warning",
        "Gateway Temporary": "critical",
        "Gateway /var/log": "warning",
    }


def test_the_thresholds_can_be_set_in_the_settings_file(fake_client, monkeypatch, capsys, tmp_path):
    config = tmp_path / "unifi-sentinel.toml"
    config.write_text("[thresholds]\nstorage_warn_pct = 5\nstorage_critical_pct = 6\n")
    result = storage_findings(fake_client, monkeypatch, capsys, "--config", str(config))
    assert [(f["subject"], f["severity"]) for f in result] == [("Gateway Backup", "critical")]      # 6.6% used


def test_it_runs_with_the_devices_area_and_only_there(fake_client, monkeypatch, capsys):
    fill(fake_client, 0, 3800000000)
    for extra, expected in ((["--only", "devices"], 1), (["--skip", "ports", "--skip", "wifi"], 1),
                            (["--only", "ports"], 0), (["--skip", "devices"], 0), (["--only", "wan,wifi"], 0)):
        assert len(storage_findings(fake_client, monkeypatch, capsys, *extra)) == expected, extra


def test_reading_only_the_devices_area_gives_the_same_finding_as_a_full_read(fake_client):
    fill(fake_client, 0, 3900000000)
    full = [f for f in diagnose(collect_snapshot(fake_client, "default", needs_for(None, 86400)))
            if f.code == "device.storage"]
    part = [f for f in diagnose(collect_snapshot(fake_client, "default", needs_for(["devices"], 86400)),
                                areas=["devices"]) if f.code == "device.storage"]
    assert full and part == full


def test_no_extra_request_is_made_for_it(fake_client, monkeypatch, capsys):
    run(fake_client, monkeypatch, capsys, "diagnose", "--no-events", "--only", "devices")
    plain = sorted(fake_client.session.calls)
    fake_client.session.calls.clear()
    fill(fake_client, 0, 3900000000)
    run(fake_client, monkeypatch, capsys, "diagnose", "--no-events", "--only", "devices")
    assert sorted(fake_client.session.calls) == plain


def test_an_ignore_rule_can_silence_one_entry_and_not_the_other(fake_client, monkeypatch, capsys, tmp_path):
    fill(fake_client, 0, 3900000000)
    fill(fake_client, 1, 1990000000, 2000000000)
    config = tmp_path / "unifi-sentinel.toml"
    config.write_text('[[ignore]]\ncode = "device.storage"\nsubject = "Gateway Temporary"\nreason = "meant to fill up"\n')
    result = storage_findings(fake_client, monkeypatch, capsys, "--config", str(config))
    assert [f["subject"] for f in result] == ["Gateway Backup"]
    config.write_text('[[ignore]]\ncode = "device.storage"\nreason = "all of them"\n')
    assert storage_findings(fake_client, monkeypatch, capsys, "--config", str(config)) == []


def test_it_is_sent_like_any_finding_and_redaction_keeps_the_names_out(fake_client, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("NOTIFY_NTFY_URL", "https://ntfy.example/topic-for-the-test")
    fill(fake_client, 0, 3900000000)
    argv = ["diagnose", "--no-events", "--json", "--notify", "--notify-dry-run", "--notify-state",
            str(tmp_path / "state.json")]
    _, _, err = run(fake_client, monkeypatch, capsys, *argv)
    assert "[WARNING] NEW  Gateway Backup: storage 97.5% used" in err
    _, _, redacted = run(fake_client, monkeypatch, capsys, *argv, "--notify-redact")
    assert "Backup" not in redacted.split("[CRITICAL]")[-1] and "storage" in redacted


def test_when_the_legacy_data_is_unreadable_there_is_one_notice_and_no_finding(fake_client, monkeypatch, capsys):
    fill(fake_client, 0, 3900000000)
    legacy_stat = fake_client.legacy_stat

    def fail_device_read(site_ref, resource):
        if resource == "device":
            raise UniFiAPIError("HTTP 500 forced")
        return legacy_stat(site_ref, resource)

    monkeypatch.setattr(fake_client, "legacy_stat", fail_device_read)
    code, out, _ = run(fake_client, monkeypatch, capsys, "diagnose", "--no-events", "--json")
    findings = json.loads(out)["findings"]
    assert not [f for f in findings if f["code"] == "device.storage"]
    (notice,) = [f for f in findings if f["code"] == "controller.legacy_unavailable"]
    assert "port, overheating and storage checks were skipped" in notice["message"]


# -- the settings --------------------------------------------------------------------------------------------------

def test_the_defaults_and_the_example_file():
    assert (DiagnoseSettings().storage_warn_pct, DiagnoseSettings().storage_critical_pct) == (90, 98)
    from docs_support import ROOT
    example = load_settings(ROOT / "unifi-sentinel.example.toml")
    assert (example.storage_warn_pct, example.storage_critical_pct) == (90, 98)


def write(tmp_path, text):
    path = tmp_path / "unifi-sentinel.toml"
    path.write_text(text)
    return path


def test_both_thresholds_load_and_either_may_be_left_out(tmp_path):
    both = load_settings(write(tmp_path, "[thresholds]\nstorage_warn_pct = 70\nstorage_critical_pct = 80\n"))
    assert (both.storage_warn_pct, both.storage_critical_pct) == (70, 80)
    only_warn = load_settings(write(tmp_path, "[thresholds]\nstorage_warn_pct = 95\n"))
    assert (only_warn.storage_warn_pct, only_warn.storage_critical_pct) == (95, 98)
    only_critical = load_settings(write(tmp_path, "[thresholds]\nstorage_critical_pct = 99.5\n"))
    assert (only_critical.storage_warn_pct, only_critical.storage_critical_pct) == (90, 99.5)


@pytest.mark.parametrize("text, message", [
    ("storage_warn_pct = 99\nstorage_critical_pct = 95\n", "storage_warn_pct must not exceed storage_critical_pct"),
    ("storage_warn_pct = 99.5\n", "must not exceed"),                     # the default critical is 98
    ("storage_warn_pct = 101\n", "between 0 and 100"), ("storage_critical_pct = -1\n", "between 0 and 100"),
    ('storage_warn_pct = "high"\n', "must be a number"), ("storage_critical_pct = true\n", "must be a number"),
    ("storage_warn_pct = nan\n", "must be finite"), ("storage_critical_pct = inf\n", "must be finite"),
])
def test_bad_thresholds_are_a_config_error_naming_the_setting(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_settings(write(tmp_path, f"[thresholds]\n{text}"))


def test_the_names_are_listed_among_the_valid_thresholds(tmp_path):
    with pytest.raises(ConfigError, match=r"unknown \[thresholds\] key\(s\): storage_typo") as caught:
        load_settings(write(tmp_path, "[thresholds]\nstorage_typo = 1\n"))
    assert "storage_critical_pct" in str(caught.value) and "storage_warn_pct" in str(caught.value)
