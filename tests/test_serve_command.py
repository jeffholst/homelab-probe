"""`hlp serve`: the command that starts the web server (issues #183 and #184)."""

import re
from pathlib import Path

import pytest

from homelab_probe import cli, commands
from homelab_probe.accounts import AccountStore

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

from fastapi.testclient import TestClient  # noqa: E402
from server_support import PASSWORD, origin_of  # noqa: E402

from homelab_probe.server import runner  # noqa: E402


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("UNIFI_URL", "https://controller.example")
    monkeypatch.setenv("UNIFI_API_KEY", "the-api-key-0123456789")


@pytest.fixture
def data(tmp_path):
    """A data directory with an administrator, which the server insists on."""
    directory = tmp_path / "data"
    AccountStore(directory).add("alice", "admin", PASSWORD)
    return directory


@pytest.fixture(autouse=True)
def served(monkeypatch):
    """Stand in for uvicorn for every test of this module (so no test can ever open a socket): record what it would
    have been asked to run."""
    calls = []
    monkeypatch.setattr(runner.uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    return calls


def test_it_starts_uvicorn_on_loopback_with_nothing_extra_exposed(configured, served, data, capsys):
    assert cli.main(["serve", "--data-dir", str(data)]) == 0
    ((app, kwargs),) = served
    assert kwargs == {"host": "127.0.0.1", "port": 8787, "log_config": None, "access_log": False,
                      "server_header": False, "date_header": False, "proxy_headers": False}
    assert app.state.demo is False and app.state.state_dir == data and app.state.settings_path is None
    err = capsys.readouterr().err
    assert "Serving on http://127.0.0.1:8787" in err and "Log in with an account made by `hlp web-user`" in err
    assert "the-api-key" not in err


def test_the_options_reach_the_server(configured, served, data, tmp_path, capsys):
    settings = tmp_path / "hlp.toml"
    settings.write_text("")
    assert cli.main(["serve", "--host", "::1", "--port", "9000", "--data-dir", str(data), "--config",
                     str(settings)]) == 0
    ((app, kwargs),) = served
    assert (kwargs["host"], kwargs["port"]) == ("::1", 9000)
    assert app.state.state_dir == data and app.state.settings_path == settings
    assert "Serving on http://[::1]:9000" in capsys.readouterr().err


def test_without_an_administrator_it_refuses_to_start_and_says_how_to_make_one(configured, served, tmp_path, capsys):
    empty = tmp_path / "empty"
    assert cli.main(["serve", "--data-dir", str(empty)]) == 3 and served == []
    err = capsys.readouterr().err
    assert "no administrator to log in as" in err and f"hlp web-user add NAME --role admin --data-dir {empty}" in err


def test_a_viewer_or_a_disabled_administrator_is_not_enough(configured, served, tmp_path, capsys):
    import json

    viewers = tmp_path / "viewers"
    AccountStore(viewers).add("bob", "viewer", PASSWORD)
    assert cli.main(["serve", "--data-dir", str(viewers)]) == 3
    disabled = tmp_path / "disabled"
    AccountStore(disabled).add("alice", "admin", PASSWORD)
    path = disabled / "users.json"
    document = json.loads(path.read_text())
    document["users"][0]["disabled"] = True                           # as a hand-edited file could be
    path.write_text(json.dumps(document))
    assert cli.main(["serve", "--data-dir", str(disabled)]) == 3
    assert capsys.readouterr().err.count("no administrator to log in as") == 2 and served == []


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::", "example.com", ""])
def test_any_other_address_is_a_usage_error_until_login_is_open_to_the_network(host, configured, served, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["serve", "--host", host])
    assert caught.value.code == 64 and "loopback" in capsys.readouterr().err and served == []


@pytest.mark.parametrize("host", ["127.0.0.1", "127.8.9.10", "::1", "localhost", "LOCALHOST"])
def test_loopback_addresses_are_accepted(host, configured, served, data):
    assert cli.main(["serve", "--host", host, "--data-dir", str(data)]) == 0 and served[0][1]["host"] == host


@pytest.mark.parametrize("port", ["0", "65536", "-1", "http", "80.5", ""])
def test_a_bad_port_is_a_usage_error(port, configured, served, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["serve", "--port", port])
    assert caught.value.code == 64 and "invalid port" in capsys.readouterr().err


def test_the_runner_refuses_a_non_loopback_host_by_itself_too(configured, served, data):
    with pytest.raises(ValueError, match="loopback"):
        runner.run(commands.Config(controller_url="https://c.example", api_key="k"), "0.0.0.0", 1, None, data)
    assert served == []


def test_no_proxy_header_is_trusted_so_a_local_process_cannot_pick_the_address_the_throttle_sees(
        configured, served, data):
    cli.main(["serve", "--data-dir", str(data)])
    assert served[0][1]["proxy_headers"] is False


def test_the_demo_has_a_throwaway_administrator_whose_password_is_shown_once(served, capsys, monkeypatch):
    monkeypatch.delenv("UNIFI_URL", raising=False)
    seen = {}

    def fake_run(app, **kwargs):
        seen["directory"] = app.state.state_dir
        match = re.search(r"password (\S+)", capsys.readouterr().err)
        seen["password"] = match.group(1) if match else ""
        client = TestClient(app, base_url="http://localhost")
        seen["login"] = client.post("/api/v1/auth/login", json={"username": "demo", "password": seen["password"]},
                                    headers={"Origin": origin_of(client)}).status_code
        seen["unknown"] = client.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD},
                                      headers={"Origin": origin_of(client)}).status_code
        seen["demo"] = app.state.demo
        served.append(app)

    monkeypatch.setattr(runner.uvicorn, "run", fake_run)
    assert cli.main(["--demo", "serve"]) == 0
    assert seen["login"] == 200 and seen["unknown"] == 401 and seen["demo"] is True
    assert len(seen["password"]) >= 12 and not Path(seen["directory"]).exists()      # removed when the server stops
    assert seen["password"] not in capsys.readouterr().err                           # shown once, not again


def test_the_demo_serves_the_synthetic_controller_and_needs_no_settings(served, capsys, monkeypatch):
    monkeypatch.delenv("UNIFI_URL", raising=False)
    assert cli.main(["--demo", "serve"]) == 0
    ((app, _),) = served
    assert app.state.demo is True
    client = app.state.service.client()
    assert client.info()["applicationVersion"] and client.base_url.endswith(".invalid")
    assert app.state.service.client() is not client and app.state.service.client().session is client.session
    assert "Demo login: user demo, password " in capsys.readouterr().err


def test_a_real_run_builds_a_real_client_from_the_configuration(configured, served, data):
    cli.main(["serve", "--data-dir", str(data)])
    client = served[0][0].state.service.client()
    assert client.base_url == "https://controller.example" and client.session.headers["X-API-KEY"].startswith("the-api")


def test_a_missing_settings_file_that_was_named_fails_before_the_server_starts(configured, served, tmp_path, capsys):
    assert cli.main(["serve", "--config", str(tmp_path / "missing.toml")]) == 3 and served == []
