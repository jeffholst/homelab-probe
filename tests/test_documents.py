"""Documents (``documents.py``): the data a command prints, as a dict, with the warnings of the read behind it."""

import ast
import contextlib
import datetime
import io
import json
import os
import sys
import time
import types
from pathlib import Path

import pytest
from golden_support import run_command

from homelab_probe import documents, logs
from homelab_probe.client import UniFiAPIError
from homelab_probe.diagnose import Finding, findings_from_document
from homelab_probe.doctor import CHECKS, OK, make
from homelab_probe.documents import (
    Document,
    audit_document,
    doctor_document,
    firewall_document,
    info_document,
    topology_document,
    wan_document,
    wifi_document,
)
from homelab_probe.settings import DiagnoseSettings, IgnoreRule

PACKAGE = Path(__file__).resolve().parent.parent / "homelab_probe"
NOW_MS = 1_800_000_000_000


def freeze_time(monkeypatch):
    """``wan`` ages its speedtests from the clock; pin it so two runs can be compared."""
    monkeypatch.setattr("homelab_probe.wan.time", types.SimpleNamespace(time=lambda: NOW_MS / 1000))


@contextlib.contextmanager
def utc():
    """``run_command`` runs in UTC (times are rendered in local time), so a document built to compare with it must too."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ")
        else:
            os.environ["TZ"] = previous
        time.tzset()


def break_speedtests(fake_client):
    real = fake_client.legacy_v2

    def legacy_v2(site, resource, *args, **kwargs):
        if resource == "speedtest":
            raise UniFiAPIError("HTTP 500")
        return real(site, resource, *args, **kwargs)

    fake_client.legacy_v2 = legacy_v2


# -- wan --------------------------------------------------------------------------------------------

def test_the_wan_document_is_what_wan_json_prints(fake_client, monkeypatch):
    freeze_time(monkeypatch)
    code, out, err = run_command(fake_client, ["wan", "--json"])
    assert code == 0
    with utc():
        document = wan_document(fake_client, "default", echo=False)
    assert document.name == "wan"
    assert err == "".join(f"Warning: {w}\n" for w in document.warnings)      # the fixture has one degraded read
    assert document.data == json.loads(out)
    assert document.to_json() == out.rstrip("\n")
    assert list(document.data)[0] == "version"
    json.dumps(document.data)                                    # serializable as it is


def test_the_text_output_is_rendered_from_the_document(fake_client, monkeypatch):
    from homelab_probe.wan import render_text

    freeze_time(monkeypatch)
    code, out, _ = run_command(fake_client, ["wan"])
    with utc():
        expected = render_text(wan_document(fake_client, "default", echo=False).data)
    assert code == 0 and out.rstrip("\n") == expected


def test_the_days_and_settings_reach_the_report(fake_client, monkeypatch):
    freeze_time(monkeypatch)
    short = wan_document(fake_client, "default", days=1, echo=False).data["speedtests"]
    long = wan_document(fake_client, "default", days=3650, echo=False).data["speedtests"]
    assert (short["days"], long["days"]) == (1, 3650) and short["count"] < long["count"]
    strict = DiagnoseSettings(wan_speed_drop_pct=99)
    loose = DiagnoseSettings(wan_speed_drop_pct=1)
    assert wan_document(fake_client, "default", settings=strict, echo=False).data["speedtests"]["slow_threshold_pct"] == 99
    assert wan_document(fake_client, "default", settings=loose, echo=False).data["speedtests"]["slow_threshold_pct"] == 1


def test_a_degraded_read_is_in_the_document_and_not_printed_when_quiet(fake_client, capsys):
    break_speedtests(fake_client)
    document = wan_document(fake_client, "default", echo=False)
    assert any(w.startswith("speedtest history unavailable") for w in document.warnings)
    assert capsys.readouterr().err == ""
    assert document.data["speedtests"]["count"] == 0
    json.dumps(document.data)


def test_the_command_line_still_prints_the_warning_and_the_document_has_it_too(fake_client, capsys):
    break_speedtests(fake_client)
    document = wan_document(fake_client, "default")                 # echo: the command-line behavior
    err = capsys.readouterr().err
    assert err == "".join(f"Warning: {w}\n" for w in document.warnings)
    assert document.warnings[-1].startswith("speedtest history unavailable")
    code, _, cli_err = run_command(fake_client, ["wan"])
    assert code == 0 and cli_err == err


def test_a_quiet_document_logs_the_warning_at_debug_only():
    stream = _capture("DEBUG")
    with logs.collect_warnings(quiet=True) as collected:
        logs.warn("one")
    assert collected == ["one"] and '"level": "DEBUG"' in stream.getvalue() and '"event": "warning"' in stream.getvalue()
    stream = _capture("INFO")
    with logs.collect_warnings(quiet=True):
        logs.warn("two")
    assert stream.getvalue() == ""
    with logs.collect_warnings(quiet=True):
        pass
    stream = _capture("INFO")
    logs.warn("three")                                              # quiet ended with its block
    assert '"level": "WARNING"' in stream.getvalue()


def _capture(level):
    stream = io.StringIO()
    logs.configure("json", level, stream=stream)
    return stream


# -- wifi, topology, firewall and audit ----------------------------------------------------------------

REPORTS = {
    "wifi": (["wifi", "--json"], lambda c: wifi_document(c, "default", echo=False)),
    "topology": (["topology", "--json"], lambda c: topology_document(c, "default", echo=False)),
    "topology-clients": (["topology", "--json", "--clients"],
                         lambda c: topology_document(c, "default", with_clients=True, echo=False)),
    "firewall": (["firewall", "--json"], lambda c: firewall_document(c, "default", echo=False)),
    "firewall-all": (["firewall", "--json", "--all", "--search", "block"],
                     lambda c: firewall_document(c, "default", True, "block", echo=False)),
    "audit": (["audit", "--json"], lambda c: audit_document(c, "default", echo=False)),
}


@pytest.mark.parametrize("name", REPORTS)
def test_each_document_is_what_the_json_output_prints(fake_client, name):
    argv, make = REPORTS[name]
    code, out, err = run_command(fake_client, argv)
    document = make(fake_client)
    assert document.name == argv[0] and code in (0, 1)
    assert err == "".join(f"Warning: {w}\n" for w in document.warnings)
    assert document.data == json.loads(out) and document.to_json() == out.rstrip("\n")
    assert list(document.data)[0] == "version"
    json.dumps(document.data)


def test_the_text_of_the_other_reports_is_rendered_from_their_documents(fake_client):
    from homelab_probe.diagnose import render_findings
    from homelab_probe.firewall import render_text as render_firewall
    from homelab_probe.topology import render_text as render_topology
    from homelab_probe.wifi import render_text as render_wifi

    cases = [
        (["wifi", "--all"], lambda: render_wifi(wifi_document(fake_client, "default", echo=False).data, True, "")),
        (["topology", "--no-emoji", "--clients"],
         lambda: render_topology(topology_document(fake_client, "default", with_clients=True, echo=False).data,
                                 False, True)),
        (["firewall", "--zones", "--no-emoji"],
         lambda: render_firewall(firewall_document(fake_client, "default", echo=False).data, True, False)),
        (["audit", "--no-emoji"], lambda: render_findings(audit_document(fake_client, "default", echo=False).data,
                                                          False)),
    ]
    for argv, expected in cases:
        _, out, _ = run_command(fake_client, argv)
        assert out.rstrip("\n") == expected(), argv


def test_every_new_document_reads_exactly_what_its_command_declares():
    assert documents.WIFI_NEEDS.neighbors and documents.FIREWALL_NEEDS.firewall and documents.FIREWALL_NEEDS.reservations
    assert documents.AUDIT_NEEDS.wlans and documents.AUDIT_NEEDS.offline and not documents.AUDIT_NEEDS.legacy_devices
    assert documents.TOPOLOGY_NEEDS == documents.TOPOLOGY_NEEDS.__class__()


def test_the_firewall_document_carries_its_degraded_reads_as_data(fake_client, capsys):
    real = fake_client.legacy_rest

    def broken(site_ref, resource):
        if resource == "portforward":
            raise UniFiAPIError("HTTP 503")
        return real(site_ref, resource)

    fake_client.legacy_rest = broken
    document = firewall_document(fake_client, "default", echo=False)
    assert any(w.startswith("port forwards unavailable") for w in document.warnings)
    assert capsys.readouterr().err == ""                              # quiet: nothing printed, all in the document
    printed = firewall_document(fake_client, "default")
    assert capsys.readouterr().err == "".join(f"Warning: {w}\n" for w in printed.warnings)


def test_the_audit_document_says_when_the_wifi_settings_could_not_be_read(fake_client, capsys):
    real = fake_client.legacy_rest

    def broken(site_ref, resource):
        if resource == "wlanconf":
            raise UniFiAPIError("HTTP 500")
        return real(site_ref, resource)

    fake_client.legacy_rest = broken
    document = audit_document(fake_client, "default", echo=False)
    assert any(w.startswith("Wi-Fi network settings unavailable") for w in document.warnings)
    assert "audit.wifi_unavailable" in {f["code"] for f in document.data["findings"]}
    assert capsys.readouterr().err == ""


def test_the_audit_document_applies_the_ignore_list_as_of_today(fake_client):
    rule = IgnoreRule(subject="Lobby", message="open network", reason="on purpose", until=datetime.date(2030, 1, 1))
    settings = DiagnoseSettings(ignore=(rule,))
    plain = audit_document(fake_client, "default", settings, echo=False, today=datetime.date(2029, 12, 31))
    assert plain.data["summary"]["ignored"] == 1 and "ignored" not in plain.data
    listed = audit_document(fake_client, "default", settings, show_ignored=True, echo=False,
                            today=datetime.date(2029, 12, 31))
    assert listed.data["ignored"] == [{**listed.data["ignored"][0], "reason": "on purpose", "until": "2030-01-01"}]
    expired = audit_document(fake_client, "default", settings, show_ignored=True, echo=False,
                             today=datetime.date(2030, 1, 2))
    assert expired.data["summary"]["ignored"] == 0 and expired.data["ignored"] == []
    assert len(expired.data["findings"]) == len(plain.data["findings"]) + 1


def test_findings_come_back_from_a_document_as_findings(fake_client):
    data = audit_document(fake_client, "default", echo=False).data
    findings = findings_from_document(data)
    assert [f.to_dict() for f in findings] == data["findings"]
    assert all(isinstance(f, Finding) and f.code for f in findings)
    assert findings_from_document({"findings": [{"severity": "info", "code": "x", "subject": "s", "message": "m",
                                                 "mac": ""}]})[0].target_mac is None


def test_a_firewall_zone_without_an_id_has_a_null_id_and_keeps_its_name(fake_client):
    fake_client.session.fx["legacy_v2"]["firewall/zone"][0].pop("_id")
    data = firewall_document(fake_client, "default", echo=False).data
    assert data["zones"][0]["id"] is None and data["zones"][1]["id"]
    json.dumps(data)


# -- info and doctor --------------------------------------------------------------------------------

def test_the_info_document_has_the_application_and_the_sites(fake_client):
    document = info_document(fake_client)
    assert document.name == "info" and document.warnings == []
    assert document.data["application"] == fake_client.info()
    assert [s["ref"] for s in document.data["sites"]] == [s.get("internalReference") for s in fake_client.sites()]
    assert set(document.data["sites"][0]) == {"name", "ref", "id"}
    json.dumps(document.data)


def test_info_prints_what_it_always_printed(fake_client):
    code, out, _ = run_command(fake_client, ["info"])
    assert code == 0 and out.splitlines()[0] == f"Application: {fake_client.info()}"
    assert out.splitlines()[1].startswith("Site: ") and " ref=" in out and " id=" in out


def test_the_doctor_document_is_its_json(capsys):
    check_id = next(iter(CHECKS))
    document = doctor_document([make(check_id, OK, "fine")])
    assert document.name == "doctor" and document.warnings == []
    assert document.data["checks"][0]["id"] == check_id and document.data["version"] == 1
    assert document.to_json() == json.dumps(document.data, indent=2)


def test_the_doctor_command_prints_its_document(capsys):
    from homelab_probe import cli

    cli.main(["doctor", "--json", "--no-events"])
    printed = json.loads(capsys.readouterr().out)
    assert printed["version"] == 1 and printed["checks"]


def test_document_defaults_and_equality():
    assert Document("x", {"a": 1}).warnings == [] and Document("x", {"a": 1}) == Document("x", {"a": 1})
    with pytest.raises(AttributeError):
        Document("x", {}).name = "y"                                 # frozen


# -- the module's own rules --------------------------------------------------------------------------

def test_documents_import_only_the_standard_library_and_this_package():
    imported = set()
    for node in ast.walk(ast.parse((PACKAGE / "documents.py").read_text())):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add("." * node.level + (node.module or "").split(".")[0] if node.level else node.module.split(".")[0])
    outside = {name for name in imported if not name.startswith(".") and name not in sys.stdlib_module_names}
    assert outside == set(), outside
    assert not any("server" in name for name in imported)


def test_documents_never_print():
    tree = ast.parse((PACKAGE / "documents.py").read_text())
    called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "print" not in called and "say" not in called
    assert documents.WAN_NEEDS.health and documents.WAN_NEEDS.speedtests
