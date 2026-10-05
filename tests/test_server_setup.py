"""The guided setup of the server (issue #185): setup mode, the setup token, the draft, the certificate and the tests of
the connection. The TLS steps run against a real local TLS server (``tls_fixtures``); the controller is the synthetic
one."""

import json
import os
import stat

import pytest

pytest.importorskip("fastapi")

import requests  # noqa: E402
import tls_fixtures as fx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from server_support import CONFIG, PASSWORD, auth_for, login, origin_of  # noqa: E402

from homelab_probe import logs, tlsprobe  # noqa: E402
from homelab_probe.client import UniFiClient  # noqa: E402
from homelab_probe.config import Config  # noqa: E402
from homelab_probe.demo import demo_client  # noqa: E402
from homelab_probe.server.app import create_app  # noqa: E402
from homelab_probe.server.auth import SETUP_ENDPOINTS, AuthState  # noqa: E402
from homelab_probe.server.throttle import LoginThrottle  # noqa: E402
from homelab_probe.server.wizard import MODE_SETUP, UNVERIFIED_PHRASE, SetupState  # noqa: E402

TOKEN = "setup-token-0123456789abcdef"
KEY = "the-typed-api-key-9876543210"
LOCAL = "127.0.0.1"
STANDIN = Config(controller_url="https://unconfigured.invalid", api_key="unconfigured")
PREFIX = "/api/v1/setup"
PUBLIC = {("GET", "/"), ("GET", "/healthz"), ("GET", "/readyz"), ("GET", "/api/v1/meta"), ("POST", "/api/v1/auth/login")}


class Clock:
    def __init__(self):
        self.now = 10_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def made():
    """The clients that the connection tests were given: [(config, client)]."""
    return []


@pytest.fixture
def state(made):
    def factory(config):
        client = demo_client(config)
        made.append((config, client))
        return client

    return SetupState(MODE_SETUP, "no_config", TOKEN, resolver=lambda host, port: ["192.168.1.1"],
                      client_factory=factory)


@pytest.fixture
def app(tmp_path, state, clock):
    auth = AuthState.for_directory(tmp_path, STANDIN)
    auth.throttle = LoginThrottle(clock=clock)
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


def filled(client):
    response = draft(client, url="https://192.168.1.1", site="default", api_key=KEY)
    assert response.status_code == 200, response.text
    return response


def audit_lines(tmp_path):
    return [json.loads(line) for line in (tmp_path / "audit.log").read_text().splitlines()]


# -- the mode ---------------------------------------------------------------------------------------------------

def test_a_server_in_setup_mode_says_so_and_is_not_ready(client, app):
    assert client.get("/api/v1/meta").json()["needs_setup"] is True
    assert client.get("/api/v1/meta").json()["setup_mode"] == "setup"
    assert client.get("/healthz").json() == {"status": "ok"}
    response = client.get("/readyz")
    assert response.status_code == 503 and response.json() == {"ready": False}
    assert app.state.service is None            # nothing can read a controller


def test_a_configured_server_does_not_say_it_needs_setup(tmp_path):
    app = create_app(CONFIG, state_dir=tmp_path, hosts=["testserver"], auth=auth_for(tmp_path))
    meta = TestClient(app).get("/api/v1/meta").json()
    assert meta["needs_setup"] is False and meta["setup_mode"] is None


def test_in_setup_mode_every_other_endpoint_answers_503_and_the_setup_ones_want_the_token(app, client, clock):
    spec = app.openapi()
    routes = [(method.upper(), path) for path, item in spec["paths"].items() for method in item]
    setup = {(m, p) for m, p in routes if p.startswith(PREFIX)}
    assert len(routes) > 20 and len(setup) == 7 and (PREFIX + "/status") in {p for _, p in setup}
    fill = {"{site}": "default", "{mac}": "BB:00:00:00:00:01", "{name}": "wan"}
    for method, template in routes + [("GET", "/api/v1/openapi.json"), ("GET", "/api/v1/unifi/sites")]:
        if (method, template) in PUBLIC:
            continue
        path = template
        for key, value in fill.items():
            path = path.replace(key, value)
        response = client.request(method, path, headers={"Origin": origin_of(client)})
        if (method, template) in setup:
            clock.now += 1000                       # the wrong tokens are counted: let the wait pass
            assert response.status_code == 401 and response.json()["error"] == "invalid_setup_token", (method, template)
        else:
            assert response.status_code == 503 and response.json()["error"] == "not_configured", (method, template)


def test_even_a_valid_looking_session_does_not_open_anything_in_setup_mode(tmp_path, state):
    auth = auth_for(tmp_path)
    app = create_app(STANDIN, state_dir=tmp_path, hosts=["testserver"], auth=auth, setup=state)
    client = TestClient(app)
    login(client, "alice")
    assert client.get("/api/v1/auth/me").status_code == 503
    assert client.get("/api/v1/unifi/sites").json()["error"] == "not_configured"


def test_the_setup_endpoints_are_exactly_these_seven():
    assert sorted(SETUP_ENDPOINTS) == ["setup_certificate", "setup_connection", "setup_draft", "setup_finish",
                                       "setup_notifications", "setup_preview", "setup_status"]


# -- the token --------------------------------------------------------------------------------------------------

def test_the_right_token_opens_the_setup(client):
    response = send(client, "GET", "/status")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "setup" and body["reason"] == "no_config" and body["unverified_phrase"] == UNVERIFIED_PHRASE
    assert body["draft"] == {"url": "", "site": "default", "api_key_set": False, "verify": "true",
                             "certificate": None, "connection_ok": None, "notify": []}


@pytest.mark.parametrize("token", [None, "", "wrong", TOKEN.upper(), TOKEN + " ", TOKEN[:-1], "x" * 5000])
def test_a_missing_or_wrong_token_is_refused_the_same_way(client, token):
    response = send(client, "GET", "/status", token=token)
    assert response.status_code == 401 and response.json() == {
        "error": "invalid_setup_token", "message": "The setup token is missing or wrong."}


def test_wrong_tokens_are_counted_slowed_down_and_audited_without_the_token(client, clock, tmp_path):
    for guess in ("a-wrong-token-1", "a-wrong-token-2", "a-wrong-token-3", "a-wrong-token-4"):
        send(client, "GET", "/status", token=guess)
    refused = send(client, "GET", "/status")                       # the right token, but the address must wait
    assert refused.status_code == 429 and refused.json()["error"] == "too_many_attempts"
    assert int(refused.headers["Retry-After"]) > 0
    clock.now += 600
    assert send(client, "GET", "/status").status_code == 200
    entries = audit_lines(tmp_path)
    assert [e["event"] for e in entries] == ["setup.token_failed"] * 4
    assert {e["actor"] for e in entries} == {"(setup)"} and all("address" in e for e in entries)
    text = (tmp_path / "audit.log").read_text()
    assert TOKEN not in text and "a-wrong-token" not in text


def test_the_token_header_alone_is_not_enough_for_a_cross_site_post(client):
    response = client.post(f"{PREFIX}/draft", json={"site": "x"}, headers={"X-Setup-Token": TOKEN})
    assert response.status_code == 403 and response.json()["error"] == "csrf_origin"
    response = client.post(f"{PREFIX}/draft", json={"site": "x"},
                           headers={"X-Setup-Token": TOKEN, "Origin": "http://evil.example"})
    assert response.status_code == 403


def test_the_token_is_hidden_from_log_records(state):
    assert TOKEN not in logs.scrub(f"the token is {TOKEN}")


# -- the draft --------------------------------------------------------------------------------------------------

def test_the_draft_is_kept_on_the_server_and_never_shows_the_key(client, state):
    body = filled(client).json()
    assert body["changed"] == ["UNIFI_URL", "UNIFI_API_KEY"]
    assert body["draft"]["url"] == "https://192.168.1.1" and body["draft"]["api_key_set"] is True
    assert KEY not in json.dumps(body) and KEY not in json.dumps(send(client, "GET", "/status").json())
    assert state.draft.api_key == KEY and KEY not in repr(state.draft)
    assert KEY not in logs.scrub(f"sent {KEY}")


def test_a_trailing_slash_and_spaces_are_cleaned_and_an_unchanged_value_changes_nothing(client):
    draft(client, url="  https://192.168.1.1/  ", api_key=f" {KEY} ")
    again = draft(client, url="https://192.168.1.1", api_key=KEY, site="default").json()
    assert again["changed"] == [] and again["draft"]["url"] == "https://192.168.1.1"


@pytest.mark.parametrize("fields, setting", [
    ({"url": "http://192.168.1.1"}, "UNIFI_URL"), ({"url": "192.168.1.1"}, "UNIFI_URL"), ({"url": ""}, "UNIFI_URL"),
    ({"url": "https://user:pw@192.168.1.1"}, "UNIFI_URL"), ({"url": "https://192.168.1.1/?a=b"}, "UNIFI_URL"),
    ({"url": "https://bad host"}, "UNIFI_URL"), ({"url": "https://[::1"}, "UNIFI_URL"),
    ({"url": "https://[not-an-address]"}, "UNIFI_URL"), ({"site": ""}, "UNIFI_SITE_ID"), ({"site": "a/b"}, "UNIFI_SITE_ID"),
    ({"api_key": ""}, "UNIFI_API_KEY"), ({"api_key": "your-api-key-here"}, "UNIFI_API_KEY"),
])
def test_a_value_that_does_not_validate_is_a_422_with_the_setting(client, fields, setting):
    response = draft(client, **fields)
    assert response.status_code == 422 and response.json()["error"] == "invalid_setting"
    assert response.json()["setting"] == setting and response.json()["message"]
    assert "pw" not in response.text and "user:" not in response.text


def test_the_draft_changes_all_or_nothing(client, state):
    filled(client)
    response = draft(client, url="https://192.168.1.2", site="bad/site")
    assert response.status_code == 422
    assert state.draft.url == "https://192.168.1.1" and state.draft.site == "default"


@pytest.mark.parametrize("fields", [{"nonsense": 1}, {"verify": "maybe"}, {"url": 5}, {"url": "x" * 3000},
                                    {"api_key": "k" * 600}])
def test_unknown_fields_and_oversize_or_mistyped_values_are_a_422(client, fields):
    assert draft(client, **fields).status_code == 422


def test_a_new_address_forgets_what_was_decided_for_the_old_one(client):
    with fx.tls_server(fx.GOOD_CERT, fx.GOOD_KEY) as port:
        url = f"https://{LOCAL}:{port}"
        draft(client, url=url, api_key=KEY)
        found = send(client, "POST", "/certificate").json()["draft"]["certificate"]
        assert draft(client, verify="pin", fingerprint=found["fingerprint"]).json()["draft"]["verify"] == "pin"
        after = draft(client, url="https://192.168.1.9").json()["draft"]
        assert after["verify"] == "true" and after["certificate"] is None and after["connection_ok"] is None
        assert draft(client, verify="pin", fingerprint=found["fingerprint"]).json()["error"] == "certificate_required"
    draft(client, verify="false", confirm=UNVERIFIED_PHRASE)
    assert draft(client, url="https://192.168.1.10").json()["draft"]["verify"] == "true"


# -- the certificate --------------------------------------------------------------------------------------------

def test_a_certificate_is_fetched_tested_and_accepted_by_its_fingerprint(client, state):
    with fx.tls_server(fx.GOOD_CERT, fx.GOOD_KEY) as port:
        draft(client, url=f"https://{LOCAL}:{port}", api_key=KEY)
        found = send(client, "POST", "/certificate").json()["draft"]["certificate"]
        assert found["usable"] is True and found["problem"] == "" and len(found["fingerprint"].split(":")) == 32
        compact = found["fingerprint"].replace(":", "").lower()
        assert draft(client, verify="pin", fingerprint=compact).json()["changed"] == ["UNIFI_VERIFY_SSL"]
    assert state.draft.verify == "pin" and state.draft.certificate.pem.strip() == fx.GOOD_CERT.strip()


def test_a_fingerprint_that_is_not_the_fetched_certificates_is_refused(client, state):
    with fx.tls_server(fx.GOOD_CERT, fx.GOOD_KEY) as port:
        draft(client, url=f"https://{LOCAL}:{port}")
        send(client, "POST", "/certificate")
        for wrong in (None, "", "AA:BB", "00" * 32):
            response = draft(client, verify="pin", **({} if wrong is None else {"fingerprint": wrong}))
            assert response.status_code == 422 and response.json()["error"] == "fingerprint_mismatch", wrong
    assert state.draft.verify == "true"


def test_a_pin_needs_a_fetched_certificate(client):
    draft(client, url="https://192.168.1.1")
    response = draft(client, verify="pin", fingerprint="AA")
    assert response.status_code == 422 and response.json()["error"] == "certificate_required"


def test_a_certificate_that_would_not_be_accepted_cannot_be_pinned(client, state):
    with fx.tls_server(fx.OTHERNAME_CERT, fx.OTHERNAME_KEY) as port:
        draft(client, url=f"https://{LOCAL}:{port}")
        found = send(client, "POST", "/certificate").json()["draft"]["certificate"]
        assert found["usable"] is False and found["problem"] == "hostname_mismatch"
        assert "not valid for this host" in found["problem_message"]
        response = draft(client, verify="pin", fingerprint=found["fingerprint"])
        assert response.status_code == 422 and response.json()["error"] == "certificate_unusable"
        assert response.json()["reason"] == "hostname_mismatch"
    assert state.draft.verify == "true"


def test_turning_checking_off_needs_the_sentence_typed_exactly(client, state):
    for wrong in (None, "", "yes", UNVERIFIED_PHRASE.upper(), UNVERIFIED_PHRASE[:-1]):
        response = draft(client, verify="false", **({} if wrong is None else {"confirm": wrong}))
        assert response.status_code == 422 and response.json()["error"] == "confirmation_required", wrong
    assert state.draft.verify == "true"
    assert draft(client, verify="false", confirm=f"  {UNVERIFIED_PHRASE}  ").json()["draft"]["verify"] == "false"
    assert draft(client, verify="true").json()["draft"]["verify"] == "true"       # back on needs nothing


def test_the_certificate_step_needs_an_address(client):
    response = send(client, "POST", "/certificate")
    assert response.status_code == 409 and response.json()["error"] == "draft_incomplete"


@pytest.mark.parametrize("url, status, reason", [
    ("https://169.254.169.254", 422, "blocked_address"), ("https://0.0.0.0", 422, "blocked_address"),
    ("https://8.8.8.8", 422, "public_address"), ("https://[fe80::1]", 422, "blocked_address"),
])
def test_an_address_that_cannot_be_a_controller_is_refused_before_any_connection(client, monkeypatch, url, status, reason):
    def forbidden(*args, **kwargs):
        raise AssertionError("no connection may be made")

    monkeypatch.setattr(tlsprobe.socket, "create_connection", forbidden)
    draft(client, url=url)
    response = send(client, "POST", "/certificate")
    assert response.status_code == status and response.json()["error"] == "controller_unavailable"
    assert response.json()["reason"] == reason


def test_a_name_that_resolves_to_a_refused_address_is_refused(client, state):
    state.resolver = lambda host, port: ["192.168.1.1", "169.254.169.254"]
    draft(client, url="https://unifi.lan")
    assert send(client, "POST", "/certificate").json()["reason"] == "blocked_address"


def test_a_controller_that_cannot_be_reached_is_a_502_with_a_fixed_reason(client):
    with fx.tls_server(fx.GOOD_CERT, fx.GOOD_KEY) as port:
        pass
    draft(client, url=f"https://{LOCAL}:{port}")
    response = send(client, "POST", "/certificate")
    assert response.status_code == 502 and response.json()["reason"] == "connection"
    assert LOCAL not in response.text


def test_a_certificate_fetched_for_an_address_that_was_changed_meanwhile_is_dropped(client, state, monkeypatch):
    with fx.tls_server(fx.GOOD_CERT, fx.GOOD_KEY) as port:
        draft(client, url=f"https://{LOCAL}:{port}")
        real = tlsprobe.check_pin

        def slow(*args, **kwargs):
            draft(client, url="https://192.168.1.9")             # the owner edits the address while the test runs
            return real(*args, **kwargs)

        monkeypatch.setattr(tlsprobe, "check_pin", slow)
        send(client, "POST", "/certificate")
    assert state.draft.url == "https://192.168.1.9" and state.draft.certificate is None


# -- the connection ---------------------------------------------------------------------------------------------

def test_the_connection_test_runs_the_doctor_checks_on_the_draft(client, made):
    filled(client)
    response = send(client, "POST", "/connection")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and {c["section"] for c in body["checks"]} == {"controller", "endpoint"}
    assert all(c["status"] in ("ok", "warn", "fail", "skip", "info") for c in body["checks"])
    config = made[0][0]
    assert config.controller_url == "https://192.168.1.1" and config.api_key == KEY and config.timeout <= 10
    assert len(made) == 2 and all(c.session.max_redirects == 0 for _, c in made)      # a redirect would carry the key away
    assert KEY not in response.text and "192.168.1.1" not in response.text
    assert send(client, "GET", "/status").json()["draft"]["connection_ok"] is True


def test_a_changed_draft_forgets_the_result_of_the_test(client):
    filled(client)
    send(client, "POST", "/connection")
    assert draft(client, site="other").json()["draft"]["connection_ok"] is None


def test_the_connection_test_needs_an_address_and_a_key(client, made):
    assert send(client, "POST", "/connection").json()["error"] == "draft_incomplete"
    draft(client, url="https://192.168.1.1")
    assert send(client, "POST", "/connection").json()["error"] == "draft_incomplete"
    assert made == []


def test_a_pinned_certificate_is_a_private_file_only_while_the_test_runs(client, made, state, tmp_path):
    seen = {}

    def factory(config):
        path = config.verify_ssl
        seen["path"] = path
        seen["text"] = open(path).read()
        seen["mode"] = stat.S_IMODE(os.stat(path).st_mode)
        return demo_client(config)

    state.client_factory = factory
    with fx.tls_server(fx.GOOD_CERT, fx.GOOD_KEY) as port:
        draft(client, url=f"https://{LOCAL}:{port}", api_key=KEY)
        found = send(client, "POST", "/certificate").json()["draft"]["certificate"]
        draft(client, verify="pin", fingerprint=found["fingerprint"])
        assert send(client, "POST", "/connection").json()["ok"] is True
    assert seen["text"].strip() == fx.GOOD_CERT.strip() and seen["mode"] == 0o600
    assert isinstance(seen["path"], str) and not os.path.exists(seen["path"])
    assert not os.path.exists(os.path.dirname(seen["path"]))


def test_turning_checking_off_reaches_the_client(client, made):
    filled(client)
    draft(client, verify="false", confirm=UNVERIFIED_PHRASE)
    send(client, "POST", "/connection")
    assert made[0][0].verify_ssl is False


def test_an_address_that_cannot_be_a_controller_is_refused_by_the_connection_test_too(client, made, state):
    state.resolver = lambda host, port: ["169.254.169.254"]
    draft(client, url="https://unifi.lan", api_key=KEY)
    response = send(client, "POST", "/connection")
    assert response.status_code == 422 and response.json()["reason"] == "blocked_address" and made == []


class Unreachable(requests.Session):
    def request(self, *args, **kwargs):
        raise requests.exceptions.ConnectionError(f"boom {KEY} https://192.168.1.1/x")


def test_a_controller_that_cannot_be_read_fails_the_test_with_fixed_words_only(client, state):
    def factory(config):
        unreachable = UniFiClient.from_config(config)
        unreachable.session = Unreachable()
        return unreachable

    state.client_factory = factory
    filled(client)
    response = send(client, "POST", "/connection")
    body = response.json()
    assert response.status_code == 200 and body["ok"] is False
    assert any(c["status"] == "fail" for c in body["checks"])
    assert KEY not in response.text and "boom" not in response.text and "192.168.1.1" not in response.text
    assert send(client, "GET", "/status").json()["draft"]["connection_ok"] is False


def test_a_message_that_repeats_the_key_or_the_address_is_scrubbed(client, state, monkeypatch):
    from homelab_probe.doctor import Check
    from homelab_probe.server import wizard

    filled(client)
    leaky = [Check("controller.reachable", "fail", f"failed for https://192.168.1.1 with {KEY}", "try 192.168.1.1")]
    monkeypatch.setattr(wizard, "check_controller", lambda config, factory: leaky)
    text = send(client, "POST", "/connection").text
    assert KEY not in text and "192.168.1.1" not in text and "<controller>" in text and "***" in text


# -- the preview ------------------------------------------------------------------------------------------------

def test_the_preview_runs_diagnose_without_the_event_log(client, made):
    filled(client)
    response = send(client, "POST", "/preview")
    assert response.status_code == 200
    body = response.json()
    assert "events" not in body["areas"] and set(body["areas"]) >= {"devices", "health"}
    assert set(body["summary"]) >= {"critical", "warning", "info"} and body["total"] >= len(body["findings"])
    assert len(body["findings"]) <= 25 and {"code", "severity", "message"} <= set(body["findings"][0])
    assert made[0][1].session.max_redirects == 0 and KEY not in response.text


def test_the_preview_reports_an_unreadable_controller_by_kind(client, state):
    def factory(config):
        unreachable = UniFiClient.from_config(config)
        unreachable.session = Unreachable()
        return unreachable

    state.client_factory = factory
    filled(client)
    response = send(client, "POST", "/preview")
    assert response.status_code == 502 and response.json()["error"] == "controller_unreachable"
    assert KEY not in response.text and "192.168.1.1" not in response.text


def test_the_preview_refuses_an_address_that_cannot_be_a_controller(client, state, made):
    state.resolver = lambda host, port: ["0.0.0.0"]
    draft(client, url="https://unifi.lan", api_key=KEY)
    response = send(client, "POST", "/preview")
    assert response.status_code == 422 and response.json()["reason"] == "blocked_address" and made == []


# -- the audit trail --------------------------------------------------------------------------------------------

def test_every_step_is_audited_with_the_address_and_never_a_key_or_a_token(client, tmp_path):
    with fx.tls_server(fx.GOOD_CERT, fx.GOOD_KEY) as port:
        draft(client, url=f"https://{LOCAL}:{port}", api_key=KEY)
        send(client, "POST", "/certificate")
        send(client, "POST", "/preview")
    send(client, "POST", "/connection")
    entries = audit_lines(tmp_path)
    assert [e["event"] for e in entries] == ["setup.draft_changed", "setup.certificate_fetched", "setup.preview_run",
                                             "setup.connection_tested"]
    assert entries[0]["settings"] == "UNIFI_URL,UNIFI_API_KEY" and entries[0]["url"].startswith("https://127.")
    assert entries[1]["usable"] is True and len(entries[1]["fingerprint"].split(":")) == 32
    assert {e["actor"] for e in entries} == {"(setup)"}
    text = (tmp_path / "audit.log").read_text()
    assert KEY not in text and TOKEN not in text


def test_a_draft_that_changes_nothing_is_not_audited(client, tmp_path):
    draft(client, site="default")
    assert not (tmp_path / "audit.log").exists() or audit_lines(tmp_path) == []


def test_the_log_never_holds_the_key_or_the_token(client, caplog):
    caplog.set_level("DEBUG")
    filled(client)
    send(client, "POST", "/connection")
    send(client, "GET", "/status", token="wrong-wrong-wrong")
    rendered = " ".join(logs.scrub(record.getMessage()) for record in caplog.records)
    assert KEY not in rendered and TOKEN not in rendered


# -- once the server is set up ----------------------------------------------------------------------------------

def test_once_set_up_the_routes_want_an_administrator_and_the_token_stops_working(tmp_path, state):
    state.mode = None
    app = create_app(CONFIG, state_dir=tmp_path, hosts=["testserver"], auth=auth_for(tmp_path), setup=state)
    anonymous = TestClient(app)
    assert send(anonymous, "GET", "/status").json()["error"] == "not_logged_in"          # the token is not a login
    viewer = TestClient(app)
    login(viewer, "bob")
    assert send(viewer, "GET", "/status", token=None).json()["error"] == "forbidden"
    boss = TestClient(app)
    csrf = login(boss, "alice")
    assert send(boss, "GET", "/status", token=None).status_code == 200
    assert send(boss, "POST", "/draft", token=None, body={"site": "x"}).json()["error"] == "csrf_token"
    response = send(boss, "POST", "/draft", token=None, body={"site": "x"}, **{"X-CSRF-Token": csrf})
    assert response.status_code == 200 and response.json()["changed"] == ["UNIFI_SITE_ID"]
    assert audit_lines(tmp_path)[-1]["actor"] == "alice"
    assert boss.get("/api/v1/meta").json()["needs_setup"] is False


def test_an_app_without_a_setup_has_the_routes_but_they_say_there_is_nothing(tmp_path):
    app = create_app(CONFIG, state_dir=tmp_path, hosts=["testserver"], auth=auth_for(tmp_path))
    boss = TestClient(app)
    csrf = login(boss, "alice")
    response = send(boss, "GET", "/status", token=None, **{"X-CSRF-Token": csrf})
    assert response.status_code == 404 and response.json()["error"] == "no_setup"
    assert PASSWORD


def test_a_verify_setting_that_is_already_in_force_changes_nothing(client):
    assert draft(client, verify="true").json()["changed"] == []


def test_settings_that_do_not_fit_together_are_a_422_at_the_test(client, monkeypatch):
    from homelab_probe.config import ConfigError
    from homelab_probe.server import wizard

    filled(client)

    def refuse(values):
        raise ConfigError("nope")

    monkeypatch.setattr(wizard.config_module, "build_config", refuse)
    response = send(client, "POST", "/connection")
    assert response.status_code == 422 and response.json()["error"] == "invalid_setting"
    assert "nope" not in response.text


@pytest.mark.parametrize("key, url", [("abc", "https://ab"), ("a-long-enough-key", "https://192.168.1.1")])
def test_the_scrub_leaves_short_values_alone_and_hides_long_ones(state, key, url):
    state.draft.api_key, state.draft.url = key, url
    text = state.scrub(f"key {key} at {url} on ab")
    assert (key not in text) is (len(key) >= 4)
    assert ("<controller>" in text) is True and url not in text


def test_the_connection_test_offers_the_sites_for_the_picker(client):
    filled(client)
    sites = send(client, "POST", "/connection").json()["sites"]
    assert sites and {"name", "ref", "id"} == set(sites[0]) and all(isinstance(v, str) for v in sites[0].values())


def test_a_failed_connection_offers_no_sites_and_an_unreadable_site_list_does_not_fail_the_test(client, state, monkeypatch):
    from homelab_probe.client import UniFiAPIError
    from homelab_probe.server import wizard

    filled(client)

    def broken(client):
        raise UniFiAPIError("no", kind="timeout")

    monkeypatch.setattr(wizard, "info_document", broken)
    body = send(client, "POST", "/connection").json()
    assert body["ok"] is True and body["sites"] == []


def test_site_names_from_the_controller_are_cleaned(client, monkeypatch):
    from homelab_probe.documents import Document
    from homelab_probe.server import wizard

    filled(client)
    dirty = [{"name": "Home\x1b[31m", "ref": "default", "id": None}]
    monkeypatch.setattr(wizard, "info_document", lambda client: Document("info", {"application": {}, "sites": dirty}))
    sites = send(client, "POST", "/connection").json()["sites"]
    assert sites == [{"name": "Home[31m", "ref": "default", "id": ""}]


# -- an administrator replaces the token ------------------------------------------------------------------------

def test_once_an_administrator_exists_the_token_stops_working_and_a_session_is_needed(tmp_path, state):
    auth = auth_for(tmp_path)
    app = create_app(STANDIN, state_dir=tmp_path, hosts=["testserver"], auth=auth, setup=state)
    anonymous = TestClient(app)
    assert send(anonymous, "GET", "/status").json()["error"] == "not_logged_in"          # the right token, no use
    viewer = TestClient(app)
    login(viewer, "bob")
    assert send(viewer, "GET", "/status", token=None).json()["error"] == "forbidden"
    boss = TestClient(app)
    csrf = login(boss, "alice")
    assert send(boss, "GET", "/status", token=None).json()["mode"] == "setup"
    response = send(boss, "POST", "/draft", token=None, body={"site": "x"}, **{"X-CSRF-Token": csrf})
    assert response.status_code == 200
    assert audit_lines(tmp_path)[-1]["actor"] == "alice"
    assert boss.get("/api/v1/unifi/sites").json()["error"] == "not_configured"             # everything else stays closed


def test_a_disabled_administrator_does_not_count(tmp_path, state):
    auth = AuthState.for_directory(tmp_path, STANDIN)
    auth.accounts.store.add("alice", "admin", PASSWORD)
    users = json.loads((tmp_path / "users.json").read_text())
    users["users"][0]["disabled"] = True
    (tmp_path / "users.json").write_text(json.dumps(users))
    app = create_app(STANDIN, state_dir=tmp_path, hosts=["testserver"], auth=auth, setup=state)
    assert send(TestClient(app), "GET", "/status").status_code == 200


def test_an_unreadable_accounts_file_is_a_500_with_a_fixed_message(tmp_path, state):
    auth = AuthState.for_directory(tmp_path, STANDIN)
    (tmp_path / "users.json").write_text("not json")
    app = create_app(STANDIN, state_dir=tmp_path, hosts=["testserver"], auth=auth, setup=state)
    response = send(TestClient(app), "GET", "/status")
    assert response.status_code == 500 and response.json()["error"] == "accounts_unreadable"


def test_a_failed_connection_does_not_go_on_to_read_the_sites(client, state, monkeypatch):
    from homelab_probe.server import wizard

    def factory(config):
        unreachable = UniFiClient.from_config(config)
        unreachable.session = Unreachable()
        return unreachable

    def forbidden(client):
        raise AssertionError("the sites are read only after the checks passed")

    state.client_factory = factory
    monkeypatch.setattr(wizard, "info_document", forbidden)
    filled(client)
    body = send(client, "POST", "/connection").json()
    assert body["ok"] is False and body["sites"] == []


def test_a_draft_changed_while_the_connection_is_tested_is_not_marked_as_tested(client, state):
    def factory(config):
        draft(client, site="other")                       # the owner edits the draft while the controller answers
        return demo_client(config)

    state.client_factory = factory
    filled(client)
    assert send(client, "POST", "/connection").json()["ok"] is True
    assert send(client, "GET", "/status").json()["draft"]["connection_ok"] is None


def test_a_connection_test_that_nothing_interrupted_is_recorded(client):
    filled(client)
    send(client, "POST", "/connection")
    assert send(client, "GET", "/status").json()["draft"]["connection_ok"] is True
