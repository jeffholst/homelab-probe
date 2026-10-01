"""The API key is a credential: the file that holds it, the URL it travels to, and every place
it could be printed."""

import os
import sys
from pathlib import Path

import pytest
import requests

from unifi_sentinel import cli
from unifi_sentinel.client import UniFiAPIError, UniFiClient
from unifi_sentinel.config import (Config, ConfigError, env_file_warning, load_config,
                                   validate_controller_url)

KEY = "sekret-key-0123456789"
GOOD = f"CONTROLLER_URL=https://controller.example\nAPI_KEY={KEY}\n"

posix_only = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX file modes")


def write(path: Path, text: str = GOOD, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


# -- the permissions of the .env file ---------------------------------------------

@posix_only
@pytest.mark.parametrize("mode", [0o600, 0o400, 0o700, 0o200])
def test_a_private_env_file_gives_no_warning(tmp_path, mode):
    assert env_file_warning(write(tmp_path / ".env", mode=mode)) is None


@posix_only
@pytest.mark.parametrize("mode", [0o640, 0o644, 0o604, 0o660, 0o666, 0o610, 0o601])
def test_an_env_file_others_can_reach_gives_a_warning_with_the_fix(tmp_path, mode):
    path = write(tmp_path / ".env", mode=mode)
    message = env_file_warning(path)
    assert message is not None
    assert str(path) in message and f"{mode:04o}" in message and "chmod 600" in message
    assert KEY not in message


@posix_only
def test_the_chmod_suggestion_quotes_an_awkward_path(tmp_path):
    path = write(tmp_path / "my lab" / ".env", mode=0o644)
    assert f"chmod 600 '{path}'" in env_file_warning(path)


@posix_only
def test_a_symlink_is_judged_by_the_file_it_points_to(tmp_path):
    private = write(tmp_path / "private.env", mode=0o600)
    open_file = write(tmp_path / "open.env", mode=0o644)
    (tmp_path / "link-private").symlink_to(private)
    (tmp_path / "link-open").symlink_to(open_file)
    assert env_file_warning(tmp_path / "link-private") is None
    assert env_file_warning(tmp_path / "link-open") is not None


def test_a_missing_file_gives_no_warning(tmp_path):
    assert env_file_warning(tmp_path / "missing.env") is None


@posix_only
def test_nothing_is_checked_where_modes_mean_little(tmp_path, monkeypatch):
    path = write(tmp_path / ".env", mode=0o666)
    monkeypatch.setattr(sys, "platform", "win32")
    assert env_file_warning(path) is None


@posix_only
def test_load_config_collects_the_warning_for_the_file_that_was_read(tmp_path):
    write(Path.cwd() / ".env", mode=0o644)
    cfg = load_config()
    assert len(cfg.warnings) == 1 and "chmod 600" in cfg.warnings[0]
    write(Path.cwd() / ".env", mode=0o600)
    assert load_config().warnings == ()


@posix_only
def test_an_explicit_env_file_is_checked_too(tmp_path):
    path = write(tmp_path / "lab.env", mode=0o640)
    assert len(load_config(path).warnings) == 1


def test_no_file_means_nothing_to_check(monkeypatch):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("API_KEY", KEY)
    assert load_config().warnings == ()


@posix_only
def test_cli_prints_one_warning_to_stderr_and_carries_on(monkeypatch, tmp_path, fake_client, capsys):
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    path = write(tmp_path / "lab.env", mode=0o644)
    assert cli.main(["--env-file", str(path), "info"]) == 0
    captured = capsys.readouterr()
    assert captured.err.count("accessible to other users") == 1 and "chmod 600" in captured.err
    assert KEY not in captured.out + captured.err and "Site:" in captured.out
    path.chmod(0o600)
    assert cli.main(["--env-file", str(path), "info"]) == 0
    assert "accessible to other users" not in capsys.readouterr().err


# -- the controller URL -----------------------------------------------------------

@pytest.mark.parametrize("given, expected", [
    ("https://controller.example", "https://controller.example"),
    ("https://controller.example/", "https://controller.example"),
    ("  https://controller.example:8443//  ", "https://controller.example:8443"),
    ("HTTPS://Controller.example", "HTTPS://Controller.example"),
    ("https://192.0.2.1:443", "https://192.0.2.1:443"),
    ("https://[2001:db8::1]:443", "https://[2001:db8::1]:443"),
    ("https://proxy.example/unifi/", "https://proxy.example/unifi"),
])
def test_a_good_https_url_is_accepted_and_normalised(given, expected):
    assert validate_controller_url(given) == expected


@pytest.mark.parametrize("given", [
    "http://controller.example", "HTTP://controller.example:8080", "http://192.0.2.1",
])
def test_http_is_refused_unless_the_opt_in_is_given(given):
    with pytest.raises(ConfigError, match="clear text") as caught:
        validate_controller_url(given)
    assert "ALLOW_INSECURE_HTTP" in str(caught.value) and "https://" in str(caught.value)
    assert validate_controller_url(given, allow_http=True) == given.rstrip("/")


@pytest.mark.parametrize("given", [
    "", "   ", None, "controller.example", "controller.example:443", "192.0.2.1:443",
    "//controller.example", "https://", "https:///path", "ftp://controller.example",
    "file:///etc/passwd", "javascript:alert(1)",
])
def test_a_url_without_a_scheme_or_host_is_refused(given):
    with pytest.raises(ConfigError, match="scheme and a host|not a valid URL"):
        validate_controller_url(given)


@pytest.mark.parametrize("given", ["https://controller.example:99999", "https://controller.example:abc"])
def test_a_bad_port_is_refused(given):
    with pytest.raises(ConfigError, match="not a valid URL"):
        validate_controller_url(given)


@pytest.mark.parametrize("given", [
    "https://admin:hunter2@controller.example", "https://admin@controller.example",
    "https://:hunter2@controller.example",
])
def test_a_url_with_a_login_is_refused_and_never_echoed(given):
    with pytest.raises(ConfigError, match="user name or password") as caught:
        validate_controller_url(given)
    assert "hunter2" not in str(caught.value) and "admin" not in str(caught.value)


@pytest.mark.parametrize("given", [
    "https://host\n.example", "https://ho st.example", "https://host.example\t/x",
    "https://host.example/a b", "https://good.example\\@evil.example", "https://host.example\x1b[0m",
    "https://host\x85.example",
])
def test_whitespace_backslashes_and_control_characters_are_refused_here_not_later(given):
    """They used to pass and only failed inside the HTTP library, as a confusing connection error."""
    with pytest.raises(ConfigError, match="spaces, backslashes or control characters"):
        validate_controller_url(given)


@pytest.mark.parametrize("given", ["https://controller.example?x=1", "https://controller.example/#top"])
def test_a_query_or_fragment_is_refused(given):
    with pytest.raises(ConfigError, match="query"):
        validate_controller_url(given)


def test_load_config_refuses_http_and_allows_it_with_the_opt_in(monkeypatch):
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("CONTROLLER_URL", "http://192.0.2.1:8080/")
    with pytest.raises(ConfigError, match="clear text"):
        load_config()
    monkeypatch.setenv("ALLOW_INSECURE_HTTP", "true")
    assert load_config().controller_url == "http://192.0.2.1:8080"


def test_using_the_opt_in_is_not_silent(monkeypatch):
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("CONTROLLER_URL", "http://192.0.2.1")
    monkeypatch.setenv("ALLOW_INSECURE_HTTP", "1")
    cfg = load_config()
    assert len(cfg.warnings) == 1 and "clear text" in cfg.warnings[0] and KEY not in cfg.warnings[0]
    monkeypatch.setenv("CONTROLLER_URL", "https://192.0.2.1")
    assert load_config().warnings == ()


def test_the_opt_in_can_come_from_the_env_file(tmp_path):
    path = write(tmp_path / "lab.env", GOOD.replace("https", "http") + "ALLOW_INSECURE_HTTP=yes\n")
    assert load_config(path).controller_url == "http://controller.example"


def test_the_opt_in_value_is_validated(tmp_path):
    bad = write(tmp_path / "bad.env", GOOD.replace("https", "http") + "ALLOW_INSECURE_HTTP=maybe\n")
    with pytest.raises(ConfigError, match="ALLOW_INSECURE_HTTP must be one of"):
        load_config(bad)


def test_the_opt_in_does_nothing_for_https_and_is_off_by_default(monkeypatch):
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("ALLOW_INSECURE_HTTP", "no")
    assert load_config().controller_url == "https://controller.example"


def test_cli_reports_a_bad_url_with_the_config_exit_code(monkeypatch, tmp_path, capsys):
    path = write(tmp_path / "bad.env", GOOD.replace("https://controller.example", "http://controller.example"))
    assert cli.main(["--env-file", str(path), "info"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "clear text" in err and KEY not in err


# -- the key never appears --------------------------------------------------------

def test_repr_of_the_config_hides_the_key():
    cfg = Config("https://controller.example", KEY)
    assert KEY not in repr(cfg) and KEY not in str(cfg)
    assert cfg.api_key == KEY


class Response:
    def __init__(self, status, text, body=None):
        self.status_code, self.text, self.ok, self._body = status, text, 200 <= status < 300, body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class Stub:
    """A session that fails in one chosen way, echoing the key where a server or proxy might."""

    def __init__(self, headers, outcome):
        self.headers, self.outcome = headers, outcome

    def _answer(self, url):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def get(self, url, **kwargs):
        return self._answer(url)

    def post(self, url, **kwargs):
        return self._answer(url)


FAILURES = {
    "401": Response(401, f"unauthorized {KEY}"),
    "403": Response(403, f"forbidden: header X-API-KEY: {KEY}"),
    "500": Response(500, f"error, request headers were {{'X-API-KEY': '{KEY}'}}"),
    "502 html": Response(502, f"<html>bad gateway for {KEY}</html>"),
    "not json": Response(200, f"<html>{KEY}</html>"),
    "tls": requests.exceptions.SSLError(f"certificate verify failed for {KEY}"),
    "connection": requests.exceptions.ConnectionError(f"could not connect, key {KEY}"),
    "timeout": requests.exceptions.ReadTimeout(f"timed out; header {KEY}"),
}


def stubbed_client(outcome):
    client = UniFiClient("https://controller.example", KEY)
    client.session = Stub(client.session.headers, outcome)
    return client


@pytest.mark.parametrize("name", FAILURES)
def test_no_failure_message_contains_the_key(name):
    client = stubbed_client(FAILURES[name])
    for call in (client.info, lambda: client.legacy_stat("default", "sta"),
                 lambda: client.system_log("default", {"pageNumber": 0})):
        with pytest.raises(UniFiAPIError) as caught:
            call()
        assert KEY not in str(caught.value), name
        assert KEY not in repr(caught.value), name


def test_a_long_echoed_body_is_redacted_before_it_is_cut():
    """The key must not survive by being split across the 500-character cut."""
    body = "x" * 490 + KEY
    with pytest.raises(UniFiAPIError) as caught:
        stubbed_client(Response(500, body)).info()
    assert KEY not in str(caught.value) and KEY[:8] not in str(caught.value)


def test_the_redaction_does_not_touch_ordinary_text():
    with pytest.raises(UniFiAPIError, match="HTTP 500 for .*: plain failure"):
        stubbed_client(Response(500, "plain failure")).info()


@pytest.mark.parametrize("name", FAILURES)
def test_no_command_prints_the_key_on_any_failure_path(monkeypatch, capsys, name):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setattr(cli.UniFiClient, "from_config",
                        classmethod(lambda cls, cfg: stubbed_client(FAILURES[name])))
    for argv in (["info"], ["query", "devices"], ["events"], ["diagnose"], ["client", "x"]):
        code = cli.main(argv)
        captured = capsys.readouterr()
        assert KEY not in captured.out + captured.err, (name, argv)
        assert code == cli.EXIT_ERROR, (name, argv, code)


@pytest.mark.parametrize("bad", [
    {"CONTROLLER_URL": "controller.example"},
    {"CONTROLLER_URL": "https://controller.example", "VERIFY_SSL": "sometimes"},
    {"CONTROLLER_URL": "https://controller.example", "SITE_ID": "a/b"},
    {"CONTROLLER_URL": "http://controller.example"},
    {"CONTROLLER_URL": f"https://admin:{KEY}@controller.example"},
])
def test_configuration_errors_do_not_print_the_key(monkeypatch, capsys, bad):
    for name, value in {"API_KEY": KEY, **bad}.items():
        monkeypatch.setenv(name, value)
    assert cli.main(["info"]) == cli.EXIT_ERROR
    captured = capsys.readouterr()
    assert KEY not in captured.out + captured.err and "ERROR:" in captured.err


def test_a_placeholder_key_is_refused_without_printing_anything_secret(monkeypatch, capsys):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("API_KEY", "your-api-key-here")
    assert cli.main(["info"]) == cli.EXIT_ERROR
    assert "placeholder" in capsys.readouterr().err
