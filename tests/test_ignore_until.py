"""Ignore rules with an ``until`` date: a temporary ignore that expires (issue #135)."""

import copy
import datetime
import json
import types

import pytest
from docs_support import ROOT
from jsonschema import Draft202012Validator

from unifi_sentinel import cli, commands
from unifi_sentinel.audit import AUDIT_AREAS
from unifi_sentinel.config import ConfigError
from unifi_sentinel.diagnose import Finding, apply_ignores, findings_json, format_ignored
from unifi_sentinel.diagnose import output as output_module
from unifi_sentinel.settings import IgnoreRule, expired_rules, load_settings

DATE = datetime.date
TEN = DATE(2026, 10, 10)
LONG_AGO, FAR_AHEAD = "2000-01-01", "2999-12-31"          # fixed dates, so a CLI test needs no clock


def write(tmp_path, text):
    path = tmp_path / "unifi-sentinel.toml"
    path.write_text(text, encoding="utf-8")
    return path


def rules(tmp_path, text):
    return load_settings(write(tmp_path, text)).ignore


def finding(code="device.offline", subject="Garage AP", message="is offline"):
    return Finding("critical", subject, message, code=code)


def rule_text(until, code="device.offline", subject="Garage AP", extra=""):
    return f'[[ignore]]\ncode = "{code}"\nsubject = "{subject}"\n{extra}until = {until}\nreason = "spare, unplugged"\n'


# -- reading the date ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("written", ["2026-10-10", '"2026-10-10"'])
def test_a_date_is_read_as_a_toml_date_or_as_a_string(tmp_path, written):
    (only,) = rules(tmp_path, rule_text(written))
    assert only.until == TEN


def test_a_rule_without_a_date_has_none_and_never_expires(tmp_path):
    (only,) = rules(tmp_path, '[[ignore]]\ncode = "device.offline"\nreason = "spare"\n')
    assert only.until is None and not only.expired(DATE(9999, 12, 31))


@pytest.mark.parametrize("written", [
    '"2026-1-1"', '"20261010"', '"2026-W41-1"', '"2026-02-30"', '"2026-13-01"', '"2026-10-10 "', '" 2026-10-10"',
    '""', '"tomorrow"', '"\\u0662\\u0660\\u0662\\u0666-10-10"', '"2026-10-10\\n"', '"0000-01-01"', "2026", "true", "[2026]", '["2026-10-10"]',
])
def test_anything_but_a_date_is_a_config_error_that_names_the_rule(tmp_path, written):
    with pytest.raises(ConfigError, match=r"\[\[ignore\]\] #1: until must be a date like 2026-10-10"):
        rules(tmp_path, rule_text(written))


def test_an_impossible_unquoted_date_is_refused_by_the_toml_reader_itself(tmp_path):
    with pytest.raises(ConfigError, match="invalid TOML"):
        rules(tmp_path, rule_text("2026-02-30"))


@pytest.mark.parametrize("written", ["2026-10-10T12:00:00", "2026-10-10T12:00:00Z", "2026-10-10 12:00:00"])
def test_a_date_with_a_time_is_refused(tmp_path, written):
    with pytest.raises(ConfigError, match="a date, without a time"):
        rules(tmp_path, rule_text(written))


def test_the_message_names_the_right_rule(tmp_path):
    text = rule_text(f'"{LONG_AGO}"') + rule_text('"soon"')
    with pytest.raises(ConfigError, match=r"#2: until must be a date"):
        rules(tmp_path, text)


def test_until_alone_is_not_a_rule_and_a_reason_is_still_required(tmp_path):
    with pytest.raises(ConfigError, match="give a code, a subject and/or a message"):
        rules(tmp_path, '[[ignore]]\nuntil = 2026-10-10\nreason = "x"\n')
    with pytest.raises(ConfigError, match="a reason is required"):
        rules(tmp_path, '[[ignore]]\ncode = "device.offline"\nuntil = 2026-10-10\nreason = " "\n')


def test_the_other_fields_must_still_be_strings(tmp_path):
    with pytest.raises(ConfigError, match="must be strings"):
        rules(tmp_path, '[[ignore]]\nsubject = 5\nuntil = 2026-10-10\nreason = "x"\n')


# -- when a rule applies -----------------------------------------------------------------------------------------

def test_a_rule_applies_through_its_last_day_and_not_after():
    rule = IgnoreRule(code="device.offline", reason="spare", until=TEN)
    assert not rule.expired(TEN - datetime.timedelta(days=1))
    assert not rule.expired(TEN)                                  # the day itself still counts as active
    assert rule.expired(TEN + datetime.timedelta(days=1))


def test_apply_ignores_gives_an_expired_rules_findings_back():
    rule = IgnoreRule(code="device.offline", reason="spare", until=TEN)
    for today, silenced in ((TEN - datetime.timedelta(days=30), True), (TEN, True),
                            (TEN + datetime.timedelta(days=1), False)):
        kept, ignored = apply_ignores([finding()], (rule,), today)
        assert (not kept and len(ignored) == 1) is silenced, today


def test_an_expired_rule_does_not_hide_a_live_one_behind_it():
    """The first matching rule used to win; an expired one must be skipped, not stop the search."""
    old = IgnoreRule(code="device.offline", reason="old", until=TEN)
    live = IgnoreRule(subject="Garage*", reason="still wanted")
    kept, ignored = apply_ignores([finding()], (old, live), TEN + datetime.timedelta(days=1))
    assert not kept and [r.reason for _, r in ignored] == ["still wanted"]


def test_a_rule_without_a_date_is_unaffected_by_the_clock():
    kept, ignored = apply_ignores([finding()], (IgnoreRule(code="device.offline", reason="x"),), DATE(9999, 12, 31))
    assert not kept and len(ignored) == 1


def test_the_default_day_is_today():
    today = DATE.today()
    yesterday, tomorrow = today - datetime.timedelta(days=1), today + datetime.timedelta(days=1)
    assert apply_ignores([finding()], (IgnoreRule(code="device.offline", reason="x", until=yesterday),))[1] == []
    assert len(apply_ignores([finding()], (IgnoreRule(code="device.offline", reason="x", until=tomorrow),))[1]) == 1


def test_expired_rules_lists_each_with_its_date():
    old = IgnoreRule(code="device.offline", reason="old", until=TEN)
    live = IgnoreRule(code="port.errors", reason="live", until=TEN + datetime.timedelta(days=5))
    plain = IgnoreRule(code="port.slow_link", reason="forever")
    today = TEN + datetime.timedelta(days=1)
    assert expired_rules((old, live, plain), today) == [(old, TEN)]
    assert expired_rules((live, plain), today) == [] and expired_rules((), today) == []


def test_describe_names_the_selecting_fields_and_cleans_them():
    rule = IgnoreRule(code="device.offline", subject="Gar\x1b[31mage", message="offline", reason="x")
    assert rule.describe() == 'code "device.offline", subject "Gar[31mage", message "offline"'
    assert IgnoreRule(subject="Lobby", reason="x").describe() == 'subject "Lobby"'


# -- what the output says ----------------------------------------------------------------------------------------

def test_show_ignored_text_shows_the_end_date_only_for_a_temporary_rule():
    temporary = format_ignored([(finding(), IgnoreRule(code="device.offline", reason="why", until=TEN))])
    assert "(code: device.offline; ignored until 2026-10-10: why)" in temporary
    forever = format_ignored([(finding(), IgnoreRule(code="device.offline", reason="why"))])
    assert "(code: device.offline; ignored: why)" in forever and "until" not in forever


def test_json_carries_until_only_for_a_rule_that_has_one():
    ignored = [(finding(), IgnoreRule(code="device.offline", reason="a", until=TEN)),
               (finding(code="port.errors"), IgnoreRule(code="port.errors", reason="b"))]
    entries = json.loads(findings_json([], ignored, show_ignored=True))["ignored"]
    assert entries[0]["until"] == "2026-10-10" and "until" not in entries[1]


@pytest.mark.parametrize("name", ["diagnose", "audit"])
def test_the_schema_declares_until_and_the_output_matches_it(name):
    schema = json.loads((ROOT / "docs" / "schemas" / f"{name}.v1.schema.json").read_text(encoding="utf-8"))
    strict = copy.deepcopy(schema)
    strict["$defs"]["ignored"]["additionalProperties"] = False        # an undeclared key must fail
    ignored = [(finding(code="device.offline"), IgnoreRule(code="device.offline", reason="a", until=TEN))]
    areas = AUDIT_AREAS if name == "audit" else None
    document = json.loads(findings_json([], ignored, show_ignored=True, areas=areas))
    Draft202012Validator(strict).validate(document)
    document["ignored"][0]["until"] = "10/10/2026"                    # and the declared one has a shape
    assert not Draft202012Validator(strict).is_valid(document)
    del strict["$defs"]["ignored"]["properties"]["until"]
    document["ignored"][0]["until"] = "2026-10-10"
    assert not Draft202012Validator(strict).is_valid(document)        # the check can fail


# -- the commands ----------------------------------------------------------------------------------------------

def run(fake_client, monkeypatch, argv):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    return cli.main(argv)


def diagnose(fake_client, monkeypatch, capsys, config, *extra):
    code = run(fake_client, monkeypatch, ["diagnose", "--no-events", "--json", "--config", str(config), *extra])
    out = capsys.readouterr()
    return code, json.loads(out.out), out.err


def offline(doc):
    return [f for f in doc["findings"] if f["code"] == "device.offline"]


def test_diagnose_an_expired_rule_brings_the_finding_back_with_a_warning_on_stderr(fake_client, monkeypatch, capsys,
                                                                                    tmp_path):
    base_code, base, _ = diagnose(fake_client, monkeypatch, capsys, write(tmp_path, ""))
    config = write(tmp_path, rule_text(f'"{LONG_AGO}"'))
    code, doc, err = diagnose(fake_client, monkeypatch, capsys, config)
    assert offline(doc) and offline(doc) == offline(base) and code == base_code      # as if the rule were absent
    assert doc["summary"]["ignored"] == 0
    assert 'Warning: the ignore rule for code "device.offline", subject "Garage AP" expired on 2000-01-01' in err
    assert "spare, unplugged" in err and err.count("expired on") == 1


def test_diagnose_a_rule_that_has_not_ended_silences_the_finding_without_a_warning(fake_client, monkeypatch, capsys,
                                                                                    tmp_path):
    config = write(tmp_path, rule_text(f'"{FAR_AHEAD}"', subject="*"))
    code, doc, err = diagnose(fake_client, monkeypatch, capsys, config, "--show-ignored")
    assert not offline(doc) and doc["summary"]["ignored"] >= 1 and "expired" not in err
    assert {entry["until"] for entry in doc["ignored"]} == {FAR_AHEAD}
    run(fake_client, monkeypatch, ["diagnose", "--no-events", "--no-emoji", "--show-ignored", "--config", str(config)])
    assert f"ignored until {FAR_AHEAD}: spare, unplugged" in capsys.readouterr().out


def test_the_warning_never_changes_stdout_or_the_exit_code(fake_client, monkeypatch, capsys, tmp_path):
    plain_code = run(fake_client, monkeypatch, ["diagnose", "--no-events", "--no-emoji", "--config",
                                                str(write(tmp_path, ""))])
    plain = capsys.readouterr().out
    code = run(fake_client, monkeypatch, ["diagnose", "--no-events", "--no-emoji", "--config",
                                          str(write(tmp_path, rule_text(f'"{LONG_AGO}"')))])
    out = capsys.readouterr()
    assert out.out == plain and "expired on" in out.err and code == plain_code != 0


def test_audit_warns_and_applies_the_date_the_same_way(fake_client, monkeypatch, capsys, tmp_path):
    def audit(until):
        config = write(tmp_path, rule_text(f'"{until}"', code="audit.wifi_open", subject="Lobby"))
        run(fake_client, monkeypatch, ["audit", "--json", "--config", str(config)])
        out = capsys.readouterr()
        return [f["code"] for f in json.loads(out.out)["findings"]], out.err
    codes, err = audit(LONG_AGO)
    assert "audit.wifi_open" in codes and 'code "audit.wifi_open", subject "Lobby" expired on 2000-01-01' in err
    codes, err = audit(FAR_AHEAD)
    assert "audit.wifi_open" not in codes and "expired" not in err


def test_an_expired_rule_lets_the_finding_reach_a_notification(fake_client, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("NOTIFY_NTFY_URL", "https://ntfy.example/topic-for-the-test")

    def dry_run(until):
        config = write(tmp_path, rule_text(f'"{until}"'))
        run(fake_client, monkeypatch, ["diagnose", "--no-events", "--json", "--config", str(config), "--notify",
                                       "--notify-dry-run", "--notify-state", str(tmp_path / "state.json")])
        return capsys.readouterr().err
    assert "Garage AP" in dry_run(LONG_AGO)
    assert "Garage AP" not in dry_run(FAR_AHEAD)


def test_watch_warns_once_and_not_on_every_pass(fake_client, monkeypatch, capsys, tmp_path):
    waits = []

    def sleeper(seconds):
        waits.append(seconds)
        if len(waits) >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(commands, "WATCH_SLEEP", sleeper)
    run(fake_client, monkeypatch, ["diagnose", "--no-events", "--no-emoji", "--watch", "30", "--config",
                                   str(write(tmp_path, rule_text(f'"{LONG_AGO}"')))])
    assert len(waits) == 3 and capsys.readouterr().err.count("expired on") == 1


def test_a_rule_that_runs_out_during_a_watch_stops_applying_and_its_finding_is_announced(fake_client, monkeypatch,
                                                                                         capsys, tmp_path):
    class Day(datetime.date):
        current = DATE(2026, 12, 31)                              # the rule's last day

        @classmethod
        def today(cls):
            return cls.current

    monkeypatch.setattr(output_module, "datetime", types.SimpleNamespace(date=Day))
    waits = []

    def sleeper(seconds):
        waits.append(seconds)
        if len(waits) >= 2:
            raise KeyboardInterrupt
        Day.current = DATE(2027, 1, 1)                            # midnight passes while the watch sleeps

    monkeypatch.setattr(commands, "WATCH_SLEEP", sleeper)
    config = write(tmp_path, rule_text("2026-12-31"))
    run(fake_client, monkeypatch, ["diagnose", "--no-events", "--no-emoji", "--watch", "30", "--config", str(config)])
    lines = capsys.readouterr().out.strip().splitlines()
    assert "Garage AP" in lines[-1] and "offline" in lines[-1]          # announced once the date had passed
    assert not any("Garage AP" in line for line in lines[:-1])           # and silenced up to its last day


def test_topology_and_client_apply_the_date_but_say_nothing(fake_client, monkeypatch, capsys, tmp_path):
    for until, shown in ((LONG_AGO, True), (FAR_AHEAD, False)):
        config = write(tmp_path, rule_text(f'"{until}"'))
        run(fake_client, monkeypatch, ["topology", "--json", "--config", str(config)])
        out = capsys.readouterr()
        assert ("device.offline" in out.out) is shown and "expired" not in out.err


def test_a_bad_date_stops_the_command_before_it_contacts_the_controller(fake_client, monkeypatch, capsys, tmp_path):
    config = write(tmp_path, rule_text('"next week"'))
    code = run(fake_client, monkeypatch, ["diagnose", "--config", str(config)])
    assert code == 3 and "until must be a date like 2026-10-10" in capsys.readouterr().err
    assert fake_client.session.calls == []


# -- the documentation -------------------------------------------------------------------------------------------

def test_the_example_file_shows_a_temporary_rule_that_loads():
    settings = load_settings(ROOT / "unifi-sentinel.example.toml")
    assert any(rule.until is not None for rule in settings.ignore)
