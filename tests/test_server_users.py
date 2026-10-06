"""User management over the API (issue #186): /api/v1/users."""

import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("tomlkit")

from fastapi.testclient import TestClient  # noqa: E402
from server_support import CONFIG, PASSWORD, auth_for, logged_in, login  # noqa: E402

from homelab_probe.accounts import AccountError, AccountStore, PolicyError  # noqa: E402
from homelab_probe.demo.session import DemoSession  # noqa: E402
from homelab_probe.server import users_api  # noqa: E402
from homelab_probe.server.app import create_app  # noqa: E402
from homelab_probe.server.service import ControllerService  # noqa: E402

URL = "/api/v1/users"
NEW_PASSWORD = "a brand new password"


def make_app(tmp_path, **kwargs):
    return create_app(CONFIG, state_dir=tmp_path, hosts=["testserver"], auth=auth_for(tmp_path),
                      service=ControllerService(CONFIG, session=DemoSession()), **kwargs)


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def admin(app):
    return logged_in(app, "alice")


@pytest.fixture
def viewer(app):
    return logged_in(app, "bob")


def store(tmp_path):
    return AccountStore(tmp_path)


def audit_lines(tmp_path):
    return [json.loads(line) for line in (tmp_path / "audit.log").read_text().splitlines()]


def add(client, username="carol", password=NEW_PASSWORD, **body):
    return client.post(URL, json={"username": username, "password": password, **body})


def patch(client, username, **body):
    return client.patch(f"{URL}/{username}", json=body)


def reset(client, username, password=NEW_PASSWORD):
    return client.post(f"{URL}/{username}/password", json={"password": password})


# -- who may ----------------------------------------------------------------------------------------------------

def test_every_user_route_is_for_an_administrator_with_the_csrf_token(app, viewer, tmp_path):
    anonymous = TestClient(app)
    no_token = logged_in(app, "alice")
    del no_token.headers["X-CSRF-Token"]
    calls = [("GET", URL, None), ("POST", URL, {"username": "dave", "password": NEW_PASSWORD}),
             ("PATCH", f"{URL}/bob", {"role": "admin"}), ("POST", f"{URL}/bob/password", {"password": NEW_PASSWORD})]
    for method, path, body in calls:
        assert anonymous.request(method, path, json=body, headers={"Origin": "http://testserver"}).status_code == 401
        assert viewer.request(method, path, json=body).status_code == 403, (method, path)
        if method != "GET":
            assert no_token.request(method, path, json=body).json()["error"] == "csrf_token", (method, path)
            assert TestClient(app).request(method, path, json=body).status_code == 403                # no Origin
    assert [u.username for u in store(tmp_path).users()] == ["alice", "bob"] and store(tmp_path).get("bob").role == "viewer"


def test_a_read_only_server_refuses_every_change_and_still_lists(tmp_path):
    admin = logged_in(make_app(tmp_path, read_only=True), "alice")
    assert admin.get(URL).status_code == 200
    for response in (add(admin), patch(admin, "bob", role="admin"), reset(admin, "bob")):
        assert response.status_code == 403 and response.json()["error"] == "read_only"
    assert store(tmp_path).get("bob").role == "viewer" and store(tmp_path).get("carol") is None


# -- listing ----------------------------------------------------------------------------------------------------

def test_the_list_has_every_user_and_never_a_password_or_a_hash(admin):
    response = admin.get(URL)
    body = response.json()
    assert body["total"] == 2 and [u["username"] for u in body["items"]] == ["alice", "bob"]
    assert set(body["items"][0]) == {"username", "role", "disabled", "created_at", "last_login"}
    assert body["items"][0]["last_login"] is not None and "scrypt" not in response.text
    assert "password" not in response.text


def test_an_accounts_file_that_cannot_be_used_is_a_500_with_a_fixed_message(admin, tmp_path, monkeypatch):
    def broken(request):
        raise AccountError(f"{tmp_path}/users.json is not valid JSON")

    monkeypatch.setattr(users_api, "all_users", broken)
    response = admin.get(URL)
    assert response.status_code == 500 and response.json()["error"] == "accounts_unreadable"
    assert str(tmp_path) not in response.text


def test_a_refusal_that_is_not_one_of_the_kinds_is_a_500_that_does_not_repeat_the_text(tmp_path):
    error = users_api.failure(AccountError(f"{tmp_path}/users.json is not valid JSON"))
    assert (error.status, error.code) == (500, "accounts_unreadable") and str(tmp_path) not in error.message


# -- adding -----------------------------------------------------------------------------------------------------

def test_a_user_is_added_and_can_log_in(app, admin):
    response = add(admin, "Carol", role="admin")
    assert response.status_code == 201
    assert response.json()["username"] == "carol" and response.json()["role"] == "admin"
    assert response.json()["disabled"] is False and response.json()["last_login"] is None
    assert login(TestClient(app), "carol", NEW_PASSWORD)
    assert "password" not in response.text


def test_the_role_defaults_to_viewer(admin):
    assert add(admin).json()["role"] == "viewer"


@pytest.mark.parametrize("body, status, error", [
    ({"username": "carol", "password": "short"}, 422, "invalid_user"),
    ({"username": "a", "password": NEW_PASSWORD}, 422, "invalid_user"),
    ({"username": "Bob", "password": NEW_PASSWORD}, 409, "user_exists"),
    ({"username": "carol", "password": NEW_PASSWORD, "role": "root"}, 422, "invalid_parameter"),
    ({"username": "carol", "password": NEW_PASSWORD, "extra": 1}, 422, "invalid_parameter"),
    ({"username": "carol"}, 422, "invalid_parameter"), ({"username": "c" * 300, "password": NEW_PASSWORD}, 422, None),
])
def test_a_user_that_cannot_be_added_is_refused_with_the_reason(admin, tmp_path, body, status, error):
    response = admin.post(URL, json=body)
    assert response.status_code == status and (error is None or response.json()["error"] == error)
    assert NEW_PASSWORD not in response.text and [u.username for u in store(tmp_path).users()] == ["alice", "bob"]


@pytest.mark.parametrize("body, message", [
    ({"password": "short"}, "The password must have at least 12 characters."),
    ({"password": "x" * 2000}, "The password must have at most 1024 characters."),
    ({"username": "a"}, "A user name has 3 to 64 characters"),
])
def test_a_broken_rule_is_worded_by_the_api_by_the_kind_of_rule(admin, body, message):
    assert message in add(admin, **body).json()["message"]


def test_a_policy_message_of_the_accounts_module_never_reaches_a_response(admin, monkeypatch):
    from homelab_probe.server import errors

    def leaky(self, *args, **kwargs):
        raise PolicyError("the password hunter2 is on a list", "unknown_rule")

    monkeypatch.setattr(AccountStore, "add", leaky)
    response = add(admin)
    assert response.status_code == 422 and "hunter2" not in response.text
    assert response.json()["message"] == "That value is not acceptable."
    assert errors.policy_message(PolicyError("x", "role")) == "The role must be viewer or admin."


def test_adding_is_audited_without_the_password(admin, tmp_path):
    add(admin, "carol", role="admin")
    entry = audit_lines(tmp_path)[-1]
    assert (entry["event"], entry["actor"], entry["user"], entry["role"]) == ("user.added", "alice", "carol", "admin")
    assert "address" in entry and NEW_PASSWORD not in (tmp_path / "audit.log").read_text()


def test_an_audit_log_that_cannot_be_written_stops_the_change(app, admin, tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(app.state.auth.audit, "write", broken)
    response = add(admin)
    assert response.status_code == 500 and response.json()["error"] == "audit_unavailable"
    assert store(tmp_path).get("carol") is None and "disk full" not in response.text


# -- changing the role or disabling ---------------------------------------------------------------------------

def test_a_role_change_ends_the_sessions_of_that_user_who_logs_in_again_with_the_new_role(app, admin, viewer):
    assert viewer.get(URL).status_code == 403
    assert patch(admin, "bob", role="admin").json()["role"] == "admin"
    assert viewer.get(URL).status_code == 401                         # the old session is over: it was a viewer's
    boss = logged_in(app, "bob")
    assert boss.get(URL).status_code == 200
    assert patch(admin, "bob", role="viewer").json()["role"] == "viewer"
    assert boss.get(URL).status_code == 401                           # and an administrator's session ends on demotion
    assert logged_in(app, "bob").get(URL).status_code == 403


def test_a_disabled_user_is_out_at_once_and_can_come_back(app, admin, viewer):
    assert viewer.get("/api/v1/auth/me").status_code == 200
    assert patch(admin, "bob", disabled=True).json()["disabled"] is True
    assert viewer.get("/api/v1/auth/me").status_code == 401
    assert TestClient(app).post("/api/v1/auth/login", json={"username": "bob", "password": PASSWORD},
                                headers={"Origin": "http://testserver"}).status_code == 401
    assert patch(admin, "bob", disabled=False).json()["disabled"] is False
    assert login(TestClient(app), "bob")


def test_role_and_disabled_are_one_change_and_one_audit_entry(admin, tmp_path):
    before = len(audit_lines(tmp_path))
    body = patch(admin, "bob", role="admin", disabled=True).json()
    assert (body["role"], body["disabled"]) == ("admin", True)
    entries = audit_lines(tmp_path)[before:]
    assert [e["event"] for e in entries] == ["user.updated"]
    assert (entries[0]["user"], entries[0]["role"], entries[0]["disabled"]) == ("bob", "admin", True)
    patch(admin, "bob", disabled=False)
    assert audit_lines(tmp_path)[-1]["event"] == "user.enabled"
    patch(admin, "bob", role="viewer")
    assert audit_lines(tmp_path)[-1]["event"] == "user.role_changed"


def test_a_failed_audit_write_leaves_no_record_of_half_a_change(app, admin, tmp_path, monkeypatch):
    attempts = []

    def failing(event, actor, **fields):
        attempts.append(event)
        raise OSError("disk full")

    monkeypatch.setattr(app.state.auth.audit, "write", failing)
    response = patch(admin, "bob", role="admin", disabled=True)
    assert response.status_code == 500 and response.json()["error"] == "audit_unavailable"
    assert store(tmp_path).get("bob").role == "viewer" and not store(tmp_path).get("bob").disabled
    assert attempts == ["user.updated"]                               # one entry for the whole change, so none is left over


def test_the_last_enabled_administrator_cannot_be_demoted_or_disabled(admin, tmp_path):
    for body in ({"role": "viewer"}, {"disabled": True}, {"role": "viewer", "disabled": True}):
        response = patch(admin, "alice", **body)
        assert response.status_code == 409 and response.json()["error"] == "last_administrator", body
    assert store(tmp_path).get("alice").role == "admin" and not store(tmp_path).get("alice").disabled


def test_with_a_second_administrator_one_can_go_and_then_the_other_is_the_last(app, admin):
    patch(admin, "bob", role="admin")
    assert patch(admin, "alice", disabled=True).status_code == 200                # alice disables herself
    assert admin.get(URL).status_code == 401                                       # and is out
    boss = logged_in(app, "bob")
    assert patch(boss, "bob", role="viewer").json()["error"] == "last_administrator"


def test_a_change_that_would_break_the_rule_changes_nothing_at_all(admin, tmp_path):
    before = store(tmp_path).get("alice")
    patch(admin, "alice", role="viewer", disabled=False)
    assert store(tmp_path).get("alice") == before


def test_a_change_that_changes_nothing_is_ok_and_audits_nothing(admin, tmp_path):
    count = len(audit_lines(tmp_path)) if (tmp_path / "audit.log").exists() else 0
    assert patch(admin, "bob", role="viewer", disabled=False).status_code == 200
    assert (len(audit_lines(tmp_path)) if (tmp_path / "audit.log").exists() else 0) == count


@pytest.mark.parametrize("body", [{}, {"role": "root"}, {"disabled": "yes"}, {"nonsense": 1},
                                  {"role": None, "disabled": None}])
def test_a_change_needs_a_valid_role_and_or_disabled(admin, body):
    assert patch(admin, "bob", **body).status_code == 422


def test_an_unknown_user_is_a_404_and_a_bad_name_never_reaches_the_store(admin):
    assert patch(admin, "nobody", role="admin").json()["error"] == "user_not_found"
    for name in ("a%20b", "-x", "x" * 80, "a%2Fb"):
        assert admin.patch(f"{URL}/{name}", json={"role": "admin"}).status_code in (404, 422)


def test_the_name_in_the_path_is_normalized_like_everywhere(admin):
    assert patch(admin, "BOB", disabled=True).json()["username"] == "bob"


# -- resetting a password --------------------------------------------------------------------------------------

def test_a_reset_password_works_and_the_old_one_does_not(app, admin):
    assert reset(admin, "bob").status_code == 200
    wrong = TestClient(app).post("/api/v1/auth/login", json={"username": "bob", "password": PASSWORD},
                                 headers={"Origin": "http://testserver"})
    assert wrong.status_code == 401 and login(TestClient(app), "bob", NEW_PASSWORD)
    assert NEW_PASSWORD not in reset(admin, "bob", "another new password").text


def test_a_reset_ends_the_sessions_of_that_user_and_of_the_administrator_resetting_their_own(admin, viewer):
    assert viewer.get("/api/v1/auth/me").status_code == 200
    reset(admin, "bob")
    assert viewer.get("/api/v1/auth/me").status_code == 401
    assert admin.get(URL).status_code == 200                         # the administrator's own session is untouched
    reset(admin, "alice")
    assert admin.get(URL).status_code == 401


@pytest.mark.parametrize("password", ["short", "x" * 2000])
def test_a_password_that_breaks_the_rules_is_refused_and_not_echoed(admin, tmp_path, password):
    before = store(tmp_path).get("bob").password_hash
    response = reset(admin, "bob", password)
    assert response.status_code == 422 and password not in response.text
    assert store(tmp_path).get("bob").password_hash == before


def test_a_reset_of_an_unknown_user_is_a_404(admin):
    assert reset(admin, "nobody").json()["error"] == "user_not_found"


def test_a_reset_is_audited_without_the_password(admin, tmp_path):
    reset(admin, "bob")
    entry = audit_lines(tmp_path)[-1]
    assert (entry["event"], entry["actor"], entry["user"]) == ("user.password_reset", "alice", "bob")
    assert NEW_PASSWORD not in (tmp_path / "audit.log").read_text()


def test_a_password_never_reaches_the_log(admin, caplog):
    caplog.set_level("DEBUG")
    add(admin, "dave", password="never-in-a-log-0123")
    reset(admin, "bob", "never-in-a-log-4567")
    assert "never-in-a-log" not in " ".join(r.getMessage() for r in caplog.records)


def test_a_store_that_fails_while_changing_is_a_500_with_a_fixed_message(admin, tmp_path, monkeypatch):
    def broken(self, *args, **kwargs):
        raise AccountError(f"{tmp_path}/users.json is damaged")

    monkeypatch.setattr(AccountStore, "update", broken)
    response = patch(admin, "bob", role="admin")
    assert response.status_code == 500 and response.json()["error"] == "accounts_unreadable"
    assert str(tmp_path) not in response.text


def test_the_openapi_wording_is_that_of_each_route(app):
    spec = app.openapi()["paths"]
    users = spec["/api/v1/users/{username}"]["patch"]["responses"]
    snapshots = spec["/api/v1/unifi/sites/{site}/snapshots"]["get"]["responses"]
    assert users["404"]["description"] == "No such user" and snapshots["404"]["description"] == "No such site or snapshot"
    assert "snapshot" not in users["404"]["description"] and "user" not in snapshots["404"]["description"]
    assert "accounts" in users["500"]["description"] and "snapshots" in snapshots["500"]["description"]


def test_every_error_is_in_the_openapi_document(app):
    spec = app.openapi()["paths"]
    for path, method, codes in ((URL, "get", {"401", "403", "500"}), (URL, "post", {"409", "422"}),
                                (URL + "/{username}", "patch", {"404", "409", "422"}),
                                (URL + "/{username}/password", "post", {"404", "422"})):
        assert codes <= set(spec[path][method]["responses"]), (path, method)
