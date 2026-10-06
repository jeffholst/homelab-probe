"""``GET /api/v1/unifi/sites/{site}/dashboard`` over HTTP (issue #244)."""

import io
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("tomlkit")

import requests  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from jsonschema import Draft202012Validator  # noqa: E402
from server_support import CONFIG, auth_for, logged_in  # noqa: E402
from test_json_schemas import strict  # noqa: E402

from homelab_probe import logs  # noqa: E402
from homelab_probe.demo.session import DemoSession  # noqa: E402
from homelab_probe.diagnose import CODES  # noqa: E402
from homelab_probe.server import apischema, findings_api  # noqa: E402
from homelab_probe.server.app import create_app  # noqa: E402
from homelab_probe.server.service import ControllerService  # noqa: E402
from homelab_probe.triage import finding_id  # noqa: E402

SITE = "/api/v1/unifi/sites/default"
URL = f"{SITE}/dashboard"
SECRETS = ("controller.example", "the-api-key", "secret.host")


def make_app(tmp_path, session=None, settings=None, **service):
    session = session or DemoSession()
    app = create_app(CONFIG, settings, tmp_path, hosts=["testserver"], auth=auth_for(tmp_path),
                     service=ControllerService(CONFIG, session=session, **service))
    app.state.fake = session
    return app


@pytest.fixture
def session():
    return DemoSession()


@pytest.fixture
def app(tmp_path, session):
    return make_app(tmp_path, session)


@pytest.fixture
def viewer(app):
    return logged_in(app, "bob")


@pytest.fixture
def admin(app):
    return logged_in(app, "alice")


def healthy_settings(tmp_path):
    """A settings file that ignores every finding code: a controller with nothing to report."""
    path = tmp_path / "hlp.toml"
    path.write_text("".join(f'[[ignore]]\ncode = "{code}"\nreason = "test"\n\n' for code in CODES), encoding="utf-8")
    return path


def valid(body):
    schema = apischema.response_schema("dashboard")
    Draft202012Validator(schema).validate(body)
    Draft202012Validator(strict(schema)).validate(body)


def go_down(session, error=None):
    def get(*args, **kwargs):
        raise error or requests.exceptions.ConnectionError("secret.host refused")

    session.get = get


# -- who can read it, and what ------------------------------------------------------------------------------------

def test_both_roles_read_it_and_it_validates_against_its_schema(admin, viewer):
    bodies = [client.get(URL) for client in (admin, viewer)]
    assert [b.status_code for b in bodies] == [200, 200]
    first, second = bodies[0].json(), bodies[1].json()
    assert {**first, "generated_at": ""} == {**second, "generated_at": ""}     # stamped to the second: two reads may differ
    body = bodies[0].json()
    valid(body)
    assert body["generated_at"].endswith("Z") and isinstance(body["warnings"], list)
    assert body["status"] == "critical" and body["findings"]["triage_available"] is True
    assert body["findings"]["by_state"] == {"open": 16, "acknowledged": 0, "snoozed": 0}


def test_it_needs_a_login_a_known_site_and_a_valid_site_name(app, viewer):
    assert TestClient(app).get(URL).status_code == 401
    assert viewer.get("/api/v1/unifi/sites/nowhere/dashboard").status_code == 404
    assert viewer.get("/api/v1/unifi/sites/a%3Fb/dashboard").status_code == 422
    assert viewer.get(URL + "?refresh=maybe").status_code == 422


def test_it_answers_nothing_but_get(viewer):
    for method in ("post", "put", "patch", "delete"):
        assert getattr(viewer, method)(URL).status_code == 405, method


def test_a_read_writes_nothing(app, viewer, tmp_path):
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    viewer.get(URL)
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == before


def test_it_reads_what_diagnose_reads_through_the_same_cache(viewer, session):
    viewer.get(f"{SITE}/diagnose")
    reads, posts = len(session.calls), len(session.posts)
    body = viewer.get(URL).json()
    assert body["status"] == "critical" and (len(session.calls), len(session.posts)) == (reads, posts)


def test_refresh_reads_the_controller_again(viewer, session):
    viewer.get(URL)
    first = len(session.calls)
    assert viewer.get(URL + "?refresh=true").status_code == 200
    assert len(session.calls) == 2 * first


def test_the_request_log_names_the_route_template_not_the_site(viewer):
    stream = io.StringIO()
    logs.configure("json", "INFO", stream=stream)
    viewer.get(URL)
    lines = [json.loads(line) for line in stream.getvalue().splitlines() if "server.request" in line]
    assert lines and lines[0]["route"] == "/api/v1/unifi/sites/{site}/dashboard"


# -- a healthy site, a partial read -------------------------------------------------------------------------------

def test_a_complete_fresh_read_with_nothing_found_is_ok(tmp_path, session):
    client = logged_in(make_app(tmp_path, session, healthy_settings(tmp_path)))
    body = client.get(URL).json()
    valid(body)
    assert body["status"] == "ok" and body["complete"] is True and body["stale"] is False
    assert body["findings"]["total"] == 0 and body["warnings"] == [
        "detail/statistics unavailable for 1 device(s)"]


def test_a_failed_optional_read_is_not_ok_and_says_so(tmp_path, session):
    real = session.get

    def get(url, params=None, verify=True, timeout=None):
        from conftest import FakeResponse

        return FakeResponse(503, {}) if url.endswith("/stat/health") else real(url, params, verify, timeout)

    session.get = get
    client = logged_in(make_app(tmp_path, session, healthy_settings(tmp_path)))
    body = client.get(URL).json()
    valid(body)
    assert body["status"] == "unknown" and body["complete"] is False and body["wan"] == {"available": False}
    assert any("health" in w for w in body["warnings"])


# -- stale and unreachable ----------------------------------------------------------------------------------------

def test_an_old_answer_served_when_the_controller_is_down_is_stale_and_never_ok(tmp_path, session):
    client = logged_in(make_app(tmp_path, session, healthy_settings(tmp_path), ttl=0))
    assert client.get(URL).json()["status"] == "ok"
    go_down(session)
    body = client.get(URL).json()
    valid(body)
    assert body["stale"] is True and body["status"] == "unknown" and body["controller"] == {"state": "unreachable"}
    assert body["complete"] is True and body["site"] == {"id": "site-1", "name": "Default"}
    assert any("served from the cache" in w for w in body["warnings"])
    assert not any(secret in client.get(URL).text for secret in SECRETS)


def test_an_old_critical_answer_is_still_critical_and_stale(tmp_path, session):
    client = logged_in(make_app(tmp_path, session, ttl=0))
    client.get(URL)
    go_down(session, requests.exceptions.SSLError("secret.host certificate"))
    body = client.get(URL).json()
    assert body["status"] == "critical" and body["stale"] is True and body["controller"] == {"state": "certificate"}


def test_the_next_request_after_the_controller_is_back_is_fresh_again(tmp_path, session):
    client = logged_in(make_app(tmp_path, session, healthy_settings(tmp_path), ttl=0, error_ttl=0))
    client.get(URL)
    real = session.get
    go_down(session)
    assert client.get(URL).json()["stale"] is True
    session.get = real
    body = client.get(URL).json()
    assert body["stale"] is False and body["status"] == "ok" and body["controller"] == {"state": "ok"}


@pytest.mark.parametrize("make_error, state", [
    (lambda: requests.exceptions.ConnectionError("secret.host refused"), "unreachable"),
    (lambda: requests.exceptions.Timeout("secret.host timed out"), "unreachable"),
    (lambda: requests.exceptions.SSLError("secret.host certificate"), "certificate")])
def test_an_unreachable_controller_is_a_200_with_nothing_available(viewer, session, make_error, state):
    go_down(session, make_error())
    response = viewer.get(URL)
    assert response.status_code == 200
    body = response.json()
    valid(body)
    assert body["status"] == "unknown" and body["controller"] == {"state": state} and body["site"] is None
    assert body["complete"] is False and body["stale"] is False
    assert all(body[name] == {"available": False} for name in ("findings", "devices", "clients", "wan", "wifi",
                                                              "events"))
    assert body["warnings"] == [f"the controller could not be read ({state}); nothing is known about the network"]
    assert not any(secret in response.text for secret in SECRETS)


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_key_is_a_200_that_says_so(viewer, session, status):
    session.status = status
    body = viewer.get(URL).json()
    valid(body)
    assert body["controller"] == {"state": "key_rejected"} and body["status"] == "unknown"
    assert not any(secret in json.dumps(body) for secret in SECRETS)


def test_an_answer_that_cannot_be_used_is_a_502_like_every_other_report(viewer, session):
    session.status = 500
    response = viewer.get(URL)
    assert response.status_code == 502 and response.json()["error"] == "controller_error"


# -- triage ---------------------------------------------------------------------------------------------------------

def overheating_id(client):
    items = client.get(f"{SITE}/findings").json()["items"]
    found = next(i for i in items if i["code"] == "device.overheating")
    assert found["id"] == finding_id(found["code"], found["subject"], found["mac"] or None)
    return found["id"]


def test_acknowledged_and_snoozed_findings_are_counted_by_state_but_not_hidden(admin, viewer, monkeypatch):
    monkeypatch.setattr(findings_api, "CLOCK", lambda: 1_900_000_000.0)
    ident = overheating_id(admin)
    assert admin.put(f"{SITE}/findings/{ident}/triage", json={"state": "acknowledged"}).status_code == 200
    body = viewer.get(URL).json()
    valid(body)
    findings = body["findings"]
    assert findings["by_state"] == {"open": 15, "acknowledged": 1, "snoozed": 0} and findings["total"] == 16
    assert ident not in {i["id"] for i in findings["attention"]}
    assert findings["by_severity"]["critical"] == 1 and body["status"] == "critical"     # acknowledged is not resolved


def test_a_triage_file_that_cannot_be_used_leaves_the_counts_null_and_the_rest_intact(app, viewer, tmp_path):
    directory = tmp_path / "snapshots" / "site-1"
    directory.mkdir(parents=True)
    (directory / "triage.json").write_text("this is not json", encoding="utf-8")
    body = viewer.get(URL).json()
    valid(body)
    assert body["findings"]["by_state"] is None and body["findings"]["triage_available"] is False
    assert body["findings"]["total"] == 16 and body["status"] == "critical"


def test_a_triage_file_that_names_another_site_is_not_used(viewer, tmp_path):
    directory = tmp_path / "snapshots" / "site-1"
    directory.mkdir(parents=True)
    (directory / "triage.json").write_text(json.dumps({"version": 1, "site": "other", "entries": {}}),
                                           encoding="utf-8")
    assert viewer.get(URL).json()["findings"]["triage_available"] is False
