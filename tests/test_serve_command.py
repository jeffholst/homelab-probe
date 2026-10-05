"""`hlp serve`: the command that starts the web server (issue #183)."""

from pathlib import Path

import pytest

from homelab_probe import cli, commands

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

from homelab_probe.server import runner  # noqa: E402


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("UNIFI_URL", "https://controller.example")
    monkeypatch.setenv("UNIFI_API_KEY", "the-api-key-0123456789")


@pytest.fixture(autouse=True)
def served(monkeypatch):
    """Stand in for uvicorn for every test of this module (so no test can ever open a socket): record what it would
    have been asked to run."""
    calls = []
    monkeypatch.setattr(runner.uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    return calls


def test_it_starts_uvicorn_on_loopback_with_nothing_extra_exposed(configured, served, capsys):
    assert cli.main(["serve"]) == 0
    ((app, kwargs),) = served
    assert kwargs == {"host": "127.0.0.1", "port": 8787, "log_config": None, "access_log": False,
                      "server_header": False, "date_header": False}
    assert app.state.demo is False and str(app.state.state_dir) == "." and app.state.settings_path is None
    err = capsys.readouterr().err
    assert "Serving on http://127.0.0.1:8787" in err and "login is not built in yet" in err
    assert "the-api-key" not in err


def test_the_options_reach_the_server(configured, served, tmp_path, capsys):
    settings = tmp_path / "hlp.toml"
    settings.write_text("")
    assert cli.main(["serve", "--host", "::1", "--port", "9000", "--data-dir", str(tmp_path), "--config",
                     str(settings)]) == 0
    ((app, kwargs),) = served
    assert (kwargs["host"], kwargs["port"]) == ("::1", 9000)
    assert app.state.state_dir == tmp_path and app.state.settings_path == settings
    assert "Serving on http://[::1]:9000" in capsys.readouterr().err


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::", "example.com", ""])
def test_any_other_address_is_a_usage_error_until_login_exists(host, configured, served, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["serve", "--host", host])
    assert caught.value.code == 64 and "loopback" in capsys.readouterr().err and served == []


@pytest.mark.parametrize("host", ["127.0.0.1", "127.8.9.10", "::1", "localhost", "LOCALHOST"])
def test_loopback_addresses_are_accepted(host, configured, served):
    assert cli.main(["serve", "--host", host]) == 0 and served[0][1]["host"] == host


@pytest.mark.parametrize("port", ["0", "65536", "-1", "http", "80.5", ""])
def test_a_bad_port_is_a_usage_error(port, configured, served, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["serve", "--port", port])
    assert caught.value.code == 64 and "invalid port" in capsys.readouterr().err


def test_the_runner_refuses_a_non_loopback_host_by_itself_too(configured, served):
    with pytest.raises(ValueError, match="loopback"):
        runner.run(commands.Config(controller_url="https://c.example", api_key="k"), "0.0.0.0", 1, None,
                   Path("."), lambda: None)
    assert served == []


def test_the_demo_serves_the_synthetic_controller_and_needs_no_settings(served, capsys, monkeypatch):
    monkeypatch.delenv("UNIFI_URL", raising=False)
    assert cli.main(["--demo", "serve"]) == 0
    ((app, _),) = served
    assert app.state.demo is True
    client = app.state.service.client()
    assert client.info()["applicationVersion"] and client.base_url.endswith(".invalid")
    assert app.state.service.client() is not client and app.state.service.client().session is client.session


def test_a_real_run_builds_a_real_client_from_the_configuration(configured, served):
    cli.main(["serve"])
    client = served[0][0].state.service.client()
    assert client.base_url == "https://controller.example" and client.session.headers["X-API-KEY"].startswith("the-api")


def test_a_missing_settings_file_that_was_named_fails_before_the_server_starts(configured, served, tmp_path, capsys):
    assert cli.main(["serve", "--config", str(tmp_path / "missing.toml")]) == 3 and served == []
