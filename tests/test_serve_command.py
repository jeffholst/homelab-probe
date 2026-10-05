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


@pytest.mark.parametrize("host", ["0.0.0.0", "::", ""])
def test_listening_on_every_address_needs_an_allowed_host(host, configured, served, data, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["serve", "--host", host, "--data-dir", str(data)])
    err = capsys.readouterr().err
    assert caught.value.code == 64 and "--allowed-host" in err and served == []
    assert cli.main(["serve", "--host", host, "--allowed-host", "hlp.lan", "--data-dir", str(data)]) == 0
    assert served[0][1]["host"] == host


@pytest.mark.parametrize("host", ["192.168.1.5", "example.com", "fd00::5"])
def test_a_specific_address_can_be_bound_and_is_one_of_the_names_it_answers_to(host, configured, served, data, capsys):
    assert cli.main(["serve", "--host", host, "--data-dir", str(data)]) == 0
    app = served[0][0]
    shown = f"[{host}]" if ":" in host else host
    assert TestClient(app, base_url=f"http://{shown}:8787").get("/healthz").status_code == 200
    assert TestClient(app, base_url="http://evil.example").get("/healthz").status_code == 400
    err = capsys.readouterr().err
    assert "can be reached from other machines" in err and "clear text" in err


def test_loopback_binds_say_nothing_about_other_machines(configured, served, data, capsys):
    cli.main(["serve", "--data-dir", str(data)])
    assert "other machines" not in capsys.readouterr().err


def test_the_allowed_hosts_are_the_only_other_names_the_server_answers_to(configured, served, data):
    cli.main(["serve", "--host", "0.0.0.0", "--allowed-host", "HLP.lan", "--allowed-host", "192.168.1.5:9000",
              "--allowed-host", "[fd00::5]", "--data-dir", str(data)])
    app = served[0][0]
    for name in ("hlp.lan", "192.168.1.5", "[fd00::5]", "localhost", "127.0.0.1"):
        assert TestClient(app, base_url=f"http://{name}").get("/healthz").status_code == 200, name
    for name in ("evil.example", "hlp.lan.evil.example", "0.0.0.0", "192.168.1.6", "other.lan"):
        assert TestClient(app, base_url=f"http://{name}").get("/healthz").status_code == 400, name


@pytest.mark.parametrize("value", ["hlp.lan", "HLP.LAN", "192.168.1.5", "192.168.1.5:8787", "[fd00::5]",
                                   "[fd00::5]:8787", "a-b.example.com", "localhost"])
def test_good_allowed_hosts_are_accepted(value, configured, served, data):
    assert cli.main(["serve", "--allowed-host", value, "--data-dir", str(data)]) == 0


@pytest.mark.parametrize("value", ["*", "*.lan", "", " ", "http://hlp.lan", "hlp.lan/path", "a b", "under_score.lan",
                                   "hlp.lan:99999x", "[fd00::5", "[not-an-address]", "bad-.lan", "x" * 300,
                                   "hlp.lan?x=1", "user@hlp.lan"])
def test_hosts_that_are_wildcards_or_not_hosts_are_usage_errors(value, configured, served, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["serve", "--allowed-host", value])
    assert caught.value.code == 64 and "not a host name or address" in capsys.readouterr().err and served == []


def test_no_proxy_is_believed_unless_one_is_named(configured, served, data):
    cli.main(["serve", "--data-dir", str(data)])
    kwargs = served[0][1]
    assert kwargs["proxy_headers"] is False and "forwarded_allow_ips" not in kwargs


@pytest.mark.parametrize("value, shown", [("127.0.0.1", "127.0.0.1"), ("10.0.0.0/8, ::1", "10.0.0.0/8,::1"),
                                          ("192.168.1.2,192.168.1.3", "192.168.1.2,192.168.1.3")])
def test_named_proxies_are_believed_and_only_those(value, shown, configured, served, data, capsys):
    assert cli.main(["serve", "--forwarded-allow-ips", value, "--data-dir", str(data)]) == 0
    kwargs = served[0][1]
    assert kwargs["proxy_headers"] is True and kwargs["forwarded_allow_ips"] == shown
    assert f"Believing X-Forwarded-For and X-Forwarded-Proto from: {shown}" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["*", "", ",", "proxy.lan", "10.0.0.300", "10.0.0.0/40", "1.2.3.4;5.6.7.8"])
def test_trusting_everyone_or_a_name_that_is_not_an_address_is_a_usage_error(value, configured, served, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["serve", "--forwarded-allow-ips", value])
    assert caught.value.code == 64 and "--forwarded-allow-ips" in capsys.readouterr().err and served == []


@pytest.mark.parametrize("host", ["127.0.0.1", "127.8.9.10", "::1", "localhost", "LOCALHOST"])
def test_loopback_addresses_are_accepted(host, configured, served, data):
    assert cli.main(["serve", "--host", host, "--data-dir", str(data)]) == 0 and served[0][1]["host"] == host


@pytest.mark.parametrize("port", ["0", "65536", "-1", "http", "80.5", ""])
def test_a_bad_port_is_a_usage_error(port, configured, served, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["serve", "--port", port])
    assert caught.value.code == 64 and "invalid port" in capsys.readouterr().err


def test_the_runner_refuses_to_listen_on_every_address_without_an_allowed_host_by_itself_too(configured, served, data):
    config = commands.Config(controller_url="https://c.example", api_key="k")
    with pytest.raises(ValueError, match="--allowed-host"):
        runner.run(config, "0.0.0.0", 1, None, data)
    assert served == []
    runner.run(config, "0.0.0.0", 1, None, data, allowed=["hlp.lan"])
    assert len(served) == 1


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


@pytest.mark.parametrize("content", ["not json", '{"version": 9, "users": []}', '{"version": 1, "users": [5]}'])
def test_a_damaged_accounts_file_is_a_start_up_error_not_a_traceback(content, configured, served, tmp_path, capsys):
    directory = tmp_path / "data"
    directory.mkdir()
    (directory / "users.json").write_text(content)
    assert cli.main(["serve", "--data-dir", str(directory)]) == 3 and served == []
    err = capsys.readouterr().err
    assert err.startswith("Serving on") or "ERROR:" in err
    assert "Traceback" not in err and "ERROR:" in err


@pytest.mark.parametrize("host", ["*", "*.lan", "a b", "http://x"])
def test_a_host_that_is_not_a_name_or_an_address_is_a_usage_error(host, configured, served, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["serve", "--host", host])
    assert caught.value.code == 64 and "not a host name or address" in capsys.readouterr().err and served == []


@pytest.mark.parametrize("value", ["0.0.0.0/0", "::/0", "10.0.0.1,0.0.0.0/0"])
def test_a_network_that_means_everyone_is_not_a_proxy_either(value, configured, served, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["serve", "--forwarded-allow-ips", value])
    assert caught.value.code == 64 and "every client" in capsys.readouterr().err and served == []


def test_the_runner_refuses_a_wildcard_allowed_host_for_a_caller_that_skips_the_command_line(configured, served, data):
    config = commands.Config(controller_url="https://c.example", api_key="k")
    for allowed in (["*"], ["*.lan"]):
        with pytest.raises(ValueError, match="not a host name or address"):
            runner.run(config, "127.0.0.1", 1, None, data, allowed=allowed)
    assert served == []
