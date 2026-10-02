"""Ignore rules that match a finding's code (`[[ignore]] code = "port.slow_link"`)."""

import json
import re
import sys
from pathlib import Path

import pytest

from unifi_sentinel import cli
from unifi_sentinel.audit import AUDIT_CODES
from unifi_sentinel.config import ConfigError
from unifi_sentinel.diagnose import CODES, Finding, apply_ignores, format_ignored
from unifi_sentinel.settings import IgnoreRule, known_codes, load_settings

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover  (Python 3.10 only)
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent


def write(tmp_path, text):
    path = tmp_path / "unifi-sentinel.toml"
    path.write_text(text, encoding="utf-8")
    return path


def rule(text, tmp_path):
    (only,) = load_settings(write(tmp_path, text)).ignore
    return only


def finding(code, subject="Office Switch port 2", message="negotiated at 100 Mbps"):
    return Finding("info", subject, message, code=code)


# -- the rule ----------------------------------------------------------------------------------------------

def test_a_code_alone_silences_that_check_everywhere():
    r = IgnoreRule(code="port.slow_link", reason="x")
    assert r.matches("any subject", "any message", "port.slow_link")
    assert not r.matches("any subject", "any message", "port.errors")
    assert not r.matches("any subject", "any message")                      # a finding without a code never matches


def test_a_code_with_a_subject_needs_both():
    r = IgnoreRule(code="port.slow_link", subject="* port 2", reason="x")
    assert r.matches("Office Switch port 2", "m", "port.slow_link")
    assert not r.matches("Office Switch port 3", "m", "port.slow_link")
    assert not r.matches("Office Switch port 2", "m", "port.errors")


def test_a_code_with_a_message_needs_both():
    r = IgnoreRule(code="port.slow_link", message="NEGOTIATED", reason="x")
    assert r.matches("s", "negotiated at 100 Mbps", "port.slow_link")
    assert not r.matches("s", "link is half duplex", "port.slow_link")
    assert not r.matches("s", "negotiated at 100 Mbps", "port.stp")


def test_all_three_fields_must_match():
    r = IgnoreRule(code="port.slow_link", subject="*port 2", message="100", reason="x")
    assert r.matches("Sw port 2", "at 100 Mbps", "port.slow_link")
    assert not any([r.matches("Sw port 3", "at 100 Mbps", "port.slow_link"),
                    r.matches("Sw port 2", "at 10 Mbps", "port.slow_link"),
                    r.matches("Sw port 2", "at 100 Mbps", "port.errors")])


def test_rules_without_a_code_behave_as_before():
    r = IgnoreRule(subject="Garage AP", message="offline", reason="x")
    assert r.matches("garage ap", "Device is OFFLINE") and r.matches("garage ap", "Device is OFFLINE", "device.offline")
    assert not r.matches("Office AP", "offline")


def test_matching_is_exact_and_case_sensitive():
    r = IgnoreRule(code="port.slow_link", reason="x")
    for near in ("Port.Slow_Link", "PORT.SLOW_LINK", "port.slow_link ", " port.slow_link", "port.slow", "port.*",
                 "port.slow_linkX"):
        assert not r.matches("s", "m", near), near


def test_apply_ignores_uses_the_code_and_reports_the_rule():
    findings = [finding("port.slow_link"), finding("port.errors", message="3 rx/tx errors")]
    rules = (IgnoreRule(code="port.slow_link", reason="printer only supports 100 Mbps"),)
    kept, ignored = apply_ignores(findings, rules)
    assert [f.code for f in kept] == ["port.errors"]
    assert [(f.code, r.reason) for f, r in ignored] == [("port.slow_link", "printer only supports 100 Mbps")]


def test_show_ignored_text_carries_the_code_to_copy_into_a_rule():
    text = format_ignored([(finding("port.slow_link"), IgnoreRule(code="port.slow_link", reason="why"))])
    assert "Office Switch port 2: negotiated at 100 Mbps  (code: port.slow_link; ignored: why)" in text
    assert "(ignored: why)" in format_ignored([(finding(""), IgnoreRule(subject="x", reason="why"))])


# -- the settings file ---------------------------------------------------------------------------------------

def test_a_code_rule_loads_with_and_without_other_fields(tmp_path):
    assert rule('[[ignore]]\ncode = "device.offline"\nreason = "r"\n', tmp_path) == IgnoreRule(
        code="device.offline", reason="r")
    full = rule('[[ignore]]\ncode = "port.slow_link"\nsubject = "* port 2"\nmessage = "at"\nreason = "r"\n', tmp_path)
    assert (full.code, full.subject, full.message) == ("port.slow_link", "* port 2", "at")


def test_an_unknown_code_is_a_config_error_that_lists_the_valid_ones_and_suggests_one(tmp_path):
    path = write(tmp_path, '[[ignore]]\ncode = "port.slow_lnk"\nreason = "typo"\n')
    with pytest.raises(ConfigError) as caught:
        load_settings(path)
    message = str(caught.value)
    assert "[[ignore]] #1: unknown code 'port.slow_lnk'" in message and "did you mean 'port.slow_link'?" in message
    assert "Valid codes: " in message and all(code in message for code in CODES) and str(path) in message


@pytest.mark.parametrize("code", ["Device.Offline", "device.*", "device", "device.offline ", "", "nothing.at_all"])
def test_codes_are_exact_no_wildcards_no_case_folding_no_spaces(tmp_path, code):
    text = f'[[ignore]]\ncode = "{code}"\nsubject = "x"\nreason = "r"\n'
    if code == "":
        assert rule(text, tmp_path).code == ""                             # an empty code is simply not given
        return
    with pytest.raises(ConfigError, match="unknown code"):
        load_settings(write(tmp_path, text))


def test_an_unrelated_typo_gets_no_suggestion(tmp_path):
    with pytest.raises(ConfigError) as caught:
        load_settings(write(tmp_path, '[[ignore]]\ncode = "zzzzzz"\nreason = "r"\n'))
    assert "did you mean" not in str(caught.value) and "Valid codes:" in str(caught.value)


def test_a_rule_with_only_a_reason_is_rejected_and_a_reason_is_still_required(tmp_path):
    with pytest.raises(ConfigError, match="give a code, a subject and/or a message"):
        load_settings(write(tmp_path, '[[ignore]]\nreason = "r"\n'))
    with pytest.raises(ConfigError, match="a reason is required"):
        load_settings(write(tmp_path, '[[ignore]]\ncode = "device.offline"\n'))
    with pytest.raises(ConfigError, match="a reason is required"):
        load_settings(write(tmp_path, '[[ignore]]\ncode = "device.offline"\nreason = "  "\n'))


@pytest.mark.parametrize("value", ["1", "true", "[]", '["device.offline"]'])
def test_a_code_that_is_not_text_is_rejected(tmp_path, value):
    with pytest.raises(ConfigError, match="must be strings"):
        load_settings(write(tmp_path, f'[[ignore]]\ncode = {value}\nreason = "r"\n'))


def test_the_codes_a_rule_may_name_are_those_of_diagnose_and_audit():
    codes = known_codes()
    assert set(codes) == set(CODES) | set(AUDIT_CODES) and "audit.wifi_open" in codes and "device.offline" in codes


# -- everywhere ignore rules are used -----------------------------------------------------------------------------

def run(fake_client, monkeypatch, argv):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    return cli.main(argv)


def diagnose_json(fake_client, monkeypatch, capsys, config, *extra):
    code = run(fake_client, monkeypatch, ["diagnose", "--no-events", "--json", "--config", str(config), *extra])
    return code, json.loads(capsys.readouterr().out)


def test_diagnose_json_excludes_ignored_findings_and_lists_them_with_their_code(fake_client, monkeypatch, capsys,
                                                                                 tmp_path):
    _, full = diagnose_json(fake_client, monkeypatch, capsys, write(tmp_path, ""))
    config = write(tmp_path, '[[ignore]]\ncode = "port.slow_link"\nreason = "printer only supports 100 Mbps"\n')
    _, doc = diagnose_json(fake_client, monkeypatch, capsys, config, "--show-ignored")
    gone = [f for f in full["findings"] if f["code"] == "port.slow_link"]
    assert gone and not any(f["code"] == "port.slow_link" for f in doc["findings"])
    assert doc["summary"]["ignored"] == len(gone) and len(doc["findings"]) == len(full["findings"]) - len(gone)
    assert {f["code"] for f in doc["ignored"]} == {"port.slow_link"}
    assert all(f["reason"] == "printer only supports 100 Mbps" for f in doc["ignored"])


def test_an_ignored_code_changes_the_exit_code_like_any_ignore(fake_client, monkeypatch, capsys, tmp_path):
    _, full = diagnose_json(fake_client, monkeypatch, capsys, write(tmp_path, ""))
    codes = sorted({f["code"] for f in full["findings"] if f["severity"] != "info"})
    rules = "".join(f'[[ignore]]\ncode = "{c}"\nreason = "accepted"\n' for c in codes)
    code, doc = diagnose_json(fake_client, monkeypatch, capsys, write(tmp_path, rules))
    assert code == 0 and not any(f["severity"] != "info" for f in doc["findings"])


def test_a_code_rule_is_not_the_wording(fake_client, monkeypatch, capsys, tmp_path):
    """The point of a code: it keeps working when the message changes."""
    config = write(tmp_path, '[[ignore]]\ncode = "device.offline"\nreason = "spare"\n')
    _, doc = diagnose_json(fake_client, monkeypatch, capsys, config)
    assert not any(f["code"] == "device.offline" for f in doc["findings"])
    for device in fake_client.session.fx["devices"]:
        device["name"] = "Renamed " + device["name"]
    _, doc = diagnose_json(fake_client, monkeypatch, capsys, config)
    assert not any(f["code"] == "device.offline" for f in doc["findings"])


def test_ignored_findings_are_not_notified(fake_client, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("NOTIFY_NTFY_URL", "https://ntfy.example/topic-for-the-test")
    config = write(tmp_path, '[[ignore]]\ncode = "device.offline"\nreason = "spare"\n')
    run(fake_client, monkeypatch, ["diagnose", "--no-events", "--json", "--config", str(config), "--notify",
                                   "--notify-dry-run", "--notify-state", str(tmp_path / "state.json")])
    err = capsys.readouterr().err
    assert "Notification dry run" in err and "device.offline" not in err and "Garage AP" not in err
    run(fake_client, monkeypatch, ["diagnose", "--no-events", "--json", "--notify", "--notify-dry-run",
                                   "--notify-state", str(tmp_path / "state.json")])
    assert "Garage AP" in capsys.readouterr().err                          # without the rule it is reported


def test_topology_and_client_honor_code_rules(fake_client, monkeypatch, capsys, tmp_path):
    run(fake_client, monkeypatch, ["topology", "--json"])
    plain = json.loads(capsys.readouterr().out)
    assert "device.offline" in json.dumps(plain)
    config = write(tmp_path, '[[ignore]]\ncode = "device.offline"\nreason = "spare"\n')
    run(fake_client, monkeypatch, ["topology", "--json", "--config", str(config)])
    assert "device.offline" not in capsys.readouterr().out
    run(fake_client, monkeypatch, ["client", "desktop", "--json"])
    before = capsys.readouterr().out
    assert '"code"' in before
    codes = sorted(set(re.findall(r'"code": "([a-z_.]+)"', before)))
    rules = "".join(f'[[ignore]]\ncode = "{c}"\nreason = "accepted"\n' for c in codes)
    run(fake_client, monkeypatch, ["client", "desktop", "--json", "--config", str(write(tmp_path, rules))])
    assert not set(re.findall(r'"code": "([a-z_.]+)"', capsys.readouterr().out)) & set(codes)


def test_audit_codes_work_in_the_same_file(fake_client, monkeypatch, capsys, tmp_path):
    config = write(tmp_path, '[[ignore]]\ncode = "audit.wifi_open"\nsubject = "Lobby"\nreason = "open on purpose"\n')
    run(fake_client, monkeypatch, ["audit", "--json", "--config", str(config), "--show-ignored"])
    doc = json.loads(capsys.readouterr().out)
    assert not any(f["code"] == "audit.wifi_open" for f in doc["findings"])
    assert [f["code"] for f in doc["ignored"]] == ["audit.wifi_open"]
    run(fake_client, monkeypatch, ["diagnose", "--no-events", "--json", "--config", str(config)])      # valid there too
    assert json.loads(capsys.readouterr().out)["version"] == 1


# -- the documentation ---------------------------------------------------------------------------------------------

def test_the_example_file_parses_and_shows_a_code_rule():
    settings = load_settings(ROOT / "unifi-sentinel.example.toml")
    coded = [r for r in settings.ignore if r.code]
    assert coded and coded[0].code == "port.slow_link" and coded[0].subject and coded[0].reason
    assert any(not r.code for r in settings.ignore)                       # the older style is still shown


def test_every_toml_block_in_the_readme_parses_with_the_real_loader(tmp_path):
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```toml\n(.*?)\n```", readme, re.S)
    assert any("code =" in block for block in blocks)
    for block in blocks:
        tomllib.loads(block)
        if "[[ignore]]" in block or "[thresholds]" in block:
            load_settings(write(tmp_path, block))
