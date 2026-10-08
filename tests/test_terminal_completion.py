"""Completion is static metadata, not authority to run a suggested command (#274)."""

import asyncio
import builtins
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx2")

from fastapi import Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from server_support import logged_in  # noqa: E402
from test_terminal_api import app as app  # noqa: E402
from test_terminal_api import client as client  # noqa: E402
from test_terminal_api import execute, refuse_service  # noqa: E402

from homelab_probe import cli, commands, config, logs  # noqa: E402
from homelab_probe.completion import spec  # noqa: E402
from homelab_probe.server import terminal_api as api  # noqa: E402
from homelab_probe.server import terminal_completion as completion  # noqa: E402
from homelab_probe.server import terminal_policy as policy  # noqa: E402

AT = "/api/v1/terminal/complete"


def complete(client, argv, token_index=None, cursor=None, **extra):
    index = len(argv) - 1 if token_index is None else token_index
    offset = len(argv[index]) if cursor is None else cursor
    return client.post(AT, json={"argv": argv, "tokenIndex": index, "cursor": offset,
                                 "requestId": "completion-1", **extra})


def labels(client, argv, **kwargs):
    response = complete(client, argv, **kwargs)
    assert response.status_code == 200, response.text
    return {c["label"] for c in response.json()["candidates"]}


@pytest.mark.parametrize("argv,expected", [
    (["q"], {"query"}), (["hlp", "q"], {"query"}), (["query", "cl"], {"clients"}),
    (["query", "--j"], {"--json"}), (["query", "--c"], set()),
    (["query", "clients", "--se"], {"--search"}),
    (["wifi", "--band", ""], {"2.4", "5", "6"}), (["wifi", "--band=5"], {"--band=5"}),
    (["topology", "--format", ""], {"text"}),
    (["diagnose", "--only", "wan,w"], {"wan,wifi"}),
    (["diagnose", "--only=wan,w"], {"--only=wan,wifi"}),
    (["diagnose", "--only", "wan,wan"], set()),
    (["events", "--severity", ""], {"high", "medium", "low"}),
    (["--site", "default", "q"], {"query"}),
    (["help", "q"], {"query"}), (["--help", "q"], {"query"}),
    (["help", "query", ""], set()), (["version", ""], set()),
    (["query", "--help", ""], set()), (["--version", ""], set()),
    (["--site", ""], set()), (["query", "--ssid", ""], set()),
    (["client", ""], {"--help", "-h", "--json", "--no-events", "--no-emoji", "--since"}),
    (["diff", ""], set()), (["completion", ""], set()), (["snapshot", "/"], set()),
    (["--env-file", ""], set()), (["query", "--config", ""], set()),
    (["query", "--json", "--j"], set()), (["query", "-s", "x", "--se"], set()),
    (["query", "--json=true", ""], set()), (["query", "--jso", ""], set()),
    (["wifi", "--band=7", ""], set()), (["wifi", "--band", "7", ""], set()),
    (["wifi", "--band", "5", "--band="], set()), (["query", "clients", "clients", ""], set()),
    (["query", "not-a-kind", ""], set()), (["query", "--search=x", "--se"], set()),
    (["query", "--unknown="], set()), (["query", "--json="], set()),
    (["diagnose", "--only", "private,w"], set()),
    (["events", "--severity", "low", "--severity="],
     {"--severity=high", "--severity=medium", "--severity=low"}),
])
def test_static_contexts(client, argv, expected):
    assert labels(client, argv) == expected


def test_mid_token_cursor_replaces_whole_token_and_ignores_later_tokens(client):
    response = complete(client, ["query", "clJUNK", "--csv"], token_index=1, cursor=2)
    assert response.status_code == 200
    assert response.json() == {"correlationId": response.headers["x-request-id"], "requestId": "completion-1",
                              "tokenIndex": 1, "candidates": [
                                  {"label": "clients", "description": "List and filter devices, clients, reservations, switch ports, networks and Wi-Fi networks", "kind": "choice"}],
                              "truncated": False}


def test_commands_and_options_match_reviewed_registry(client):
    choices = labels(client, [""])
    expected = {name for name, cap in policy.CAPABILITIES.items() if cap.operation is not None}
    expected |= {"help", "version"} | {flag for flag, p in policy.GLOBAL_OPTIONS.items() if p.status != "unavailable"}
    assert choices == expected
    for command in spec(cli.build_parser(strict=True)).commands:
        cap = policy.CAPABILITIES[command.name]
        result = labels(client, [command.name, ""])
        if cap.operation is None:
            assert result == set()
        else:
            assert result == set(cap.choices) | {f for f, p in cap.options.items() if p.status != "unavailable"}


@pytest.mark.parametrize("fields", [
    {"argv": []}, {"argv": "info"}, {"argv": [1]}, {"argv": [None]}, {"argv": [True]},
    {"argv": ["x" * 257]}, {"argv": [""] * 65}, {"argv": ["\x1bq"]}, {"argv": ["q\n"]},
    {"argv": ["q\u202e"]}, {"argv": ["q\t"]}, {"argv": ["q"], "tokenIndex": 1},
    {"tokenIndex": -1}, {"tokenIndex": 64}, {"tokenIndex": True}, {"tokenIndex": "0"},
    {"cursor": 2}, {"cursor": -1}, {"cursor": 257}, {"cursor": True}, {"cursor": "0"},
    {"cursor": 0.5}, {"requestId": "../private"}, {"extra": "private"},
])
def test_strict_completion_body(client, fields):
    response = client.post(AT, json={"argv": ["q"], "tokenIndex": 0, "cursor": 1,
                                   "requestId": "test", **fields})
    assert response.status_code == 422
    assert "private" not in response.text


def test_unicode_cursor_counts_codepoints_and_empty_active_token_is_valid(client):
    assert complete(client, ["\U0001f600"], cursor=1).status_code == 200
    assert complete(client, ["\U0001f600"], cursor=2).status_code == 422
    assert complete(client, [""]).status_code == 200


def test_security_guards(app, client):
    refuse_service(app)
    anonymous = TestClient(app, headers={"Origin": "http://testserver"})
    assert complete(anonymous, ["q"]).status_code == 401
    assert anonymous.post(AT, content=b" " * (policy.BODY_BYTES + 1),
                          headers={"Content-Type": "application/json"}).status_code == 401
    for header, value in (("Origin", "https://evil.example"), ("X-CSRF-Token", "wrong")):
        old = client.headers[header]
        client.headers[header] = value
        response = complete(client, ["q"])
        assert response.status_code == 403
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-request-id"]
        client.headers[header] = old
    del client.headers["X-CSRF-Token"]
    assert complete(client, ["q"]).status_code == 403
    app.state.auth.sessions._sessions.clear()
    assert complete(client, ["q"]).status_code == 401


def test_streamed_json_boundary_and_media_type(client):
    assert client.post(AT, content=b"{}", headers={"Content-Type": "text/plain"}).status_code == 415
    assert client.post(AT, content=b"{", headers={"Content-Type": "application/json"}).status_code == 422
    assert client.post(AT, content=iter([b" " * 8192] * 3),
                       headers={"Content-Type": "application/json"}).status_code == 413


def test_completion_cannot_execute_or_consult_configuration(app, client, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Completion attempted execution, configuration, filesystem or environment access")

    refuse_service(app)
    monkeypatch.setattr(api, "work", forbidden)
    monkeypatch.setattr(api, "cleaner", forbidden)
    monkeypatch.setattr(config, "build_config", forbidden)
    monkeypatch.setattr(cli, "main", forbidden)
    monkeypatch.setattr(app.state.terminal.pool, "submit", forbidden)
    monkeypatch.setattr(commands, "COMMANDS", tuple(replace(command, run=forbidden, run_local=forbidden)
                                                    for command in commands.COMMANDS))
    assert labels(client, ["q"]) == {"query"}
    # The auth guard legitimately reads users.json and may audit denials. The suggestion path does neither.
    with monkeypatch.context() as pure:
        class NoEnvironment(dict):
            __getitem__ = get = __iter__ = __len__ = forbidden

        for owner, name in ((builtins, "open"), (Path, "open"), (Path, "resolve"), (Path, "iterdir"),
                            (os, "open"), (os, "listdir"), (os, "scandir"), (os, "getenv")):
            pure.setattr(owner, name, forbidden)
        pure.setattr(os, "environ", NoEnvironment())
        assert completion.complete(["wifi", "--band", ""], 2, 0, "viewer")[0]


def test_endpoint_itself_reads_no_files_after_auth(app, client, monkeypatch):
    _, session = app.state.auth.sessions.create(app.state.auth.accounts.store.get("bob"), "test")
    endpoint = next(r.endpoint for r in api.router().routes if r.name == "terminal_complete")
    payload = json.dumps({"argv": ["q"], "tokenIndex": 0, "cursor": 1, "requestId": "pure"}).encode()

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    request = Request({"type": "http", "headers": [(b"content-type", b"application/json")],
                       "app": app, "state": {"session": session}}, receive)

    def forbidden(*args, **kwargs):
        pytest.fail("Completion endpoint read a file")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    assert asyncio.run(endpoint(request))["candidates"][0]["label"] == "query"


def test_roles_and_users_are_request_local_and_execute_revalidates(app, client, monkeypatch):
    administrator = logged_in(app, "alice")
    changed = dict(policy.CAPABILITIES)
    changed["query"] = replace(changed["query"], minimum_role="admin")
    monkeypatch.setattr(policy, "CAPABILITIES", changed)
    assert labels(administrator, ["q"]) == {"query"}
    assert labels(client, ["q"]) == set()
    assert labels(client, ["query", "--j"]) == set()
    assert labels(administrator, ["query", "cl"]) == {"clients"}
    assert "query" not in {c["name"] for c in client.get("/api/v1/terminal/capabilities").json()["commands"]}
    monkeypatch.setattr(policy, "CAPABILITIES", dict(policy.CAPABILITIES, query=replace(changed["query"], minimum_role="viewer")))
    assembled = [next(iter(labels(client, ["q"])))]
    assembled.append(next(iter(labels(client, assembled + ["cl"]))))
    assembled.append(next(iter(labels(client, assembled + ["--j"]))))
    assert execute(client, assembled).status_code == 200
    # Neither a previous suggestion nor its requestId is an authorization token.
    monkeypatch.setattr(policy, "CAPABILITIES", changed)
    refuse_service(app)
    assert execute(client, assembled).status_code == 403
    assert execute(client, [*assembled, "--csv"]).status_code == 422
    assert completion.complete(["q"], 0, 1, "unknown") == ([], False)


def test_same_account_role_change_invalidates_old_session(app, client, monkeypatch):
    changed = dict(policy.CAPABILITIES)
    changed["query"] = replace(changed["query"], minimum_role="admin")
    monkeypatch.setattr(policy, "CAPABILITIES", changed)
    app.state.auth.accounts.store.update("bob", role="admin", disabled=False)
    assert complete(client, ["q"]).status_code == 401
    client = logged_in(app)
    assert labels(client, ["q"]) == {"query"}
    app.state.auth.accounts.store.update("bob", role="viewer", disabled=False)
    assert complete(client, ["q"]).status_code == 401
    client = logged_in(app)
    assert labels(client, ["q"]) == set()


def test_completion_shares_execute_rate_and_concurrency_limits(app, client):
    for number in range(policy.USER_RATE):
        response = complete(client, ["q"]) if number % 2 else execute(client, ["version"])
        assert response.status_code == 200
    assert complete(logged_in(app), ["q"]).status_code == 429
    assert execute(client, ["version"]).status_code == 429
    app.state.terminal.rates.clear()
    app.state.terminal.acquire("bob")
    assert complete(client, ["q"]).status_code == 429
    app.state.terminal.release("bob")
    for name in ("u1", "u2", "u3", "u4"):
        app.state.terminal.acquire(name)
    response = complete(client, ["q"])
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"
    assert response.json()["retry_after"] == 1


def test_grammar_drift_refuses_completion_and_releases_slot(app, client, monkeypatch):
    monkeypatch.setattr(policy, "compatibility_errors", lambda _: ("changed",))
    assert complete(client, ["q"]).json()["error"] == "terminal_unreviewed"
    assert not app.state.terminal.active


def test_unexpected_completion_failure_is_fixed_and_releases_slot(app, client, monkeypatch):
    def fail(*args):
        raise RuntimeError("private upstream detail")

    monkeypatch.setattr(completion, "complete", fail)
    response = complete(client, ["q"])
    assert response.status_code == 500
    assert response.json() == {"error": "terminal_internal", "message": "The terminal request could not be completed.",
                               "correlationId": response.headers["x-request-id"]}
    assert not app.state.terminal.active


def test_candidate_bounds_and_plain_text(monkeypatch):
    grammar = spec(cli.build_parser(strict=True))
    query = next(c for c in grammar.commands if c.name == "query")
    hostile = replace(query, help="evil\x1b[31m\u202e\n" + "x" * 600)
    monkeypatch.setattr(completion, "GRAMMAR", replace(grammar, commands=(hostile,)))
    monkeypatch.setattr(policy, "compatibility_errors", lambda _: ())
    rows, truncated = completion.complete(["q"], 0, 1, "viewer")
    assert truncated is True
    assert len(rows[0]["description"]) <= policy.COMPLETION_DESCRIPTION_CHARS
    assert all(c not in rows[0]["description"] for c in ("\x1b", "\u202e", "\n"))
    monkeypatch.setattr(policy, "COMPLETION_COUNT", 1)
    rows, truncated = completion.complete([""], 0, 0, "viewer")
    assert len(rows) == 1 and truncated is True
    monkeypatch.setattr(policy, "TOKEN_CHARS", 1)
    assert completion.complete(["q"], 0, 1, "viewer") == ([], True)


def test_scrubbed_label_is_discarded_not_offered_as_a_different_argument(monkeypatch):
    monkeypatch.setattr(logs, "_secrets", ["query"])
    assert completion.complete(["q"], 0, 1, "viewer") == ([], True)


def test_completion_is_available_on_a_read_only_server(app, client):
    app.state.read_only = True
    refuse_service(app)
    assert labels(client, ["q"]) == {"query"}


def test_response_contracts_and_metadata(client):
    from generate_terminal_contracts import schemas
    from jsonschema import Draft202012Validator

    generated = schemas()
    request = {"argv": ["q"], "tokenIndex": 0, "cursor": 1, "requestId": "contract"}
    Draft202012Validator(json.loads(generated["terminal-complete-request.v1.schema.json"])).validate(request)
    response = client.post(AT, json=request)
    Draft202012Validator(json.loads(generated["terminal-complete-result.v1.schema.json"])).validate(response.json())
    assert response.headers["cache-control"] == "no-store"
    caps = client.get("/api/v1/terminal/capabilities").json()
    diagnose = next(c for c in caps["commands"] if c["name"] == "diagnose")
    only = next(o for o in diagnose["options"] if "--only" in o["flags"])
    assert only["repeatable"] is True and only["commaList"] is True and only["takesValue"] is True
    assert caps["limits"]["completionCandidates"] == policy.COMPLETION_COUNT


def test_frontend_handoff_fixture_matches_contracts_and_static_metadata(client):
    from generate_terminal_contracts import ROOT, schemas
    from jsonschema import Draft202012Validator

    fixture = json.loads((ROOT / "web/src/test/fixtures/terminal.v1.json").read_text())
    contracts = schemas()
    for field, contract in (("capabilities", "capabilities"), ("completionRequest", "complete-request"),
                            ("completionResponse", "complete-result"), ("executeRequest", "execute-request"),
                            ("executeResponse", "execute-result")):
        Draft202012Validator(json.loads(contracts[f"terminal-{contract}.v1.schema.json"])).validate(fixture[field])
    for error in fixture["errors"]:
        Draft202012Validator(json.loads(contracts["terminal-error.v1.schema.json"])).validate(error["body"])
    actual = client.get("/api/v1/terminal/capabilities").json()
    actual["commands"] = [dict(c, options=[o for o in c["options"] if "--json" in o["flags"]])
                          for c in actual["commands"] if c["name"] == "query"]
    actual["globalOptions"] = [o for o in actual["globalOptions"] if "--site" in o["flags"]]
    assert actual == fixture["capabilities"]
    response = client.post(AT, json=fixture["completionRequest"]).json()
    response["correlationId"] = fixture["completionResponse"]["correlationId"]
    assert response == fixture["completionResponse"]
