"""`diagnose --json` and the stable finding codes it depends on."""

import ast
import json
import re
from pathlib import Path

import pytest
from docs_support import all_docs_text

from unifi_sentinel import cli
from unifi_sentinel import diagnose as diagnose_module
from unifi_sentinel.diagnose import CODES, Finding, apply_ignores, findings_json
from unifi_sentinel.settings import IgnoreRule

DIAGNOSE_PACKAGE = Path(diagnose_module.__file__).parent
FINDING_KEYS = {"severity", "code", "subject", "message", "mac"}


def run(fake_client, monkeypatch, argv):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    return cli.main(argv)


def run_json(fake_client, monkeypatch, capsys, *extra):
    code = run(fake_client, monkeypatch, ["diagnose", "--json", *extra])
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


# -- the codes ---------------------------------------------------------------------

def code_strings(node):
    """The code strings an expression can evaluate to: a literal, or either branch of a conditional."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return code_strings(node.body) | code_strings(node.orelse)
    raise AssertionError(f"code= must be a string literal or a conditional of literals (line {node.lineno})")


def finding_calls():
    """Every ``Finding(...)`` call in the diagnose package, as ``(file, line, set of code strings it can pass)``."""
    calls = []
    for path in sorted(DIAGNOSE_PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Finding":
                codes = set().union(*(code_strings(kw.value) for kw in node.keywords if kw.arg == "code"))
                calls.append((f"{path.name}:{node.lineno}", codes))
    return calls


def test_every_finding_in_the_checks_has_a_code_from_the_catalogue():
    calls = finding_calls()
    assert len(calls) >= 40                                    # the scan found the checks, not nothing
    missing = [where for where, codes in calls if not codes]
    assert not missing, f"Finding(...) without code= at {missing}"
    unknown = {(where, c) for where, codes in calls for c in codes if c not in CODES}
    assert not unknown, f"codes not in CODES: {sorted(unknown)}"


def test_every_code_in_the_catalogue_is_used():
    used = set().union(*(codes for _, codes in finding_calls()))
    assert set(CODES) - used == set(), "codes in CODES that no check emits"


def test_codes_have_the_documented_shape_and_a_description():
    for code, description in CODES.items():
        assert re.fullmatch(r"[a-z]+\.[a-z_]+", code), code
        assert description and description == description.strip()


def test_the_published_codes_are_pinned():
    """A code is an interface for scripts: changing or removing one must be a deliberate edit here."""
    assert sorted(CODES) == [
        "client.link_local_ip", "client.no_ip", "client.private_mac_summary", "controller.legacy_unavailable", "controller.pending_adoption",
        "device.cpu_high", "device.memory_high", "device.offline", "device.overheating", "event.client_disconnects",
        "event.client_roams", "event.device_unreachable", "event.internet_latency", "event.ip_conflict",
        "event.log_truncated", "health.device_subsystem", "health.subsystem", "internet.drops",
        "internet.latency", "internet.speedtest_failed", "ip.duplicate", "link.below_capability",
        "port.drops", "port.errors", "port.half_duplex", "port.link_flaps", "port.poe_budget",
        "port.slow_link", "port.stp", "reservation.duplicate", "reservation.in_dhcp_pool",
        "reservation.ip_in_use", "reservation.ip_mismatch", "reservation.never_seen", "reservation.offline",
        "reservation.outside_subnet", "reservation.pool_unknown", "reservation.private_mac",
        "wan.availability",
        "wan.cgnat", "wan.double_nat", "wan.link_local_address", "wan.monitor_availability", "wan.speedtest_slow", "wifi.client_retries",
        "wifi.client_satisfaction", "wifi.radio_retries", "wifi.radio_satisfaction",
        "wifi.radio_utilization", "wifi.weak_signal"]


def test_finding_to_dict_and_default_code():
    f = Finding("warning", "Garage AP", "device is offline", "AA:00:00:00:00:04", code="device.offline")
    assert f.to_dict() == {"severity": "warning", "code": "device.offline", "subject": "Garage AP",
                           "message": "device is offline", "mac": "AA:00:00:00:00:04"}
    assert Finding("info", "x", "y").code == "" and Finding("info", "x", "y").to_dict()["mac"] == ""


# -- the JSON document ---------------------------------------------------------------

def test_json_has_the_documented_shape(fake_client, monkeypatch, capsys):
    code, doc, err = run_json(fake_client, monkeypatch, capsys)
    assert set(doc) == {"version", "areas", "summary", "findings"} and doc["version"] == 1
    assert doc["areas"] == ["devices", "health", "wan", "clients", "reservations", "ports", "wifi", "events"]
    assert set(doc["summary"]) == {"critical", "warning", "info", "ignored"}
    assert all(set(f) == FINDING_KEYS for f in doc["findings"])
    assert all(f["code"] in CODES for f in doc["findings"])             # the real checks never emit a blank code
    assert sum(doc["summary"][s] for s in ("critical", "warning", "info")) == len(doc["findings"])
    assert doc["summary"]["ignored"] == 0
    assert "Warning:" in err                                           # warnings stay on stderr, out of the JSON


def test_json_findings_match_the_text_output(fake_client, monkeypatch, capsys):
    run(fake_client, monkeypatch, ["diagnose", "--no-emoji"])
    text = capsys.readouterr().out
    _, doc, _ = run_json(fake_client, monkeypatch, capsys)
    for f in doc["findings"]:
        assert f"{f['subject']}: {f['message']}" in text
    assert f"{doc['summary']['warning']} warnings" in text


def test_the_codes_cover_the_checks_the_fixture_triggers(fake_client, monkeypatch, capsys):
    _, doc, _ = run_json(fake_client, monkeypatch, capsys)
    codes = {f["code"] for f in doc["findings"]}
    assert {"device.offline", "device.cpu_high", "port.link_flaps", "port.errors", "port.half_duplex",
            "port.drops", "port.stp", "port.poe_budget", "port.slow_link", "link.below_capability",
            "reservation.outside_subnet", "event.ip_conflict", "health.device_subsystem"} <= codes


def test_no_findings_is_a_valid_empty_document():
    doc = json.loads(findings_json([], []))
    assert doc == {"version": 1, "areas": ["devices", "health", "wan", "clients", "reservations", "ports", "wifi",
                                           "events"],
                   "summary": {"critical": 0, "warning": 0, "info": 0, "ignored": 0}, "findings": []}


def test_json_ignores_emoji_options(fake_client, monkeypatch, capsys):
    _, plain, _ = run_json(fake_client, monkeypatch, capsys)
    _, no_emoji, _ = run_json(fake_client, monkeypatch, capsys, "--no-emoji")
    assert plain == no_emoji


def test_json_respects_no_events(fake_client, monkeypatch, capsys):
    _, doc, _ = run_json(fake_client, monkeypatch, capsys, "--no-events")
    assert not any(f["code"].startswith("event.") for f in doc["findings"])
    assert fake_client.session.posts == []                              # the one approved POST was not sent


# -- exit codes ----------------------------------------------------------------------

def test_exit_codes_are_the_same_with_and_without_json(fake_client, monkeypatch, capsys):
    for extra in ([], ["--fail-on", "critical"], ["--fail-on", "info"], ["--no-events"]):
        text_code = run(fake_client, monkeypatch, ["diagnose", *extra])
        capsys.readouterr()
        json_code, _, _ = run_json(fake_client, monkeypatch, capsys, *extra)
        assert json_code == text_code, extra
    assert run(fake_client, monkeypatch, ["diagnose", "--json", "--fail-on", "critical"]) == 0


def test_a_critical_finding_exits_2_with_json(fake_client, monkeypatch, capsys):
    for device in fake_client.session.fx["devices"]:
        if device["name"] == "Gateway":
            device["state"] = "OFFLINE"
    code, doc, _ = run_json(fake_client, monkeypatch, capsys)
    assert code == 2 and doc["summary"]["critical"] >= 1
    assert any(f["severity"] == "critical" and f["code"] == "device.offline" for f in doc["findings"])


def test_a_config_error_prints_no_json_and_exits_3(fake_client, monkeypatch, capsys, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("[thresholds]\nnot_a_setting = 1\n")
    assert run(fake_client, monkeypatch, ["diagnose", "--json", "--config", str(bad)]) == cli.EXIT_ERROR
    captured = capsys.readouterr()
    assert captured.out == "" and "ERROR:" in captured.err


# -- ignored findings ------------------------------------------------------------------

IGNORES = ('[[ignore]]\nsubject = "Garage AP"\nmessage = "offline"\nreason = "spare AP, kept unplugged on purpose"\n')


def test_ignored_findings_are_counted_and_listed_only_with_show_ignored(fake_client, monkeypatch, capsys):
    Path("unifi-sentinel.toml").write_text(IGNORES)
    _, doc, _ = run_json(fake_client, monkeypatch, capsys)
    assert doc["summary"]["ignored"] == 1 and "ignored" not in doc
    assert not any(f["subject"] == "Garage AP" for f in doc["findings"])

    _, doc, _ = run_json(fake_client, monkeypatch, capsys, "--show-ignored")
    (item,) = doc["ignored"]
    assert set(item) == FINDING_KEYS | {"reason"}
    assert item["subject"] == "Garage AP" and item["code"] == "device.offline"
    assert item["reason"] == "spare AP, kept unplugged on purpose"


def test_ignored_findings_do_not_affect_the_exit_code(fake_client, monkeypatch, capsys):
    Path("unifi-sentinel.toml").write_text('[[ignore]]\nsubject = "*"\nreason = "everything"\n')
    code, doc, _ = run_json(fake_client, monkeypatch, capsys)
    assert code == 0 and doc["findings"] == [] and doc["summary"]["ignored"] > 0


def test_findings_json_pairs_each_ignored_finding_with_its_rule():
    kept, ignored = apply_ignores(
        [Finding("warning", "A", "x", code="device.offline"), Finding("info", "B", "y", code="port.slow_link")],
        (IgnoreRule(subject="B", reason="known"),))
    doc = json.loads(findings_json(kept, ignored, show_ignored=True))
    assert [f["subject"] for f in doc["findings"]] == ["A"]
    assert doc["ignored"][0]["reason"] == "known" and doc["summary"] == {
        "critical": 0, "warning": 1, "info": 0, "ignored": 1}


# -- names ------------------------------------------------------------------------------

def test_hostile_names_stay_valid_json_and_are_not_altered(fake_client, monkeypatch, capsys):
    hostile = "evil\x1b[31m\n[CRITICAL] forged\u202e"
    for device in fake_client.session.fx["devices"]:
        if device["name"] == "Garage AP":
            device["name"] = "Garage AP" + hostile
    for legacy in fake_client.session.fx["legacy"]["device"]:
        if legacy.get("name") == "Garage AP":
            legacy["name"] = "Garage AP" + hostile
    run(fake_client, monkeypatch, ["diagnose", "--json"])
    raw = capsys.readouterr().out
    assert "\x1b" not in raw and "\u202e" not in raw and "\n[CRITICAL]" not in raw    # escaped by json
    doc = json.loads(raw)
    assert any(f["subject"] == "Garage AP" + hostile for f in doc["findings"])        # raw once decoded


# -- the other views carry the codes too -----------------------------------------------------

def test_topology_json_findings_have_codes(fake_client, monkeypatch, capsys):
    run(fake_client, monkeypatch, ["topology", "--json"])
    tree = json.loads(capsys.readouterr().out)

    def findings(nodes):
        for n in nodes:
            yield from n["findings"]
            yield from findings(n["children"])

    found = list(findings(tree["roots"]))
    assert found and all(f["code"] in CODES for f in found)


def test_client_json_findings_have_codes(fake_client, monkeypatch, capsys):
    run(fake_client, monkeypatch, ["client", "old-printer", "--json"])
    detail = json.loads(capsys.readouterr().out)
    assert detail["findings"] and all(f["code"] in CODES for f in detail["findings"])
    assert all(set(f) == {"severity", "code", "subject", "message"} for f in detail["findings"])


@pytest.mark.parametrize("argv", [["diagnose", "--json"]])
def test_json_goes_to_stdout_only(fake_client, monkeypatch, capsys, argv):
    run(fake_client, monkeypatch, argv)
    captured = capsys.readouterr()
    json.loads(captured.out)                                                # the whole of stdout is one document
    assert captured.err.count("\n") >= 1 and "{" not in captured.err


def test_the_readme_documents_every_code_and_the_flag():
    readme = all_docs_text()
    missing = [code for code in CODES if f"`{code}`" not in readme]
    assert not missing, f"codes missing from the README table: {missing}"
    assert "diagnose --json" in readme
