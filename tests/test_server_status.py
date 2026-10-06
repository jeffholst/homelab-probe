"""The status contract and the test notification (issue #223)."""

import dataclasses
import json
import os

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("tomlkit")

import requests  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from server_support import CONFIG, auth_for, logged_in  # noqa: E402
from test_notify import FakePost  # noqa: E402

from homelab_probe import notify as notify_module  # noqa: E402
from homelab_probe.demo.session import DemoSession  # noqa: E402
from homelab_probe.server import status_api  # noqa: E402
from homelab_probe.server.app import create_app  # noqa: E402
from homelab_probe.server.cache import WireStatus  # noqa: E402
from homelab_probe.server.service import ControllerService  # noqa: E402

STATUS = "/api/v1/status"
TEST = "/api/v1/notifications/test"
NTFY = "https://ntfy.example/topic-9f8e7d6c5b4a"
HOOK = "https://hooks.example/h-1234567890abcdef"


def make_app(tmp_path, destinations=True, scheduler=False, **kwargs):
    changes = {"notify_ntfy_url": NTFY, "notify_webhook_url": HOOK, "notify_ntfy_token": "tk-secret-token-0123"} \
        if destinations else {}
    config = dataclasses.replace(CONFIG, **changes)
    session = DemoSession()
    session.fx["legacy"]["device"][0]["overheating"] = False
    app = create_app(config, state_dir=tmp_path, hosts=["testserver"], auth=auth_for(tmp_path), scheduler=scheduler,
                     service=ControllerService(config, session=session, ttl=0), **kwargs)
    app.state.fake = session
    return app


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def admin(app):
    return logged_in(app, "alice")


@pytest.fixture
def viewer(app):
    return logged_in(app, "bob")


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(status_api, "TEST_INTERVAL", 0.0)


# -- the controller ---------------------------------------------------------------------------------------------

def test_before_anything_was_read_the_controller_is_unknown_not_ok(admin):
    body = admin.get(STATUS).json()
    assert body["controller"] == {"state": "unknown", "last_read_ok_at": None, "last_failure_at": None,
                                  "last_failure": None}
    assert body["data"] == {"stale": False, "warnings": 0, "last_response_at": None}


def test_after_a_read_the_controller_is_ok_and_says_when(app, admin):
    assert admin.get("/api/v1/unifi/sites/default/devices").status_code == 200
    body = admin.get(STATUS).json()
    assert body["controller"]["state"] == "ok" and body["controller"]["last_read_ok_at"].endswith("Z")
    assert body["data"]["last_response_at"].endswith("Z") and body["problems"] == []


def test_an_error_status_from_the_controller_is_still_an_answer_so_it_is_not_unreachable(app, admin):
    assert admin.get("/api/v1/unifi/sites/default/devices").status_code == 200
    app.state.fake.status = 500
    admin.get("/api/v1/unifi/sites/default/diagnose")                             # the controller answers with errors
    body = admin.get(STATUS).json()
    assert body["controller"]["state"] == "ok" and body["controller"]["last_failure"] is None


@pytest.mark.parametrize("kind, state", [("timeout", "unreachable"), ("connection", "unreachable"),
                                         ("tls", "certificate"), ("unauthorized", "key_rejected"),
                                         ("forbidden", "key_rejected")])
def test_what_the_last_read_said_about_the_connection(app, admin, kind, state):
    from homelab_probe.client import UniFiAPIError

    cache = app.state.service.cache
    cache.fetch("good", lambda: {"ok": True}, "good")
    assert admin.get(STATUS).json()["controller"]["state"] == "ok"

    def failing():
        raise UniFiAPIError("no", kind=kind)

    with pytest.raises(UniFiAPIError):
        cache.fetch("bad", failing, "bad")
    body = admin.get(STATUS).json()
    assert body["controller"]["state"] == state and body["controller"]["last_failure"] == kind
    assert body["controller"]["last_failure_at"].endswith("Z") and f"controller:{state}" in body["problems"]
    cache.fetch("good2", lambda: {"ok": True}, "good2")                       # it answers again
    again = admin.get(STATUS).json()
    assert again["controller"]["state"] == "ok" and not [p for p in again["problems"] if p.startswith("controller")]


def test_an_old_answer_served_because_a_read_failed_is_stale_until_a_read_succeeds(app, admin):
    from homelab_probe.client import UniFiAPIError

    cache = app.state.service.cache
    cache.ttl = 0
    cache.fetch("k", lambda: {"v": 1}, "k")

    def failing():
        raise UniFiAPIError("no", kind="timeout")

    assert cache.fetch("k", failing, "k") == {"v": 1}                          # served from the cache
    body = admin.get(STATUS).json()
    assert body["data"]["stale"] is True and "data:stale" in body["problems"]
    cache.fetch("other", lambda: 1, "other")
    assert admin.get(STATUS).json()["data"]["stale"] is False


def test_the_warnings_of_the_last_response_are_counted(app, admin):
    app.state.service.build(lambda client: __import__("homelab_probe.documents", fromlist=["x"]).Document(
        "x", {}, ["an optional read failed", "another"]))
    assert admin.get(STATUS).json()["data"]["warnings"] == 2


def test_the_wire_state_words(monkeypatch):
    wire = WireStatus()
    assert wire.state == "unknown"
    wire.answered_at = 10.0
    assert wire.state == "ok"
    wire.failed_at, wire.failed_kind = 20.0, "connection"
    assert wire.state == "unreachable"
    wire.answered_at = 30.0
    assert wire.state == "ok"


# -- roles ------------------------------------------------------------------------------------------------------

def test_a_viewer_sees_the_controller_the_data_and_the_scheduler_but_no_reasons_destinations_or_storage(viewer):
    body = viewer.get(STATUS).json()
    assert set(body) == {"generated_at", "read_only", "controller", "data", "scheduler"}
    assert set(body["controller"]) == {"state", "last_read_ok_at"}
    assert body["scheduler"] == {"enabled": False, "jobs": {}}


def test_an_administrator_also_sees_destinations_by_kind_storage_and_the_problems(admin):
    body = admin.get(STATUS).json()
    assert body["notifications"] == {"destinations": ["ntfy", "webhook"], "last_delivery": {}}
    assert body["storage"] == {"data_directory": "ok", "audit_log": "ok", "snapshots": "absent", "disk": "ok"}
    assert body["problems"] == []


def test_the_status_needs_a_login(app):
    assert TestClient(app).get(STATUS).status_code == 401


def test_nothing_secret_is_ever_in_the_status(app, admin, viewer, tmp_path):
    admin.post(TEST, json={})
    text = admin.get(STATUS).text + viewer.get(STATUS).text
    for secret in (NTFY, HOOK, "topic-9f8e7d6c5b4a", "h-1234567890abcdef", "tk-secret-token", "the-api-key", str(tmp_path),
                   "controller.example", "ntfy.example"):
        assert secret not in text, secret


def test_the_status_makes_no_request_of_its_own(app, admin):
    before = list(app.state.fake.calls)
    admin.get(STATUS)
    viewer = logged_in(app, "bob")
    viewer.get(STATUS)
    assert app.state.fake.calls == before


# -- the scheduler ----------------------------------------------------------------------------------------------

def test_the_jobs_are_reported_with_their_last_run_and_last_success(tmp_path, monkeypatch):
    monkeypatch.setattr(notify_module.requests, "post", FakePost(200))
    app = make_app(tmp_path, scheduler=True)
    admin, viewer = logged_in(app, "alice"), logged_in(app, "bob")
    assert admin.get(STATUS).json()["scheduler"]["jobs"]["diagnose"]["last_run_at"] is None
    app.state.scheduler.tick()
    jobs = admin.get(STATUS).json()["scheduler"]["jobs"]
    assert jobs["diagnose"]["result"] == "ok" and jobs["diagnose"]["reason"] == "baseline"
    assert jobs["diagnose"]["last_success_at"] is not None and jobs["snapshot"]["reason"] == "saved"
    assert jobs["diagnose"]["next_run_at"] is not None and isinstance(jobs["diagnose"]["duration_ms"], int)
    shown = viewer.get(STATUS).json()["scheduler"]
    assert shown["enabled"] is True and set(shown["jobs"]["diagnose"]) == {
        "last_run_at", "result", "last_success_at", "next_run_at"}


def test_a_failed_job_is_a_problem_and_a_success_after_it_clears_it(tmp_path, monkeypatch):
    monkeypatch.setattr(notify_module.requests, "post", FakePost(200))
    app = make_app(tmp_path, scheduler=True)
    admin = logged_in(app, "alice")
    app.state.fake.status = 503
    app.state.scheduler.tick()
    body = admin.get(STATUS).json()
    assert "scheduler.diagnose:failed" in body["problems"] and body["scheduler"]["jobs"]["diagnose"]["last_success_at"] is None
    app.state.fake.status = None
    app.state.scheduler._due.clear()
    app.state.scheduler.tick()
    again = admin.get(STATUS).json()
    assert not [p for p in again["problems"] if p.startswith("scheduler")]
    assert again["scheduler"]["jobs"]["diagnose"]["last_success_at"] is not None


# -- notifications ----------------------------------------------------------------------------------------------

def test_a_failed_delivery_by_the_scheduler_is_a_problem_until_one_succeeds(tmp_path, monkeypatch):
    fake = FakePost(500)
    monkeypatch.setattr(notify_module.requests, "post", fake)
    app = make_app(tmp_path, scheduler=True)
    admin = logged_in(app, "alice")
    app.state.scheduler.tick()                                                  # baseline: nothing is sent
    saved = json.loads((tmp_path / "snapshots/site-1/notify-state.json").read_text())
    del saved["active"][next(k for k in saved["active"] if k.startswith("device.offline|"))]
    (tmp_path / "snapshots/site-1/notify-state.json").write_text(json.dumps(saved))
    app.state.scheduler._due.clear()
    app.state.scheduler.tick()
    body = admin.get(STATUS).json()
    last = body["notifications"]["last_delivery"]
    assert last["ntfy"]["delivered"] is False and last["ntfy"]["reason"] == "HTTP 500"
    assert "notifications.ntfy:failed" in body["problems"]
    monkeypatch.setattr(notify_module.requests, "post", FakePost(200))
    assert admin.post(TEST, json={}).json()["delivered"] is True
    assert "notifications.ntfy:failed" not in admin.get(STATUS).json()["problems"]


def test_the_test_notification_sends_one_fixed_message_to_every_destination(app, admin, monkeypatch):
    fake = FakePost(200)
    monkeypatch.setattr(notify_module.requests, "post", fake)
    response = admin.post(TEST, json={})
    assert response.status_code == 200 and response.json() == {"delivered": True, "results": [
        {"destination": "ntfy", "delivered": True, "reason": "HTTP 200"},
        {"destination": "webhook", "delivered": True, "reason": "HTTP 200"}]}
    (url1, ntfy), (url2, hook) = fake.calls
    assert url1 == NTFY and url2 == HOOK
    assert ntfy["data"].decode() == notify_module.TEST_BODY and ntfy["headers"]["Title"] == notify_module.TEST_TITLE
    assert ntfy["headers"]["Authorization"] == "Bearer tk-secret-token-0123"
    assert hook["json"]["test"] is True and hook["json"]["events"] == [] and hook["json"]["title"] == notify_module.TEST_TITLE
    assert NTFY not in response.text and "tk-secret" not in response.text


def test_a_failing_destination_is_reported_with_a_fixed_reason_and_never_a_url(app, admin, monkeypatch):
    def post(url, **kwargs):
        if url == NTFY:
            raise requests.exceptions.ConnectionError(f"could not connect to {url} with tk-secret-token-0123")
        return type("R", (), {"status_code": 403})()

    monkeypatch.setattr(notify_module.requests, "post", post)
    response = admin.post(TEST, json={})
    assert response.json() == {"delivered": False, "results": [
        {"destination": "ntfy", "delivered": False, "reason": "connection error"},
        {"destination": "webhook", "delivered": False, "reason": "HTTP 403"}]}
    assert "ntfy.example" not in response.text and "tk-secret" not in response.text
    assert admin.get(STATUS).json()["notifications"]["last_delivery"]["webhook"]["reason"] == "HTTP 403"


def test_a_test_with_one_failing_destination_still_says_delivered_when_another_took_it(app, admin, monkeypatch):
    def post(url, **kwargs):
        return type("R", (), {"status_code": 200 if url == HOOK else 500})()

    monkeypatch.setattr(notify_module.requests, "post", post)
    body = admin.post(TEST, json={}).json()
    assert body["delivered"] is True and [r["delivered"] for r in body["results"]] == [False, True]


def test_without_a_destination_nothing_is_sent_and_it_says_so(tmp_path, monkeypatch):
    fake = FakePost(200)
    monkeypatch.setattr(notify_module.requests, "post", fake)
    admin = logged_in(make_app(tmp_path, destinations=False), "alice")
    response = admin.post(TEST, json={})
    assert response.status_code == 409 and response.json()["error"] == "no_destination" and fake.calls == []


def test_only_an_administrator_with_the_csrf_token_and_an_origin_can_send_a_test(app, viewer, monkeypatch):
    fake = FakePost(200)
    monkeypatch.setattr(notify_module.requests, "post", fake)
    assert TestClient(app).post(TEST, json={}, headers={"Origin": "http://testserver"}).status_code == 401
    assert viewer.post(TEST, json={}).status_code == 403
    assert TestClient(app).post(TEST, json={}).status_code == 403                    # no Origin
    no_token = logged_in(app, "alice")
    del no_token.headers["X-CSRF-Token"]
    assert no_token.post(TEST, json={}).json()["error"] == "csrf_token"
    assert fake.calls == []                                                          # nothing is ever sent by itself


def test_a_test_is_never_sent_without_being_asked(app, admin, monkeypatch):
    fake = FakePost(200)
    monkeypatch.setattr(notify_module.requests, "post", fake)
    admin.get(STATUS)
    admin.get("/api/v1/unifi/sites/default/diagnose")
    assert fake.calls == []


def test_tests_are_limited_to_one_every_few_seconds(app, admin, monkeypatch):
    fake = FakePost(200)
    monkeypatch.setattr(notify_module.requests, "post", fake)
    monkeypatch.setattr(status_api, "TEST_INTERVAL", 60.0)
    assert admin.post(TEST, json={}).status_code == 200
    again = admin.post(TEST, json={})
    assert again.status_code == 429 and again.json()["error"] == "too_soon"
    assert 1 <= int(again.headers["Retry-After"]) <= 60 and len(fake.calls) == 2           # two destinations, one test


def test_a_test_is_audited_by_kind_and_count_never_by_content(app, admin, tmp_path, monkeypatch):
    monkeypatch.setattr(notify_module.requests, "post", FakePost(200))
    admin.post(TEST, json={})
    entry = [json.loads(line) for line in (tmp_path / "audit.log").read_text().splitlines()][-1]
    assert (entry["event"], entry["actor"], entry["destinations"], entry["delivered"]) == (
        "notification.tested", "alice", "ntfy,webhook", 2)
    text = (tmp_path / "audit.log").read_text()
    assert NTFY not in text and "tk-secret" not in text


def test_a_read_only_server_can_still_send_a_test_because_it_writes_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(notify_module.requests, "post", FakePost(200))
    admin = logged_in(make_app(tmp_path, read_only=True), "alice")
    assert admin.post(TEST, json={}).status_code == 200
    assert admin.get(STATUS).json()["read_only"] is True


# -- storage ----------------------------------------------------------------------------------------------------

def test_the_storage_says_what_is_wrong_in_fixed_words(app, admin, tmp_path, monkeypatch):
    (tmp_path / "snapshots").mkdir()
    assert admin.get(STATUS).json()["storage"]["snapshots"] == "ok"
    os.chmod(tmp_path / "snapshots", 0o500)
    assert admin.get(STATUS).json()["storage"]["snapshots"] == "not_writable" or os.geteuid() == 0
    os.chmod(tmp_path / "snapshots", 0o700)
    (tmp_path / "snapshots").rmdir()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "snapshots").symlink_to(tmp_path / "elsewhere")
    body = admin.get(STATUS).json()
    assert body["storage"]["snapshots"] == "unsafe" and "storage.snapshots:unsafe" in body["problems"]
    monkeypatch.setattr(status_api.shutil, "disk_usage", lambda path: type("U", (), {"free": 1024})())
    body = admin.get(STATUS).json()
    assert body["storage"]["disk"] == "low" and "storage.disk:low" in body["problems"]

    def broken(path):
        raise OSError("gone")

    monkeypatch.setattr(status_api.shutil, "disk_usage", broken)
    assert admin.get(STATUS).json()["storage"]["disk"] == "unknown"


def test_an_unwritable_audit_log_and_a_missing_data_directory(tmp_path):
    from types import SimpleNamespace

    audit = tmp_path / "audit.log"
    audit.write_text("")
    os.chmod(audit, 0o400)
    state = SimpleNamespace(state_dir=tmp_path)
    expected = "ok" if os.geteuid() == 0 else "not_writable"
    assert status_api.storage(SimpleNamespace(state=state))["audit_log"] == expected
    gone = SimpleNamespace(state=SimpleNamespace(state_dir=tmp_path / "gone"))
    found = status_api.storage(gone)
    assert found["data_directory"] == "missing" and found["audit_log"] == "not_writable"
    plain = tmp_path / "file"
    plain.write_text("x")
    assert status_api.storage(SimpleNamespace(state=SimpleNamespace(state_dir=plain)))["data_directory"] == "not_writable"


def test_iso_times_are_utc_and_none_stays_none():
    assert status_api.iso(0) == "1970-01-01T00:00:00Z" and status_api.iso(None) is None


def test_the_openapi_document_describes_both_routes(app):
    spec = app.openapi()["paths"]
    assert "401" in spec[STATUS]["get"]["responses"] and "409" in spec[TEST]["post"]["responses"]
    assert spec[TEST]["post"]["responses"]["409"]["description"] == "No notification destination is configured"
