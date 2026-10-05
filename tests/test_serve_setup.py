"""`hlp serve` without usable settings: the server starts in the setup mode instead of refusing (issue #185)."""

import os
import re

import pytest

from homelab_probe import cli
from homelab_probe.accounts import AccountStore
from homelab_probe.config import ConfigError

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

from fastapi.testclient import TestClient  # noqa: E402
from server_support import PASSWORD  # noqa: E402

from homelab_probe.server import runner  # noqa: E402

GOOD = "UNIFI_URL=https://controller.example\nUNIFI_API_KEY=the-api-key-0123456789\n"


@pytest.fixture(autouse=True)
def served(monkeypatch):
    calls = []
    monkeypatch.setattr(runner.uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    return calls


def serve(*argv):
    return cli.main(["serve", *argv])


def token_of(err):
    match = re.search(r"with this token: (\S+)", err)
    return match.group(1) if match else ""


# -- starting ---------------------------------------------------------------------------------------------------

def test_without_any_settings_the_server_starts_in_the_setup_mode_and_shows_a_token_once(served, tmp_path, capsys):
    assert serve("--data-dir", str(tmp_path)) == 0
    ((app, kwargs),) = served
    setup = app.state.setup
    err = capsys.readouterr().err
    assert setup.mode == "setup" and setup.reason == "no_config" and app.state.service is None
    assert "Not configured" in err and token_of(err) == setup.token and len(setup.token) >= 24
    assert err.count(setup.token) == 1 and "Log in with an account" not in err
    assert kwargs["host"] == "127.0.0.1"
    assert not (tmp_path / ".env").exists() and "UNIFI_URL" not in os.environ        # nothing written, nothing set


def test_each_start_makes_a_new_token(served, tmp_path):
    serve("--data-dir", str(tmp_path))
    serve("--data-dir", str(tmp_path))
    assert served[0][0].state.setup.token != served[1][0].state.setup.token


def test_an_existing_administrator_replaces_the_token(served, tmp_path, capsys):
    AccountStore(tmp_path).add("alice", "admin", PASSWORD)
    assert serve("--data-dir", str(tmp_path)) == 0
    err = capsys.readouterr().err
    assert "log in as an administrator" in err and served[0][0].state.setup.token not in err


def test_a_damaged_accounts_file_stops_the_setup_from_starting(served, tmp_path, capsys):
    (tmp_path / "users.json").write_text("not json")
    assert serve("--data-dir", str(tmp_path)) == 3 and served == []


def test_public_controllers_are_refused_unless_the_operator_opts_in(served, tmp_path):
    serve("--data-dir", str(tmp_path))
    serve("--allow-public-controller", "--data-dir", str(tmp_path))
    assert served[0][0].state.setup.allow_public is False and served[1][0].state.setup.allow_public is True


def test_no_administrator_is_needed_to_start_the_setup(served, tmp_path):
    assert serve("--data-dir", str(tmp_path / "new")) == 0 and len(served) == 1


def test_a_token_chosen_by_the_operator_is_used_and_not_repeated(served, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HLP_SETUP_TOKEN", "an-operator-token-0123456789")
    assert serve("--data-dir", str(tmp_path)) == 0
    err = capsys.readouterr().err
    assert served[0][0].state.setup.token == "an-operator-token-0123456789"
    assert "an-operator-token" not in err and "HLP_SETUP_TOKEN" in err


def test_a_token_that_is_too_short_is_a_start_up_error(served, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HLP_SETUP_TOKEN", "short")
    assert serve("--data-dir", str(tmp_path)) == 3 and served == []
    assert "HLP_SETUP_TOKEN must be at least 16 characters" in capsys.readouterr().err


@pytest.mark.parametrize("env", [
    {"UNIFI_URL": "http://controller.example", "UNIFI_API_KEY": "the-api-key-0123456789"},   # plain http
    {"UNIFI_URL": "https://controller.example"},                                               # no key
    {"UNIFI_API_KEY": "the-api-key-0123456789"},                                               # no address
    {"UNIFI_URL": "https://controller.example", "UNIFI_API_KEY": "the-api-key-0123456789", "UNIFI_TIMEOUT": "never"},
])
def test_settings_that_exist_but_are_broken_still_fail_loudly(env, served, tmp_path, capsys, monkeypatch):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert serve("--data-dir", str(tmp_path)) == 3 and served == []
    assert "ERROR:" in capsys.readouterr().err


def test_a_broken_setting_beside_nothing_configured_also_fails_loudly(served, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SESSION_IDLE_MINUTES", "nonsense")
    assert serve("--data-dir", str(tmp_path)) == 3 and served == []
    assert "SESSION_IDLE_MINUTES" in capsys.readouterr().err


def test_blank_settings_count_as_nothing_configured(served, tmp_path):
    (tmp_path / ".env").write_text("UNIFI_URL=\nUNIFI_API_KEY=\n")
    assert serve("--data-dir", str(tmp_path)) == 0 and served[0][0].state.setup is not None


def test_the_standin_keeps_the_servers_own_settings(served, tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_IDLE_MINUTES", "5")
    monkeypatch.setenv("AUDIT_LOG_FILES", "3")
    cli.main(["--timeout", "7", "--parallel", "2", "serve", "--data-dir", str(tmp_path)])
    config = served[0][0].state.config
    assert (config.session_idle_minutes, config.audit_log_files, config.timeout, config.parallel) == (5, 3, 7, 2)


# -- where the settings come from -------------------------------------------------------------------------------

def test_the_env_file_of_the_data_directory_is_read(served, tmp_path):
    (tmp_path / ".env").write_text(GOOD)
    AccountStore(tmp_path).add("alice", "admin", PASSWORD)
    assert serve("--data-dir", str(tmp_path)) == 0
    app = served[0][0]
    assert app.state.setup is None and app.state.service.client().base_url == "https://controller.example"
    assert "UNIFI_URL" not in os.environ                                           # read without touching the environment


def test_configured_settings_without_an_administrator_start_the_admin_mode(served, tmp_path, capsys):
    (tmp_path / ".env").write_text(GOOD)
    assert serve("--data-dir", str(tmp_path)) == 0 and served[0][0].state.setup.mode == "admin"
    assert "No administrator yet" in capsys.readouterr().err


def test_the_current_directory_is_not_read_when_another_data_directory_is_named(served, tmp_path, monkeypatch):
    here, data = tmp_path / "here", tmp_path / "data"
    here.mkdir()
    (here / ".env").write_text(GOOD)
    monkeypatch.chdir(here)
    assert serve("--data-dir", str(data)) == 0 and served[0][0].state.setup is not None


def test_a_named_env_file_beats_the_data_directory(served, tmp_path):
    (tmp_path / ".env").write_text("UNIFI_URL=https://wrong.example\nUNIFI_API_KEY=wrong-wrong-wrong\n")
    named = tmp_path / "named.env"
    named.write_text(GOOD)
    AccountStore(tmp_path).add("alice", "admin", PASSWORD)
    assert cli.main(["--env-file", str(named), "serve", "--data-dir", str(tmp_path)]) == 0
    assert served[0][0].state.service.client().base_url == "https://controller.example"


def test_hlp_env_names_the_file_too_and_a_missing_one_is_an_error(served, tmp_path, capsys, monkeypatch):
    named = tmp_path / "named.env"
    named.write_text(GOOD)
    AccountStore(tmp_path).add("alice", "admin", PASSWORD)
    monkeypatch.setenv("HLP_ENV", str(named))
    assert serve("--data-dir", str(tmp_path)) == 0 and served[0][0].state.setup is None
    monkeypatch.setenv("HLP_ENV", str(tmp_path / "missing.env"))
    assert serve("--data-dir", str(tmp_path)) == 3 and len(served) == 1
    assert "env file not found" in capsys.readouterr().err


def test_the_environment_beats_the_file(served, tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(GOOD)
    AccountStore(tmp_path).add("alice", "admin", PASSWORD)
    monkeypatch.setenv("UNIFI_URL", "https://from-environment.example")
    serve("--data-dir", str(tmp_path))
    assert served[0][0].state.service.client().base_url == "https://from-environment.example"


def test_a_loose_env_file_is_warned_about(served, tmp_path, capsys):
    (tmp_path / ".env").write_text(GOOD)
    (tmp_path / ".env").chmod(0o644)
    AccountStore(tmp_path).add("alice", "admin", PASSWORD)
    serve("--data-dir", str(tmp_path))
    assert "accessible to other users" in capsys.readouterr().err


# -- what the setup server answers ------------------------------------------------------------------------------

def test_the_started_app_is_the_setup_app(served, tmp_path):
    serve("--data-dir", str(tmp_path))
    app = served[0][0]
    client = TestClient(app, base_url="http://localhost")
    assert client.get("/api/v1/meta").json()["needs_setup"] is True
    assert client.get("/api/v1/unifi/sites").json()["error"] == "not_configured"
    assert client.get("/readyz").status_code == 503


def test_listening_beyond_this_machine_warns_that_the_token_and_key_would_travel_in_clear(served, tmp_path, capsys):
    serve("--data-dir", str(tmp_path), "--host", "192.168.1.5")
    err = capsys.readouterr().err
    assert "setup token and the API key you type into the setup" in err


def test_a_bad_settings_file_still_fails_before_the_server_starts(served, tmp_path, capsys):
    assert serve("--data-dir", str(tmp_path), "--config", str(tmp_path / "missing.toml")) == 3 and served == []


def test_resolve_config_is_pure(tmp_path, monkeypatch):
    before = dict(os.environ)
    (tmp_path / ".env").write_text(GOOD + "LOG_LEVEL=INFO\n")
    config, setup, problem = runner.resolve_config(None, tmp_path)
    assert setup is None and problem == "" and config.log_level == "INFO"
    assert dict(os.environ) == before
    with pytest.raises(ConfigError):
        runner.resolve_config(tmp_path / "nope.env", tmp_path)


def test_the_demo_ignores_an_implicit_settings_file_but_honors_a_named_one(served, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hlp.toml").write_text("this is [not toml")
    assert cli.main(["--demo", "serve"]) == 0 and len(served) == 1
    assert cli.main(["--demo", "serve", "--config", str(tmp_path / "hlp.toml")]) == 3 and len(served) == 1


# -- finishing through the server the command started ------------------------------------------------------------

def finish_flow(app, *, token=None):
    """Type the settings into the setup of ``app`` (its controller is the synthetic one) and finish it."""
    from homelab_probe.demo import demo_client
    from homelab_probe.demo.session import DemoSession
    from homelab_probe.server.service import ControllerService

    state = app.state.setup
    state.client_factory = demo_client
    state.service_factory = lambda config: ControllerService(config, session=DemoSession())
    state.resolver = lambda host, port: ["192.168.1.1"]
    client = TestClient(app, base_url="http://localhost")
    headers = {"Origin": "http://localhost", "X-Setup-Token": token or state.token}

    def post(path, body):
        return client.post(f"/api/v1/setup/{path}", json=body, headers=headers)

    post("draft", {"url": "https://192.168.1.1", "api_key": "the-typed-key-0123456789"})
    assert post("connection", {}).json()["ok"] is True
    return client, post


def test_finishing_in_a_served_setup_reloads_the_configuration_the_way_the_server_read_it(served, tmp_path):
    cli.main(["--timeout", "7", "--parallel", "2", "serve", "--data-dir", str(tmp_path)])
    app = served[0][0]
    client, post = finish_flow(app)
    assert post("finish", {"username": "ada", "password": "a long enough password"}).json()["finished"] is True
    assert (tmp_path / ".env").exists()
    config = app.state.config
    assert (config.controller_url, config.timeout, config.parallel) == ("https://192.168.1.1", 7, 2)


def test_a_finish_that_leaves_the_settings_missing_is_a_reload_error(served, tmp_path, monkeypatch):
    from homelab_probe.server import wizard

    cli.main(["serve", "--data-dir", str(tmp_path)])
    app = served[0][0]
    monkeypatch.setattr(wizard, "_write_settings", lambda state, directory, written: None)     # nothing is written
    _, post = finish_flow(app)
    response = post("finish", {"username": "ada", "password": "a long enough password"})
    assert response.status_code == 500 and response.json()["error"] == "reload_failed"
    assert app.state.setup.mode == "setup"


def test_the_admin_mode_of_a_served_configuration_finishes_with_the_command_line_overrides(served, tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(GOOD)
    cli.main(["--timeout", "9", "serve", "--data-dir", str(tmp_path)])
    app = served[0][0]
    state = app.state.setup
    client = TestClient(app, base_url="http://localhost")
    body = {"username": "ada", "password": "a long enough password"}
    headers = {"Origin": "http://localhost", "X-Setup-Token": state.token}
    from homelab_probe.demo.session import DemoSession
    from homelab_probe.server.service import ControllerService

    state.service_factory = lambda config: ControllerService(config, session=DemoSession())
    assert client.post("/api/v1/setup/finish", json=body, headers=headers).json()["admin_created"] is True
    assert app.state.config.timeout == 9 and app.state.service is not None
