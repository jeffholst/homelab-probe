"""The dashboard summary (``dashboard.py``, ``documents.dashboard_document``; issue #244).

The states it must tell apart: healthy, warning, critical, partial (an optional read failed), stale (an old cached
answer) and unreachable, and above all that none of the last three ever reads as healthy.
"""

import datetime

import pytest
import requests
from conftest import FakeResponse
from jsonschema import Draft202012Validator
from test_json_schemas import load, strict

from homelab_probe import dashboard, documents
from homelab_probe.client import UniFiAPIError
from homelab_probe.diagnose import CODES
from homelab_probe.diagnose import areas as diagnose_areas
from homelab_probe.documents import dashboard_document, diagnose_document
from homelab_probe.settings import DiagnoseSettings, IgnoreRule
from homelab_probe.sitefile import StoreError
from homelab_probe.triage import TriageStore, finding_id

HEALTHY = DiagnoseSettings(ignore=tuple(IgnoreRule(code=code, reason="test") for code in CODES))
NOT_CRITICAL = DiagnoseSettings(ignore=(IgnoreRule(code="device.overheating", reason="test"),))
NOW = 1_800_000_000.0


def build(fake_client, settings=None, **kwargs):
    kwargs.setdefault("echo", False)
    document = dashboard_document(fake_client, "default", settings, **kwargs)
    strictly = strict(load("dashboard"))
    Draft202012Validator(load("dashboard")).validate(document.data)
    Draft202012Validator(strictly).validate(document.data)          # nothing the schema does not declare
    return document


def fail(fake_client, *suffixes, status=503):
    """Make the reads that end in one of ``suffixes`` fail with an HTTP error."""
    get = fake_client.session.get

    def request(url, *args, **kwargs):
        if any(url.endswith(suffix) for suffix in suffixes):
            return FakeResponse(status, {})
        return get(url, *args, **kwargs)

    fake_client.session.get = request


def refuse(fake_client, error):
    def request(*args, **kwargs):
        raise error

    fake_client.session.get = request


# -- the full picture -------------------------------------------------------------------------------------------

def test_the_fixture_is_critical_and_the_counts_are_those_of_diagnose(fake_client):
    document = build(fake_client, now=NOW)
    data = document.data
    diagnosed = diagnose_document(fake_client, "default", echo=False).data
    assert list(data)[0] == "version" and data["version"] == dashboard.JSON_VERSION
    assert data["status"] == "critical" and data["complete"] is True and data["stale"] is False
    assert data["controller"] == {"state": "ok"} and data["site"] == {"id": "site-1", "name": "Default"}
    findings = data["findings"]
    assert findings["available"] is True and findings["total"] == len(diagnosed["findings"])
    assert findings["by_severity"] == {k: diagnosed["summary"][k] for k in ("critical", "warning", "info")}
    assert findings["ignored"] == diagnosed["summary"]["ignored"] == 0
    assert document.meta == {"complete": True}


def test_the_attention_list_is_the_open_findings_in_the_order_of_the_findings_page_and_is_short(fake_client):
    attention = build(fake_client, now=NOW).data["findings"]["attention"]
    assert len(attention) == dashboard.ATTENTION == 5
    assert [item["rank"] for item in attention] == [1, 2, 3, 4, 5]
    assert attention[0]["code"] == "device.overheating" and attention[0]["severity"] == "critical"
    assert all(set(item) == {"id", "rank", "severity", "code", "subject", "message"} for item in attention)
    diagnosed = diagnose_document(fake_client, "default", echo=False).data["findings"]
    gateway = next(f for f in diagnosed if f["code"] == "device.overheating")
    assert attention[0]["id"] == finding_id(gateway["code"], gateway["subject"], gateway["mac"] or None)


def test_devices_clients_wan_wifi_and_events_have_the_fixtures_numbers(fake_client):
    data = build(fake_client).data
    assert data["devices"] == {"available": True, "total": 4, "online": 3, "offline": 1, "other": 0}
    assert data["clients"] == {"available": True, "connected": 2, "wired": 1, "wireless": 1, "offline": 3}
    assert data["wan"]["status"] == "ok" and data["wan"]["internet_status"] == "ok" and data["wan"]["nat"] == "public"
    assert data["wan"]["availability_pct"] == 100.0 and data["wan"]["latency_ms"] == 20.0
    assert data["wan"]["last_speedtest"]["download_mbps"] == 880.0
    assert data["wifi"] == {"available": True, "access_points": 2, "access_points_online": 1, "radios": 2,
                            "wireless_clients": 1, "lowest_satisfaction": 99.0, "highest_utilization": 30.0}
    events = data["events"]
    assert events["available"] is True and events["window_seconds"] == 86400 and events["truncated"] is False
    assert events["total"] == 10 and events["notable_total"] == 4 == len(events["notable"])
    assert {e["severity"] for e in events["notable"]} == {"high", "medium"}           # low severity is not notable
    assert all(set(e) == {"timestamp", "severity", "category", "event", "message"} for e in events["notable"])
    assert [e["timestamp"] for e in events["notable"]] == sorted((e["timestamp"] for e in events["notable"]),
                                                                  reverse=True)         # newest first


def test_only_the_five_newest_notable_events_are_listed_and_all_are_counted(fake_client):
    template = next(e for e in fake_client.session.events if e["severity"] == "HIGH")
    for number in range(8):
        fake_client.session.events.append({**template, "id": f"extra{number}", "age_s": 10 + number})
    events = build(fake_client).data["events"]
    assert events["notable_total"] == 12 and len(events["notable"]) == dashboard.NOTABLE_EVENTS


def test_a_device_that_is_neither_online_nor_offline_is_counted_as_other(fake_client):
    fake_client.session.fx["devices"][0]["state"] = "UPDATING"
    devices = build(fake_client).data["devices"]
    assert (devices["online"], devices["offline"], devices["other"]) == (2, 1, 1)


# -- healthy, warning, critical -----------------------------------------------------------------------------------

def test_a_complete_fresh_read_with_nothing_found_is_ok(fake_client):
    data = build(fake_client, HEALTHY, now=NOW).data
    assert data["status"] == "ok" and data["complete"] is True and data["stale"] is False
    findings = data["findings"]
    assert findings["total"] == 0 and findings["by_severity"] == {"critical": 0, "warning": 0, "info": 0}
    assert findings["attention"] == [] and findings["ignored"] == 17            # what the ignore list hid is counted


def test_a_warning_without_a_critical_is_a_warning(fake_client):
    data = build(fake_client, NOT_CRITICAL, now=NOW).data
    assert data["status"] == "warning" and data["findings"]["by_severity"]["critical"] == 0
    assert data["findings"]["by_severity"]["warning"] > 0


def test_information_alone_is_ok(fake_client):
    only_info = DiagnoseSettings(ignore=tuple(IgnoreRule(code=code, reason="test") for code in CODES
                                              if code not in ("device.recent_reboot", "port.slow_link")))
    data = build(fake_client, only_info).data
    assert data["findings"]["by_severity"] == {"critical": 0, "warning": 0, "info": 2} and data["status"] == "ok"


def test_an_expired_ignore_rule_does_not_hide_findings(fake_client):
    expired = DiagnoseSettings(ignore=tuple(IgnoreRule(code=code, reason="test", until=datetime.date(2020, 1, 1))
                                            for code in CODES))
    data = build(fake_client, expired, today=datetime.date(2026, 1, 1)).data
    assert data["findings"]["total"] > 0 and data["findings"]["ignored"] == 0 and data["status"] == "critical"


# -- partial reads never read as healthy -------------------------------------------------------------------------

def test_a_failed_optional_read_makes_the_document_incomplete_and_never_ok(fake_client):
    fail(fake_client, "/stat/health")
    document = build(fake_client, HEALTHY)
    data = document.data
    assert data["complete"] is False and data["status"] == "unknown" and document.meta == {"complete": False}
    assert data["wan"] == {"available": False}                                   # not "ok", not zero: not read
    assert any("health" in w for w in document.warnings)
    assert data["devices"]["available"] is True and data["clients"]["available"] is True


def test_a_failed_optional_read_does_not_hide_a_warning_or_a_critical(fake_client):
    fail(fake_client, "/stat/health")
    assert build(fake_client).data["status"] == "critical"
    assert build(fake_client, NOT_CRITICAL).data["status"] == "warning"


def test_without_the_client_history_the_offline_count_is_unknown_not_zero(fake_client):
    fail(fake_client, "/stat/alluser")
    document = build(fake_client, HEALTHY)
    assert document.data["clients"]["offline"] is None and document.data["clients"]["connected"] == 2
    assert document.data["complete"] is False and document.data["status"] == "unknown"


def test_an_empty_client_history_that_was_read_is_none_offline_not_unknown(fake_client):
    fake_client.session.fx["legacy"]["alluser"] = []              # a successful, empty answer with clients connected
    document = build(fake_client, HEALTHY)
    assert document.data["clients"]["connected"] == 2 and document.data["clients"]["offline"] == 0
    assert document.data["complete"] is True and document.data["status"] == "ok"


def test_no_clients_and_no_history_is_zero_offline(fake_client):
    fake_client.session.fx["clients"] = []
    fake_client.session.fx["legacy"]["alluser"] = []
    clients = build(fake_client).data["clients"]
    assert (clients["connected"], clients["offline"]) == (0, 0)


def test_without_the_legacy_device_list_the_wifi_section_is_unavailable(fake_client):
    fail(fake_client, "/stat/device")
    data = build(fake_client, HEALTHY).data
    assert data["wifi"] == {"available": False} and data["status"] == "unknown" and data["complete"] is False


def test_without_the_event_log_the_events_section_is_unavailable_and_the_read_incomplete(fake_client):
    fake_client.session.post = lambda *args, **kwargs: FakeResponse(503, {})
    data = build(fake_client, HEALTHY).data
    assert data["events"] == {"available": False} and data["complete"] is False and data["status"] == "unknown"


def test_a_controller_with_no_gateway_health_is_not_a_healthy_internet(fake_client):
    fake_client.session.fx["legacy"]["health"] = [h for h in fake_client.session.fx["legacy"]["health"]
                                                  if h["subsystem"] != "wan"]
    assert build(fake_client).data["wan"] == {"available": False}


def test_monitors_with_no_availability_give_none_not_a_perfect_score(fake_client):
    for health in fake_client.session.fx["legacy"]["health"]:
        if health["subsystem"] == "wan":
            for stats in health["uptime_stats"].values():
                stats["availability"] = None
                for monitors in (stats.get("monitors", []), stats.get("alerting_monitors", [])):
                    for monitor in monitors:
                        monitor["availability"] = None
    fake_client.session.fx["legacy_v2"]["speedtest"]["data"] = []
    wan = build(fake_client).data["wan"]
    assert wan["availability_pct"] is None and wan["last_speedtest"] is None


def test_access_points_without_radio_numbers_give_none(fake_client):
    for device in fake_client.session.fx["legacy"]["device"]:
        device.pop("radio_table_stats", None)
    wifi = build(fake_client).data["wifi"]
    assert wifi["radios"] == 0 and wifi["lowest_satisfaction"] is None and wifi["highest_utilization"] is None


# -- stale answers ------------------------------------------------------------------------------------------------

def test_an_answer_served_from_an_older_cache_is_stale_and_never_ok(fake_client):
    data = build(fake_client, HEALTHY, stale=lambda: ["connection"]).data
    assert data["stale"] is True and data["status"] == "unknown" and data["controller"] == {"state": "unreachable"}
    assert data["complete"] is True                                       # the content is whole, only old


@pytest.mark.parametrize("kinds, state", [(["timeout"], "unreachable"), (["tls"], "certificate"),
                                          (["unauthorized"], "key_rejected"), (["forbidden"], "key_rejected"),
                                          (["http"], "ok"), (["http", "tls"], "certificate"), ([""], "ok")])
def test_what_stale_answers_say_about_the_connection(fake_client, kinds, state):
    data = build(fake_client, HEALTHY, stale=lambda: kinds).data
    assert data["controller"] == {"state": state} and data["stale"] is True and data["status"] == "unknown"


def test_a_stale_critical_is_still_critical_and_says_it_is_stale(fake_client):
    data = build(fake_client, stale=lambda: ["timeout"]).data
    assert data["status"] == "critical" and data["stale"] is True


def test_nothing_stale_is_not_stale(fake_client):
    data = build(fake_client, HEALTHY, stale=lambda: []).data
    assert data["stale"] is False and data["controller"] == {"state": "ok"} and data["status"] == "ok"


# -- unreachable --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("error, state", [
    (requests.exceptions.ConnectionError("secret.host refused"), "unreachable"),
    (requests.exceptions.Timeout("secret.host timed out"), "unreachable"),
    (requests.exceptions.SSLError("secret.host certificate"), "certificate")])
def test_a_controller_that_cannot_be_reached_is_a_document_not_an_error(fake_client, error, state):
    refuse(fake_client, error)
    document = build(fake_client, HEALTHY)
    data = document.data
    assert data["status"] == "unknown" and data["complete"] is False and data["stale"] is False
    assert data["controller"] == {"state": state} and data["site"] is None
    for section in ("findings", "devices", "clients", "wan", "wifi", "events"):
        assert data[section] == {"available": False}, section
    assert document.meta == {"complete": False}
    assert document.warnings == [f"the controller could not be read ({state}); nothing is known about the network"]
    assert "secret" not in str(document.warnings) + str(data)


@pytest.mark.parametrize("status", [401, 403])
def test_a_controller_that_refuses_the_key_is_key_rejected(fake_client, status):
    fake_client.session.status = status
    data = build(fake_client).data
    assert data["controller"] == {"state": "key_rejected"} and data["status"] == "unknown"


def test_a_failure_that_is_not_about_the_connection_is_raised_as_for_every_other_document(fake_client):
    fake_client.session.status = 500
    with pytest.raises(UniFiAPIError) as raised:
        dashboard_document(fake_client, "default", echo=False)
    assert raised.value.kind == "http"
    fake_client.session.status = 200
    with pytest.raises(UniFiAPIError) as missing:
        dashboard_document(fake_client, "no-such-site", echo=False)
    assert missing.value.kind == "site"


def test_the_unreachable_document_is_the_same_whatever_the_error_text(fake_client):
    refuse(fake_client, requests.exceptions.ConnectionError("one text"))
    first = build(fake_client).data
    refuse(fake_client, requests.exceptions.ConnectionError("another text"))
    assert build(fake_client).data == first == dashboard.unreachable_dashboard("unreachable")


# -- triage -------------------------------------------------------------------------------------------------------

def entry(state, code, until=None):
    return {"code": code, "state": state, "by": "alice", "at": NOW, "until": until, "note": "",
            "first_seen_at": NOW - 100, "last_seen_at": NOW}


def finding_ids(fake_client):
    found = diagnose_document(fake_client, "default", echo=False).data["findings"]
    return {f["code"]: finding_id(f["code"], f["subject"], f["mac"] or None) for f in found}


def test_counts_by_triage_state_and_acknowledged_findings_leave_the_attention_list_but_not_the_total(fake_client):
    ids = finding_ids(fake_client)
    entries = {ids["device.overheating"]: entry("acknowledged", "device.overheating"),
               ids["device.cpu_high"]: entry("snoozed", "device.cpu_high", until=NOW + 86400)}
    data = build(fake_client, triage=lambda site: entries, now=NOW).data
    findings = data["findings"]
    assert findings["triage_available"] is True and sum(findings["by_state"].values()) == findings["total"] == 17
    assert (findings["by_state"]["acknowledged"], findings["by_state"]["snoozed"]) == (1, 1)
    assert findings["by_state"]["open"] == 15
    assert ids["device.overheating"] not in {i["id"] for i in findings["attention"]}
    assert ids["device.cpu_high"] not in {i["id"] for i in findings["attention"]}


def test_an_acknowledged_critical_is_still_a_critical_status(fake_client):
    ids = finding_ids(fake_client)
    entries = {ids["device.overheating"]: entry("acknowledged", "device.overheating")}
    data = build(fake_client, triage=lambda site: entries, now=NOW).data
    assert data["findings"]["by_severity"]["critical"] == 1 and data["status"] == "critical"


def test_a_snooze_that_has_ended_is_open_again(fake_client):
    ids = finding_ids(fake_client)
    entries = {ids["device.cpu_high"]: entry("snoozed", "device.cpu_high", until=NOW - 1)}
    states = build(fake_client, triage=lambda site: entries, now=NOW).data["findings"]["by_state"]
    assert states["snoozed"] == 0 and states["open"] == 17


def test_no_triage_file_means_all_open_and_no_triage_means_unknown(fake_client):
    empty = build(fake_client, triage=lambda site: {}, now=NOW).data["findings"]
    assert empty["triage_available"] is True and empty["by_state"] == {"open": 17, "acknowledged": 0, "snoozed": 0}
    none = build(fake_client).data["findings"]
    assert none["triage_available"] is False and none["by_state"] is None


def test_a_triage_file_that_cannot_be_used_gives_null_counts_not_zero_and_the_rest_still_works(fake_client):
    def unusable(site):
        raise StoreError("unreadable", "The triage file cannot be read.")

    findings = build(fake_client, triage=unusable).data["findings"]
    assert findings["triage_available"] is False and findings["by_state"] is None and findings["total"] == 17
    assert len(findings["attention"]) == 5


def test_the_triage_reader_is_given_the_site_of_the_read(fake_client):
    seen = []
    build(fake_client, triage=lambda site: seen.append(site["id"]) or {})
    assert seen == ["site-1"]


def test_a_triage_store_is_read_through_the_same_function_the_route_uses(fake_client, tmp_path):
    ids = finding_ids(fake_client)
    store = TriageStore(tmp_path, "site-1")
    store.set_state(ids["device.offline"], "device.offline", "acknowledged", "alice", NOW)
    data = build(fake_client, triage=lambda site: store.load(), now=NOW).data
    assert data["findings"]["by_state"]["acknowledged"] == 1


# -- the status rule on its own ----------------------------------------------------------------------------------

@pytest.mark.parametrize("severities, complete, stale, controller, status", [
    ({"critical": 1, "warning": 0, "info": 0}, True, False, "ok", "critical"),
    ({"critical": 1, "warning": 3, "info": 0}, False, True, "unreachable", "critical"),
    ({"critical": 0, "warning": 1, "info": 0}, False, False, "ok", "warning"),
    ({"critical": 0, "warning": 0, "info": 4}, True, False, "ok", "ok"),
    ({"critical": 0, "warning": 0, "info": 0}, False, False, "ok", "unknown"),
    ({"critical": 0, "warning": 0, "info": 0}, True, True, "ok", "unknown"),
    ({"critical": 0, "warning": 0, "info": 0}, True, False, "unreachable", "unknown"),
    ({"critical": 0, "warning": 0, "info": 0}, True, False, "certificate", "unknown"),
    ({"critical": 0, "warning": 0, "info": 0}, True, False, "key_rejected", "unknown"),
    (None, True, False, "ok", "unknown")])
def test_the_status_rule(severities, complete, stale, controller, status):
    assert dashboard.overall_status(severities, complete, stale, controller) == status
    assert status in dashboard.STATUSES


@pytest.mark.parametrize("kinds, state", [
    (["timeout", "unauthorized"], "key_rejected"), (["unauthorized", "timeout"], "key_rejected"),
    (["timeout", "tls"], "certificate"), (["tls", "timeout"], "certificate"),
    (["forbidden", "tls", "connection"], "key_rejected"), (["connection", "tls", "forbidden"], "key_rejected"),
    (["http", "timeout"], "unreachable"), (["timeout", "http"], "unreachable"), (["http", "bad_body"], "ok")])
def test_the_state_of_several_stale_failures_does_not_depend_on_the_order_they_finished_in(kinds, state):
    assert dashboard.stale_state(kinds) == state == dashboard.stale_state(list(reversed(kinds)))


def test_connection_states():
    assert [dashboard.connection_state(k) for k in ("connection", "timeout", "tls", "unauthorized", "forbidden")] == [
        "unreachable", "unreachable", "certificate", "key_rejected", "key_rejected"]
    assert [dashboard.connection_state(k) for k in ("http", "bad_body", "site", "request", "", None)] == ["ok"] * 6
    assert set(dashboard.CONTROLLER_STATES) == {"ok", "unreachable", "certificate", "key_rejected"}
    assert dashboard.stale_state([]) == "ok"


# -- what it reads --------------------------------------------------------------------------------------------------

def test_the_dashboard_reads_what_a_full_diagnose_reads_and_nothing_else(fake_client):
    assert documents.DASHBOARD_NEEDS == diagnose_areas.needs_for(None, 86400)
    build(fake_client)
    dashboard_calls = sorted(fake_client.session.calls)
    dashboard_posts = len(fake_client.session.posts)
    fake_client.session.calls.clear()
    fake_client.session.posts.clear()
    diagnose_document(fake_client, "default", echo=False)
    assert dashboard_calls == sorted(fake_client.session.calls) and dashboard_posts == len(fake_client.session.posts) == 1


def test_the_document_is_all_json(fake_client):
    import json

    document = build(fake_client)
    assert json.loads(json.dumps(document.data)) == document.data
