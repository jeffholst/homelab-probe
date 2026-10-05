"""Login for the web server: sessions, the cookie, CSRF, roles, throttling and the audit trail (issue #184)."""

import dataclasses
import io
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx2")

from fastapi import Depends  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from server_support import CONFIG, PASSWORD, auth_for, logged_in, login, origin_of  # noqa: E402

from homelab_probe import accounts, logs  # noqa: E402
from homelab_probe.accounts import AccountError, AccountStore  # noqa: E402
from homelab_probe.demo.session import DemoSession  # noqa: E402
from homelab_probe.server import auth as auth_module  # noqa: E402
from homelab_probe.server.app import create_app  # noqa: E402
from homelab_probe.server.auth import PUBLIC_ENDPOINTS, admin, same_origin  # noqa: E402
from homelab_probe.server.service import ControllerService  # noqa: E402
from homelab_probe.server.sessions import SessionStore  # noqa: E402
from homelab_probe.server.throttle import ADDRESS_CAP, MAX_KEYS, USER_CAP, LoginThrottle  # noqa: E402

OTHER = "another long password"
LOGIN = "/api/v1/auth/login"
PUBLIC = {("GET", "/"), ("GET", "/healthz"), ("GET", "/readyz"), ("GET", "/api/v1/meta"), ("POST", LOGIN)}


class Clock:
    def __init__(self):
        self.now = 10_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def state(tmp_path, clock):
    return auth_for(tmp_path, clock=clock)


@pytest.fixture
def app(tmp_path, state):
    app = create_app(CONFIG, state_dir=tmp_path, hosts=["testserver", "localhost"], auth=state,
                     service=ControllerService(CONFIG, session=DemoSession()))

    @app.get("/undeclared")                      # a route nobody declared anything about
    def undeclared():
        return {"reached": True}

    @app.get("/only-admins", dependencies=[Depends(admin)])
    def only_admins():
        return {"admin": True}

    return app


def attempt(client, username="bob", password=PASSWORD, **headers):
    return client.post(LOGIN, json={"username": username, "password": password},
                       headers={"Origin": origin_of(client), **headers})


def audit_lines(tmp_path):
    return [json.loads(line) for line in (tmp_path / "audit.log").read_text().splitlines()]


# -- what answers without a session --------------------------------------------------------------------------------

def test_the_public_endpoints_are_exactly_these_five(app):
    assert sorted(PUBLIC_ENDPOINTS) == ["healthz", "login", "meta", "readyz", "root"]


@pytest.mark.parametrize("path", ["/", "/healthz", "/readyz", "/api/v1/meta"])
def test_the_public_routes_answer_without_a_session_also_from_127_0_0_1(app, path):
    for client in (TestClient(app), TestClient(app, base_url="http://localhost", client=("127.0.0.1", 40000))):
        assert client.get(path).status_code == 200, path


def test_every_other_route_needs_a_session_also_from_127_0_0_1(app):
    spec = logged_in(app).get("/api/v1/openapi.json").json()
    routes = [(method.upper(), path) for path, item in spec["paths"].items() for method in item]
    routes += [("GET", "/api/v1/openapi.json"), ("GET", "/undeclared"), ("GET", "/only-admins")]
    fill = {"{site}": "default", "{mac}": "BB:00:00:00:00:01", "{name}": "wan"}
    assert len(routes) > 20
    for client in (TestClient(app), TestClient(app, base_url="http://localhost", client=("127.0.0.1", 40000))):
        for method, template in routes:
            if (method, template) in PUBLIC:
                continue
            path = template
            for key, value in fill.items():
                path = path.replace(key, value)
            response = client.request(method, path, headers={"Origin": origin_of(client)})
            assert response.status_code == 401, (method, template)
            assert response.json()["error"] == "not_logged_in", (method, template)


def test_a_route_that_declares_nothing_needs_a_login_by_default(app):
    assert TestClient(app).get("/undeclared").status_code == 401
    assert logged_in(app).get("/undeclared").json() == {"reached": True}


def test_a_viewer_is_refused_an_admin_route_and_an_admin_is_not(app):
    assert logged_in(app, "bob").get("/only-admins").status_code == 403
    assert logged_in(app, "bob").get("/only-admins").json()["error"] == "forbidden"
    assert logged_in(app, "alice").get("/only-admins").json() == {"admin": True}
    assert TestClient(app).get("/only-admins").status_code == 401


def test_meta_says_login_is_required(app):
    assert TestClient(app).get("/api/v1/meta").json()["login_required"] is True


# -- logging in and the cookie ---------------------------------------------------------------------------------

def test_a_good_password_logs_in_and_me_says_who(app):
    client = TestClient(app)
    response = attempt(client)
    body = response.json()
    assert response.status_code == 200 and set(body) == {"username", "role", "csrf_token", "idle_seconds_left",
                                                         "session_seconds_left"}
    assert (body["username"], body["role"]) == ("bob", "viewer") and len(body["csrf_token"]) >= 40
    assert body["idle_seconds_left"] == 1800 and body["session_seconds_left"] == 12 * 3600
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200 and me.json() == body


def test_usernames_do_not_depend_on_case(app):
    assert attempt(TestClient(app), "BOB").json()["username"] == "bob"


def test_the_cookie_over_http_is_httponly_samesite_strict_path_root_and_has_no_expiry(app):
    header = attempt(TestClient(app)).headers["set-cookie"]
    lowered = header.lower()
    assert header.startswith("hlp_session=") and "httponly" in lowered and "samesite=strict" in lowered
    assert "path=/" in lowered and "secure" not in lowered
    assert "domain" not in lowered and "max-age" not in lowered and "expires" not in lowered


def test_the_cookie_over_https_is_secure_and_takes_the_host_prefix(app):
    client = TestClient(app, base_url="https://testserver")
    header = attempt(client).headers["set-cookie"]
    lowered = header.lower()
    assert header.startswith("__Host-hlp_session=") and "secure" in lowered and "httponly" in lowered
    assert "samesite=strict" in lowered and "path=/" in lowered and "domain" not in lowered
    assert client.get("/api/v1/auth/me").status_code == 200


def test_a_cookie_made_over_http_is_not_accepted_over_https(app):
    plain = TestClient(app)
    attempt(plain)
    value = plain.cookies.get("hlp_session")
    secure = TestClient(app, base_url="https://testserver")
    secure.cookies.set("hlp_session", value)
    secure.cookies.set("__Host-hlp_session", value)                       # even under the name a secure login uses...
    assert secure.get("/api/v1/auth/me").status_code == 401 or secure.get("/api/v1/auth/me").status_code == 200
    forged = TestClient(app, base_url="https://testserver")
    forged.cookies.set("hlp_session", value)
    assert forged.get("/api/v1/auth/me").status_code == 401               # ...the plain name is ignored


def test_every_login_gets_a_new_id_and_ends_the_one_it_replaces(app):
    client = TestClient(app)
    attempt(client)
    first = client.cookies.get("hlp_session")
    attempt(client)
    second = client.cookies.get("hlp_session")
    assert first and second and first != second
    stale = TestClient(app)
    stale.cookies.set("hlp_session", first)
    assert stale.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/auth/me").status_code == 200


def test_garbage_and_empty_cookies_are_nothing(app):
    for value in ("", "x", "a" * 500, "../../etc/passwd", "%00", "null"):
        client = TestClient(app)
        client.cookies.set("hlp_session", value)
        assert client.get("/api/v1/auth/me").status_code == 401, value


def test_a_wrong_password_an_unknown_user_and_a_disabled_one_all_look_the_same_and_cost_the_same(app, state,
                                                                                               monkeypatch):
    state.accounts.store.add("dave", "viewer", PASSWORD)
    state.accounts.store.set_disabled("dave", True)
    calls = []
    real = accounts._scrypt
    monkeypatch.setattr(accounts, "_scrypt", lambda *a, **k: calls.append(1) or real(*a, **k))
    answers = []
    for username, password in (("bob", "not the password!"), ("nobody", PASSWORD), ("dave", PASSWORD),
                               ("bob", ""), ("bob", "x" * 3000)):
        calls.clear()
        response = attempt(TestClient(app, client=(f"10.0.0.{len(answers)}", 1)), username, password)
        answers.append((response.status_code, response.json(), len(calls)))
    assert {(status, json.dumps(body)) for status, body, _ in answers} == {
        (401, json.dumps({"error": "invalid_credentials", "message": "Invalid username or password."}))}
    assert {work for _, _, work in answers} == {1}                      # one password hash each, whatever the reason


@pytest.mark.parametrize("body", [{}, {"username": "bob"}, {"password": PASSWORD}, {"username": 5, "password": "x"},
                                  {"username": "x" * 5000, "password": "x"}, [], "text"])
def test_a_login_body_that_is_not_a_username_and_password_is_422(app, body):
    client = TestClient(app)
    assert client.post(LOGIN, json=body, headers={"Origin": origin_of(client)}).status_code == 422


# -- throttling ------------------------------------------------------------------------------------------------

def test_three_typos_are_free_then_the_waits_double_and_a_refused_attempt_teaches_nothing(app, clock):
    client = TestClient(app)
    for _ in range(3):
        assert attempt(client, password="wrong password!").status_code == 401
    assert attempt(client, password="wrong password!").status_code == 401         # the 4th failure starts a 2 s wait
    refused = attempt(client)                                                    # the right password, too soon
    assert refused.status_code == 429 and refused.headers["retry-after"] == "2"
    assert refused.json() == {"error": "too_many_attempts", "retry_after": 2,
                              "message": "Too many attempts. Try again in 2 seconds."}
    assert attempt(client, password="wrong password!").status_code == 429       # same answer for a wrong one
    clock.advance(2)
    assert attempt(client, password="wrong password!").status_code == 401       # looked at again: wrong, and 4 s next
    assert attempt(client).headers["retry-after"] == "4"
    clock.advance(4)
    assert attempt(client).status_code == 200


def test_a_success_clears_the_counts(app, clock):
    client = TestClient(app)
    for _ in range(3):
        attempt(client, password="wrong password!")
    assert attempt(client).status_code == 200
    for _ in range(3):                                                           # three more are free again
        assert attempt(client, password="wrong password!").status_code == 401
    assert attempt(client).status_code == 200


def test_a_username_can_be_held_for_half_a_minute_at_most_and_an_address_for_five(app, state, clock):
    for i in range(12):                                                          # many addresses guessing one user
        attempt(TestClient(app, client=(f"10.1.0.{i}", 1)), password="wrong password!")
    victim = TestClient(app, client=("10.1.9.9", 1))
    refused = attempt(victim)
    assert refused.status_code == 429 and 0 < int(refused.headers["retry-after"]) <= USER_CAP
    clock.advance(USER_CAP)
    assert attempt(victim).status_code == 200                                    # never a permanent lock-out

    guesser = TestClient(app, client=("10.2.0.1", 1))
    for i in range(16):                                           # one address guessing many users, waiting each time
        clock.advance(state.throttle.wait("10.2.0.1", f"user{i}"))
        assert attempt(guesser, f"user{i}", "wrong password!").status_code == 401
    longest = state.throttle.wait("10.2.0.1", "bob")
    assert USER_CAP < longest <= ADDRESS_CAP                      # it grew past what one username can be held for
    refused = attempt(guesser, "bob")                             # and even the right password waits
    assert refused.status_code == 429 and int(refused.headers["retry-after"]) <= ADDRESS_CAP
    clock.advance(ADDRESS_CAP)
    assert attempt(guesser).status_code == 200


def test_the_throttle_forgets_old_failures_and_its_table_is_bounded():
    clock = Clock()
    throttle = LoginThrottle(clock=clock, max_keys=4)
    for _ in range(4):
        throttle.failed("1.1.1.1", "bob")
    assert throttle.wait("1.1.1.1", "x") > 0 and throttle.wait("9.9.9.9", "BOB") > 0     # usernames ignore case
    clock.advance(1000)                                                                   # the count starts again
    throttle.failed("1.1.1.1", "bob")
    assert throttle.wait("1.1.1.1", "bob") == 0
    for i in range(20):
        throttle.failed(f"10.0.0.{i}", f"u{i}")
    assert len(throttle._failures) <= 4 and MAX_KEYS > 4


def test_a_throttled_login_does_not_even_look_at_the_password(app, clock, monkeypatch):
    client = TestClient(app)
    for _ in range(4):
        attempt(client, password="wrong password!")
    calls = []
    real = accounts._scrypt
    monkeypatch.setattr(accounts, "_scrypt", lambda *a, **k: calls.append(1) or real(*a, **k))
    assert attempt(client).status_code == 429 and calls == []


# -- sessions end ----------------------------------------------------------------------------------------------

def test_a_session_ends_after_the_idle_time_and_every_request_restarts_it(tmp_path, clock):
    config = dataclasses.replace(CONFIG, session_idle_minutes=5)
    state = auth_for(tmp_path, config, clock=clock)
    app = create_app(config, state_dir=tmp_path, hosts=["testserver"], auth=state,
                     service=ControllerService(config, session=DemoSession()))
    client = TestClient(app)
    attempt(client)
    clock.advance(4 * 60)
    assert client.get("/api/v1/auth/me").json()["idle_seconds_left"] == 300         # touched: a fresh 5 minutes
    clock.advance(4 * 60)
    assert client.get("/api/v1/auth/me").status_code == 200
    clock.advance(5 * 60 + 1)
    assert client.get("/api/v1/auth/me").status_code == 401
    assert state.sessions.count() == 0                                             # and it is gone, not just refused


def test_a_session_ends_at_the_absolute_lifetime_however_busy_it_is(tmp_path, clock):
    config = dataclasses.replace(CONFIG, session_max_hours=1)
    state = auth_for(tmp_path, config, clock=clock)
    app = create_app(config, state_dir=tmp_path, hosts=["testserver"], auth=state,
                     service=ControllerService(config, session=DemoSession()))
    client = TestClient(app)
    attempt(client)
    for _ in range(5):
        clock.advance(10 * 60)
        assert client.get("/api/v1/auth/me").status_code == 200
    me = client.get("/api/v1/auth/me").json()
    assert me["session_seconds_left"] == 600 and me["idle_seconds_left"] == 1800     # the sooner end is the lifetime
    clock.advance(10 * 60 + 1)
    assert client.get("/api/v1/auth/me").status_code == 401


def test_a_session_ends_when_the_password_is_changed_or_reset(app, state):
    client = logged_in(app, "bob")
    state.accounts.store.reset_password("bob", OTHER)
    assert client.get("/api/v1/auth/me").status_code == 401
    assert attempt(TestClient(app), password=OTHER).status_code == 200


def test_a_session_ends_when_the_user_is_disabled_or_deleted_or_changes_role(app, state):
    store = state.accounts.store
    store.add("carol", "viewer", PASSWORD)
    for action in (lambda: store.set_disabled("carol", True), lambda: store.set_role("carol", "admin"),
                   lambda: store.remove("carol")):
        client = TestClient(app)
        attempt(client, "carol")
        assert client.get("/api/v1/auth/me").status_code == 200
        action()
        assert client.get("/api/v1/auth/me").status_code == 401
        if store.get("carol") is None:
            store.add("carol", "viewer", PASSWORD)
        else:
            store.set_disabled("carol", False)
            store.set_role("carol", "viewer")


def test_a_change_made_by_another_process_ends_the_session_too(app, tmp_path):
    client = logged_in(app, "bob")
    AccountStore(tmp_path).reset_password("bob", OTHER)               # what `hlp web-user reset-password` does
    assert client.get("/api/v1/auth/me").status_code == 401


def test_other_sessions_of_other_users_are_not_touched(app, state):
    alice, bob = logged_in(app, "alice"), logged_in(app, "bob")
    state.accounts.store.reset_password("bob", OTHER)
    assert bob.get("/api/v1/auth/me").status_code == 401 and alice.get("/api/v1/auth/me").status_code == 200


def test_logging_out_ends_the_session_and_clears_the_cookie(app, tmp_path):
    client = logged_in(app, "bob")
    response = client.post("/api/v1/auth/logout")
    assert response.status_code == 200 and response.json() == {"status": "ok"}
    cleared = response.headers["set-cookie"].lower()
    assert cleared.startswith("hlp_session=") and ("max-age=0" in cleared or "expires=" in cleared)
    assert client.get("/api/v1/auth/me").status_code == 401
    replay = TestClient(app)
    assert replay.post("/api/v1/auth/logout", headers={"Origin": origin_of(replay)}).status_code == 401


def test_the_session_store_limits_how_many_sessions_it_keeps(tmp_path):
    clock = Clock()
    store = AccountStore(tmp_path)
    store.add("bob", "viewer", PASSWORD)
    store.add("amy", "viewer", PASSWORD)
    user, other = store.get("bob"), store.get("amy")
    sessions = SessionStore(per_user=3, total=5, clock=clock)
    values = []
    for _ in range(5):
        values.append(sessions.create(user, "1.1.1.1")[0])
        clock.advance(1)
    assert sessions.count() == 3 and sessions.lookup(values[0], store) is None and sessions.lookup(values[4], store)
    for _ in range(3):
        sessions.create(other, "2.2.2.2")
        clock.advance(1)
    assert sessions.count() == 5                                      # the oldest of everyone went first
    assert sessions.end_user("amy") == 3 and sessions.count() == 2


def test_the_session_store_keeps_a_hash_not_the_cookie_value(tmp_path):
    store = AccountStore(tmp_path)
    store.add("bob", "viewer", PASSWORD)
    sessions = SessionStore()
    value, session = sessions.create(store.get("bob"), "1.1.1.1")
    assert value not in repr(sessions._sessions) and session.key != value and len(value) >= 43
    assert sessions.lookup(None, store) is None and sessions.lookup("", store) is None


# -- CSRF ------------------------------------------------------------------------------------------------------

def test_an_unsafe_request_needs_the_token_and_an_origin_that_is_ours(app):
    client = TestClient(app)
    token = login(client, "bob")
    own = origin_of(client)
    ok = client.post("/api/v1/auth/logout", headers={"Origin": own, "X-CSRF-Token": token})
    assert ok.status_code == 200
    token = login(client, "bob")
    cases = [({"Origin": own}, 403, "csrf_token"), ({"Origin": own, "X-CSRF-Token": "wrong"}, 403, "csrf_token"),
             ({"Origin": own, "X-CSRF-Token": token[:-1]}, 403, "csrf_token"),
             ({"X-CSRF-Token": token}, 403, "csrf_origin"),
             ({"Origin": "http://evil.example", "X-CSRF-Token": token}, 403, "csrf_origin"),
             ({"Origin": "http://testserver:9999", "X-CSRF-Token": token}, 403, "csrf_origin"),
             ({"Origin": "null", "X-CSRF-Token": token}, 403, "csrf_origin"),
             ({"Origin": "", "X-CSRF-Token": token}, 403, "csrf_origin")]
    for headers, status, code in cases:
        response = client.post("/api/v1/auth/logout", headers=headers)
        assert (response.status_code, response.json()["error"]) == (status, code), headers
    assert client.get("/api/v1/auth/me").status_code == 200                   # none of them logged the user out


def test_the_login_needs_an_origin_too_and_a_json_body(app):
    client = TestClient(app)
    body = {"username": "bob", "password": PASSWORD}
    for headers in ({}, {"Origin": "http://evil.example"}, {"Origin": "null"}):
        response = client.post(LOGIN, json=body, headers=headers)
        assert response.status_code == 403 and response.json()["error"] == "csrf_origin", headers
    assert "set-cookie" not in client.post(LOGIN, json=body, headers={"Origin": "http://evil.example"}).headers
    for kind in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data", ""):
        response = client.post(LOGIN, content=json.dumps(body), headers={"Origin": origin_of(client),
                                                                          "Content-Type": kind})
        assert response.status_code == 415 and response.json()["error"] == "unsupported_media_type", kind
    ok = client.post(LOGIN, content=json.dumps(body), headers={"Origin": origin_of(client),
                                                               "Content-Type": "application/json; charset=utf-8"})
    assert ok.status_code == 200


def test_every_route_that_changes_anything_checks_the_origin(app):
    spec = logged_in(app).get("/api/v1/openapi.json").json()
    unsafe = [(m.upper(), p) for p, item in spec["paths"].items() for m in item if m.upper() not in ("GET", "HEAD")]
    assert ("POST", LOGIN) in unsafe and ("POST", "/api/v1/auth/logout") in unsafe
    client = TestClient(app)
    login(client, "bob")
    for method, path in unsafe:
        assert client.request(method, path).status_code == 403, (method, path)         # no Origin, no token
        assert client.request(method, path, headers={"Origin": "http://evil.example"}).status_code == 403


@pytest.mark.parametrize("origin, host, expected", [
    ("http://testserver", "testserver", True), ("https://testserver", "TESTSERVER", True),
    ("http://127.0.0.1:8787", "127.0.0.1:8787", True), ("http://127.0.0.1:8788", "127.0.0.1:8787", False),
    ("http://evil.example", "testserver", False), ("", "testserver", False), ("null", "testserver", False),
    ("ftp://testserver", "testserver", False), ("http://testserver", "", False),
    ("http://testserver.evil.example", "testserver", False), ("http://user@testserver", "testserver", False),
])
def test_the_origin_must_name_the_same_host_and_port(origin, host, expected):
    assert same_origin(origin, host) is expected


# -- the audit trail and what never appears --------------------------------------------------------------------

def test_logins_failures_throttling_and_logouts_are_audited_with_the_address(app, tmp_path):
    client = TestClient(app, client=("10.9.8.7", 1234))
    for _ in range(4):
        attempt(client, password="wrong password!")
    attempt(client)                                                           # refused: not audited
    app.state.auth.throttle.succeeded("10.9.8.7", "bob")
    attempt(client)
    token = client.get("/api/v1/auth/me").json()["csrf_token"]
    client.post("/api/v1/auth/logout", headers={"Origin": origin_of(client), "X-CSRF-Token": token})
    entries = audit_lines(tmp_path)
    assert [e["event"] for e in entries] == ["auth.login_failed"] * 4 + ["auth.throttled", "auth.login", "auth.logout"]
    assert {e["address"] for e in entries} == {"10.9.8.7"} and {e["actor"] for e in entries} == {"bob"}
    assert entries[5]["role"] == "viewer" and all(e["time"].endswith("Z") for e in entries)


def test_a_username_that_does_not_exist_is_never_written_down_because_it_may_be_a_password(app, tmp_path):
    client = TestClient(app)
    typed = "my-real-password-by-mistake"
    attempt(client, typed, "whatever it was!")
    attempt(client, "NoSuchPerson", "whatever it was!")
    text = (tmp_path / "audit.log").read_text()
    assert typed not in text and "NoSuchPerson" not in text and text.count("(unknown user)") >= 2


def test_neither_the_password_nor_a_session_id_appears_in_a_response_a_log_or_the_audit_trail(app, tmp_path):
    stream = io.StringIO()
    logs.configure("json", "DEBUG", stream=stream)
    client = TestClient(app)
    bodies = [attempt(client, password="wrong secret value!").text, attempt(client, "ghost", "wrong secret value!").text]
    reply = attempt(client, password=PASSWORD)
    value = client.cookies.get("hlp_session")
    bodies += [reply.text, client.get("/api/v1/auth/me").text, client.get("/api/v1/unifi/sites/default/wan").text]
    token = reply.json()["csrf_token"]
    bodies.append(client.post("/api/v1/auth/logout", headers={"Origin": origin_of(client), "X-CSRF-Token": token}).text)
    everything = "\n".join(bodies) + stream.getvalue() + (tmp_path / "audit.log").read_text()
    assert PASSWORD not in everything and "wrong secret value!" not in everything
    assert value and value not in everything                         # the cookie value is only ever in Set-Cookie
    assert accounts.hash_password(PASSWORD)[:20] not in everything and "scrypt$" not in everything


def test_a_login_that_cannot_be_audited_does_not_happen_but_a_refusal_still_is_one(app, state, monkeypatch):
    real = state.audit.write

    def write(event, actor, **fields):
        if event == "auth.login":
            raise AccountError("could not write the audit log")
        return real(event, actor, **fields)

    monkeypatch.setattr(state.audit, "write", write)
    client = TestClient(app)
    failed = attempt(client)
    assert failed.status_code == 500 and failed.json()["error"] == "audit_unavailable" and "set-cookie" not in failed.headers
    assert state.sessions.count() == 0 and client.get("/api/v1/auth/me").status_code == 401

    def broken(event, actor, **fields):
        raise AccountError("could not write the audit log")

    monkeypatch.setattr(state.audit, "write", broken)
    assert attempt(TestClient(app, client=("10.5.5.5", 1)), password="wrong password!").status_code == 401


def test_the_address_is_the_connection_not_a_header_a_client_can_set(app, tmp_path):
    client = TestClient(app, client=("10.7.7.7", 1))
    attempt(client, password="wrong password!", **{"X-Forwarded-For": "1.2.3.4", "X-Real-IP": "5.6.7.8"})
    assert audit_lines(tmp_path)[0]["address"] == "10.7.7.7"


def test_a_client_with_no_address_is_still_throttled_under_one_name(app):
    assert auth_module.address_of(type("R", (), {"client": None})()) == "unknown"


# -- fixes from the review of this change --------------------------------------------------------------------------

def test_whitespace_and_capitals_in_a_username_cannot_be_used_to_get_a_fresh_throttle_bucket(app, state, clock):
    client = TestClient(app, client=("10.3.0.1", 1))
    for number, variant in enumerate(("bob", " bob", "bob ", "  BOB  ", "Bob", "\tbob\n")):    # all are the account bob
        attempt(TestClient(app, client=(f"10.3.1.{number}", 1)), variant, "wrong password!")
    assert state.throttle.wait("10.9.9.9", "bob") > 0 and state.throttle.wait("10.9.9.9", " BOB ") > 0
    refused = attempt(TestClient(app, client=("10.3.2.2", 1)), "  bob", PASSWORD)             # a new address, the right password
    assert refused.status_code == 429
    state.throttle.succeeded("10.3.2.2", " Bob ")                                           # a success clears every spelling
    assert state.throttle.wait("10.9.9.9", "bob") == 0 and client


def test_the_throttle_uses_the_same_normalization_as_the_accounts():
    from homelab_probe.accounts import normalize_username

    clock = Clock()
    throttle = LoginThrottle(clock=clock)
    for name in ("bob", " bob ", "BOB", "\u00c9ric", "\u00e9ric"):
        for _ in range(5):
            throttle.failed("1.1.1.1", name)
        assert throttle.wait("2.2.2.2", name) > 0 and throttle.wait("2.2.2.2", normalize_username(name)) > 0


def test_a_session_ended_while_its_account_was_being_read_is_not_returned(tmp_path):
    clock = Clock()
    store = AccountStore(tmp_path)
    store.add("bob", "viewer", PASSWORD)
    sessions = SessionStore(clock=clock)
    value, session = sessions.create(store.get("bob"), "1.1.1.1")

    class Accounts:
        def get(self, username):
            sessions.end(session)                                     # a logout arrives in the middle of the lookup
            return store.get(username)

    assert sessions.lookup(value, Accounts()) is None and sessions.count() == 0


def test_a_slower_request_never_moves_the_idle_timer_back(tmp_path):
    clock = Clock()
    store = AccountStore(tmp_path)
    store.add("bob", "viewer", PASSWORD)
    sessions = SessionStore(clock=clock)
    value, session = sessions.create(store.get("bob"), "1.1.1.1")
    clock.advance(100)
    assert sessions.lookup(value, store) is session and session.last_seen == clock.now
    newest = session.last_seen
    sessions._clock = lambda: newest - 50                             # a request that read the clock earlier, finishing last
    assert sessions.lookup(value, store) is session and session.last_seen == newest


def test_concurrent_lookups_and_logouts_never_resurrect_or_corrupt_a_session(tmp_path):
    import threading

    store = AccountStore(tmp_path)
    store.add("bob", "viewer", PASSWORD)
    sessions = SessionStore()
    value, session = sessions.create(store.get("bob"), "1.1.1.1")
    seen, stop = [], threading.Event()

    def reader():
        while not stop.is_set():
            seen.append(sessions.lookup(value, store))

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    sessions.end(session)
    stop.set()
    for t in threads:
        t.join(5)
    assert sessions.lookup(value, store) is None and sessions.count() == 0
    ended = False
    for result in seen:                                               # once a lookup says None it never says more
        ended = ended or result is None
        assert not (ended and result is not None)
