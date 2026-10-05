"""The end of the guided setup (issue #185): notifications, finishing without a restart, the admin mode and the
fallbacks for settings that cannot be saved here."""

import json
import os
import stat

import pytest

pytest.importorskip("fastapi")

import tls_fixtures as fx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from server_support import CONFIG, PASSWORD, login, origin_of  # noqa: E402

from homelab_probe import setup as setup_engine  # noqa: E402
from homelab_probe.accounts import AccountError  # noqa: E402
from homelab_probe.config import ConfigError  # noqa: E402
from homelab_probe.demo import demo_client  # noqa: E402
from homelab_probe.demo.session import DemoSession  # noqa: E402
from homelab_probe.server import wizard  # noqa: E402
from homelab_probe.server.app import create_app  # noqa: E402
from homelab_probe.server.auth import AuthState  # noqa: E402
from homelab_probe.server.service import ControllerService  # noqa: E402
from homelab_probe.server.throttle import LoginThrottle  # noqa: E402
from homelab_probe.server.wizard import MODE_ADMIN, MODE_SETUP, UNVERIFIED_PHRASE, SetupState  # noqa: E402

TOKEN = "setup-token-0123456789abcdef"
KEY = "the-typed-api-key-9876543210"
NTFY = "https://ntfy.example/topic-9f8e7d6c5b4a"
ADMIN = {"username": "Ada", "password": "a long enough password"}
STANDIN = CONFIG.__class__(controller_url="https://unconfigured.invalid", api_key="unconfigured")
PREFIX = "/api/v1/setup"


def make_state(**kwargs):
    kwargs.setdefault("resolver", lambda host, port: ["192.168.1.1"])
    kwargs.setdefault("client_factory", demo_client)
    kwargs.setdefault("service_factory", lambda config: ControllerService(config, session=DemoSession()))
    return SetupState(MODE_SETUP, "no_config", TOKEN, **kwargs)


@pytest.fixture
def state():
    return make_state()


@pytest.fixture
def app(tmp_path, state):
    auth = AuthState.for_directory(tmp_path, STANDIN)
    auth.throttle = LoginThrottle()
    return create_app(STANDIN, state_dir=tmp_path, hosts=["testserver"], auth=auth, setup=state)


@pytest.fixture
def client(app):
    return TestClient(app)


def send(client, method, path, token=TOKEN, body=None, **headers):
    sent = {"Origin": origin_of(client), **headers}
    if token is not None:
        sent["X-Setup-Token"] = token
    kwargs = {} if body is None else {"json": body}
    return client.request(method, f"{PREFIX}{path}", headers=sent, **kwargs)


def draft(client, **fields):
    return send(client, "POST", "/draft", body=fields)


def tested(client, **fields):
    """A draft that has passed the connection test."""
    fields = {"url": "https://192.168.1.1", "site": "default", "api_key": KEY, **fields}
    assert draft(client, **fields).status_code == 200
    assert send(client, "POST", "/connection").json()["ok"] is True


def finish(client, body=None, **kwargs):
    return send(client, "POST", "/finish", body=ADMIN if body is None else body, **kwargs)


def audit_lines(tmp_path):
    return [json.loads(line) for line in (tmp_path / "audit.log").read_text().splitlines()]


# -- notifications ----------------------------------------------------------------------------------------------

def test_a_notification_destination_is_checked_with_a_dry_run_and_never_shown(client):
    body = draft(client, url="https://192.168.1.1", api_key=KEY, notify={"NOTIFY_NTFY_URL": NTFY}).json()
    assert body["changed"] == ["UNIFI_URL", "UNIFI_API_KEY", "NOTIFY_*"] and body["draft"]["notify"] == ["NOTIFY_NTFY_URL"]
    response = send(client, "POST", "/notifications")
    checks = {c["id"]: c for c in response.json()["checks"]}
    assert checks["notify.configured"]["message"] == "configured: ntfy"
    assert checks["notify.dry_run"]["message"].endswith("nothing was sent")
    assert NTFY not in response.text and "topic-9f8e7d6c5b4a" not in response.text and "topic-9f8e7d6c5b4a" not in json.dumps(body)


def test_without_a_destination_the_dry_run_says_so(client):
    draft(client, url="https://192.168.1.1", api_key=KEY)
    checks = send(client, "POST", "/notifications").json()["checks"]
    assert checks[0]["status"] == "info" and checks[1]["status"] == "skip"


def test_a_blank_value_removes_a_notification_setting(client, state):
    draft(client, notify={"NOTIFY_NTFY_URL": NTFY, "NOTIFY_NTFY_TOKEN": "a-token-value"})
    assert sorted(state.draft.notify) == ["NOTIFY_NTFY_TOKEN", "NOTIFY_NTFY_URL"]
    assert draft(client, notify={"NOTIFY_NTFY_TOKEN": ""}).json()["draft"]["notify"] == ["NOTIFY_NTFY_URL"]
    assert draft(client, notify={"NOTIFY_NTFY_TOKEN": ""}).json()["changed"] == []


@pytest.mark.parametrize("notify", [
    {"UNIFI_URL": "https://x.example"}, {"NOTIFY_NTFY_URL": "http://ntfy.example/t"}, {"NOTIFY_NTFY_URL": "x" * 2000},
    {"NOTIFY_SMTP_HOST": "mail.example"},                       # half of the mail settings
])
def test_notification_settings_that_do_not_validate_are_a_422(client, state, notify):
    response = draft(client, notify=notify)
    assert response.status_code == 422 and response.json()["setting"] == "NOTIFY_*"
    assert state.draft.notify == {}


def test_notification_secrets_are_hidden_from_logs(client):
    from homelab_probe import logs

    draft(client, notify={"NOTIFY_NTFY_URL": NTFY})
    assert "topic-9f8e7d6c5b4a" not in logs.scrub(f"posting to {NTFY}")


def test_the_dry_run_needs_a_complete_draft(client):
    assert send(client, "POST", "/notifications").json()["error"] == "draft_incomplete"


# -- finishing --------------------------------------------------------------------------------------------------

def test_finishing_writes_the_files_creates_the_administrator_and_leaves_the_setup_mode_without_a_restart(
        client, app, state, tmp_path):
    tested(client, notify={"NOTIFY_NTFY_URL": NTFY})
    response = finish(client)
    assert response.status_code == 200
    body = response.json()
    assert body["finished"] is True and body["admin_created"] is True
    assert {step["id"] for step in body["written"]} >= {"setup.env", "setup.settings", "setup.snapshots"}
    assert KEY not in response.text and NTFY not in response.text
    env = (tmp_path / ".env").read_text()
    assert f"UNIFI_API_KEY={KEY}" in env and "UNIFI_URL=https://192.168.1.1" in env and "UNIFI_VERIFY_SSL=true" in env
    assert "NOTIFY_NTFY_URL=" in env
    assert stat.S_IMODE(os.stat(tmp_path / ".env").st_mode) == 0o600
    assert (tmp_path / "hlp.toml").exists() and stat.S_IMODE(os.stat(tmp_path / "snapshots").st_mode) == 0o700
    assert state.mode is None and app.state.config.controller_url == "https://192.168.1.1"
    assert app.state.config.notify_ntfy_url == NTFY and state.draft.api_key == ""
    meta = client.get("/api/v1/meta").json()
    assert meta["needs_setup"] is False and meta["setup_mode"] is None
    assert client.get("/readyz").json() == {"ready": True}


def test_after_finishing_the_new_administrator_logs_in_and_reads_the_controller_and_the_token_is_dead(client, app):
    tested(client)
    finish(client)
    assert send(client, "GET", "/status").json()["error"] == "not_logged_in"
    anonymous = client.get("/api/v1/unifi/sites")
    assert anonymous.status_code == 401
    csrf = login(client, "ada", ADMIN["password"])
    assert client.get("/api/v1/unifi/sites").status_code == 200
    assert send(client, "GET", "/status", token=None).json()["mode"] is None
    assert csrf


def test_finishing_is_audited_without_the_key_and_the_new_administrator_is_recorded(client, tmp_path):
    tested(client)
    finish(client)
    entries = audit_lines(tmp_path)
    events = [e["event"] for e in entries]
    assert events[-2:] == ["user.added", "setup.finished"]
    assert entries[-2]["user"] == "ada" and entries[-2]["role"] == "admin" and entries[-1]["admin_created"] is True
    assert KEY not in (tmp_path / "audit.log").read_text() and TOKEN not in (tmp_path / "audit.log").read_text()


def test_a_pinned_certificate_is_saved_in_the_data_directory_and_named_by_the_settings(client, tmp_path, app):
    with fx.tls_server(fx.GOOD_CERT, fx.GOOD_KEY) as port:
        draft(client, url=f"https://127.0.0.1:{port}", api_key=KEY)
        found = send(client, "POST", "/certificate").json()["draft"]["certificate"]
        draft(client, verify="pin", fingerprint=found["fingerprint"])
        assert send(client, "POST", "/connection").json()["ok"] is True
        body = finish(client).json()
    certificate = (tmp_path / "certs" / "controller.pem").resolve()
    assert certificate.read_text().strip() == fx.GOOD_CERT.strip()
    assert stat.S_IMODE(os.stat(certificate).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "certs").st_mode) == 0o700
    assert f"UNIFI_VERIFY_SSL={certificate}" in (tmp_path / ".env").read_text()
    assert app.state.config.verify_ssl == str(certificate)
    assert {"setup.certificates", "setup.certificate"} <= {step["id"] for step in body["written"]}


def test_checking_turned_off_is_written_as_false(client, tmp_path):
    draft(client, url="https://192.168.1.1", api_key=KEY, verify="false", confirm=UNVERIFIED_PHRASE)
    send(client, "POST", "/connection")
    assert finish(client).status_code == 200
    assert "UNIFI_VERIFY_SSL=false" in (tmp_path / ".env").read_text()


def test_an_existing_env_file_is_merged_and_kept_as_a_backup(client, tmp_path):
    (tmp_path / ".env").write_text("# mine\nLOG_LEVEL=INFO\n")
    tested(client)
    finish(client)
    text = (tmp_path / ".env").read_text()
    assert text.startswith("# mine\nLOG_LEVEL=INFO\n") and "UNIFI_URL=" in text
    assert (tmp_path / ".env.bak").read_text() == "# mine\nLOG_LEVEL=INFO\n"


def test_finishing_needs_a_passing_connection_test_of_these_settings(client, tmp_path):
    draft(client, url="https://192.168.1.1", api_key=KEY)
    assert finish(client).json()["error"] == "not_tested"
    send(client, "POST", "/connection")
    draft(client, site="other")                                   # changed after the test
    assert finish(client).status_code == 409
    assert not (tmp_path / ".env").exists()


def test_finishing_needs_a_draft(client):
    assert finish(client).json()["error"] == "draft_incomplete"


@pytest.mark.parametrize("body, error", [
    ({}, "admin_required"), ({"username": "ada"}, "admin_required"), ({"password": "x" * 20}, "admin_required"),
    ({"username": "ada", "password": "short"}, "invalid_admin"), ({"username": "a", "password": "x" * 20}, "invalid_admin"),
])
def test_the_first_administrator_is_validated_before_anything_is_written(client, tmp_path, body, error):
    tested(client)
    response = finish(client, body)
    assert response.status_code == 422 and response.json()["error"] == error
    assert not (tmp_path / ".env").exists() and not (tmp_path / "users.json").exists()


def test_the_password_is_never_in_an_answer_or_the_audit_log(client, tmp_path):
    tested(client)
    answers = finish(client, {"username": "ada", "password": "short"}).text + finish(client).text
    assert ADMIN["password"] not in answers and ADMIN["password"] not in (tmp_path / "audit.log").read_text()


def test_finishing_twice_is_refused(client):
    tested(client)
    finish(client)
    csrf = login(client, "ada", ADMIN["password"])
    response = send(client, "POST", "/finish", token=None, body={}, **{"X-CSRF-Token": csrf})
    assert response.status_code == 409 and response.json()["error"] == "already_set_up"


def test_when_an_administrator_exists_the_session_finishes_the_setup_and_no_second_one_is_made(tmp_path, state):
    auth = AuthState.for_directory(tmp_path, STANDIN)
    auth.accounts.store.add("alice", "admin", PASSWORD)
    app = create_app(STANDIN, state_dir=tmp_path, hosts=["testserver"], auth=auth, setup=state)
    client = TestClient(app)
    csrf = login(client, "alice")
    headers = {"X-CSRF-Token": csrf}
    send(client, "POST", "/draft", token=None, body={"url": "https://192.168.1.1", "api_key": KEY}, **headers)
    send(client, "POST", "/connection", token=None, **headers)
    refused = send(client, "POST", "/finish", token=None, body=ADMIN, **headers)
    assert refused.status_code == 422 and refused.json()["error"] == "admin_exists"
    done = send(client, "POST", "/finish", token=None, body={}, **headers)
    assert done.json()["finished"] is True and done.json()["admin_created"] is False
    assert [u.username for u in auth.accounts.store.users()] == ["alice"]


def test_the_default_reload_reads_the_data_directory(tmp_path, state):
    with pytest.raises(ConfigError):
        wizard._reload_default(tmp_path)                           # nothing there yet
    (tmp_path / ".env").write_text("UNIFI_URL=https://c.example\nUNIFI_API_KEY=k-k-k-k-k\n")
    assert wizard._reload_default(tmp_path).controller_url == "https://c.example"


def test_the_runner_supplied_reload_is_used(client, state, tmp_path):
    seen = []

    def reload():
        seen.append(True)
        return CONFIG

    state.reload = reload
    tested(client)
    finish(client)
    assert seen == [True] and client.app.state.config is CONFIG


# -- when something goes wrong after the files are written -----------------------------------------------------

def test_a_settings_file_that_cannot_be_loaded_afterwards_is_reported_and_the_mode_stays(client, state):
    def broken():
        raise ConfigError("nope")

    state.reload = broken
    tested(client)
    response = finish(client)
    assert response.status_code == 500 and response.json()["error"] == "reload_failed"
    assert state.mode == MODE_SETUP and "nope" not in response.text


def test_an_administrator_that_cannot_be_created_is_reported_with_the_way_out(client, app, state):
    def refuse(*args, **kwargs):
        raise AccountError("disk full")

    app.state.auth.accounts.store.add = refuse
    tested(client)
    response = finish(client)
    assert response.status_code == 500 and response.json()["error"] == "admin_not_created"
    assert "hlp web-user add" in response.json()["message"] and "disk full" not in response.text
    assert state.mode == MODE_SETUP


def test_an_accounts_file_that_goes_bad_during_finishing_is_a_500(client, monkeypatch):
    tested(client)
    calls = []
    real = wizard.administrator_exists

    def flaky(store):
        calls.append(True)
        if len(calls) > 1:
            raise AccountError("damaged")
        return real(store)

    monkeypatch.setattr(wizard, "administrator_exists", flaky)
    assert finish(client).json()["error"] == "accounts_unreadable"


# -- the fallbacks ----------------------------------------------------------------------------------------------

def assert_nothing_written(tmp_path, client, state):
    assert not (tmp_path / ".env").exists() and not (tmp_path / "users.json").exists()
    assert state.mode == MODE_SETUP and client.get("/api/v1/meta").json()["needs_setup"] is True


def test_settings_that_the_environment_sets_are_not_written_and_the_finished_env_is_offered(
        client, state, tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFI_SITE_ID", "from-the-environment")
    tested(client)
    response = finish(client)
    body = response.json()
    assert body["finished"] is False and body["written"] is False and body["reason"] == "environment"
    assert body["environment_names"] == ["UNIFI_SITE_ID"]
    assert "UNIFI_API_KEY=your-api-key-here" in body["env"] and "UNIFI_URL=https://192.168.1.1" in body["env"]
    assert KEY not in response.text and "from-the-environment" not in response.text
    assert_nothing_written(tmp_path, client, state)


def test_an_environment_value_equal_to_the_draft_is_no_conflict(client, tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFI_SITE_ID", "default")
    monkeypatch.setenv("UNIFI_VERIFY_SSL", "  ")
    tested(client)
    assert finish(client).json()["finished"] is True


def test_a_settings_file_named_elsewhere_is_not_written_either(tmp_path):
    state = make_state(env_named=True)
    auth = AuthState.for_directory(tmp_path, STANDIN)
    app = create_app(STANDIN, state_dir=tmp_path, hosts=["testserver"], auth=auth, setup=state)
    client = TestClient(app)
    tested(client)
    body = finish(client).json()
    assert body["reason"] == "env_file_named" and body["environment_names"] == [] and body["finished"] is False
    assert_nothing_written(tmp_path, client, state)


@pytest.mark.parametrize("error, detail", [
    (setup_engine.SetupError("cannot write the file"), "cannot write the file"),
    (OSError(30, "Read-only file system"), "Read-only file system"),
    (OSError(), "OSError"),
])
def test_a_volume_that_cannot_be_written_gives_the_fallback(client, state, tmp_path, monkeypatch, error, detail):
    def refuse(*args, **kwargs):
        raise error

    monkeypatch.setattr(wizard.setup_engine, "apply", refuse)
    tested(client)
    response = finish(client)
    body = response.json()
    assert body["reason"] == "not_writable" and body["detail"] == detail and body["written"] is False
    assert "services:" in body["compose"] and "UNIFI_API_KEY: \"your-api-key-here\"" in body["compose"]
    assert KEY not in response.text
    assert_nothing_written(tmp_path, client, state)
    assert audit_lines(tmp_path)[-1]["event"] == "setup.finish_fallback" and audit_lines(tmp_path)[-1]["reason"] == "not_writable"


def test_the_compose_snippet_quotes_values_and_doubles_the_dollar_signs(client, monkeypatch, tmp_path):
    monkeypatch.setenv("UNIFI_SITE_ID", "other")
    tested(client, notify={"NOTIFY_NTFY_URL": "https://ntfy.example/a$b"})
    compose = finish(client).json()["compose"]
    assert 'NOTIFY_NTFY_URL: "https://ntfy.example/a$$b"' in compose and 'UNIFI_URL: "https://192.168.1.1"' in compose


def test_a_pinned_certificate_is_offered_with_a_placeholder_path_when_nothing_can_be_written(
        client, state, tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFI_SITE_ID", "other")
    with fx.tls_server(fx.GOOD_CERT, fx.GOOD_KEY) as port:
        draft(client, url=f"https://127.0.0.1:{port}", api_key=KEY)
        found = send(client, "POST", "/certificate").json()["draft"]["certificate"]
        draft(client, verify="pin", fingerprint=found["fingerprint"])
        send(client, "POST", "/connection")
        body = finish(client).json()
    assert body["certificate"].strip() == fx.GOOD_CERT.strip()
    assert "UNIFI_VERIFY_SSL=/path/to/controller.pem" in body["env"]
    assert not (tmp_path / "certs").exists()


def test_the_fallback_has_no_certificate_when_none_is_pinned(client, monkeypatch):
    monkeypatch.setenv("UNIFI_SITE_ID", "other")
    tested(client)
    assert finish(client).json()["certificate"] is None


# -- the admin mode: settings, but nobody to log in -------------------------------------------------------------

@pytest.fixture
def admin_app(tmp_path):
    state = SetupState(MODE_ADMIN, "no_admin", TOKEN, reload=lambda: CONFIG,
                       service_factory=lambda config: ControllerService(config, session=DemoSession()))
    auth = AuthState.for_directory(tmp_path, CONFIG)
    auth.throttle = LoginThrottle()
    app = create_app(CONFIG, state_dir=tmp_path, hosts=["testserver"], auth=auth, setup=state)
    return app


def test_the_admin_mode_offers_only_the_status_and_the_first_administrator(admin_app):
    client = TestClient(admin_app)
    status = send(client, "GET", "/status").json()
    assert status["mode"] == "admin" and status["reason"] == "no_admin"
    assert admin_app.state.service is None
    for path in ("/draft", "/certificate", "/connection", "/preview", "/notifications"):
        response = send(client, "POST", path, body={})
        assert response.status_code == 409 and response.json()["error"] == "step_unavailable", path
    assert client.get("/api/v1/unifi/sites").json()["error"] == "not_configured"


def test_the_token_creates_the_first_administrator_and_nothing_else_is_written(admin_app, tmp_path):
    client = TestClient(admin_app)
    response = send(client, "POST", "/finish", body=ADMIN)
    assert response.json() == {"finished": True, "written": [], "admin_created": True}
    assert not (tmp_path / ".env").exists()
    assert admin_app.state.setup.mode is None and admin_app.state.service is not None
    login(client, "ada", ADMIN["password"])
    assert client.get("/api/v1/unifi/sites").status_code == 200


def test_the_admin_mode_still_validates_the_administrator(admin_app):
    client = TestClient(admin_app)
    assert send(client, "POST", "/finish", body={}).json()["error"] == "admin_required"
    assert send(client, "POST", "/finish", body={"username": "ada", "password": "short"}).status_code == 422
    assert admin_app.state.setup.mode == MODE_ADMIN


def test_the_token_is_throttled_in_the_admin_mode_too(admin_app):
    client = TestClient(admin_app)
    for _ in range(4):
        send(client, "GET", "/status", token="wrong-wrong-wrong-1")
    assert send(client, "GET", "/status").status_code == 429


def test_everything_else_answers_503_in_the_admin_mode(admin_app):
    client = TestClient(admin_app)
    assert client.get("/api/v1/platforms").json()["error"] == "not_configured"
    assert client.get("/readyz").status_code == 503
    assert client.get("/api/v1/meta").json()["setup_mode"] == "admin"


def test_an_unusual_username_is_stored_the_way_accounts_normalize_it(admin_app):
    client = TestClient(admin_app)
    send(client, "POST", "/finish", body={"username": "  ADA  ", "password": ADMIN["password"]})
    assert [u.username for u in admin_app.state.auth.accounts.store.users()] == ["ada"]


def test_a_user_name_that_is_taken_by_a_viewer_is_refused(tmp_path, state):
    auth = AuthState.for_directory(tmp_path, STANDIN)
    auth.accounts.store.add("ada", "viewer", PASSWORD)
    app = create_app(STANDIN, state_dir=tmp_path, hosts=["testserver"], auth=auth, setup=state)
    client = TestClient(app)
    tested(client)
    response = finish(client)
    assert response.status_code == 422 and response.json()["message"] == "That user name is taken."
    assert not (tmp_path / ".env").exists()
