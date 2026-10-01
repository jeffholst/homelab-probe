import re
from pathlib import Path

import pytest

from unifi_sentinel import cli
from unifi_sentinel.config import ConfigError
from unifi_sentinel.diagnose import (Finding, apply_ignores, diagnose, exit_code,
                                     format_findings, format_ignored)
from unifi_sentinel.settings import DEFAULT_FILENAME, DiagnoseSettings, IgnoreRule, load_settings
from unifi_sentinel.snapshot import Snapshot

ROOT = Path(__file__).resolve().parent.parent


def write(tmp_path, text, name="cfg.toml"):
    path = tmp_path / name
    path.write_text(text)
    return path


# -- loading ---------------------------------------------------------------

def test_defaults_when_no_config_exists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_settings() == DiagnoseSettings()
    assert DiagnoseSettings().resource_warn_pct == 90
    assert DiagnoseSettings().resource_critical_pct == 98
    assert DiagnoseSettings().slow_link_mbps == 100


def test_default_file_in_working_directory_is_used(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "[thresholds]\nslow_link_mbps = 10\n", DEFAULT_FILENAME)
    assert load_settings().slow_link_mbps == 10


def test_partial_config_keeps_other_defaults(tmp_path):
    s = load_settings(write(tmp_path, "[thresholds]\nresource_warn_pct = 70\n"))
    assert (s.resource_warn_pct, s.resource_critical_pct, s.slow_link_mbps) == (70, 98, 100)


def test_the_shipped_example_config_loads():
    s = load_settings(ROOT / "unifi-sentinel.example.toml")
    assert (s.resource_warn_pct, s.resource_critical_pct, s.slow_link_mbps) == (90, 98, 100)
    assert [r.subject for r in s.ignore] == ["Garage AP", "* port 2"]
    assert all(r.reason for r in s.ignore)


@pytest.mark.parametrize("text, message", [
    ("[thresholds\n", "invalid TOML"),
    ("bogus = 1\n", "unknown top-level key"),
    ("[thresholds]\nbogus = 1\n", "unknown [thresholds] key"),
    ('[thresholds]\nresource_warn_pct = "high"\n', "must be a number"),
    ("[thresholds]\nresource_warn_pct = true\n", "must be a number"),
    ("[thresholds]\nresource_warn_pct = 150\n", "between 0 and 100"),
    ("[thresholds]\nresource_warn_pct = 99\nresource_critical_pct = 95\n", "must not exceed"),
    ("[thresholds]\nresource_warn_pct = nan\n", "must be finite"),
    ("[thresholds]\nslow_link_mbps = inf\n", "must be finite"),
    ("[thresholds]\nslow_link_mbps = -1\n", "at least 0"),
    ('[[ignore]]\nreason = "x"\n', "subject and/or a message"),
    ('[[ignore]]\nsubject = "x"\n', "a reason is required"),
    ('[[ignore]]\nsubject = "x"\nreason = "  "\n', "a reason is required"),
    ('[[ignore]]\nsubject = "x"\nreason = "y"\nextra = 1\n', "only subject, message and reason"),
    ('[[ignore]]\nsubject = 1\nreason = "x"\n', "must be strings"),
    ('[[ignore]]\nmessage = true\nreason = "x"\n', "must be strings"),
    ('[[ignore]]\nsubject = "x"\nreason = []\n', "must be strings"),
    ("ignore = 5\n", "[[ignore]] tables"),
])
def test_invalid_config_gives_a_clear_error(tmp_path, text, message):
    path = write(tmp_path, text)
    with pytest.raises(ConfigError, match=re.escape(message)) as exc:
        load_settings(path)
    assert str(path) in str(exc.value)  # the error names the file


def test_explicit_missing_config_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="config file not found"):
        load_settings(tmp_path / "nope.toml")


def test_unreadable_config_is_a_clear_error(tmp_path, monkeypatch):
    path = write(tmp_path, "")

    def unreadable(*args, **kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(ConfigError, match="could not read config file") as exc:
        load_settings(path)
    assert str(path) in str(exc.value)


def test_invalid_utf8_config_is_a_clear_error(tmp_path):
    path = tmp_path / "cfg.toml"
    path.write_bytes(b"\xff")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_settings(path)


# -- ignore matching -------------------------------------------------------

def test_ignore_rule_matching():
    by_subject = IgnoreRule(subject="Garage AP", reason="r")
    assert by_subject.matches("garage ap", "anything")          # case-insensitive, exact
    assert not by_subject.matches("Garage AP 2", "anything")    # exact, not a prefix
    wildcard = IgnoreRule(subject="* port 2", reason="r")
    assert wildcard.matches("Office Switch port 2", "x") and not wildcard.matches("Switch port 3", "x")
    bracketed = IgnoreRule(subject="Device [old]", reason="r")
    assert bracketed.matches("Device [old]", "x")
    assert not bracketed.matches("Device o", "x")
    by_message = IgnoreRule(message="OFFLINE", reason="r")
    assert by_message.matches("any", "device is offline (gateway)")  # substring, case-insensitive
    both = IgnoreRule(subject="Garage AP", message="offline", reason="r")
    assert both.matches("Garage AP", "device is offline")
    assert not both.matches("Garage AP", "CPU utilization 99%")      # both must match
    assert not both.matches("Other AP", "device is offline")


def test_apply_ignores_splits_findings_and_reports_the_rule():
    rule = IgnoreRule(subject="Garage AP", message="offline", reason="spare AP")
    findings = [Finding("warning", "Garage AP", "device is offline"),
                Finding("warning", "Office AP", "device is offline"),
                Finding("critical", "Garage AP", "CPU utilization 99%")]
    kept, ignored = apply_ignores(findings, (rule,))
    assert [f.subject + ": " + f.message for f in kept] == [
        "Office AP: device is offline", "Garage AP: CPU utilization 99%"]
    assert ignored == [(findings[0], rule)]
    assert apply_ignores(findings, ()) == (findings, [])  # no rules: nothing ignored


def test_ignored_findings_do_not_affect_the_exit_code():
    crit = Finding("critical", "GW", "device is offline (gateway)")
    kept, ignored = apply_ignores([crit], (IgnoreRule(subject="GW", reason="maintenance"),))
    assert exit_code([crit]) == 2 and exit_code(kept) == 0 and len(ignored) == 1


def test_formatting_notes_ignored_counts_and_reasons():
    finding = Finding("warning", "A", "x")
    assert format_findings([finding], emoji=False, ignored=2).endswith("1 warning (2 ignored)")
    assert format_findings([finding], emoji=False).endswith("1 warning")
    assert format_findings([], ignored=3) == "No issues found. (3 ignored)"
    assert format_findings([]) == "No issues found."
    out = format_ignored([(finding, IgnoreRule(subject="A", reason="on purpose"))])
    assert out.startswith("Ignored (1):") and "A: x  (ignored: on purpose)" in out


# -- thresholds take effect ------------------------------------------------

def _snapshot(cpu=None, mem=None, speed=None):
    snap = Snapshot(site={"id": "s"},
                    devices=[{"id": "d", "macAddress": "aa:01", "name": "SW", "state": "ONLINE"}],
                    clients=[], legacy_devices=[{"mac": "aa:01", "type": "usw", "name": "SW",
                                                  "port_table": [{"port_idx": 1, "up": True,
                                                                  "speed": speed or 1000,
                                                                  "full_duplex": True}]}])
    snap.device_stats = {"d": {"cpuUtilizationPct": cpu or 0, "memoryUtilizationPct": mem or 0}}
    return snap


def _messages(snap, settings=None):
    return {(f.severity, f.message) for f in diagnose(snap, settings)}


def test_resource_thresholds_are_configurable():
    snap = _snapshot(cpu=75, mem=96)
    assert _messages(snap) == {("warning", "memory utilization 96%")}          # defaults: 90 / 98
    tuned = DiagnoseSettings(resource_warn_pct=70, resource_critical_pct=95)
    assert _messages(snap, tuned) == {("warning", "CPU utilization 75%"),
                                      ("critical", "memory utilization 96%")}


def test_slow_link_threshold_is_configurable():
    snap = _snapshot(speed=1000)
    assert _messages(snap) == set()                                # 1000 Mbps is not slow by default
    assert _messages(snap, DiagnoseSettings(slow_link_mbps=1000)) == {("info", "negotiated at 1000 Mbps")}
    assert _messages(_snapshot(speed=10), DiagnoseSettings(slow_link_mbps=5)) == set()


# -- command line ----------------------------------------------------------

def _run(fake_client, monkeypatch, argv):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    return cli.main(argv)


def test_cli_ignore_list_summary_show_ignored_and_exit_code(fake_client, monkeypatch, capsys, tmp_path):
    assert _run(fake_client, monkeypatch, ["diagnose", "--no-emoji"]) == 1
    capsys.readouterr()

    cfg = write(tmp_path, '''
[[ignore]]
message = "offline"
reason = "spare AP"
[[ignore]]
subject = "Office Switch*"
reason = "known noisy switch"
[[ignore]]
subject = "old-printer"
reason = "moved to IoT later"
[[ignore]]
subject = "wlan"
reason = "explained by the offline spare AP"
[[ignore]]
subject = "10.0.0.50"
reason = "known IP conflict from the event log"
''')
    code = _run(fake_client, monkeypatch, ["diagnose", "--no-emoji", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert code == 0                                   # everything in the fixture is ignored
    assert out.startswith("No issues found. (")
    assert "Garage AP" not in out                      # hidden unless asked

    code = _run(fake_client, monkeypatch,
                ["diagnose", "--no-emoji", "--config", str(cfg), "--show-ignored"])
    out = capsys.readouterr().out
    assert "Ignored (" in out and "Garage AP: device is offline  (ignored: spare AP)" in out
    assert "(ignored: known noisy switch)" in out


def test_cli_threshold_override_changes_findings(fake_client, monkeypatch, capsys, tmp_path):
    cfg = write(tmp_path, "[thresholds]\nresource_warn_pct = 99\nresource_critical_pct = 100\n"
                          "slow_link_mbps = 0\n")
    _run(fake_client, monkeypatch, ["diagnose", "--no-emoji", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert "CPU utilization" not in out and "Office Switch port 2: negotiated at" not in out


def test_cli_bad_config_fails_before_contacting_the_controller(fake_client, monkeypatch, capsys, tmp_path):
    cfg = write(tmp_path, "[thresholds]\nbogus = 1\n")
    assert _run(fake_client, monkeypatch, ["diagnose", "--config", str(cfg)]) == cli.EXIT_ERROR
    assert "unknown [thresholds] key" in capsys.readouterr().err
    assert fake_client.session.calls == []             # no API request was made
    assert _run(fake_client, monkeypatch,
                ["diagnose", "--config", str(tmp_path / "missing.toml")]) == cli.EXIT_ERROR


def test_other_commands_ignore_the_config_file(fake_client, monkeypatch, tmp_path):
    (tmp_path / DEFAULT_FILENAME).write_text("not valid toml [")
    monkeypatch.chdir(tmp_path)
    assert _run(fake_client, monkeypatch, ["info"]) == 0  # only `diagnose` reads settings
