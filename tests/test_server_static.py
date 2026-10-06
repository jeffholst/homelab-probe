"""Serving the built web interface (issue #242): the bundle, the fallback for deep links, the policy of a page, and the
promises that nothing else is reachable through it (the API, a file outside the bundle, a method but GET and HEAD).

The bundle is the placeholder in ``tests/fixtures/web``, copied for each test next to a file outside it (the secret
that no request may return)."""

import asyncio
import io
import json
import os
import shutil
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx2")

from fastapi.testclient import TestClient  # noqa: E402
from server_support import CONFIG, auth_for, logged_in  # noqa: E402

from homelab_probe import logs  # noqa: E402
from homelab_probe.demo.session import DemoSession  # noqa: E402
from homelab_probe.server import static  # noqa: E402
from homelab_probe.server.app import create_app  # noqa: E402
from homelab_probe.server.security import CSP, WEB_CSP  # noqa: E402
from homelab_probe.server.service import ControllerService  # noqa: E402
from homelab_probe.server.wizard import MODE_SETUP, SetupState  # noqa: E402

PLACEHOLDER = Path(__file__).resolve().parent / "fixtures" / "web"
SECRET = "SECRET-OUTSIDE-THE-BUNDLE"
IMMUTABLE = "public, max-age=31536000, immutable"
ASSET = "/assets/index-0a1b2c3d.js"


@pytest.fixture
def outside(tmp_path):
    """A directory next to the bundle, with a file that must never be served."""
    directory = tmp_path / "outside"
    directory.mkdir()
    (directory / "secret.txt").write_text(SECRET, encoding="utf-8")
    return directory


@pytest.fixture
def bundle(tmp_path, outside):
    root = tmp_path / "bundle"
    shutil.copytree(PLACEHOLDER, root)
    (root / "assets" / "data.bin").write_bytes(b"\x00\x01")
    (root / "assets" / "font.WOFF2").write_bytes(b"wOF2")
    (root / ".hidden").write_text(SECRET, encoding="utf-8")
    (root / "assets" / ".hidden.js").write_text(SECRET, encoding="utf-8")
    (root / "folder").mkdir()
    (root / "folder" / "inner.txt").write_text("inner", encoding="utf-8")
    (root / "robots.txt").write_text("User-agent: *", encoding="utf-8")
    return root


def make_app(tmp_path, web_root, **kwargs):
    return create_app(CONFIG, state_dir=tmp_path, hosts=["testserver"], auth=auth_for(tmp_path),
                      service=ControllerService(CONFIG, session=DemoSession()), web_root=web_root, **kwargs)


@pytest.fixture
def app(tmp_path, bundle):
    return make_app(tmp_path, bundle)


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def api_only(tmp_path):
    """The same server with no bundle (an API-only checkout)."""
    return make_app(tmp_path / "plain", tmp_path / "does-not-exist")


def asgi_get(app, path, method="GET"):
    """One request with the path exactly as given (``scope["path"]`` is what a server hands over after decoding), so
    a test is not at the mercy of a client library that normalises ``..``. Returns (status, headers, body)."""
    sent, chunks = {}, []

    async def run():
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
                 "path": path, "raw_path": path.encode("utf-8"), "query_string": b"", "root_path": "",
                 "headers": [(b"host", b"testserver")], "client": ("127.0.0.1", 50000), "server": ("testserver", 80)}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                sent["status"] = message["status"]
                sent["headers"] = {k.decode().lower(): v.decode() for k, v in message["headers"]}
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        await app(scope, receive, send)

    asyncio.run(run())
    return sent["status"], sent["headers"], b"".join(chunks)


# -- the page and its files ---------------------------------------------------------------------------------------

def test_the_root_is_the_index_page_and_is_never_cached(client):
    response = client.get("/")
    assert response.status_code == 200 and response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.text == (PLACEHOLDER / "index.html").read_text(encoding="utf-8")
    assert response.headers["cache-control"] == "no-cache"


def test_head_of_the_root_and_of_a_file_has_headers_and_no_body(client):
    for path in ("/", ASSET, "/findings/abc"):
        response = client.head(path)
        assert response.status_code == 200 and response.content == b"", path
        assert "content-type" in response.headers and "cache-control" in response.headers


def test_hashed_assets_are_cached_for_a_year_with_the_right_type(client):
    script, sheet = client.get(ASSET), client.get("/assets/index-0a1b2c3d.css")
    assert script.status_code == 200 and script.text == (PLACEHOLDER / "assets" / "index-0a1b2c3d.js").read_text()
    assert script.headers["content-type"] == "text/javascript; charset=utf-8" and script.headers["cache-control"] == IMMUTABLE
    assert sheet.headers["content-type"] == "text/css; charset=utf-8" and sheet.headers["cache-control"] == IMMUTABLE


def test_other_files_of_the_bundle_are_served_but_revalidated(client):
    icon, robots = client.get("/favicon.svg"), client.get("/robots.txt")
    assert icon.status_code == 200 and icon.headers["content-type"] == "image/svg+xml"
    assert icon.headers["cache-control"] == "no-cache" and robots.text == "User-agent: *"
    assert client.get("/index.html").text == client.get("/").text


def test_the_content_type_comes_from_a_fixed_table_and_an_unknown_one_is_opaque(client):
    assert client.get("/assets/font.WOFF2").headers["content-type"] == "font/woff2"      # the suffix is not case-sensitive
    assert client.get("/assets/data.bin").headers["content-type"] == "application/octet-stream"
    assert set(static.CONTENT_TYPES) >= {".html", ".js", ".css", ".svg", ".png", ".ico", ".woff2", ".json", ".map"}


@pytest.mark.parametrize("path", ["/findings/abc", "/clients/10.0.0.1", "/clients/aa:bb:cc:dd:ee:ff", "/findings/", "/login",
                                  "/setup", "/settings//x", "/.env", "/robots.txt/more", "/assets.js", "/folder", "/folder/",
                                  "/.hidden", "/%20", "/some/very/deep/link/that/is/not/a/file"])
def test_a_deep_link_gets_the_index_page_and_nothing_else_of_the_bundle(client, path):
    response = client.get(path)
    assert response.status_code == 200 and response.text == (PLACEHOLDER / "index.html").read_text(encoding="utf-8"), path
    assert response.headers["cache-control"] == "no-cache"


def test_a_missing_asset_is_a_404_and_never_the_page(client):
    for path in ("/assets/missing.js", "/assets", "/assets/", "/assets/.hidden.js", "/assets/sub/missing.css", "/assets//x"):
        response = client.get(path)
        assert response.status_code == 404 and response.json() == {"detail": "Not Found"}, path
        assert response.headers["cache-control"] == "no-store" and response.headers["content-security-policy"] == CSP


def test_a_directory_is_not_listed(bundle):
    assert static.resolve(bundle, "folder") is None and static.resolve(bundle, "assets") is None
    assert static.resolve(bundle, "folder/inner.txt") == bundle / "folder" / "inner.txt"


def test_the_request_log_has_the_route_template_not_the_path(client):
    stream = io.StringIO()
    logs.configure("json", "INFO", stream=stream)
    client.get("/clients/aa:bb:cc:dd:ee:ff")
    (record,) = [json.loads(line) for line in stream.getvalue().splitlines() if "server.request" in line]
    assert record["route"] == "/{relative:path}" and "aa:bb" not in json.dumps(record)


# -- the policy ------------------------------------------------------------------------------------------------

def test_the_page_and_its_files_carry_the_policy_of_a_page(client):
    for path in ("/", "/findings/abc", ASSET, "/favicon.svg"):
        headers = client.get(path).headers
        assert headers["content-security-policy"] == WEB_CSP, path
        assert headers["x-content-type-options"] == "nosniff" and headers["x-frame-options"] == "DENY"
        assert headers["referrer-policy"] == "no-referrer" and headers["cross-origin-resource-policy"] == "same-origin"
        assert "server" not in headers


def test_the_policy_of_a_page_has_no_inline_code_no_eval_and_no_other_host():
    directives = dict(part.strip().split(" ", 1) for part in WEB_CSP.split(";"))
    assert directives["default-src"] == "'none'" and directives["script-src"] == "'self'"
    assert directives["style-src"] == "'self'" and directives["connect-src"] == "'self'"
    assert directives["frame-ancestors"] == "'none'" and directives["base-uri"] == "'none'"
    assert directives["object-src"] == "'none'" and directives["form-action"] == "'self'"
    assert directives["img-src"] == "'self' data:" and directives["font-src"] == "'self'"
    assert "unsafe" not in WEB_CSP and "*" not in WEB_CSP and "http" not in WEB_CSP


def test_the_api_keeps_its_strict_policy_and_no_store_next_to_a_bundle(client, app):
    assert client.get("/api/v1/meta").headers["content-security-policy"] == CSP
    assert client.get("/api/v1/meta").headers["cache-control"] == "no-store"
    assert client.get("/healthz").headers["content-security-policy"] == CSP
    assert logged_in(app).get("/api/v1/platforms").headers["content-security-policy"] == CSP
    assert CSP != WEB_CSP and "form-action 'none'" in CSP


def test_only_the_web_interface_can_ask_for_its_own_policy(app):
    """A handler that sets the headers itself still gets the API's (the exemption is the scope flag, not a header)."""
    from fastapi import Response

    @app.get("/probe")
    def probe(response: Response):
        response.headers["content-security-policy"] = "default-src *"
        response.headers["cache-control"] = "max-age=600"
        return {}

    app.router.routes.insert(0, app.router.routes.pop())            # before the fallback, which matches every path
    headers = logged_in(app).get("/probe").headers
    assert headers["content-security-policy"] == CSP and headers["cache-control"] == "no-store"


# -- the API and the probes are never shadowed ---------------------------------------------------------------------

def test_an_unknown_api_path_stays_the_json_404_of_a_server_without_a_bundle(client, api_only):
    plain = TestClient(api_only)
    for path in ("/api/v1/nope", "/api", "/api/", "/api/v1/findings/../x", "/healthz/x", "/readyz/x", "/api/v1/meta/x"):
        answer, expected = client.get(path), plain.get(path)
        assert (answer.status_code, answer.json()) == (expected.status_code, expected.json()), path
        assert answer.status_code == 404 and answer.headers["content-security-policy"] == CSP
    assert client.head("/api/v1/nope").status_code == 404


def test_the_real_routes_answer_as_before(client, app):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"ready": True}
    assert client.get("/api/v1/meta").json()["login_required"] is True
    assert client.get("/api/v1/platforms").status_code == 401            # a route that needs a login still does
    assert client.get("/api/v1/openapi.json").status_code == 401
    assert logged_in(app).get("/api/v1/platforms").json()[0]["id"] == "unifi"


def test_the_fallback_is_not_part_of_the_api_description(app, api_only):
    paths = lambda application: set(logged_in(application).get("/api/v1/openapi.json").json()["paths"])   # noqa: E731
    assert paths(app) == paths(api_only)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_only_get_and_head_are_answered_exactly_as_without_a_bundle(app, api_only, method):
    web, plain = logged_in(app), logged_in(api_only)
    for path in ("/", "/findings/abc", ASSET, "/api/v1/nope", "/healthz", "/api/v1/meta"):
        a, b = web.request(method, path), plain.request(method, path)
        assert (a.status_code, a.json()) == (b.status_code, b.json()), (method, path)
        assert a.status_code in (404, 405) and "html" not in a.headers.get("content-type", ""), (method, path)


def test_the_fallback_matches_only_get_and_head_and_not_the_api():
    from starlette.routing import Match

    route = static.router().routes[0]
    page = {"type": "http", "method": "GET", "path": "/findings/abc"}
    assert route.matches(page)[0] == Match.FULL
    assert route.matches({**page, "method": "HEAD"})[0] == Match.FULL
    for method in ("POST", "PUT", "DELETE", "OPTIONS", "PATCH"):
        assert route.matches({**page, "method": method})[0] == Match.NONE
    for path in ("/api", "/api/", "/api/v1/x", "/healthz", "/healthz/x", "/readyz", "/readyz/x"):
        assert route.matches({**page, "path": path})[0] == Match.NONE, path
    for path in ("/apix", "/api-docs", "/healthzz", "/API/v1", "/x/api/v1"):
        assert route.matches({**page, "path": path})[0] == Match.FULL, path
    assert route.matches({"type": "websocket", "path": "/api"})[0] == Match.NONE


# -- nothing outside the bundle ----------------------------------------------------------------------------------

TRAVERSAL = ["/../outside/secret.txt", "/assets/../../outside/secret.txt", "/../../../../etc/passwd",
             "/..", "/assets/..", "/./index.html", "/assets/./x.js", "/.",
             "/..\\outside\\secret.txt", "/assets\\..\\..\\outside\\secret.txt", "/\\outside\\secret.txt",
             "//etc/passwd", "//outside/secret.txt", "/assets//etc/passwd", "/index.html\0", "/\0", "/assets/x\0.js"]


@pytest.mark.parametrize("path", TRAVERSAL)
def test_a_path_that_leaves_the_bundle_reads_nothing(app, outside, path):
    # the path is what the server hands to the app once the URL is decoded, so the decoded forms are tried as they are
    status, headers, body = asgi_get(app, path)
    assert status == 404 and SECRET.encode() not in body and b"root:" not in body, path
    assert headers["content-security-policy"] == CSP


@pytest.mark.parametrize("target", ["/..%2foutside%2fsecret.txt", "/%2e%2e%2foutside%2fsecret.txt", "/assets/..%2f..%2foutside%2fsecret.txt",
                                    "/%2e%2e/outside/secret.txt", "/..%5coutside%5csecret.txt", "/%2fetc%2fpasswd", "/%00",
                                    "/assets/%2e%2e/%2e%2e/outside/secret.txt", "/../outside/secret.txt"])
def test_an_encoded_traversal_through_a_real_client_reads_nothing(client, outside, target):
    response = client.get(target)
    assert SECRET not in response.text and "root:" not in response.text, target
    assert response.status_code in (200, 404) and (response.status_code == 404 or "PLACEHOLDER" in response.text), target


def test_a_link_in_the_bundle_is_never_followed(tmp_path, outside, bundle):
    (bundle / "leak.txt").symlink_to(outside / "secret.txt")                    # a file outside, through a link
    (bundle / "leakdir").symlink_to(outside, target_is_directory=True)           # a directory outside
    (bundle / "assets" / "leak.js").symlink_to(outside / "secret.txt")
    (bundle / "assets" / "leakdir").symlink_to(outside, target_is_directory=True)
    (bundle / "alias.js").symlink_to(bundle / "assets" / "index-0a1b2c3d.js")    # even a link that stays inside
    (bundle / "assets" / "alias.css").symlink_to("index-0a1b2c3d.css")           # relative, in the same directory
    client = TestClient(make_app(tmp_path / "links", bundle))
    for path in ("/leak.txt", "/leakdir/secret.txt", "/assets/leak.js", "/assets/leakdir/secret.txt", "/alias.js"):
        response = client.get(path)
        assert SECRET not in response.text and "document.getElementById" not in response.text, path
        assert response.status_code in (200, 404) and (response.status_code == 404 or "PLACEHOLDER" in response.text), path
    assert client.get("/assets/alias.css").status_code == 404 and client.get("/assets/leak.js").status_code == 404
    assert static.resolve(bundle.resolve(), "leak.txt") is None and static.resolve(bundle.resolve(), "alias.js") is None
    assert static.resolve(bundle.resolve(), "leakdir/secret.txt") is None


def test_a_name_that_is_not_text_a_file_system_accepts_is_a_404_not_an_error(client):
    assert client.get("/assets/" + "x" * 5000).status_code == 404


@pytest.mark.parametrize("relative", ["", "/etc/passwd", "..", "../x", "a/../b", "a/./b", "a\\b", "a\0b", ".hidden",
                                      "assets/.hidden.js", "a//b", "assets/", "missing", "folder"])
def test_resolve_refuses_what_is_not_a_plain_file_of_the_bundle(bundle, relative):
    assert static.resolve(bundle.resolve(), relative) is None


def test_resolve_refuses_an_absolute_path_to_a_real_file(bundle, outside):
    assert static.resolve(bundle.resolve(), str(outside / "secret.txt")) is None
    assert static.resolve(bundle.resolve(), "assets/index-0a1b2c3d.js") is not None


def test_what_is_well_formed():
    assert all(static.well_formed(p) for p in ("", "a", "a/b", "a/", "a//b", "10.0.0.1", ".env", "a:b"))
    assert not any(static.well_formed(p) for p in ("..", "a/../b", "./a", "a/.", "/a", "a\\b", "a\0"))


# -- public, and only that ---------------------------------------------------------------------------------------

def test_the_page_loads_without_a_login_and_everything_else_still_needs_one(app):
    spec = logged_in(app).get("/api/v1/openapi.json").json()
    routes = [(m.upper(), p) for p, item in spec["paths"].items() for m in item]
    fill = {"{site}": "default", "{mac}": "BB:00:00:00:00:01", "{name}": "wan", "{id}": "x", "{username}": "bob"}
    public = {("GET", "/api/v1/meta"), ("POST", "/api/v1/auth/login")}
    anonymous = TestClient(app)
    assert len(routes) > 20
    for method, template in routes:
        path = template
        for key, value in fill.items():
            path = path.replace(key, value)
        status = anonymous.request(method, path, headers={"Origin": "http://testserver"}).status_code
        if (method, template) in public:
            continue
        assert status == 401, (method, template, status)
    for path in ("/", "/login", "/findings/abc", ASSET):
        assert anonymous.get(path).status_code == 200


def test_the_endpoints_marked_public_are_these(app):
    from homelab_probe.server.auth import PUBLIC_ENDPOINTS

    assert "web_file" in PUBLIC_ENDPOINTS and "root" in PUBLIC_ENDPOINTS


def test_the_page_loads_while_the_server_is_being_set_up_and_the_api_does_not(tmp_path, bundle):
    state = SetupState(MODE_SETUP, "no_config", "t" * 24)
    client = TestClient(make_app(tmp_path / "setup", bundle, setup=state))
    assert client.get("/").status_code == 200 and client.get("/setup").status_code == 200
    assert client.get(ASSET).status_code == 200
    assert client.get("/api/v1/platforms").status_code == 503
    assert client.get("/api/v1/nope").status_code == 404


# -- no bundle ------------------------------------------------------------------------------------------------

def test_without_a_bundle_the_root_is_the_notice_and_nothing_is_mounted(api_only):
    client = TestClient(api_only)
    body = client.get("/").json()
    assert body["api"] == "/api/v1" and "no web interface" in body["note"] and "docs/development.md" in body["note"]
    for path in ("/findings/abc", ASSET, "/index.html", "/favicon.svg"):
        assert client.get(path).status_code == 404 and client.get(path).json() == {"detail": "Not Found"}
    assert api_only.state.web is None
    assert not [r for r in api_only.routes if getattr(r, "path", "").startswith("/{")]
    assert client.get("/").headers["content-security-policy"] == CSP


def test_a_directory_without_a_usable_index_is_not_a_bundle(tmp_path, bundle):
    assert static.find_bundle(bundle) == bundle.resolve()
    empty = tmp_path / "empty"
    empty.mkdir()
    assert static.find_bundle(empty) is None
    (empty / "index.html").mkdir()                                   # a directory is not the page
    assert static.find_bundle(empty) is None
    assert static.find_bundle(bundle / "robots.txt") is None         # a file is not a bundle
    assert static.find_bundle(tmp_path / "missing") is None
    (bundle / "index.html").unlink()
    (bundle / "index.html").symlink_to(bundle / "robots.txt")        # a link is not the page
    assert static.find_bundle(bundle) is None


def test_a_bundle_reached_through_a_link_is_served_from_its_real_place(tmp_path, bundle):
    link = tmp_path / "current"
    link.symlink_to(bundle, target_is_directory=True)
    client = TestClient(make_app(tmp_path / "linked", link))
    assert client.get(ASSET).status_code == 200 and client.get("/findings/abc").status_code == 200


def test_the_page_disappearing_while_the_server_runs_is_a_404_not_an_error(tmp_path, bundle):
    client = TestClient(make_app(tmp_path / "gone", bundle))
    (bundle / "index.html").unlink()
    assert client.get("/").status_code == 404 and client.get("/findings/abc").status_code == 404
    assert client.get(ASSET).status_code == 200


def test_the_default_bundle_is_the_web_directory_of_the_package(monkeypatch, tmp_path, bundle):
    import subprocess
    import sys

    import homelab_probe

    # the conftest points it at nothing: ask a fresh interpreter what the code itself says
    code = "from homelab_probe.server import static; print(static.DEFAULT_ROOT)"
    shown = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert Path(shown) == Path(homelab_probe.__file__).resolve().parent / "web"
    monkeypatch.setattr(static, "DEFAULT_ROOT", bundle)
    assert static.find_bundle() == bundle.resolve()
    state = tmp_path / "d"
    app = create_app(CONFIG, state_dir=state, hosts=["testserver"], auth=auth_for(state),
                     service=ControllerService(CONFIG, session=DemoSession()))
    assert TestClient(app).get("/x").status_code == 200


def test_the_placeholder_bundle_is_small_and_is_not_a_built_interface():
    files = sorted(p.relative_to(PLACEHOLDER).as_posix() for p in PLACEHOLDER.rglob("*") if p.is_file())
    assert files == ["assets/index-0a1b2c3d.css", "assets/index-0a1b2c3d.js", "favicon.svg", "index.html"]
    assert sum(os.path.getsize(PLACEHOLDER / f) for f in files) < 4096
    assert "PLACEHOLDER" in (PLACEHOLDER / "index.html").read_text(encoding="utf-8")
