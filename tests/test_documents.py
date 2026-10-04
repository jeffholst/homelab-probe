"""Documents (``documents.py``): the data a command prints, as a dict, with the warnings of the read behind it."""

import ast
import contextlib
import io
import json
import os
import sys
import time
import types
from pathlib import Path

import pytest
from golden_support import run_command

from unifi_sentinel import documents, logs
from unifi_sentinel.client import UniFiAPIError
from unifi_sentinel.doctor import CHECKS, OK, make
from unifi_sentinel.documents import Document, doctor_document, info_document, wan_document
from unifi_sentinel.settings import DiagnoseSettings

PACKAGE = Path(__file__).resolve().parent.parent / "unifi_sentinel"
NOW_MS = 1_800_000_000_000


def freeze_time(monkeypatch):
    """``wan`` ages its speedtests from the clock; pin it so two runs can be compared."""
    monkeypatch.setattr("unifi_sentinel.wan.time", types.SimpleNamespace(time=lambda: NOW_MS / 1000))


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
    from unifi_sentinel.wan import render_text

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
    from unifi_sentinel import cli

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
