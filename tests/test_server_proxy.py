"""Behind a reverse proxy: the client's address and the scheme come from the proxy's headers, but only from a proxy that
was named with --forwarded-allow-ips (issue #184, PR B). This uses uvicorn's own middleware, the one `serve` turns on."""

import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx2")
pytest.importorskip("uvicorn")

from fastapi.testclient import TestClient  # noqa: E402
from server_support import CONFIG, PASSWORD, auth_for  # noqa: E402
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware  # noqa: E402

from homelab_probe.demo.session import DemoSession  # noqa: E402
from homelab_probe.server.app import create_app  # noqa: E402
from homelab_probe.server.service import ControllerService  # noqa: E402

PROXY = "10.0.0.1"
FORWARDED = {"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"}


@pytest.fixture
def app(tmp_path):
    return create_app(CONFIG, state_dir=tmp_path, hosts=["testserver"], auth=auth_for(tmp_path),
                      service=ControllerService(CONFIG, session=DemoSession()))


def login(client, **headers):
    return client.post("/api/v1/auth/login", json={"username": "bob", "password": PASSWORD},
                       headers={"Origin": "http://testserver", **headers})


def test_a_named_proxy_gives_the_real_client_address_and_the_https_scheme(app, tmp_path):
    client = TestClient(ProxyHeadersMiddleware(app, trusted_hosts=PROXY), client=(PROXY, 1))
    response = login(client, **FORWARDED)
    assert response.status_code == 200
    assert response.headers["set-cookie"].startswith("__Host-hlp_session=") and "secure" in response.headers[
        "set-cookie"].lower()
    entries = [json.loads(line) for line in (tmp_path / "audit.log").read_text().splitlines()]
    assert entries[-1]["event"] == "auth.login" and entries[-1]["address"] == "203.0.113.9"


def test_the_same_headers_from_anyone_else_are_ignored(app, tmp_path):
    client = TestClient(ProxyHeadersMiddleware(app, trusted_hosts=PROXY), client=("10.9.9.9", 1))
    response = login(client, **FORWARDED)
    assert response.headers["set-cookie"].startswith("hlp_session=") and "secure" not in response.headers[
        "set-cookie"].lower()
    entries = [json.loads(line) for line in (tmp_path / "audit.log").read_text().splitlines()]
    assert entries[-1]["address"] == "10.9.9.9"                      # a client cannot choose the address it is logged as


def test_without_the_middleware_nothing_is_believed(app, tmp_path):
    client = TestClient(app, client=(PROXY, 1))
    assert login(client, **FORWARDED).headers["set-cookie"].startswith("hlp_session=")
    assert json.loads((tmp_path / "audit.log").read_text().splitlines()[-1])["address"] == PROXY
