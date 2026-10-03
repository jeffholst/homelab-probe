"""`diagnose`: `device.overheating`, a device reports that it is overheating (issue #147)."""

import json

import pytest

from unifi_sentinel import cli
from unifi_sentinel.diagnose import diagnose, needs_for
from unifi_sentinel.diagnose.devices import _overheating_findings
from unifi_sentinel.snapshot import Snapshot, collect_snapshot

GATEWAY, SWITCH, AP, GARAGE = 0, 1, 2, 3                    # the order of the fixture's devices


def legacy(fake_client, index):
    return fake_client.session.fx["legacy"]["device"][index]


def snapshot(devices, legacy_devices):
    return Snapshot(site={"id": "s"}, devices=devices, clients=[], legacy_devices=legacy_devices)


def hot(**extra):
    return [{"id": "d1", "macAddress": "aa:bb:cc:00:00:01", "name": "Rack Switch", "state": "ONLINE"}], \
        [{"mac": "aa:bb:cc:00:00:01", "name": "legacy name", "overheating": True, **extra}]


def run(fake_client, monkeypatch, capsys, *argv):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def findings_json(fake_client, monkeypatch, capsys, *extra):
    code, out, _ = run(fake_client, monkeypatch, capsys, "diagnose", "--no-events", "--json", *extra)
    return code, json.loads(out)["findings"]


def overheating(findings):
    return [f for f in findings if f["code"] == "device.overheating"]


# -- the check -------------------------------------------------------------------------------------------------

def test_an_online_device_that_reports_it_is_overheating_is_one_critical_finding():
    (finding,) = _overheating_findings(snapshot(*hot()))
    assert (finding.severity, finding.subject, finding.message) == ("critical", "Rack Switch",
                                                                    "reports that it is overheating")
    assert finding.target_mac == "AA:BB:CC:00:00:01" and finding.code == "device.overheating"


@pytest.mark.parametrize("value", [False, None, "true", "True", 1, 0, 1.0, [], {}, [True], "yes"])
def test_only_a_real_true_counts(value):
    devices, records = hot()
    records[0]["overheating"] = value
    assert _overheating_findings(snapshot(devices, records)) == []


def test_a_device_without_the_flag_is_unknown_not_fine_and_not_a_finding():
    devices, records = hot()
    del records[0]["overheating"]                                # access points and some switches have none
    assert _overheating_findings(snapshot(devices, records)) == []


@pytest.mark.parametrize("state", ["OFFLINE", "UPDATING", "PENDING_ADOPTION", "ADOPTING", "", None, "online"])
def test_only_an_online_device_counts_because_an_offline_record_keeps_its_last_report(state):
    devices, records = hot()
    devices[0]["state"] = state
    assert _overheating_findings(snapshot(devices, records)) == []


def test_a_device_the_integration_api_does_not_list_is_not_reported():
    devices, records = hot()
    assert _overheating_findings(snapshot([], records)) == []
    records[0]["mac"] = "aa:bb:cc:00:00:99"
    assert _overheating_findings(snapshot(devices, records)) == []


def test_the_mac_is_joined_in_any_spelling():
    devices, records = hot()
    for spelling in ("AA-BB-CC-00-00-01", "aabb.cc00.0001", "AABBCC000001", " aa:bb:cc:00:00:01 "):
        records[0]["mac"] = spelling
        assert len(_overheating_findings(snapshot(devices, records))) == 1, spelling


def test_the_name_is_the_integration_apis_then_the_legacy_one_then_the_mac():
    devices, records = hot()
    assert _overheating_findings(snapshot(devices, records))[0].subject == "Rack Switch"
    devices[0]["name"] = ""
    assert _overheating_findings(snapshot(devices, records))[0].subject == "legacy name"
    records[0]["name"] = None
    assert _overheating_findings(snapshot(devices, records))[0].subject == "AA:BB:CC:00:00:01"


def test_every_overheating_device_gets_its_own_finding_in_the_controllers_order():
    devices = [{"id": f"d{i}", "macAddress": f"aa:bb:cc:00:00:0{i}", "name": f"Switch {i}", "state": "ONLINE"}
               for i in (1, 2, 3)]
    records = [{"mac": f"aa:bb:cc:00:00:0{i}", "overheating": i != 2} for i in (1, 2, 3)]
    assert [f.subject for f in _overheating_findings(snapshot(devices, records))] == ["Switch 1", "Switch 3"]


def test_a_malformed_legacy_record_is_skipped_without_a_crash():
    devices, _ = hot()
    records = [{}, {"overheating": True}, {"mac": None, "overheating": True}, {"mac": "", "overheating": True}]
    assert _overheating_findings(snapshot(devices, records)) == []


# -- in the command --------------------------------------------------------------------------------------------

def test_the_fixture_has_no_overheating_device(fake_client, monkeypatch, capsys):
    assert overheating(findings_json(fake_client, monkeypatch, capsys)[1]) == []
    assert legacy(fake_client, GATEWAY)["overheating"] is False        # the flag as the controller sends it


def test_an_overheating_gateway_is_critical_and_exits_two_in_every_output(fake_client, monkeypatch, capsys):
    legacy(fake_client, GATEWAY)["overheating"] = True
    code, findings = findings_json(fake_client, monkeypatch, capsys)
    (finding,) = overheating(findings)
    assert code == 2 and finding["severity"] == "critical" and finding["subject"] == "Gateway"
    assert finding["mac"] == "AA:00:00:00:00:01" and finding["message"] == "reports that it is overheating"
    code, out, _ = run(fake_client, monkeypatch, capsys, "diagnose", "--no-events", "--no-emoji")
    assert code == 2 and "[CRITICAL] Gateway: reports that it is overheating" in out
    code, _, _ = run(fake_client, monkeypatch, capsys, "diagnose", "--no-events", "--fail-on", "critical", "--json")
    assert code == 2


def test_the_critical_finding_is_listed_before_warnings(fake_client, monkeypatch, capsys):
    legacy(fake_client, SWITCH)["overheating"] = True
    _, findings = findings_json(fake_client, monkeypatch, capsys)
    assert findings[0]["severity"] == "critical"
    assert [f["severity"] for f in findings] == sorted((f["severity"] for f in findings),
                                                       key=["critical", "warning", "info"].index)


def test_an_offline_device_with_a_stale_flag_is_only_reported_as_offline(fake_client, monkeypatch, capsys):
    legacy(fake_client, GARAGE)["overheating"] = True                  # the offline access point
    _, findings = findings_json(fake_client, monkeypatch, capsys)
    assert overheating(findings) == [] and any(f["code"] == "device.offline" for f in findings)


def test_it_runs_with_the_devices_area_and_only_there(fake_client, monkeypatch, capsys):
    legacy(fake_client, GATEWAY)["overheating"] = True
    for extra, expected in ((["--only", "devices"], 1), (["--skip", "ports", "--skip", "wifi"], 1),
                            (["--only", "ports"], 0), (["--skip", "devices"], 0), (["--only", "wan,wifi"], 0)):
        code, findings = findings_json(fake_client, monkeypatch, capsys, *extra)
        assert len(overheating(findings)) == expected, extra


def test_reading_only_the_devices_area_gives_the_same_finding_as_a_full_read(fake_client):
    legacy(fake_client, SWITCH)["overheating"] = True
    full = [f for f in diagnose(collect_snapshot(fake_client, "default", needs_for(None, 86400)))
            if f.code == "device.overheating"]
    fake_client.session.calls.clear()
    part = [f for f in diagnose(collect_snapshot(fake_client, "default", needs_for(["devices"], 86400)),
                                areas=["devices"]) if f.code == "device.overheating"]
    assert full and part == full
    assert not any(path.endswith(("/alluser", "/rest/networkconf", "/stat/rogueap")) for path in fake_client.session.calls)
    assert needs_for(["devices"], 86400).legacy_devices is True       # the read it relies on is already declared


def test_no_extra_request_is_made_for_it(fake_client, monkeypatch, capsys):
    run(fake_client, monkeypatch, capsys, "diagnose", "--no-events", "--only", "devices")
    plain = sorted(fake_client.session.calls)
    fake_client.session.calls.clear()
    legacy(fake_client, GATEWAY)["overheating"] = True
    run(fake_client, monkeypatch, capsys, "diagnose", "--no-events", "--only", "devices")
    assert sorted(fake_client.session.calls) == plain


def test_when_the_legacy_data_is_unreadable_the_one_notice_says_both_checks_were_skipped(fake_client, monkeypatch,
                                                                                        capsys):
    legacy(fake_client, GATEWAY)["overheating"] = True
    fake_client.session.fx["legacy"]["device"] = []
    _, findings = findings_json(fake_client, monkeypatch, capsys)
    (notice,) = [f for f in findings if f["code"] == "controller.legacy_unavailable"]
    assert notice["message"] == "legacy device data unavailable; port and overheating checks were skipped"
    assert overheating(findings) == []


# -- everything that builds on findings ------------------------------------------------------------------------

def test_an_ignore_rule_by_code_or_by_name_silences_it(fake_client, monkeypatch, capsys, tmp_path):
    legacy(fake_client, GATEWAY)["overheating"] = True
    for rule in ('code = "device.overheating"', 'subject = "gateway"\nmessage = "overheating"',
                 'code = "device.overheating"\nsubject = "Gat*"'):
        config = tmp_path / "unifi-sentinel.toml"
        config.write_text(f'[[ignore]]\n{rule}\nreason = "in a hot cupboard on purpose"\n')
        code, findings = findings_json(fake_client, monkeypatch, capsys, "--config", str(config), "--show-ignored")
        assert overheating(findings) == [], rule
    other = tmp_path / "other.toml"
    other.write_text('[[ignore]]\ncode = "device.overheating"\nsubject = "Office Switch"\nreason = "x"\n')
    assert len(overheating(findings_json(fake_client, monkeypatch, capsys, "--config", str(other))[1])) == 1


def test_it_is_sent_like_any_critical_finding(fake_client, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("NOTIFY_NTFY_URL", "https://ntfy.example/topic-for-the-test")
    legacy(fake_client, GATEWAY)["overheating"] = True
    argv = ["diagnose", "--no-events", "--json", "--notify", "--notify-dry-run", "--notify-state",
            str(tmp_path / "state.json")]
    _, _, err = run(fake_client, monkeypatch, capsys, *argv)
    assert "[CRITICAL] NEW  Gateway: reports that it is overheating" in err
    _, _, redacted = run(fake_client, monkeypatch, capsys, *argv, "--notify-redact")
    assert "Gateway" not in redacted and "[CRITICAL] NEW  an online UniFi device reports that it is overheating" in redacted


def test_the_topology_flags_the_device(fake_client, monkeypatch, capsys):
    legacy(fake_client, SWITCH)["overheating"] = True
    _, out, _ = run(fake_client, monkeypatch, capsys, "topology", "--json")
    assert "device.overheating" in out
    legacy(fake_client, SWITCH)["overheating"] = False
    _, out, _ = run(fake_client, monkeypatch, capsys, "topology", "--json")
    assert "device.overheating" not in out


def test_the_client_view_lists_it_for_a_device_on_the_clients_path(fake_client, monkeypatch, capsys):
    legacy(fake_client, SWITCH)["overheating"] = True                  # the switch the wired desktop is plugged into
    _, out, _ = run(fake_client, monkeypatch, capsys, "client", "desktop", "--json", "--no-events")
    assert "device.overheating" in out


def test_a_snapshot_that_did_not_read_the_legacy_devices_does_not_crash():
    assert _overheating_findings(Snapshot(site={"id": "s"}, devices=[], clients=[])) == []
