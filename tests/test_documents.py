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
    client_document,
    diagnose_document,
    diff_document,
    doctor_document,
    events_document,
    export_document,
    firewall_document,
    info_document,
    new_clients_document,
    query_document,
    snapshot_document,
    topology_document,
    wan_document,
    wifi_document,
)
from homelab_probe.settings import DiagnoseSettings, IgnoreRule
from homelab_probe.snapshot import EventQuery

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
         lambda: _render_firewall_document(fake_client, render_firewall)),
        (["audit", "--no-emoji"], lambda: render_findings(audit_document(fake_client, "default", echo=False).data,
                                                          False)),
    ]
    for argv, expected in cases:
        _, out, _ = run_command(fake_client, argv)
        assert out.rstrip("\n") == expected(), argv


def _render_firewall_document(fake_client, render_firewall):
    document = firewall_document(fake_client, "default", echo=False)
    return render_firewall(document.data, True, False, zone_names=document.zone_names)


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


def test_firewall_zone_ids_stay_out_of_json_and_are_available_to_text(fake_client):
    fake_client.session.fx["legacy_v2"]["firewall/zone"][0].pop("_id")
    document = firewall_document(fake_client, "default", echo=False)
    assert all("id" not in zone for zone in document.data["zones"])
    assert "z-int" not in document.zone_names and document.zone_names["z-ext"] == "External"
    json.dumps(document.data)


# -- events, client, query and new-clients ---------------------------------------------------------------

def query_doc(**kwargs):
    return lambda c: query_document(c, "default", echo=False, **kwargs)


DAY = EventQuery(24 * 3600)
# name -> (argv, how to build the document, the document's name, whether --json is a bare array)
BATCH_2 = {
    "events": (["events", "--json", "--since", "6h"],
               lambda c: events_document(c, "default", EventQuery(6 * 3600), echo=False), "events", True),
    "events-filtered": (["events", "--json", "--client", "phone", "--limit", "2"],
                        lambda c: events_document(c, "default", DAY, "phone", limit=2, echo=False), "events", True),
    "events-summary": (["events", "--summary", "--json"],
                       lambda c: events_document(c, "default", DAY, summary=True, echo=False), "events", False),
    "client": (["client", "desktop", "--json"],
               lambda c: client_document(c, "default", "desktop", echo=False), "client", False),
    "client-no-events": (["client", "phone", "--json", "--no-events"],
                         lambda c: client_document(c, "default", "phone", events=False, echo=False), "client", False),
    "query-all": (["query", "--json"], query_doc(), "query", True),
    "query-devices": (["query", "devices", "--json"], query_doc(kind="devices"), "query", True),
    "query-clients": (["query", "clients", "--json", "--include-offline"],
                      query_doc(kind="clients", include_offline=True), "query", True),
    "query-clients-filtered": (["query", "clients", "--json", "--ssid", "iot", "--search", "a"],
                               query_doc(kind="clients", ssid="iot", search="a"), "query", True),
    "query-ports": (["query", "ports", "--json", "--down"], query_doc(kind="ports", down=True), "query", True),
    "query-reservations": (["query", "reservations", "--json"], query_doc(kind="reservations"), "query", True),
    "query-networks": (["query", "networks", "--json"], query_doc(kind="networks"), "query", True),
    "query-wlans": (["query", "wlans", "--json"], query_doc(kind="wlans"), "query", True),
    "new-clients": (["new-clients", "--json"], lambda c: new_clients_document(c, "default", echo=False),
                    "new-clients", True),
}


@pytest.fixture
def frozen(monkeypatch):
    """The fixture's event times are relative to the clock; two reads must see the same one to be compared."""
    monkeypatch.setattr("homelab_probe.demo.session.time", types.SimpleNamespace(time=lambda: NOW_MS / 1000))


@pytest.mark.parametrize("name", BATCH_2)
def test_each_document_of_the_second_batch_is_what_its_json_output_prints(fake_client, frozen, name):
    argv, make, document_name, bare_array = BATCH_2[name]
    code, out, err = run_command(fake_client, argv)
    with utc():
        document = make(fake_client)
    assert code == 0 and document.name == document_name
    assert err == "".join(f"Warning: {w}\n" for w in document.warnings)
    assert document.data == json.loads(out) and document.to_json() == out.rstrip("\n")
    assert isinstance(document.data, list) is bare_array
    json.dumps(document.data)


def test_the_text_of_the_second_batch_is_rendered_from_the_documents(fake_client, frozen):
    from homelab_probe.client_view import render_detail
    from homelab_probe.events import render_events_text
    from homelab_probe.new_clients import render_table as render_new
    from homelab_probe.query import render_csv, render_table

    with utc():
        since = EventQuery(24 * 3600)
        listing = events_document(fake_client, "default", since, limit=3, echo=False)
        summary = events_document(fake_client, "default", since, summary=True, echo=False)
        new_clients = new_clients_document(fake_client, "default", echo=False)
        cases = [
            (["events", "--limit", "3"], render_events_text(listing.data, False, **{
                "more": listing.meta["more"], "cap_truncated": listing.meta["cap_truncated"]})),
            (["events", "--summary"], render_events_text(summary.data, True, cap_truncated=False)),
            (["client", "desktop", "--no-emoji"],
             render_detail(client_document(fake_client, "default", "desktop", echo=False).data, False)),
            (["query", "ports"], render_table(query_document(fake_client, "default", "ports", echo=False).data, "ports")),
            (["query", "clients", "--csv"],
             render_csv(query_document(fake_client, "default", "clients", echo=False).data, "clients")),
            (["new-clients"], render_new(new_clients.data, *[new_clients.meta[k] for k in ("since", "ungrouped", "unknown")])),
        ]
        for argv, expected in cases:
            _, out, _ = run_command(fake_client, argv)
            assert out.rstrip("\n") == expected.rstrip("\n"), argv


def test_a_client_that_is_not_one_match_gives_the_candidates_and_no_data(fake_client, frozen):
    none = client_document(fake_client, "default", "zzz-nobody", echo=False)
    assert none.data == {} and none.meta["matches"] == []
    several = client_document(fake_client, "default", "bb0000", echo=False)
    assert several.data == {} and len(several.meta["matches"]) > 1
    code, out, err = run_command(fake_client, ["client", "bb0000", "--json"])
    assert code == 4 and out == "" and "clients match" in err


def test_the_client_document_reads_the_events_only_for_a_match_and_when_asked(fake_client):
    client_document(fake_client, "default", "zzz-nobody", echo=False)
    assert not fake_client.session.posts                               # nothing matched: no POST
    client_document(fake_client, "default", "desktop", events=False, echo=False)
    assert not fake_client.session.posts
    client_document(fake_client, "default", "desktop", echo=False)
    assert len(fake_client.session.posts) == 1


def test_the_events_document_says_when_the_limit_or_the_cap_cut_the_list(fake_client):
    cut = events_document(fake_client, "default", EventQuery(24 * 3600), limit=1, echo=False)
    assert cut.meta == {"more": True, "cap_truncated": False} and len(cut.data) == 1
    whole = events_document(fake_client, "default", EventQuery(24 * 3600), limit=0, echo=False)
    assert whole.meta["more"] is False and len(whole.data) > 1
    summary = events_document(fake_client, "default", EventQuery(24 * 3600), limit=1, summary=True, echo=False)
    assert summary.data["total"] == len(whole.data) and summary.data["truncated"] is False   # a summary ignores --limit


def test_the_events_document_says_when_the_read_cap_stopped_the_list(fake_client, monkeypatch, capsys):
    from homelab_probe import snapshot as snapshot_module
    from homelab_probe.events import render_events_text

    monkeypatch.setattr(snapshot_module, "EVENT_PAGE_SIZE", 2)
    monkeypatch.setattr(snapshot_module, "MAX_EVENTS", 4)
    document = events_document(fake_client, "default", DAY, limit=0, echo=False)
    assert len(document.data) == 4 and document.meta == {"more": True, "cap_truncated": True}
    assert "the 20,000-event read cap was reached" in render_events_text(document.data, False, **document.meta)
    code, out, _ = run_command(fake_client, ["events", "--limit", "0"])
    assert code == 0 and "the 20,000-event read cap was reached" in out


def test_a_query_that_could_not_read_what_it_lists_raises_instead_of_returning_nothing(fake_client):
    real = fake_client.legacy_rest

    def broken(site_ref, resource):
        if resource in ("networkconf", "wlanconf"):
            raise UniFiAPIError("HTTP 500")
        return real(site_ref, resource)

    fake_client.legacy_rest = broken
    for kind, text in (("networks", "no networks were returned"), ("wlans", "Wi-Fi networks could not be read")):
        with pytest.raises(UniFiAPIError, match=text):
            query_document(fake_client, "default", kind, echo=False)
    real_stat = fake_client.legacy_stat

    def no_sta(site_ref, resource):
        if resource == "sta":
            raise UniFiAPIError("HTTP 500")
        return real_stat(site_ref, resource)

    fake_client.legacy_stat = no_sta
    with pytest.raises(UniFiAPIError, match="need the connected-client details"):
        query_document(fake_client, "default", "clients", ssid="x", echo=False)


def test_the_query_document_reads_what_the_kind_needs(fake_client):
    from homelab_probe.documents import query_needs

    assert query_needs("networks") == query_needs("networks", True, "x") and not query_needs("networks").wlans
    assert query_needs("wlans").wlans and not query_needs("wlans").clients
    assert query_needs("clients", True).offline and query_needs("clients", network="").networks
    assert query_needs("reservations").reservations and not query_needs("devices").reservations


def test_the_offline_reservations_use_the_threshold_of_the_settings(fake_client):
    default = query_document(fake_client, "default", "reservations", offline=True, settings=DiagnoseSettings(),
                             echo=False)
    long = query_document(fake_client, "default", "reservations", offline=True,
                          settings=DiagnoseSettings(reserved_offline_warn_days=100_000), echo=False)
    assert default.meta == {"kind": "reservations", "offline": True, "site": documents.site_identity(
        fake_client.session.fx["sites"][0])} and len(long.data) <= len(default.data)
    none = query_document(fake_client, "default", "reservations", offline=True, echo=False)
    assert len(none.data) == len(query_document(fake_client, "default", "reservations", echo=False).data)


def test_a_degraded_read_of_the_query_is_in_the_document_and_not_printed_when_quiet(fake_client, capsys):
    real_stat = fake_client.legacy_stat

    def no_history(site_ref, resource):
        if resource == "alluser":
            raise UniFiAPIError("HTTP 500")
        return real_stat(site_ref, resource)

    fake_client.legacy_stat = no_history
    document = query_document(fake_client, "default", "clients", include_offline=True, echo=False)
    assert document.warnings and capsys.readouterr().err == ""
    loud = query_document(fake_client, "default", "clients", include_offline=True)
    assert capsys.readouterr().err == "".join(f"Warning: {w}\n" for w in loud.warnings) and loud.warnings


# -- diagnose, snapshot, diff and export -------------------------------------------------------------------

def test_the_diagnose_document_is_what_diagnose_json_prints(fake_client, frozen):
    from homelab_probe.diagnose import AREA_NAMES

    cases = [(["diagnose", "--json"], None), (["diagnose", "--json", "--no-events"], [a for a in AREA_NAMES if a != "events"]),
             (["diagnose", "--json", "--only", "wan,wifi"], ["wan", "wifi"]),
             (["diagnose", "--json", "--skip", "wifi"], [a for a in AREA_NAMES if a != "wifi"])]
    for argv, areas in cases:
        code, out, err = run_command(fake_client, argv)
        with utc():
            document = diagnose_document(fake_client, "default", areas=areas, echo=False)
        assert code in (0, 1, 2) and document.name == "diagnose", argv
        assert err == "".join(f"Warning: {w}\n" for w in document.warnings), argv
        assert document.data == json.loads(out) and document.to_json() == out.rstrip("\n"), argv
        assert document.data["areas"] == (areas or list(AREA_NAMES)) and document.meta["complete"] is True


def test_the_diagnose_text_comes_from_its_document_with_the_checked_note_and_the_ignored_list(fake_client, frozen,
                                                                                               tmp_path):
    from homelab_probe.diagnose import AREA_NAMES, render_findings

    config = tmp_path / "hlp.toml"
    config.write_text('[[ignore]]\ncode = "device.offline"\nreason = "spare"\n')
    ignoring = DiagnoseSettings(ignore=(IgnoreRule(code="device.offline", reason="spare"),))
    no_events = [a for a in AREA_NAMES if a != "events"]
    cases = [  # argv, settings, areas, show_ignored, checked note
        (["--no-events"], None, no_events, False, False),
        (["--only", "devices,wan", "--show-ignored", "--config", str(config)], ignoring, ["devices", "wan"], True, True),
        (["--skip", "wifi", "--config", str(config)], ignoring, [a for a in AREA_NAMES if a != "wifi"], False, True),
    ]
    for argv, settings, areas, show_ignored, checked in cases:
        _, out, _ = run_command(fake_client, ["diagnose", "--no-emoji", *argv])
        with utc():
            data = diagnose_document(fake_client, "default", settings, areas, show_ignored=show_ignored,
                                     echo=False).data
        assert out.rstrip("\n") == render_findings(data, False, show_ignored, checked), argv
        assert ("Checked: " in out) is checked and ("Ignored (" in out) is show_ignored, argv


def test_the_snapshot_document_is_the_saved_file(fake_client, tmp_path, frozen):
    from homelab_probe.history import list_snapshots, load_snapshot

    code, out, _ = run_command(fake_client, ["snapshot", "--dir", str(tmp_path / "saved")])
    (saved,) = list_snapshots(tmp_path / "saved")
    with utc():
        document = snapshot_document(fake_client, "default", echo=False)
    on_disk, live = load_snapshot(saved), document.data
    assert code == 0 and document.name == "snapshot"
    on_disk["captured_at"] = live["captured_at"] = ""                 # the one value that is the time of the call
    assert live == on_disk and live["controller"]["application_version"] == "10.0.0"
    json.dumps(document.data)


def test_a_failing_version_lookup_leaves_the_version_blank_not_the_snapshot_unsaved(fake_client):
    def no_info():
        raise UniFiAPIError("HTTP 500")

    fake_client.info = no_info
    assert snapshot_document(fake_client, "default", echo=False).data["controller"]["application_version"] == ""


def test_the_diff_document_is_what_diff_json_prints(fake_client, tmp_path, frozen):
    from homelab_probe.history import list_snapshots, load_snapshot

    saved_dir = tmp_path / "saved"
    run_command(fake_client, ["snapshot", "--dir", str(saved_dir)])
    fake_client.session.fx["devices"][1]["name"] = "Renamed Switch"
    code, out, err = run_command(fake_client, ["diff", "--dir", str(saved_dir), "--json"])
    old = load_snapshot(list_snapshots(saved_dir)[0])
    with utc():
        live = diff_document(fake_client, "default", old, echo=False)
    assert code == 0 and live.name == "diff" and list(live.data)[0] == "version"
    assert live.warnings and err == "".join(f"Warning: {w}\n" for w in live.warnings)   # the live read's, as data
    assert live.data == json.loads(out) and live.data["devices"]["changed"]
    newer = snapshot_document(fake_client, "default", echo=False).data
    both = diff_document(fake_client, "default", old, newer)           # two records: nothing is read
    assert both.data["devices"] == live.data["devices"] and both.warnings == []


def test_the_diff_text_is_rendered_from_its_document(fake_client, tmp_path, frozen):
    from homelab_probe.history import list_snapshots, load_snapshot, render_diff

    saved_dir = tmp_path / "saved"
    run_command(fake_client, ["snapshot", "--dir", str(saved_dir)])
    fake_client.session.fx["devices"][1]["name"] = "Renamed Switch"
    _, out, _ = run_command(fake_client, ["diff", "--dir", str(saved_dir)])
    old = load_snapshot(list_snapshots(saved_dir)[0])
    with utc():
        document = diff_document(fake_client, "default", old, echo=False)
    assert out.splitlines()[2:] == render_diff(document.data, "x", "y").splitlines()[2:]


def test_the_export_document_and_the_files_are_built_from_the_same_rows(fake_client, tmp_path, frozen):
    import csv

    from homelab_probe.export import JSON_FILENAME

    run_command(fake_client, ["export", "--format", "json", "-o", str(tmp_path / "j")])
    run_command(fake_client, ["export", "-o", str(tmp_path / "c")])
    with utc():
        document = export_document(fake_client, "default", echo=False)
    assert document.name == "export" and document.data == json.loads((tmp_path / "j" / JSON_FILENAME).read_text())
    assert [r["Name"] for r in document.meta["rows"]] == [
        r["Name"] for r in csv.DictReader((tmp_path / "c" / "unifi_clients.csv").open(newline=""))]
    assert document.meta["connected"] == (4, 2) and document.meta["site"]["name"]
    offline = export_document(fake_client, "default", include_offline=True, echo=False)
    assert len(offline.meta["rows"]) > len(document.meta["rows"])


def test_the_snapshot_functions_that_take_a_snapshot_still_agree_with_the_document_builders(fake_client):
    from homelab_probe.export import export_data, inventory_rows, switch_ports
    from homelab_probe.export import export_document as export_of_snapshot
    from homelab_probe.snapshot import Needs, collect_snapshot

    snap = collect_snapshot(fake_client, "default", Needs(offline=False))
    assert export_of_snapshot(snap) == export_data(inventory_rows(snap), switch_ports(snap))


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
