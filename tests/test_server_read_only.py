"""``serve --read-only`` (issue #186): every route that writes a file of this machine is refused, and a test lists
the unsafe routes so a new one cannot be forgotten."""

import re

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("tomlkit")

from fastapi.testclient import TestClient  # noqa: E402
from server_support import CONFIG, auth_for, logged_in, origin_of  # noqa: E402

from homelab_probe import cli  # noqa: E402
from homelab_probe.config import Config  # noqa: E402
from homelab_probe.demo import demo_client  # noqa: E402
from homelab_probe.demo.session import DemoSession  # noqa: E402
from homelab_probe.server import runner  # noqa: E402
from homelab_probe.server.app import create_app  # noqa: E402
from homelab_probe.server.auth import (
    LOCAL_WRITE_ENDPOINTS,  # noqa: E402
    AuthState,  # noqa: E402
)
from homelab_probe.server.service import ControllerService  # noqa: E402
from homelab_probe.server.wizard import MODE_SETUP, SetupState  # noqa: E402

# The unsafe (non-GET) routes that write no file of this machine: the login and logout keep sessions in memory, and
# the setup steps before `finish` keep the draft in memory and talk to the controller.
WRITES_NOTHING = {"login", "logout", "setup_draft", "setup_certificate", "setup_connection", "setup_preview",
                  "setup_notifications", "notifications_test"}
SAFE_METHODS = {"get", "head", "options"}
TOKEN = "setup-token-0123456789abcdef"
STANDIN = Config(controller_url="https://unconfigured.invalid", api_key="unconfigured")


def make_app(tmp_path, read_only=True):
    return create_app(CONFIG, state_dir=tmp_path, hosts=["testserver"], auth=auth_for(tmp_path), read_only=read_only,
                      service=ControllerService(CONFIG, session=DemoSession()))


def unsafe_operations(app):
    """(function name, method, path) of every route that is not a read, from the OpenAPI document."""
    found = []
    for path, item in app.openapi()["paths"].items():
        for method, operation in item.items():
            if method in SAFE_METHODS:
                continue
            found.append((operation["operationId"], method, path))
    return found


def name_of(operation_id, path, method):
    """The function name inside an operation id (FastAPI builds it as name + path with non-word characters as ``_``
    + method)."""
    suffix = re.sub(r"\W", "_", path) + "_" + method
    assert operation_id.endswith(suffix), (operation_id, suffix)
    return operation_id[:-len(suffix)]


def test_every_unsafe_route_is_either_marked_as_writing_or_on_the_list_of_those_that_write_nothing(tmp_path):
    operations = unsafe_operations(make_app(tmp_path))
    names = {name_of(operation_id, path, method) for operation_id, method, path in operations}
    assert len(operations) >= 9
    assert names - WRITES_NOTHING == LOCAL_WRITE_ENDPOINTS == {
        "put_settings", "setup_finish", "snapshots_save", "users_add", "users_change", "users_reset_password"}


def test_a_read_only_server_refuses_the_settings_change_and_still_serves_the_settings(tmp_path):
    admin = logged_in(make_app(tmp_path), "alice")
    body = admin.get("/api/v1/settings").json()
    assert body["read_only"] is True
    response = admin.put("/api/v1/settings", json={"version": body["version"], "thresholds": {"slow_link_mbps": 10}})
    assert response.status_code == 403 and response.json()["error"] == "read_only"
    assert not (tmp_path / "hlp.toml").exists()
    assert admin.get("/api/v1/meta").json()["read_only"] is True


def test_a_server_that_is_not_read_only_writes(tmp_path):
    admin = logged_in(make_app(tmp_path, read_only=False), "alice")
    assert admin.get("/api/v1/meta").json()["read_only"] is False
    assert admin.put("/api/v1/settings", json={"version": "absent", "thresholds": {"slow_link_mbps": 10}}).status_code == 200


def test_the_refusal_comes_after_the_login_check(tmp_path):
    app = make_app(tmp_path)
    anonymous = TestClient(app)
    assert anonymous.put("/api/v1/settings", json={"version": "x"},
                         headers={"Origin": origin_of(anonymous)}).status_code == 401
    viewer = logged_in(app, "bob")
    assert viewer.put("/api/v1/settings", json={"version": "x"}).status_code == 403          # read_only or forbidden


def test_logging_in_and_out_still_work_in_a_read_only_server(tmp_path):
    bob = logged_in(make_app(tmp_path), "bob")                    # the login itself
    assert bob.post("/api/v1/auth/logout").status_code == 200


def test_the_setup_that_writes_files_is_refused_and_the_steps_before_it_are_not(tmp_path):
    state = SetupState(MODE_SETUP, "no_config", TOKEN, resolver=lambda host, port: ["192.168.1.1"],
                       client_factory=demo_client)
    app = create_app(STANDIN, state_dir=tmp_path, hosts=["testserver"], auth=AuthState.for_directory(tmp_path, STANDIN),
                     setup=state, read_only=True)
    client = TestClient(app)
    headers = {"Origin": origin_of(client), "X-Setup-Token": TOKEN}
    assert client.post("/api/v1/setup/draft", json={"url": "https://192.168.1.1", "api_key": "k" * 20},
                       headers=headers).status_code == 200
    assert client.post("/api/v1/setup/connection", headers=headers).json()["ok"] is True
    response = client.post("/api/v1/setup/finish", json={"username": "ada", "password": "a long enough password"},
                           headers=headers)
    assert response.status_code == 403 and response.json()["error"] == "read_only"
    assert not (tmp_path / ".env").exists() and state.mode == MODE_SETUP


def test_a_wrong_token_is_still_a_401_not_a_read_only_answer(tmp_path):
    state = SetupState(MODE_SETUP, "no_config", TOKEN)
    app = create_app(STANDIN, state_dir=tmp_path, hosts=["testserver"], auth=AuthState.for_directory(tmp_path, STANDIN),
                     setup=state, read_only=True)
    client = TestClient(app)
    response = client.post("/api/v1/setup/finish", json={}, headers={"Origin": origin_of(client),
                                                                      "X-Setup-Token": "wrong-wrong-wrong-1"})
    assert response.status_code == 401


def test_the_option_reaches_the_server(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(runner.uvicorn, "run", lambda app, **kwargs: calls.append(app))
    (tmp_path / ".env").write_text("UNIFI_URL=https://controller.example\nUNIFI_API_KEY=the-api-key-0123456789\n")
    auth_for(tmp_path)
    assert cli.main(["serve", "--read-only", "--data-dir", str(tmp_path)]) == 0
    assert cli.main(["serve", "--data-dir", str(tmp_path)]) == 0
    assert [app.state.read_only for app in calls] == [True, False]
