"""Changing one's own password over the API (issue #241): POST /api/v1/auth/password and ``can_change_password``."""

import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("tomlkit")

from fastapi.testclient import TestClient  # noqa: E402
from server_support import CONFIG, PASSWORD, auth_for, logged_in, login, origin_of  # noqa: E402

from homelab_probe import accounts  # noqa: E402
from homelab_probe.accounts import AccountError, AccountStore, NoSuchUserError  # noqa: E402
from homelab_probe.demo.session import DemoSession  # noqa: E402
from homelab_probe.server import profile_api  # noqa: E402
from homelab_probe.server.app import create_app  # noqa: E402
from homelab_probe.server.service import ControllerService  # noqa: E402
from homelab_probe.server.sessions import SessionStore  # noqa: E402

URL = "/api/v1/auth/password"
ME = "/api/v1/auth/me"
NEW = "a brand new password"
THIRD = "yet another password"


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


def make_app(tmp_path, clock=None, **kwargs):
    return create_app(CONFIG, state_dir=tmp_path, hosts=["testserver"], auth=auth_for(tmp_path, clock=clock),
                      service=ControllerService(CONFIG, session=DemoSession()), **kwargs)


@pytest.fixture
def app(tmp_path, clock):
    return make_app(tmp_path, clock)


def change(client, current=PASSWORD, new=NEW, **extra):
    return client.post(URL, json={"current_password": current, "new_password": new, **extra})


def audit_lines(tmp_path):
    return [json.loads(line) for line in (tmp_path / "audit.log").read_text().splitlines()]


def can_log_in(app, username, password):
    return TestClient(app).post("/api/v1/auth/login", json={"username": username, "password": password},
                                headers={"Origin": "http://testserver"}).status_code == 200


# -- the change ---------------------------------------------------------------------------------------------------

def test_the_password_changes_and_only_the_new_one_logs_in(app):
    bob = logged_in(app, "bob")
    response = change(bob)
    assert response.status_code == 200 and response.json()["username"] == "bob"
    assert not can_log_in(app, "bob", PASSWORD)
    assert can_log_in(app, "bob", NEW)


def test_an_administrator_changes_their_own_password_too_and_a_viewer_may_and_nothing_else_changes(app, tmp_path):
    alice, bob = logged_in(app, "alice"), logged_in(app, "bob")
    other = AccountStore(tmp_path).get("alice").password_hash
    assert change(bob).status_code == 200
    assert AccountStore(tmp_path).get("alice").password_hash == other and can_log_in(app, "alice", PASSWORD)
    assert change(alice, new=THIRD).status_code == 200 and can_log_in(app, "alice", THIRD)
    assert AccountStore(tmp_path).get("bob").role == "viewer"


def test_the_other_sessions_of_the_user_end_and_the_caller_keeps_a_new_one(app):
    first, second, caller = logged_in(app, "bob"), logged_in(app, "bob"), logged_in(app, "bob")
    alice = logged_in(app, "alice")
    old_token = caller.headers["X-CSRF-Token"]
    response = change(caller)
    assert response.status_code == 200
    assert first.get(ME).status_code == 401 and second.get(ME).status_code == 401
    assert alice.get(ME).status_code == 200                                      # another user is not affected
    assert caller.get(ME).status_code == 200                                     # the cookie jar took the new cookie
    token = response.json()["csrf_token"]
    assert token and token != old_token and caller.get(ME).json()["csrf_token"] == token
    assert caller.post("/api/v1/auth/logout").json()["error"] == "csrf_token"     # the old token is no longer good
    caller.headers["X-CSRF-Token"] = token
    assert change(caller, new=THIRD, current=NEW).status_code == 200              # and the new one is


def test_the_old_cookie_value_does_not_survive_the_change(app):
    caller = logged_in(app, "bob")
    old = caller.cookies.get("hlp_session")
    change(caller)
    assert caller.cookies.get("hlp_session") != old
    replay = TestClient(app, cookies={"hlp_session": old})
    assert replay.get(ME).status_code == 401


def test_the_cookie_of_the_answer_has_the_attributes_of_a_login(app):
    caller = logged_in(app, "bob")
    header = change(caller).headers["set-cookie"]
    assert header.startswith("hlp_session=") and "HttpOnly" in header and "SameSite=strict" in header
    assert "Path=/" in header and "Domain" not in header


def test_the_change_does_not_lengthen_the_login(tmp_path, clock):
    app = make_app(tmp_path, clock)
    caller = logged_in(app, "bob")
    clock.advance(1000)
    remaining = caller.get(ME).json()["session_seconds_left"]
    response = change(caller).json()
    assert response["session_seconds_left"] == remaining                      # the start of the login is kept
    assert response["idle_seconds_left"] == CONFIG.session_idle_minutes * 60  # the idle timer restarted
    clock.advance(CONFIG.session_max_hours * 3600 - 1000 + 1)
    assert caller.get(ME).status_code == 401


def test_the_last_login_time_is_not_a_login_and_is_left_alone(app, tmp_path):
    caller = logged_in(app, "bob")
    before = AccountStore(tmp_path).get("bob").last_login
    change(caller)
    assert AccountStore(tmp_path).get("bob").last_login == before


# -- refusals ---------------------------------------------------------------------------------------------------

def test_a_wrong_current_password_is_refused_with_a_fixed_message_and_changes_nothing(app, tmp_path):
    caller = logged_in(app, "bob")
    before = AccountStore(tmp_path).get("bob").password_hash
    response = change(caller, current="not the password")
    assert response.status_code == 422 and response.json() == {"error": "invalid_current_password",
                                                               "message": "The current password is wrong."}
    assert AccountStore(tmp_path).get("bob").password_hash == before and can_log_in(app, "bob", PASSWORD)
    assert caller.get(ME).status_code == 200                                    # the session goes on


def test_a_current_password_that_is_long_is_just_wrong_and_one_over_the_body_limit_is_refused(app):
    caller = logged_in(app, "bob")
    assert change(caller, current="x" * 2000).json()["error"] == "invalid_current_password"
    assert change(caller, current="x" * 5000).json()["error"] == "invalid_parameter"


@pytest.mark.parametrize("new, message", [
    ("short", "The password must have at least 12 characters."),
    ("x" * 1025, "The password must have at most 1024 characters."),
])
def test_a_new_password_that_breaks_the_policy_is_refused_with_the_sentence_of_the_rule(app, tmp_path, new, message):
    caller = logged_in(app, "bob")
    response = change(caller, new=new)
    assert response.status_code == 422 and response.json() == {"error": "invalid_password", "message": message}
    assert new not in response.text and can_log_in(app, "bob", PASSWORD)


def test_the_policy_is_checked_first_so_it_says_nothing_about_the_current_password_and_costs_no_attempt(app):
    caller = logged_in(app, "bob")
    for _ in range(6):
        response = change(caller, current="not the password", new="short")
        assert response.json()["error"] == "invalid_password"                   # never "wrong password", never a wait
    assert change(caller).status_code == 200                                    # and none of them counted


@pytest.mark.parametrize("body", [
    {}, {"current_password": PASSWORD}, {"new_password": NEW}, {"current_password": 1, "new_password": NEW},
    {"current_password": PASSWORD, "new_password": NEW, "username": "alice"},
    {"current_password": PASSWORD, "new_password": None},
])
def test_a_body_that_is_not_exactly_the_two_passwords_is_refused_and_not_echoed(app, body):
    caller = logged_in(app, "bob")
    response = caller.post(URL, json=body)
    assert response.status_code == 422 and response.json() == {"error": "invalid_parameter",
                                                               "message": "A parameter is not valid."}
    assert can_log_in(app, "bob", PASSWORD)


def test_guessing_the_current_password_is_slowed_down_like_a_login_and_a_wait_refuses_the_right_one_too(
        app, tmp_path, clock, monkeypatch):
    caller = logged_in(app, "bob")
    for _ in range(4):
        assert change(caller, current="a wrong guess!!").status_code == 422
    calls = []
    real = accounts._scrypt
    monkeypatch.setattr(accounts, "_scrypt", lambda *a, **k: calls.append(1) or real(*a, **k))
    for current in ("a wrong guess!!", PASSWORD):
        response = change(caller, current=current)
        assert response.status_code == 429 and response.json()["error"] == "too_many_attempts"
        assert int(response.headers["Retry-After"]) >= 1 and response.json()["retry_after"] >= 1
    assert calls == []                                                          # the password was not even looked at
    assert can_log_in(app, "bob", PASSWORD) is False                            # the login of that user waits too
    clock.advance(3)
    assert change(caller).status_code == 200                                    # the wait ends by itself
    entries = [(e["event"], e["actor"]) for e in audit_lines(tmp_path)]
    assert entries.count(("auth.password_failed", "bob")) == 4 and entries.count(("auth.throttled", "bob")) == 1


def test_a_guess_at_a_password_counts_with_the_failed_logins_of_the_same_user(app, clock):
    caller = logged_in(app, "bob")
    other = TestClient(app)
    for _ in range(4):
        other.post("/api/v1/auth/login", json={"username": "bob", "password": "wrong password!"},
                   headers={"Origin": "http://testserver"})
    assert change(caller).status_code == 429
    clock.advance(31)
    assert change(caller).status_code == 200


def test_a_success_clears_the_count(app, clock):
    caller = logged_in(app, "bob")
    for _ in range(3):
        change(caller, current="a wrong guess!!")
    assert change(caller).status_code == 200
    caller.headers["X-CSRF-Token"] = caller.get(ME).json()["csrf_token"]
    for _ in range(3):                                                            # three free failures again
        assert change(caller, current="a wrong guess!!", new=THIRD).status_code == 422
    assert change(caller, current=NEW, new=THIRD).status_code == 200


def test_a_read_only_server_refuses_and_changes_nothing(tmp_path):
    app = make_app(tmp_path, read_only=True)
    caller = logged_in(app, "bob")
    response = change(caller)
    assert response.status_code == 403 and response.json()["error"] == "read_only"
    assert can_log_in(app, "bob", PASSWORD) and caller.get(ME).status_code == 200


def test_it_needs_a_session_the_csrf_token_and_the_origin(app):
    anonymous = TestClient(app)
    body = {"current_password": PASSWORD, "new_password": NEW}
    assert anonymous.post(URL, json=body, headers={"Origin": origin_of(anonymous)}).status_code == 401
    assert anonymous.post(URL, json=body).status_code == 403                        # no Origin
    caller = logged_in(app, "bob")
    del caller.headers["X-CSRF-Token"]
    assert caller.post(URL, json=body).json()["error"] == "csrf_token"
    caller.headers["X-CSRF-Token"] = "forged"
    assert caller.post(URL, json=body).json()["error"] == "csrf_token"
    assert can_log_in(app, "bob", PASSWORD) and not can_log_in(app, "bob", NEW)


def test_an_account_that_cannot_change_its_password_here_says_so_and_is_refused(app):
    caller = logged_in(app, "bob")
    assert caller.get(ME).json()["can_change_password"] is True
    app.state.auth.accounts.can_change_password = False                             # a later authenticator
    assert caller.get(ME).json()["can_change_password"] is False
    response = change(caller)
    assert response.status_code == 403 and response.json()["error"] == "password_not_changeable"
    assert can_log_in(app, "bob", PASSWORD)


def test_me_and_the_login_say_that_the_password_can_be_changed_here(app):
    client = TestClient(app)
    login(client, "alice")
    assert client.get(ME).json()["can_change_password"] is True
    answer = client.post("/api/v1/auth/login", json={"username": "bob", "password": PASSWORD},
                         headers={"Origin": origin_of(client)})
    assert answer.json()["can_change_password"] is True


# -- the audit trail and what never appears ----------------------------------------------------------------------------

def test_a_change_is_audited_with_the_user_and_the_address_and_a_refusal_is_too(app, tmp_path):
    caller = logged_in(app, "bob")
    change(caller, current="not the password")
    change(caller)
    entries = audit_lines(tmp_path)
    failed, changed = entries[-2], entries[-1]
    assert (failed["event"], failed["actor"], failed["address"]) == ("auth.password_failed", "bob", "testclient")
    assert (changed["event"], changed["actor"], changed["user"], changed["address"]) == (
        "user.password_changed", "bob", "bob", "testclient")
    assert set(changed) == {"time", "event", "actor", "user", "address"} | ({"request_id"} & set(changed))


def test_a_change_that_cannot_be_audited_is_not_made(app, tmp_path, monkeypatch):
    caller = logged_in(app, "bob")
    before = AccountStore(tmp_path).get("bob").password_hash

    def broken(event, actor, **fields):
        raise OSError("disk full")

    monkeypatch.setattr(app.state.auth.audit, "write", broken)
    response = change(caller)
    assert response.status_code == 500 and response.json()["error"] == "audit_unavailable"
    assert "disk full" not in response.text and AccountStore(tmp_path).get("bob").password_hash == before
    monkeypatch.undo()
    assert caller.get(ME).status_code == 200 and can_log_in(app, "bob", PASSWORD)


def test_a_password_never_reaches_a_response_a_header_a_log_or_the_audit_trail(app, tmp_path, caplog):
    caplog.set_level("DEBUG")
    caller = logged_in(app, "bob")
    secrets_typed = ["poison-current-0001", "poison-new-password-0002", "poison-s"]
    answers = [change(caller, current=secrets_typed[0], new=secrets_typed[1]),            # wrong current
               change(caller, new=secrets_typed[2]),                                       # too short
               change(caller, current=PASSWORD, new=secrets_typed[1])]                     # success
    assert [a.status_code for a in answers] == [422, 422, 200]
    stored = AccountStore(tmp_path).get("bob").password_hash
    seen = [a.text + str(dict(a.headers)) for a in answers]
    seen.append((tmp_path / "audit.log").read_text())
    seen.append(" ".join(r.getMessage() + str(vars(r)) for r in caplog.records))
    for text in seen:
        for secret in (*secrets_typed, PASSWORD, stored, "scrypt$"):
            assert secret not in text, secret


def test_a_refusal_of_the_store_is_a_fixed_sentence_and_never_its_text(app, tmp_path, monkeypatch):
    caller = logged_in(app, "bob")

    def broken(self, *args, **kwargs):
        raise AccountError(f"{tmp_path}/users.json is damaged, password hunter2")

    monkeypatch.setattr(AccountStore, "change_password", broken)
    response = change(caller)
    assert response.status_code == 500 and response.json()["error"] == "accounts_unreadable"
    assert str(tmp_path) not in response.text and "hunter2" not in response.text


def test_an_account_that_went_away_while_the_request_ran_is_a_401(app, monkeypatch):
    caller = logged_in(app, "bob")

    def gone(self, *args, **kwargs):
        raise NoSuchUserError("there is no user bob")

    monkeypatch.setattr(AccountStore, "change_password", gone)
    assert change(caller).json()["error"] == "not_logged_in"
    assert profile_api.failure(NoSuchUserError("x")).status == 401


def test_the_route_is_in_the_openapi_document_with_its_errors(app):
    operation = app.openapi()["paths"][URL]["post"]
    assert {"200", "401", "403", "422", "429", "500"} <= set(operation["responses"])
    assert "can_change_password" in operation["responses"]["200"]["content"]["application/json"]["schema"]["properties"]
    assert "current_password" in json.dumps(app.openapi()["components"])


# -- the store and the session store -------------------------------------------------------------------------------------

def test_the_store_checks_and_changes_in_one_step(tmp_path):
    store = AccountStore(tmp_path)
    store.add("carol", "viewer", PASSWORD)
    login_time = store.get("carol").last_login
    changed = store.change_password("carol", PASSWORD, NEW, "x")
    assert accounts.verify_password(NEW, changed.password_hash) and not accounts.verify_password(PASSWORD, changed.password_hash)
    assert store.get("carol") == changed and changed.last_login == login_time
    with pytest.raises(accounts.WrongPasswordError):
        store.change_password("carol", PASSWORD, THIRD, "x")
    with pytest.raises(accounts.PolicyError):
        store.change_password("carol", NEW, "short", "x")
    assert store.get("carol") == changed


def test_an_unknown_or_disabled_user_costs_the_same_work_and_is_not_found(tmp_path, monkeypatch):
    store = AccountStore(tmp_path)
    store.add("carol", "viewer", PASSWORD)
    store.add("dave", "admin", PASSWORD)
    store.set_disabled("carol", True)
    seen = []
    real = accounts.verify_password
    monkeypatch.setattr(accounts, "verify_password", lambda password, stored: seen.append(stored) or real(password, stored))
    for name in ("carol", "nobody"):
        with pytest.raises(NoSuchUserError):
            store.change_password(name, PASSWORD, NEW, "decoy-hash")
    assert seen == ["decoy-hash", "decoy-hash"] and accounts.verify_password(PASSWORD, store.get("carol").password_hash)


def test_the_local_accounts_change_a_password_with_their_decoy_and_say_they_can(tmp_path):
    local = accounts.LocalAccounts(AccountStore(tmp_path))
    local.store.add("carol", "viewer", PASSWORD)
    with pytest.raises(NoSuchUserError):
        local.change_password("nobody", PASSWORD, NEW)
    assert local.can_change_password is True
    assert local.change_password("carol", PASSWORD, NEW).username == "carol"


def test_renew_ends_every_session_of_the_user_and_keeps_the_start_of_the_caller(tmp_path):
    clock = Clock()
    store = SessionStore(1800, 3600, clock=clock)
    accounts_store = AccountStore(tmp_path)
    bob = accounts_store.add("bob", "viewer", PASSWORD)
    carol = accounts_store.add("carol", "viewer", PASSWORD)
    _, other = store.create(bob, "10.0.0.1")
    value, mine = store.create(bob, "10.0.0.2")
    kept, _ = store.create(carol, "10.0.0.3")
    clock.advance(100)
    changed = accounts_store.reset_password("bob", NEW)
    new_value, renewed = store.renew(mine, changed)
    assert store.lookup(value, accounts_store) is None and new_value != value and store.count() == 2
    assert store.lookup(new_value, accounts_store) is renewed and renewed.created == mine.created
    assert renewed.csrf != mine.csrf and renewed.address == "10.0.0.2" and renewed.key != other.key
    assert store.lookup(kept, accounts_store) is not None
