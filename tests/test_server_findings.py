"""Findings with their local triage over the API (issue #224)."""

import datetime
import json
import os

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("tomlkit")

from fastapi.testclient import TestClient  # noqa: E402
from server_support import CONFIG, auth_for, logged_in  # noqa: E402

from homelab_probe import sitefile  # noqa: E402
from homelab_probe import triage as triage_module  # noqa: E402
from homelab_probe.demo.session import DemoSession  # noqa: E402
from homelab_probe.server import findings_api, scheduler  # noqa: E402
from homelab_probe.server.app import create_app  # noqa: E402
from homelab_probe.server.service import ControllerService  # noqa: E402
from homelab_probe.triage import TriageStore, finding_id  # noqa: E402

SITE = "/api/v1/unifi/sites/default"
LIST = f"{SITE}/findings"
FILE = "snapshots/site-1/triage.json"


class Clock:
    def __init__(self, now=1_900_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(findings_api, "CLOCK", clock)
    return clock


def make_app(tmp_path, **kwargs):
    session = DemoSession()
    app = create_app(CONFIG, state_dir=tmp_path, hosts=["testserver"], auth=auth_for(tmp_path),
                     service=ControllerService(CONFIG, session=session, ttl=0), **kwargs)
    app.state.fake = session
    return app


@pytest.fixture
def app(tmp_path, clock):
    return make_app(tmp_path)


@pytest.fixture
def admin(app):
    return logged_in(app, "alice")


@pytest.fixture
def viewer(app):
    return logged_in(app, "bob")


def triage_url(ident):
    return f"{LIST}/{ident}/triage"


def put(client, ident, **body):
    return client.put(triage_url(ident), json=body)


def first(client, code=None):
    items = client.get(LIST).json()["items"]
    return next(i for i in items if code is None or i["code"] == code)


def audit(tmp_path):
    return [json.loads(line) for line in (tmp_path / "audit.log").read_text().splitlines()]


# -- the list ---------------------------------------------------------------------------------------------------

def test_every_finding_has_an_id_a_place_in_the_order_its_reasons_and_what_is_not_known(viewer):
    body = viewer.get(LIST).json()
    items = body["items"]
    assert items and [i["rank"] for i in items] == list(range(1, len(items) + 1))
    top = items[0]
    assert top["severity"] == "critical" and top["triage"]["state"] == "open" and top["priority"]["reasons"][0] == "critical severity"
    assert set(top) >= {"id", "rank", "severity", "code", "subject", "message", "mac", "priority", "triage",
                        "first_seen_at", "limitations", "next_checks", "next_checks_are", "docs"}
    assert top["id"] == finding_id(top["code"], top["subject"], top["mac"] or None)
    assert top["limitations"] and top["first_seen_at"] is None and top["docs"].startswith("diagnose.md#")
    assert body["summary"]["total"] == len(items) and body["summary"]["open"] == len(items)
    assert body["site"] == {"id": "site-1", "name": "Default"} and body["generated_at"].endswith("Z")


def test_both_roles_read_the_same_findings_in_the_same_order_every_time(admin, viewer):
    one, two, three = (c.get(LIST).json()["items"] for c in (admin, viewer, admin))
    assert [i["id"] for i in one] == [i["id"] for i in two] == [i["id"] for i in three]


def test_the_order_is_critical_first_and_stable_on_the_fixture(viewer):
    items = viewer.get(LIST).json()["items"]
    severities = [i["severity"] for i in items]
    assert severities == sorted(severities, key=["critical", "warning", "info"].index)
    assert items[0]["code"] == "device.overheating" or items[0]["severity"] == "critical"


def test_listing_needs_a_login_and_a_known_site(app, viewer):
    assert TestClient(app).get(LIST).status_code == 401
    assert viewer.get("/api/v1/unifi/sites/nope/findings").status_code == 404
    assert viewer.get("/api/v1/unifi/sites/a%20b%3Fc/findings").status_code in (404, 422)


def test_a_read_never_writes_anything(app, viewer, tmp_path):
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    viewer.get(LIST)
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == before


def test_a_read_without_the_event_log_says_that_it_left_checks_out(viewer):
    body = viewer.get(LIST, params={"no_events": "true"}).json()
    assert body["complete"] is False and any("partial or left some checks out" in text for text in body["limitations"])
    assert all(any("partial" in text for text in i["limitations"]) for i in body["items"])
    assert viewer.get(LIST).json()["complete"] is True


def test_an_incomplete_read_is_not_complete(app, viewer, monkeypatch):
    real = findings_api.diagnose_document

    def partial(*args, **kwargs):
        document = real(*args, **kwargs)
        document.meta["complete"] = False
        return document

    monkeypatch.setattr(findings_api, "diagnose_document", partial)
    body = viewer.get(LIST).json()
    assert body["complete"] is False and body["limitations"]


def test_the_ignore_rules_still_apply(tmp_path, clock):
    (tmp_path / "hlp.toml").write_text('[[ignore]]\ncode = "device.offline"\nreason = "all"\n')
    viewer = logged_in(make_app(tmp_path), "bob")
    assert all(i["code"] != "device.offline" for i in viewer.get(LIST).json()["items"])


def test_a_controller_that_cannot_be_read_is_a_fixed_error(app, viewer):
    app.state.fake.status = 503
    assert viewer.get(LIST).status_code in (502, 504)


def test_a_damaged_triage_file_or_a_linked_directory_still_shows_the_findings_and_says_triage_is_missing(
        app, viewer, tmp_path):
    (tmp_path / "snapshots" / "site-1").mkdir(parents=True)
    (tmp_path / FILE).write_text("not json")
    body = viewer.get(LIST).json()
    assert body["items"] and body["triage_available"] is False and str(tmp_path) not in json.dumps(body)
    assert any(text.startswith("Triage is not shown") for text in body["limitations"])
    (tmp_path / FILE).unlink()
    (tmp_path / "snapshots" / "site-1").rmdir()
    (tmp_path / "snapshots").rmdir()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "snapshots").symlink_to(tmp_path / "elsewhere")
    again = viewer.get(LIST).json()
    assert again["triage_available"] is False and list((tmp_path / "elsewhere").iterdir()) == []


def test_a_settings_file_that_cannot_be_used_is_a_fixed_500(tmp_path, clock):
    (tmp_path / "hlp.toml").write_text("[thresholds]\nresource_warn_pct = 500\n")
    response = logged_in(make_app(tmp_path), "bob").get(LIST)
    assert response.status_code == 500 and response.json()["error"] == "settings_invalid" and str(tmp_path) not in response.text


# -- acknowledging, snoozing, reopening ------------------------------------------------------------------------

def test_an_acknowledged_finding_stays_in_the_list_with_who_and_when_and_moves_after_the_open_ones(admin, viewer, tmp_path):
    top = first(admin)
    total = len(admin.get(LIST).json()["items"])
    response = put(admin, top["id"], state="acknowledged", note="known, waiting for a part")
    assert response.status_code == 200 and response.json()["triage"]["state"] == "acknowledged"
    items = viewer.get(LIST).json()["items"]
    assert len(items) == total and items[-1]["id"] == top["id"] and items[-1]["rank"] == total
    assert items[-1]["triage"]["by"] == "alice" and items[-1]["triage"]["note"] == "known, waiting for a part"
    assert "acknowledged by an administrator" in items[-1]["priority"]["reasons"]
    assert items[-1]["first_seen_at"] is not None and viewer.get(LIST).json()["summary"]["acknowledged"] == 1


def test_a_snooze_ends_on_its_date_and_the_finding_is_open_again(admin, viewer, clock):
    top = first(admin)
    day = datetime.datetime.fromtimestamp(clock() + 2 * 86400, datetime.timezone.utc).strftime("%Y-%m-%d")
    assert put(admin, top["id"], state="snoozed", until=day).json()["triage"]["until"].endswith("T00:00:00Z")
    assert viewer.get(LIST).json()["items"][-1]["triage"]["state"] == "snoozed"
    clock.now += 4 * 86400
    after = viewer.get(LIST).json()
    assert after["items"][0]["id"] == top["id"] and after["items"][0]["triage"]["state"] == "open"
    assert after["summary"]["snoozed"] == 0


def test_reopening_a_finding_clears_who_and_the_note(admin):
    top = first(admin)
    put(admin, top["id"], state="acknowledged", note="x")
    assert put(admin, top["id"], state="open").json()["triage"] == {"state": "open", "by": None, "at": None,
                                                                    "until": None, "note": ""}


@pytest.mark.parametrize("body", [
    {"state": "snoozed"}, {"state": "acknowledged", "until": "2999-01-01"}, {"state": "snoozed", "until": "tomorrow"},
    {"state": "snoozed", "until": "2026-13-45"}, {"state": "snoozed", "until": "2001-01-01"},
    {"state": "snoozed", "until": "2999-01-01"}, {"state": "done"}, {"state": "acknowledged", "note": "x" * 201},
    {"state": "acknowledged", "note": "x" * 401}, {"state": "acknowledged", "extra": 1}, {},
])
def test_a_change_that_makes_no_sense_is_a_422_and_writes_nothing(admin, tmp_path, body):
    assert put(admin, first(admin)["id"], **body).status_code == 422
    assert not (tmp_path / FILE).exists()


def test_only_a_finding_that_is_there_now_can_be_triaged(admin, tmp_path):
    response = put(admin, "0" * 16, state="acknowledged")
    assert response.status_code == 404 and response.json()["error"] == "finding_not_found"
    assert put(admin, "not-an-id", state="acknowledged").status_code == 422
    assert not (tmp_path / FILE).exists()


def test_only_an_administrator_with_the_csrf_token_and_an_origin_changes_anything(app, viewer, admin, tmp_path):
    ident = first(admin)["id"]
    assert TestClient(app).put(triage_url(ident), json={"state": "open"}, headers={"Origin": "http://testserver"}
                               ).status_code == 401
    assert put(viewer, ident, state="acknowledged").status_code == 403
    assert TestClient(app).put(triage_url(ident), json={"state": "acknowledged"}).status_code == 403
    no_token = logged_in(app, "alice")
    del no_token.headers["X-CSRF-Token"]
    assert put(no_token, ident, state="acknowledged").json()["error"] == "csrf_token"
    assert not (tmp_path / FILE).exists()


def test_a_read_only_server_refuses_the_change_and_still_lists(tmp_path, clock):
    admin = logged_in(make_app(tmp_path, read_only=True), "alice")
    response = put(admin, first(admin)["id"], state="acknowledged")
    assert response.status_code == 403 and response.json()["error"] == "read_only"
    assert not (tmp_path / FILE).exists() and admin.get(LIST).status_code == 200


def test_a_change_is_audited_by_code_and_id_never_by_a_name_a_mac_or_the_note(admin, tmp_path):
    top = first(admin)
    put(admin, top["id"], state="acknowledged", note="the secret reason")
    entry = audit(tmp_path)[-1]
    assert (entry["event"], entry["actor"], entry["finding"], entry["code"], entry["state"]) == (
        "triage.changed", "alice", top["id"], top["code"], "acknowledged")
    text = (tmp_path / "audit.log").read_text()
    assert top["subject"] not in text and "secret reason" not in text and top["mac"] not in text


def test_the_state_survives_a_restart_and_lives_in_the_site_directory_owner_only(admin, tmp_path, clock):
    top = first(admin)
    put(admin, top["id"], state="acknowledged")
    assert (tmp_path / FILE).is_file() and oct(os.stat(tmp_path / FILE).st_mode & 0o777) == "0o600"
    restarted = logged_in(_same_data(tmp_path), "bob")
    assert restarted.get(LIST).json()["items"][-1]["id"] == top["id"]


def _same_data(tmp_path):
    from homelab_probe.server.auth import AuthState

    session = DemoSession()
    app = create_app(CONFIG, state_dir=tmp_path, hosts=["testserver"], auth=AuthState.for_directory(tmp_path, CONFIG),
                     service=ControllerService(CONFIG, session=session, ttl=0))
    return app


def test_the_state_of_another_site_cannot_be_seen_or_changed_here(admin, viewer, tmp_path):
    (tmp_path / "snapshots" / "site-1").mkdir(parents=True)
    TriageStore(tmp_path / "snapshots" / "site-1", "site-2").set_state("a" * 16, "wan.availability", "acknowledged",
                                                                        "mallory", 1.0)
    body = viewer.get(LIST).json()
    assert body["triage_available"] is False and all(i["triage"]["state"] == "open" for i in body["items"])
    response = put(admin, first(admin)["id"], state="acknowledged")
    assert response.status_code == 500 and response.json()["error"] == "triage_another_site"


def test_too_many_tracked_findings_is_a_409(admin, monkeypatch):
    monkeypatch.setattr(triage_module, "MAX_ENTRIES", 0)
    assert put(admin, first(admin)["id"], state="acknowledged").json()["error"] == "triage_full"


def test_a_busy_lock_is_a_503_and_a_damaged_file_a_500_with_fixed_text(admin, tmp_path, monkeypatch):
    from homelab_probe.util import file_lock

    monkeypatch.setattr(sitefile, "LOCK_WAIT_SECONDS", 0.2)
    ident = first(admin)["id"]
    (tmp_path / "snapshots" / "site-1").mkdir(parents=True, exist_ok=True)
    with file_lock(tmp_path / "snapshots" / "site-1" / "triage.json.lock"):
        busy = put(admin, ident, state="acknowledged")
    assert busy.status_code == 503 and busy.json()["error"] == "triage_locked"
    (tmp_path / FILE).write_text("not json")
    damaged = put(admin, ident, state="acknowledged")
    assert damaged.status_code == 500 and damaged.json()["error"] == "triage_unreadable" and str(tmp_path) not in damaged.text


def test_the_openapi_document_has_both_routes_with_their_errors(app):
    spec = app.openapi()["paths"]
    listing = spec["/api/v1/unifi/sites/{site}/findings"]["get"]["responses"]
    change = spec["/api/v1/unifi/sites/{site}/findings/{finding}/triage"]["put"]["responses"]
    assert {"401", "404", "500", "502"} <= set(listing) and {"403", "404", "409", "422", "503"} <= set(change)


# -- the scheduler records what it sees and ends an entry only on a complete read ------------------------------------

def scheduled(tmp_path, clock):
    import dataclasses

    from homelab_probe.server.auth import AuthState

    config = dataclasses.replace(CONFIG, notify_ntfy_url="")
    session = DemoSession()
    session.fx["legacy"]["device"][0]["overheating"] = False
    app = create_app(config, state_dir=tmp_path, hosts=["testserver"], auth=AuthState.for_directory(tmp_path, config),
                     service=ControllerService(config, session=session, ttl=0), scheduler=True)
    app.state.scheduler._clock = clock
    return app


def test_the_scheduler_records_the_findings_so_that_how_long_is_known(tmp_path, clock):
    app = scheduled(tmp_path, clock)
    app.state.scheduler.tick()
    entries = TriageStore(tmp_path / "snapshots" / "site-1", "site-1").load()
    assert entries and all(e["state"] == "open" and e["first_seen_at"] == clock() for e in entries.values())
    assert "Garage" not in (tmp_path / FILE).read_text()
    clock.now += 8 * 86400
    app.state.scheduler._due.clear()
    app.state.scheduler.tick()
    assert all(e["first_seen_at"] != e["last_seen_at"] for e in TriageStore(
        tmp_path / "snapshots" / "site-1", "site-1").load().values())


def test_a_complete_read_that_no_longer_shows_a_finding_ends_it_and_a_partial_one_does_not(tmp_path, clock, monkeypatch):
    app = scheduled(tmp_path, clock)
    app.state.scheduler.tick()
    store = TriageStore(tmp_path / "snapshots" / "site-1", "site-1")
    entries = store.load()
    gone = next(i for i, e in entries.items() if e["code"] == "device.offline")
    store.set_state(gone, "device.offline", "acknowledged", "alice", clock())
    real = scheduler.diagnose_document

    def without(complete):
        def build(*args, **kwargs):
            document = real(*args, **kwargs)
            document.data["findings"] = [f for f in document.data["findings"] if f["code"] != "device.offline"]
            document.meta["complete"] = complete
            return document
        return build

    monkeypatch.setattr(scheduler, "diagnose_document", without(False))
    app.state.scheduler._due.clear()
    app.state.scheduler.tick()
    assert gone in store.load()                                                    # a partial read ends nothing
    monkeypatch.setattr(scheduler, "diagnose_document", without(True))
    app.state.scheduler._due.clear()
    app.state.scheduler.tick()
    assert gone not in store.load()                                                # a complete one does


def test_a_triage_file_that_cannot_be_updated_is_a_warning_and_not_a_failed_run(tmp_path, clock):
    app = scheduled(tmp_path, clock)
    (tmp_path / "snapshots" / "site-1").mkdir(parents=True)
    (tmp_path / FILE).write_text("not json")
    results = app.state.scheduler.tick()
    assert [r.result for r in results if r.job == "diagnose"] == ["ok"]


def test_a_refresh_reads_again_and_a_failure_of_the_checks_themselves_is_a_fixed_error(app, viewer, monkeypatch):
    assert viewer.get(LIST, params={"refresh": "true"}).status_code == 200
    from homelab_probe.client import UniFiAPIError

    def timeout(*args, **kwargs):
        raise UniFiAPIError("slow", kind="timeout")

    monkeypatch.setattr(findings_api, "diagnose_document", timeout)
    response = viewer.get(LIST)
    assert response.status_code == 504 and response.json()["error"] == "controller_timeout"


def test_a_directory_that_cannot_be_made_for_the_lock_is_a_fixed_error(admin, tmp_path):
    (tmp_path / "snapshots").write_text("a file where the directory should be")
    response = put(admin, first(admin)["id"], state="acknowledged")
    assert response.status_code == 500 and response.json()["error"] == "triage_unreadable"
    assert str(tmp_path) not in response.text
