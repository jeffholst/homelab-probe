"""The web server's skeleton: the routes without data, the security middleware and the request log (issue #183)."""

import io
import json
import re

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx2")

from fastapi.testclient import TestClient  # noqa: E402
from server_support import CONFIG, auth_for, logged_in  # noqa: E402

from homelab_probe import __version__, logs  # noqa: E402
from homelab_probe.config import Config  # noqa: E402
from homelab_probe.demo.session import DemoSession  # noqa: E402
from homelab_probe.server.app import create_app  # noqa: E402
from homelab_probe.server.security import CSP, SECURITY_HEADERS, allowed_hosts  # noqa: E402
from homelab_probe.server.service import ControllerService  # noqa: E402


@pytest.fixture
def app(tmp_path):
    return create_app(CONFIG, state_dir=tmp_path, hosts=["testserver"], auth=auth_for(tmp_path),
                      service=ControllerService(CONFIG, session=DemoSession()))


@pytest.fixture
def client(app):
    return TestClient(app)


# -- the routes ------------------------------------------------------------------------------------------------

def test_healthz_says_ok_and_nothing_else(client):
    response = client.get("/healthz")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


def test_readyz_and_meta_say_what_a_client_may_know_before_login(client):
    assert client.get("/readyz").json() == {"ready": True}
    assert client.get("/api/v1/meta").json() == {"version": __version__, "needs_setup": False,
                                                  "login_required": True, "demo": False}


def test_a_demo_app_says_so(app, tmp_path):
    demo = create_app(app.state.config, state_dir=tmp_path / "demo", demo=True, hosts=["testserver"])
    assert TestClient(demo).get("/api/v1/meta").json()["demo"] is True


def test_the_only_platform_is_unifi(app):
    assert logged_in(app).get("/api/v1/platforms").json() == [{"id": "unifi", "name": "UniFi", "configured": True}]


def test_the_root_points_at_the_api(client):
    body = client.get("/").json()
    assert body["api"] == "/api/v1" and body["version"] == __version__


def test_the_openapi_document_is_served_but_the_pages_that_load_a_cdn_script_are_not(app):
    client = logged_in(app)
    spec = client.get("/api/v1/openapi.json").json()
    assert spec["info"]["title"] == "Homelab Probe" and "/api/v1/meta" in spec["paths"]
    assert "/healthz" not in spec["paths"]                                  # probes are not part of the API
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert client.get(path).status_code == 404


def test_only_get_is_answered(app):
    client = logged_in(app)
    for path in ("/healthz", "/api/v1/meta", "/api/v1/platforms", "/"):
        for method in ("post", "put", "patch", "delete"):
            assert getattr(client, method)(path).status_code == 405, (method, path)
        assert client.head(path).status_code == 405                       # GET only


def test_the_app_remembers_what_it_was_made_from(tmp_path):
    config = Config(controller_url="https://controller.example", api_key="key")
    service = ControllerService(config, session=DemoSession())
    app = create_app(config, tmp_path / "hlp.toml", tmp_path, service=service)
    assert (app.state.config, app.state.settings_path, app.state.state_dir) == (config, tmp_path / "hlp.toml", tmp_path)
    assert app.state.service is service and app.state.demo is False
    assert create_app(config, demo=True).state.service.demo is True            # made from the config when none is given


# -- the Host header and CORS ----------------------------------------------------------------------------------

def test_a_host_header_that_is_not_ours_is_refused_even_for_an_unknown_path(app):
    attacker = TestClient(app, base_url="http://evil.example")
    for path in ("/healthz", "/api/v1/meta", "/nothing-here"):
        assert attacker.get(path).status_code == 400, path
    assert TestClient(app, base_url="http://testserver").get("/healthz").status_code == 200
    assert TestClient(app).get("/healthz", headers={"Host": "evil.example"}).status_code == 400


@pytest.mark.parametrize("host, port, expected", [
    ("127.0.0.1", 8787, {"localhost", "127.0.0.1", "[::1]"}), ("::1", 8787, {"localhost", "127.0.0.1", "[::1]"}),
    ("127.0.0.2", 80, {"127.0.0.2"}), ("localhost", 1, {"localhost"}),
])
def test_the_allowed_hosts_are_the_loopback_names_and_the_bind_address(host, port, expected):
    assert expected <= set(allowed_hosts(host, port))
    assert "evil.example" not in allowed_hosts(host, port) and "0.0.0.0" not in allowed_hosts(host, port)


def test_there_is_no_cors(client):
    response = client.get("/api/v1/meta", headers={"Origin": "https://evil.example"})
    assert not [h for h in response.headers if h.lower().startswith("access-control-")]
    preflight = client.options("/api/v1/meta", headers={"Origin": "https://evil.example",
                                                         "Access-Control-Request-Method": "GET"})
    assert preflight.status_code == 405 and "access-control-allow-origin" not in preflight.headers


# -- the headers -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path, status", [("/healthz", 200), ("/api/v1/meta", 200), ("/missing", 404),
                                          ("/api/v1/openapi.json", 401)])
def test_every_response_carries_the_security_headers(client, path, status):
    response = client.get(path)
    assert response.status_code == status
    for name, value in SECURITY_HEADERS:
        assert response.headers[name.decode()] == value.decode()
    assert "default-src 'none'" in CSP and "frame-ancestors 'none'" in CSP and "'unsafe-inline'" not in CSP
    assert response.headers["cache-control"] == "no-store" and "server" not in response.headers


def test_a_refused_host_and_a_wrong_method_carry_them_too(app):
    refused = TestClient(app, base_url="http://evil.example").get("/healthz")
    wrong = logged_in(app).post("/healthz")
    for response in (refused, wrong):
        assert response.headers["content-security-policy"] == CSP and response.headers["x-content-type-options"] == "nosniff"


def test_headers_that_the_handler_set_itself_are_replaced_not_duplicated(app):
    from fastapi import Response

    @app.get("/probe")
    def probe(response: Response):
        response.headers["cache-control"] = "max-age=600"
        return {}

    response = TestClient(app).get("/probe")
    assert response.headers.get_list("cache-control") == ["no-store"]


# -- the request log -------------------------------------------------------------------------------------------

def logged(client, path, **kwargs):
    stream = io.StringIO()
    logs.configure("json", "INFO", stream=stream)
    response = client.get(path, **kwargs)
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    return response, [r for r in records if r["event"] == "server.request"]


def test_a_request_is_one_info_record_with_the_route_template_and_a_request_id(client):
    response, records = logged(client, "/api/v1/meta")
    (record,) = records
    assert (record["method"], record["route"], record["status"]) == ("GET", "/api/v1/meta", 200)
    assert isinstance(record["duration_ms"], int) and record["level"] == "INFO"
    assert re.fullmatch(r"[0-9a-f]{12}", response.headers["x-request-id"])
    assert record["request_id"] == response.headers["x-request-id"]


def test_the_log_never_holds_the_path_that_was_asked_for(client):
    mac = "bb:00:00:00:00:01"
    response, records = logged(client, f"/clients/{mac}/{'x' * 40}?name=Alice")
    (record,) = records
    assert response.status_code == 404 and record["route"] == "(no route)" and record["status"] == 404
    assert mac not in json.dumps(record) and "Alice" not in json.dumps(record)


def test_an_id_that_a_client_sends_is_ignored(client):
    response, records = logged(client, "/healthz", headers={"X-Request-ID": "forged\nINFO line"})
    assert response.headers["x-request-id"] != "forged\nINFO line" and "forged" not in json.dumps(records)


def test_each_request_gets_its_own_id(client):
    first, second = client.get("/healthz"), client.get("/healthz")
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


def test_a_refused_host_is_logged_too(app):
    stream = io.StringIO()
    logs.configure("json", "INFO", stream=stream)
    TestClient(app, base_url="http://evil.example").get("/healthz")
    (record,) = [json.loads(line) for line in stream.getvalue().splitlines() if "server.request" in line]
    assert record["status"] == 400 and record["route"] == "(no route)" and "evil" not in json.dumps(record)


def test_a_handler_that_raises_is_logged_as_a_500_without_its_message(app):
    @app.get("/boom")
    def boom():
        raise RuntimeError("secret text from an exception")

    stream = io.StringIO()
    logs.configure("json", "INFO", stream=stream)
    client = logged_in(app, raise_server_exceptions=False)
    stream.seek(0)
    stream.truncate()                                                       # the login's own request record is not the point
    response = client.get("/boom")
    assert response.status_code == 500 and "secret text" not in response.text
    for name, value in SECURITY_HEADERS:
        assert response.headers[name.decode()] == value.decode()
    (record,) = [json.loads(line) for line in stream.getvalue().splitlines() if "server.request" in line]
    assert record["status"] == 500 and record["route"] == "/boom" and "secret text" not in json.dumps(record)


def test_a_streaming_handler_failure_keeps_the_headers_on_its_started_response(app):
    from starlette.responses import StreamingResponse

    @app.get("/stream-boom")
    async def stream_boom():
        async def body():
            yield b"started"
            raise RuntimeError("secret text from a streaming exception")

        return StreamingResponse(body())

    response = logged_in(app, raise_server_exceptions=False).get("/stream-boom")
    assert response.status_code == 200
    for name, value in SECURITY_HEADERS:
        assert response.headers[name.decode()] == value.decode()


def test_the_server_never_calls_the_controller_on_its_own(app, monkeypatch):
    import requests

    def forbidden(*args, **kwargs):
        raise AssertionError("the skeleton must not make a request")

    monkeypatch.setattr(requests.Session, "request", forbidden)
    client = logged_in(app)
    for path in ("/", "/healthz", "/readyz", "/api/v1/meta", "/api/v1/platforms", "/api/v1/openapi.json"):
        assert client.get(path).status_code == 200


def test_the_middleware_lets_lifespan_and_websocket_traffic_through_untouched(app):
    with TestClient(app) as started:                       # entering the client runs the lifespan startup/shutdown
        assert started.get("/healthz").status_code == 200
    with pytest.raises(Exception):                         # noqa: B017  (no websocket route exists: any failure is right)
        with TestClient(app).websocket_connect("/ws"):
            pass


# -- readiness reads the controller (through the cache) ----------------------------------------------------------

def test_readyz_is_503_and_says_nothing_more_when_the_controller_cannot_be_read(tmp_path):
    session = DemoSession()
    session.status = 401
    app = create_app(CONFIG, state_dir=tmp_path, hosts=["testserver"], auth=auth_for(tmp_path),
                     service=ControllerService(CONFIG, session=session))
    client = TestClient(app)
    response = client.get("/readyz")
    assert response.status_code == 503 and response.json() == {"ready": False}      # public: the reason is in the log
    assert "controller.example" not in response.text and "the-api-key" not in response.text
    assert client.get("/healthz").status_code == 200                       # the process is up all the same


def test_readyz_costs_the_controller_one_read_however_often_it_is_probed(app):
    client = TestClient(app)
    for _ in range(10):
        assert client.get("/readyz").status_code == 200
    assert app.state.service.session.calls == ["/proxy/network/integration/v1/info"]
