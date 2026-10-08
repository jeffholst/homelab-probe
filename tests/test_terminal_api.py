"""Direct-request terminal security, report equivalence and worker lifetime tests (#273)."""

import asyncio
import json
import subprocess
import sys
import threading
import types
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx2")

from fastapi import Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from server_support import CONFIG, auth_for, logged_in  # noqa: E402

from homelab_probe.client import UniFiAPIError  # noqa: E402
from homelab_probe.documents import Document  # noqa: E402
from homelab_probe.server import terminal_api as api  # noqa: E402
from homelab_probe.server import terminal_policy as policy  # noqa: E402
from homelab_probe.server.app import create_app  # noqa: E402
from homelab_probe.server.errors import ApiError  # noqa: E402
from homelab_probe.server.service import Built, ControllerService  # noqa: E402

AT = "/api/v1/terminal"


@pytest.fixture
def app(tmp_path):
    application = create_app(CONFIG, state_dir=tmp_path, hosts=["testserver"], auth=auth_for(tmp_path),
                             service=ControllerService(CONFIG, demo=True))
    yield application
    application.state.terminal.pool.shutdown(wait=True)


@pytest.fixture
def client(app):
    return logged_in(app)


def execute(client, argv, **extra):
    return client.post(AT + "/execute", json={"argv": argv, "requestId": "test-1", **extra})


def refuse_service(app):
    app.state.service = types.SimpleNamespace(build=lambda _: pytest.fail("unvalidated input dispatched"))


def test_capabilities_are_authenticated_static_and_explicit(app, client):
    refuse_service(app)
    assert TestClient(app).get(AT + "/capabilities").status_code == 401
    response = client.get(AT + "/capabilities")
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["version"] == 1
    assert body["limits"]["outputBytes"] == policy.OUTPUT_BYTES
    assert {c["name"] for c in body["commands"]} == set(policy.CAPABILITIES)
    query = next(c for c in body["commands"] if c["name"] == "query")
    assert "clients" in query["choices"]
    assert next(o for o in query["options"] if "--csv" in o["flags"])["status"] == "unavailable"
    assert execute(client, ["help"]).status_code == 200
    assert execute(client, ["client", "--help"]).status_code == 200
    assert execute(client, ["hlp", "--version"]).json()["operation"] == "version"


@pytest.mark.parametrize("argv", [
    ["--env-file", "/etc/passwd", "info"], ["--site", "http://evil/a", "wifi"],
    ["query", "--jso"], ["query", "@file"], ["info", ";", "id"], ["snapshot"],
    ["diagnose", "--notify"], ["diagnose", "--watch", "60"], ["wan", "--config", "../../x"],
    ["query", "clients", "--csv"], ["topology", "--format", "dot"],
    ["events", "--since", "3w"], ["client", "x", "--since", "3w"], ["wan", "--days", "3651"],
    ["events", "--category", "c" * 65], ["wifi", "--min-signal", "nan"],
    ["diagnose", "--only", "bogus"], ["query", "clients", "--ssid", " "],
    ["wifi", "--band", "7"], ["info", "--json"],
    ["query", "--search", "x" * 121],
])
def test_hostile_or_unsupported_arguments_never_dispatch(app, client, argv):
    refuse_service(app)
    response = execute(client, argv)
    assert response.status_code == 422, response.text
    assert response.json()["executionStatus"] == "did_not_run"
    assert response.json()["correlationId"] == response.headers["x-request-id"]
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("fields", [
    {"argv": []}, {"argv": "info"}, {"argv": [1]}, {"argv": [True]}, {"argv": [None]},
    {"argv": ["x" * 257]}, {"argv": ["x"] * 65}, {"argv": ["info\n"]}, {"argv": ["\x1binfo"]},
    {"argv": ["info\u202e"]}, {"argv": ["query", "--search", "a\tb"]}, {"requestId": 1},
    {"requestId": "../sensitive"}, {"requestId": "a\n"}, {"requestId": ""}, {"version": 2},
])
def test_strict_schema_does_not_echo_input(app, client, fields):
    refuse_service(app)
    response = execute(client, ["info"], **fields) if "argv" not in fields else client.post(
        AT + "/execute", json={"requestId": "test", **fields})
    assert response.status_code == 422
    assert "sensitive" not in response.text


@pytest.mark.parametrize("headers, status", [
    ({"Origin": "https://evil.example"}, 403), ({"X-CSRF-Token": "wrong"}, 403),
    ({"Content-Type": "text/plain"}, 415),
])
def test_auth_guards_precede_dispatch(app, client, headers, status):
    refuse_service(app)
    response = client.post(AT + "/execute", json={"argv": ["info"], "requestId": "test"}, headers=headers)
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"


def test_anonymous_expired_and_missing_csrf_never_dispatch(app, client):
    refuse_service(app)
    anonymous = TestClient(app)
    assert execute(anonymous, ["info"]).status_code == 403  # missing origin
    anonymous.headers["Origin"] = "http://testserver"
    assert execute(anonymous, ["info"]).status_code == 401
    del client.headers["X-CSRF-Token"]
    assert execute(client, ["info"]).status_code == 403
    app.state.auth.sessions._sessions.clear()
    assert execute(client, ["info"]).status_code == 401


@pytest.mark.parametrize("argv, path", [
    (["diagnose", "--no-events", "--json"], "diagnose?no_events=true"),
    (["diagnose", "--only", "wan", "--json"], "diagnose?only=wan"),
    (["audit", "--json"], "audit"), (["wan", "--json"], "wan"), (["wifi", "--json"], "wifi"),
    (["topology", "--clients", "--json"], "topology?clients=true"),
    (["firewall", "--all", "--json"], "firewall?all=true"), (["events", "--json"], "events"),
    (["events", "--summary", "--json"], "events/summary"),
    (["query", "clients", "--json"], "clients"), (["query", "devices", "--json"], "devices"),
    (["query", "reservations", "--offline", "--json"], "reservations?offline=true"),
    (["query", "networks", "--json"], "networks"), (["query", "wlans", "--json"], "wlans"),
    (["query", "ports", "--down", "--json"], "ports?down=true"),
    (["new-clients", "--json"], "new-clients"),
    (["client", "BB:00:00:00:00:01", "--no-events", "--json"], "clients/BB:00:00:00:00:01?events=false"),
])
def test_json_reports_match_existing_routes(client, argv, path):
    expected = client.get("/api/v1/unifi/sites/default/" + path)
    assert expected.status_code == 200, expected.text
    data = expected.json()
    for key in ("generated_at", "warnings", "read_cap_reached"):
        data.pop(key, None)
    if path == "events":
        data.pop("truncated", None)
    response = execute(client, argv)
    assert response.status_code == 200, response.text
    assert json.loads(response.json()["output"]) == data.get("items", data)
    assert response.json()["truncated"] is False


@pytest.mark.parametrize("argv", [
    ["info"], ["diagnose", "--no-events"], ["audit"], ["wan"], ["wifi", "--all"], ["topology"],
    ["firewall", "--zones"], ["events"], ["events", "--summary"], ["query"], ["new-clients"],
    ["client", "BB:00:00:00:00:01", "--no-events"],
])
def test_each_text_renderer_is_used(client, argv):
    response = execute(client, argv)
    assert response.status_code == 200, response.text
    assert response.json()["output"].strip()
    assert "\x1b" not in response.json()["output"]


@pytest.mark.parametrize("query", ["does-not-exist", "bb"])
def test_no_single_client_is_safe_bounded_conflict(client, query):
    response = execute(client, ["client", query])
    assert response.status_code == 409
    assert len(response.json()["candidates"]) <= 20


def test_large_and_chunked_bodies_are_capped_before_json(app, client):
    refuse_service(app)
    response = client.post(AT + "/execute", content=b" " * (policy.BODY_BYTES + 1),
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    response = client.post(AT + "/execute", content=iter([b" " * 8192] * 3),
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert client.post(AT + "/execute", content=b"{", headers={"Content-Type": "application/json"}).status_code == 422


def test_output_text_json_warnings_and_candidates_redact(app, client):
    csrf = client.headers["X-CSRF-Token"]
    cookie = next(iter(client.cookies.values()))
    secret = f"{CONFIG.api_key} {csrf} {cookie} {app.state.state_dir}"
    hostile = "evil\x1b]52;clipboard\x07\u202e" + secret
    document = Document("query", [{"Name": hostile, "password": "unknown-secret", "key": "value"}])
    app.state.service = types.SimpleNamespace(build=lambda _: Built(document, "now", [hostile] * 40))
    response = execute(client, ["query", "--json"])
    body = response.json()
    for value in (CONFIG.api_key, csrf, cookie, str(app.state.state_dir), "unknown-secret", "\x1b", "\u202e"):
        assert value not in body["output"]
        assert all(value not in w for w in body["warnings"])
    assert json.loads(body["output"])[0]["password"] == "[redacted]"
    assert len(body["warnings"]) == 32
    response = execute(client, ["query"])
    assert response.status_code == 200
    assert CONFIG.api_key not in response.json()["output"]


def test_utf8_output_cap_and_exact_boundary(monkeypatch):
    monkeypatch.setattr(policy, "OUTPUT_BYTES", 5)
    assert api.bounded(["ééé"]) == ("éé", True)
    assert api.bounded(["ab", "cde"]) == ("abcde", False)
    assert api.bounded([]) == ("", False)


def test_rate_limits_clock_and_global_concurrency():
    now = [10.0]
    state = api.TerminalState(clock=lambda: now[0])
    for _ in range(policy.USER_RATE):
        state.acquire("bob")
        state.release("bob")
    with pytest.raises(ApiError) as error:
        state.acquire("bob")
    assert error.value.headers["Retry-After"] == "60"
    now[0] = 71
    for user in ("a", "b", "c", "d"):
        state.acquire(user)
    with pytest.raises(ApiError):
        state.acquire("e")
    with pytest.raises(ApiError):
        state.acquire("a")
    for user in ("a", "b", "c", "d"):
        state.release(user)
    state.acquire("bob")
    state.release("bob")
    now[0] = 120
    state.acquire("bob")
    state.release("bob")
    now[0] = 132
    state.acquire("bob")  # retains recent entries while pruning the oldest one
    state.release("bob")
    state.pool.shutdown()


def test_timeout_retains_slot_until_worker_finishes(app, client):
    started, finish = threading.Event(), threading.Event()

    def slow(_):
        started.set()
        assert finish.wait(5)
        return Built(Document("info", {"application": {}, "sites": []}), "now", [])

    app.state.config = replace(CONFIG, timeout=0.02)
    app.state.service = types.SimpleNamespace(build=slow)
    try:
        response = execute(client, ["info"])
        assert started.is_set()
        assert response.status_code == 504
        assert response.json()["executionStatus"] == "outcome_unknown"
        assert app.state.terminal.active == {"bob"}
        assert execute(client, ["version"]).status_code == 429
    finally:
        finish.set()
        app.state.terminal.pool.shutdown(wait=True)
    assert not app.state.terminal.active


def test_global_slots_and_rate_limit_are_enforced_by_http(app, client):
    finish = threading.Event()

    def slow(_):
        assert finish.wait(10)
        return Built(Document("info", {"application": {}, "sites": []}), "now", [])

    app.state.config = replace(CONFIG, timeout=0.02)
    app.state.service = types.SimpleNamespace(build=slow)
    try:
        for name in ("bob", "alice", "carol", "david"):
            if name not in ("bob", "alice"):
                app.state.auth.accounts.store.add(name, "viewer", "correct horse battery")
            assert execute(logged_in(app, name), ["info"]).status_code == 504
        app.state.auth.accounts.store.add("ellen", "viewer", "correct horse battery")
        response = execute(logged_in(app, "ellen"), ["version"])
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "1"
        assert len(app.state.terminal.active) == 4
    finally:
        finish.set()
        app.state.terminal.pool.shutdown(wait=True)
    assert not app.state.terminal.active


def test_http_per_user_rate_is_not_a_per_session_limit(app, client):
    other = logged_in(app)
    for number in range(policy.USER_RATE):
        assert execute(client if number % 2 else other, ["version"]).status_code == 200
    response = execute(other, ["version"])
    assert response.status_code == 429
    assert 1 <= int(response.headers["Retry-After"]) <= 60


def test_streamed_body_is_counted_across_asgi_chunks_before_parsing(app):
    chunks = iter([b"x" * 8000, b"y" * 8000, b"z" * 1000])

    async def receive():
        return {"type": "http.request", "body": next(chunks), "more_body": True}

    request = Request({"type": "http", "headers": [(b"content-type", b"application/json")]}, receive)
    with pytest.raises(ApiError) as error:
        asyncio.run(api.read_body(request))
    assert error.value.status == 413


def test_static_help_survives_grammar_drift(app, client, monkeypatch):
    refuse_service(app)
    monkeypatch.setattr(policy, "compatibility_errors", lambda _: ("changed",))
    assert execute(client, ["help", "query"]).status_code == 200
    assert execute(client, ["version"]).status_code == 200
    assert client.get(AT + "/capabilities").status_code == 503
    assert execute(client, ["query"]).status_code == 422


def test_terminal_contracts_match_models_and_validate_responses(client):
    from jsonschema import Draft202012Validator

    from tools.generate_terminal_contracts import ROOT, schemas

    for name, text in schemas().items():
        assert (ROOT / "docs" / "terminal" / name).read_text() == text
        Draft202012Validator.check_schema(json.loads(text))
    generated = schemas()
    Draft202012Validator(json.loads(generated["terminal-capabilities.v1.schema.json"])).validate(
        client.get(AT + "/capabilities").json())
    Draft202012Validator(json.loads(generated["terminal-execute-result.v1.schema.json"])).validate(
        execute(client, ["version"]).json())


def test_contract_generator_runs_as_a_command(tmp_path):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "terminal"
    result = subprocess.run([sys.executable, "-m", "tools.generate_terminal_contracts", "--output", str(output)],
                            cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert len(list(output.glob("*.json"))) == 4


def test_shell_characters_remain_literal_search(client):
    for literal in ("; id", "$(id)", "`id`", "x | id", "> /etc/passwd"):
        response = execute(client, ["query", "--search", literal])
        assert response.status_code == 200


def test_worker_result_and_candidates_are_bounded(app, client, monkeypatch):
    monkeypatch.setattr(policy, "OUTPUT_BYTES", 101)
    app.state.service = types.SimpleNamespace(build=lambda _: Built(Document("query", [
        {"Name": "é" * 200}]), "now", []))
    body = execute(client, ["query", "--json"]).json()
    assert body["truncated"] is True and len(body["output"].encode()) <= 101
    matches = [{"name": "n" * 10000, "mac": "BB:00:00:00:00:01", "ip": "192.0.2.1", "online": True}] * 40
    app.state.service = types.SimpleNamespace(build=lambda _: Built(Document("client", {}, meta={"matches": matches}),
                                                                   "now", []))
    response = execute(client, ["client", "x"])
    assert response.status_code == 409
    assert len(response.json()["candidates"]) == 20
    assert len(response.json()["candidates"][0]["Name"]) == 256


def test_endpoint_checks_json_type_without_origin_middleware(app):
    request = Request({"type": "http", "headers": [(b"content-type", b"text/plain")]})
    with pytest.raises(ApiError) as error:
        asyncio.run(api.read_body(request))
    assert error.value.status == 415


@pytest.mark.parametrize("error,status,code", [
    (UniFiAPIError("secret upstream", kind="timeout"), 504, "controller_timeout"),
    (UniFiAPIError("secret upstream", kind="connection"), 502, "controller_unreachable"),
    (RuntimeError("secret upstream"), 500, "terminal_internal"),
])
def test_errors_are_fixed_and_release_slots(app, client, error, status, code):
    def fail(_):
        raise error

    app.state.service = types.SimpleNamespace(build=fail)
    response = execute(client, ["info"])
    assert response.status_code == status
    assert response.json()["error"] == code
    assert "secret upstream" not in response.text
    assert not app.state.terminal.active


def test_audit_records_only_operation_outcome_correlation_and_duration(app, client, tmp_path):
    assert execute(client, ["query", "--search", "private-search"]).status_code == 200
    for _ in range(5):
        execute(client, ["query", "--jso"])
    records = [json.loads(line) for line in (tmp_path / "audit.log").read_text().splitlines()]
    terminal = [r for r in records if r["event"].startswith("terminal.")]
    assert len(terminal) == 2
    text = json.dumps(terminal)
    assert "private-search" not in text and "--jso" not in text
    assert "correlation_id" in text and "duration_ms" in text and "completed" in text


def test_audit_failure_does_not_block_readonly_operation(app, client, monkeypatch):
    monkeypatch.setattr(app.state.auth.audit, "write", lambda *a, **k: (_ for _ in ()).throw(OSError("secret")))
    assert execute(client, ["version"]).status_code == 200


def test_capabilities_drift_fails_closed(monkeypatch):
    monkeypatch.setattr(policy, "compatibility_errors", lambda _: ("changed",))
    with pytest.raises(ApiError, match="terminal_unreviewed"):
        api.capabilities("viewer")


def test_role_filter_and_enforcement(app, client, monkeypatch):
    changed = dict(policy.CAPABILITIES)
    changed["info"] = replace(changed["info"], minimum_role="admin")
    monkeypatch.setattr(policy, "CAPABILITIES", changed)
    assert "info" not in {c["name"] for c in api.capabilities("viewer")["commands"]}
    refuse_service(app)
    assert execute(client, ["info"]).status_code == 403


def test_submission_failure_releases_slot(app, client, monkeypatch):
    monkeypatch.setattr(app.state.terminal.pool, "submit", lambda *a: (_ for _ in ()).throw(RuntimeError("private")))
    assert execute(client, ["version"]).status_code == 500
    assert not app.state.terminal.active


def test_http_task_cancellation_does_not_release_worker_slot(app):
    started, finish = threading.Event(), threading.Event()
    _, session = app.state.auth.sessions.create(app.state.auth.accounts.store.get("bob"), "test")
    scope = {"type": "http", "method": "POST", "path": AT + "/execute", "headers": [
        (b"content-type", b"application/json")], "app": app, "state": {"session": session}}
    payload = json.dumps({"argv": ["info"], "requestId": "cancel-test"}).encode()

    def slow(_):
        started.set()
        assert finish.wait(5)
        return Built(Document("info", {"application": {}, "sites": []}), "now", [])

    app.state.service = types.SimpleNamespace(build=slow)

    async def run():
        async def receive():
            return {"type": "http.request", "body": payload, "more_body": False}

        endpoint = next(r.endpoint for r in api.router().routes if r.name == "terminal_execute")
        task = asyncio.create_task(endpoint(Request(scope, receive)))
        while not started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert app.state.terminal.active == {"bob"}

    try:
        asyncio.run(run())
    finally:
        finish.set()
        app.state.terminal.pool.shutdown(wait=True)
    assert not app.state.terminal.active
