"""The report routes of the server: every document as a GET, validated against its schema (issue #183, PR 3)."""

import json
import os
import threading
import time
import types
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx2")

from conftest import FakeResponse  # noqa: E402
from contract import RecordingSession  # noqa: E402
from docs_support import ROOT  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from jsonschema import Draft202012Validator  # noqa: E402
from test_json_schemas import strict  # noqa: E402

from homelab_probe import documents  # noqa: E402
from homelab_probe.client import UniFiClient  # noqa: E402
from homelab_probe.config import Config  # noqa: E402
from homelab_probe.demo.session import DemoSession  # noqa: E402
from homelab_probe.server import apischema  # noqa: E402
from homelab_probe.server.app import create_app  # noqa: E402
from homelab_probe.server.errors import ApiError  # noqa: E402
from homelab_probe.server.routes import _client_found  # noqa: E402
from homelab_probe.server.service import ControllerService  # noqa: E402
from homelab_probe.snapshot import EventQuery  # noqa: E402

CONFIG = Config(controller_url="https://controller.example", api_key="the-api-key-0123456789", parallel=4)
SITE = "/api/v1/unifi/sites/default"
NOW_MS = 1_800_000_000_000
DESKTOP = "BB:00:00:00:00:01"


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    """The fixture's times are relative to the clock and rendered in local time: pin both."""
    monkeypatch.setattr("homelab_probe.demo.session.time", types.SimpleNamespace(time=lambda: NOW_MS / 1000))
    monkeypatch.setattr("homelab_probe.wan.time", types.SimpleNamespace(time=lambda: NOW_MS / 1000))
    monkeypatch.setattr("homelab_probe.snapshot.time", types.SimpleNamespace(time=lambda: NOW_MS / 1000))
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def session():
    return DemoSession()


@pytest.fixture
def app(session):
    return create_app(CONFIG, hosts=["testserver"], service=ControllerService(CONFIG, session=session))


@pytest.fixture
def client(app):
    return TestClient(app)


def plain(session):
    """A client that reads the same data without the service, to build the document the command line would."""
    client = UniFiClient("https://controller.example", "key")
    client.session = session
    return client


# path, schema name, extra declared keys, the document the route must return (built the way the command does)
CASES = {
    "diagnose": (f"{SITE}/diagnose", "diagnose", None, lambda c: documents.diagnose_document(c, "default", echo=False)),
    "diagnose-only": (f"{SITE}/diagnose?only=wan&only=wifi&show_ignored=true", "diagnose", None,
                      lambda c: documents.diagnose_document(c, "default", areas=["wan", "wifi"], show_ignored=True,
                                                            echo=False)),
    "diagnose-no-events": (f"{SITE}/diagnose?no_events=true&since=7d", "diagnose", None,
                           lambda c: documents.diagnose_document(
                               c, "default", areas=[a for a in __import__("homelab_probe.diagnose", fromlist=["x"])
                                                    .AREA_NAMES if a != "events"], since=7 * 86400, echo=False)),
    "audit": (f"{SITE}/audit", "audit", None, lambda c: documents.audit_document(c, "default", echo=False)),
    "firewall": (f"{SITE}/firewall?all=true&search=block", "firewall", None,
                 lambda c: documents.firewall_document(c, "default", True, "block", echo=False)),
    "topology": (f"{SITE}/topology?clients=true", "topology", None,
                 lambda c: documents.topology_document(c, "default", with_clients=True, echo=False)),
    "wifi": (f"{SITE}/wifi?band=2.4&ap=office&min_signal=-70", "wifi", None,
             lambda c: documents.wifi_document(c, "default", -70.0, "ng", "office", echo=False)),
    "wan": (f"{SITE}/wan?days=90", "wan", None, lambda c: documents.wan_document(c, "default", 90, echo=False)),
    "events": (f"{SITE}/events?since=6h&client=phone&limit=2", "events", apischema.EVENT_NOTES,
               lambda c: documents.events_document(c, "default", EventQuery(6 * 3600), "phone", limit=2, echo=False)),
    "events-summary": (f"{SITE}/events/summary?since=7d", "events-summary", None,
                       lambda c: documents.events_document(c, "default", EventQuery(7 * 86400), summary=True,
                                                           echo=False)),
    "clients": (f"{SITE}/clients?include_offline=true", "query-clients", None,
                lambda c: documents.query_document(c, "default", "clients", include_offline=True, echo=False)),
    "clients-ssid": (f"{SITE}/clients?ssid=home&search=a", "query-clients", None,
                     lambda c: documents.query_document(c, "default", "clients", "a", ssid="home", echo=False)),
    "devices": (f"{SITE}/devices", "query-devices", None,
                lambda c: documents.query_document(c, "default", "devices", echo=False)),
    "networks": (f"{SITE}/networks", "query-networks", None,
                 lambda c: documents.query_document(c, "default", "networks", echo=False)),
    "wlans": (f"{SITE}/wlans?search=guest", "query-wlans", None,
              lambda c: documents.query_document(c, "default", "wlans", "guest", echo=False)),
    "ports": (f"{SITE}/ports?down=true&switch=office", "query-ports", None,
              lambda c: documents.query_document(c, "default", "ports", switch="office", down=True, echo=False)),
    "reservations": (f"{SITE}/reservations", "query-reservations", None,
                     lambda c: documents.query_document(c, "default", "reservations", echo=False)),
    "reservations-offline": (f"{SITE}/reservations?offline=true", "query-reservations", None,
                             lambda c: documents.query_document(c, "default", "reservations", offline=True,
                                                                settings=documents.DiagnoseSettings(), echo=False)),
    "new-clients": (f"{SITE}/new-clients", "new-clients", None,
                    lambda c: documents.new_clients_document(c, "default", echo=False)),
    "client": (f"{SITE}/clients/{DESKTOP}", "client", None,
               lambda c: documents.client_document(c, "default", DESKTOP, echo=False)),
}


def validate(schema_name, extra, body):
    schema = apischema.response_schema(schema_name, extra)
    Draft202012Validator(schema).validate(body)
    Draft202012Validator(strict(schema)).validate(body)         # nothing the schema does not declare


@pytest.mark.parametrize("name", CASES)
def test_each_route_returns_its_document_with_when_it_was_read_and_validates_against_its_schema(
        client, session, name):
    path, schema_name, extra, make = CASES[name]
    response = client.get(path)
    assert response.status_code == 200, response.text
    body = response.json()
    validate(schema_name, extra, body)
    assert body["generated_at"].endswith("Z") and isinstance(body["warnings"], list)
    document = make(plain(session))
    meta = {"generated_at", "warnings", *(extra or {})}
    shown = {k: v for k, v in body.items() if k not in meta}
    assert shown == ({"items": document.data} if isinstance(document.data, list) else document.data), name


def test_the_sites_route_returns_the_controller_and_its_sites(client, session):
    body = client.get(f"{__import__('homelab_probe.server.routes', fromlist=['x']).UNIFI}/sites").json()
    from homelab_probe.server.routes import INFO_SCHEMA

    Draft202012Validator(INFO_SCHEMA).validate(body)
    assert body["application"] == session.fx["info"] and body["sites"][0]["ref"]


def test_a_list_document_is_wrapped_and_an_object_document_is_not(client):
    assert set(client.get(f"{SITE}/devices").json()) == {"items", "generated_at", "warnings"}
    assert {"version", "findings", "generated_at", "warnings"} <= set(client.get(f"{SITE}/diagnose").json())
    events = client.get(f"{SITE}/events?limit=1").json()
    assert events["truncated"] is True and events["read_cap_reached"] is False and len(events["items"]) == 1


def test_the_client_route_takes_a_mac_in_any_spelling(client):
    for spelling in ("bb:00:00:00:00:01", "BB-00-00-00-00-01", "bb0000.000001", "BB0000000001"):
        response = client.get(f"{SITE}/clients/{spelling}")
        assert response.status_code == 200 and response.json()["identity"]["mac"] == DESKTOP, spelling


def test_a_client_that_is_not_there_is_a_404_and_one_that_is_ambiguous_a_409_with_the_candidates():
    class Doc:
        def __init__(self, data, matches):
            self.data, self.meta = data, {"matches": matches}

    with pytest.raises(ApiError) as none:
        _client_found(Doc({}, []))
    assert (none.value.status, none.value.code) == (404, "client_not_found")
    matches = [{"mac": "bb:00:00:00:00:01", "name": "a", "ip": "10.0.0.1", "online": True},
               {"mac": "bb:00:00:00:00:02", "name": "b", "ip": "", "online": False}]
    with pytest.raises(ApiError) as several:
        _client_found(Doc({}, matches))
    assert several.value.status == 409 and [c["Status"] for c in several.value.extra["candidates"]] == [
        "Online", "Offline"]
    _client_found(Doc({"identity": {}}, matches[:1]))                    # one match: nothing to say


def test_an_unknown_client_over_http(client):
    response = client.get(f"{SITE}/clients/AA:AA:AA:AA:AA:AA")
    assert response.status_code == 404 and response.json()["error"] == "client_not_found"


# -- degraded reads and controller errors ----------------------------------------------------------------------

def test_a_degraded_read_is_a_200_with_warnings(client, session):
    real = session.get

    def get(url, params=None, verify=True, timeout=None):
        return FakeResponse(503, {}) if url.endswith("/stat/rogueap") else real(url, params, verify, timeout)

    session.get = get
    response = client.get(f"{SITE}/wifi")
    body = response.json()
    assert response.status_code == 200 and body["warnings"] and body["neighbors"]["available"] is False
    validate("wifi", None, body)


@pytest.mark.parametrize("status, http, code", [(401, 502, "controller_unauthorized"),
                                                 (403, 502, "controller_forbidden"),
                                                 (500, 502, "controller_error")])
def test_a_controller_error_is_a_fixed_sentence_and_never_its_text(session, status, http, code):
    session.status = status
    service = ControllerService(CONFIG, session=session)
    client = TestClient(create_app(CONFIG, hosts=["testserver"], service=service))
    response = client.get(f"{SITE}/wan")
    assert response.status_code == http and response.json()["error"] == code
    for secret in ("controller.example", "the-api-key", "forced"):
        assert secret not in response.text


@pytest.mark.parametrize("raised, http, code", [
    ("timeout", 504, "controller_timeout"), ("connection", 502, "controller_unreachable"),
    ("tls", 502, "controller_tls")])
def test_transport_failures_have_their_own_codes(session, raised, http, code):
    import requests

    errors = {"timeout": requests.exceptions.Timeout("secret.host timed out"),
              "connection": requests.exceptions.ConnectionError("secret.host refused"),
              "tls": requests.exceptions.SSLError("secret.host certificate")}

    def get(*args, **kwargs):
        raise errors[raised]

    session.get = get
    client = TestClient(create_app(CONFIG, hosts=["testserver"], service=ControllerService(CONFIG, session=session)))
    response = client.get(f"{SITE}/wan")
    assert response.status_code == http and response.json()["error"] == code and "secret" not in response.text


def test_an_unknown_site_is_a_404(client):
    response = client.get("/api/v1/unifi/sites/nowhere/wan")
    assert response.status_code == 404 and response.json()["error"] == "site_not_found"


def test_a_settings_file_that_cannot_be_used_is_a_500_that_does_not_quote_it(tmp_path, session):
    bad = tmp_path / "hlp.toml"
    bad.write_text("this is [not toml\n")
    app = create_app(CONFIG, bad, hosts=["testserver"], service=ControllerService(CONFIG, session=session))
    response = TestClient(app).get(f"{SITE}/diagnose")
    assert response.status_code == 500 and response.json()["error"] == "settings_invalid"
    assert "toml" not in response.text.lower() and str(tmp_path) not in response.text


# -- parameters ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("query", [
    "/diagnose?since=soon", "/diagnose?only=nothing", "/diagnose?only=wan&skip=wifi", "/diagnose?no_events=true&only=events",
    "/wifi?band=7", "/wifi?min_signal=5", "/wifi?min_signal=-200", "/wan?days=0", "/wan?days=99999", "/events?limit=-1",
    "/events?limit=20001", "/events?since=0h", "/events?severity=extreme", "/events?event=" + "x" * 121,
    "/clients?network=", "/clients?ssid=%20", "/clients?ap=", "/firewall?search=" + "y" * 121,
    "/clients/not-a-mac", "/clients/BB:00:00:00:00", "/diagnose?no_events=maybe",
])
def test_bad_parameters_are_422_and_read_nothing(client, session, query):
    response = client.get(SITE + query)
    assert response.status_code == 422, query
    assert session.calls == [] and session.posts == []


@pytest.mark.parametrize("query", ["/wifi?min_signal=5", "/firewall?search=" + "x" * 121,
                                  "/diagnose?no_events=maybe"])
def test_fastapi_parameter_validation_uses_the_documented_error_body(client, session, query):
    response = client.get(SITE + query)
    assert response.status_code == 422
    assert response.json() == {"error": "invalid_parameter", "message": "A parameter is not valid."}
    assert session.calls == [] and session.posts == []


def test_a_site_name_the_cli_would_refuse_is_422(client):
    assert client.get("/api/v1/unifi/sites/a%3Fb/wan").status_code == 422
    assert client.get("/api/v1/unifi/sites/" + "s" * 121 + "/wan").status_code == 422


# -- schemas, OpenAPI, the cache, safety ------------------------------------------------------------------------

def test_the_schemas_are_listed_and_served_by_name_and_nothing_else_is(client):
    names = client.get("/api/v1/schemas").json()
    assert names == apischema.names() and "wan" in names and "diagnose" in names
    wan = client.get("/api/v1/schemas/wan")
    assert wan.status_code == 200 and wan.json() == json.loads(
        (ROOT / "docs" / "schemas" / "wan.v1.schema.json").read_text(encoding="utf-8"))
    for bad in ("nothing", "..%2F..%2Fpyproject", "wan.v1.schema.json", "%2e%2e", "WAN", "x" * 121):
        assert client.get(f"/api/v1/schemas/{bad}").status_code in (404, 422), bad


def test_every_report_route_is_a_get_without_a_path_parameter_and_only_named_parameters_are_taken(app):
    routes = [r for r in app.routes if isinstance(r, APIRoute)]
    assert routes
    for route in routes:
        assert route.methods == {"GET"}, route.path
        assert ":path" not in route.path, route.path                  # nothing can carry a path to forward
        assert set(route.param_convertors) <= {"site", "mac", "name"}, route.path
        assert all(p.name not in {"url", "path", "target", "host", "proxy"} for p in route.dependant.query_params), \
            route.path


def test_the_openapi_document_lists_every_route_with_its_schema_and_matches_the_checked_in_copy(client):
    spec = client.get("/api/v1/openapi.json").json()
    text = json.dumps(spec, indent=2, sort_keys=True) + "\n"
    assert f"{SITE}/diagnose".replace("default", "{site}") in spec["paths"]
    for path, item in spec["paths"].items():
        if path.startswith("/api/v1/unifi"):
            assert "application/json" in item["get"]["responses"]["200"]["content"], path
    client_path = f"{SITE.replace('default', '{site}')}/clients/{{mac}}"
    client_responses = spec["paths"][client_path]["get"]["responses"]
    assert "409" in client_responses and "500" in client_responses
    assert "500" in spec["paths"][f"{SITE.replace('default', '{site}')}/diagnose"]["get"]["responses"]
    assert "500" not in spec["paths"]["/api/v1/unifi/sites"]["get"]["responses"]
    candidates = client_responses["409"]["content"]["application/json"]["schema"]["properties"]["candidates"]
    assert candidates["items"]["required"] == ["Name", "MAC Address", "IP Address", "Status"]
    golden = Path(__file__).parent / "golden" / "openapi.json"
    if os.environ.get("UPDATE_GOLDEN"):
        golden.write_text(text, encoding="utf-8")
    assert text == golden.read_text(encoding="utf-8"), "UPDATE_GOLDEN=1 uv run pytest tests/test_server_routes.py"


def test_many_requests_for_the_same_report_read_the_controller_once(app, session):
    client = TestClient(app)
    statuses = []

    def ask():
        statuses.append(client.get(f"{SITE}/diagnose").status_code)

    threads = [threading.Thread(target=ask) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert statuses == [200] * 6
    counts = [session.calls.count(path) for path in set(session.calls)]
    assert counts and max(counts) == 1 and len(session.posts) == 1


def test_the_second_request_is_answered_from_the_cache_and_a_refresh_reads_again(client, session):
    client.get(f"{SITE}/wan")
    first = len(session.calls)
    client.get(f"{SITE}/wan")
    assert len(session.calls) == first
    refreshed = client.get(f"{SITE}/wan?refresh=true")
    assert refreshed.status_code == 200 and not any("refresh skipped" in w for w in refreshed.json()["warnings"])
    assert len(session.calls) == 2 * first
    again = client.get(f"{SITE}/wan?refresh=true")                           # too soon: ignored, and says so
    assert any("refresh skipped" in w for w in again.json()["warnings"]) and len(session.calls) == 2 * first


def test_only_gets_and_the_one_event_log_post_reach_the_controller_whatever_the_routes_ask(session):
    recording = RecordingSession(session)
    client = TestClient(create_app(CONFIG, hosts=["testserver"],
                                   service=ControllerService(CONFIG, session=recording)))
    for name in CASES:
        assert client.get(CASES[name][0]).status_code == 200, name
    assert {method for method, _, _ in recording.exchanges} <= {"GET", "POST"}
    assert all(path.endswith("/system-log/all") for method, path, _ in recording.exchanges if method == "POST")


def test_the_report_routes_answer_nothing_but_get(client):
    for path in (f"{SITE}/wan", f"{SITE}/clients/{DESKTOP}", "/api/v1/schemas/wan", "/api/v1/unifi/sites"):
        for method in ("post", "put", "patch", "delete", "head"):
            assert getattr(client, method)(path).status_code == 405, (method, path)


def test_the_request_log_names_the_route_template_not_the_site_or_the_mac(client):
    import io

    from homelab_probe import logs

    stream = io.StringIO()
    logs.configure("json", "INFO", stream=stream)
    client.get(f"{SITE}/clients/{DESKTOP}")
    lines = [json.loads(line) for line in stream.getvalue().splitlines() if "server.request" in line]
    assert lines and lines[0]["route"] == "/api/v1/unifi/sites/{site}/clients/{mac}"
    assert "BB:00" not in stream.getvalue() and DESKTOP.lower() not in stream.getvalue()
