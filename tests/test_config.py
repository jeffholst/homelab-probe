import os
import stat
import sys
from pathlib import Path

import pytest

from unifi_sentinel import cli
from unifi_sentinel.client import UniFiClient
from unifi_sentinel.config import (ConfigError, ENV_FILE_VAR, MAX_SITE_LENGTH, find_env_file, load_config,
                                   parse_bool, validate_site)
from unifi_sentinel.settings import DEFAULT_FILENAME, DiagnoseSettings, load_settings

GOOD = "CONTROLLER_URL=https://controller.example\nAPI_KEY=file-key\n"


def write(path: Path, text: str = GOOD) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# -- the isolation fixture -------------------------------------------------------

def test_every_test_starts_in_an_empty_directory_with_no_configuration():
    assert list(Path.cwd().iterdir()) == []
    assert Path.cwd().name == "cwd"
    for name in ("CONTROLLER_URL", "API_KEY", "SITE_ID", "VERIFY_SSL", ENV_FILE_VAR):
        assert name not in os.environ
    assert load_settings() == DiagnoseSettings()                       # no settings file in the working directory
    assert find_env_file() is None


def test_files_in_the_developers_directory_do_not_reach_the_tests(monkeypatch, tmp_path):
    """The failure the fixture prevents: a real .env and settings file next to the code."""
    real = tmp_path / "developer-checkout"
    write(real / ".env", GOOD + "SITE_ID=real-site\nVERIFY_SSL=false\n")
    write(real / DEFAULT_FILENAME, "[thresholds]\nresource_warn_pct = 1\n")
    # the fixture's working directory is a different, empty one
    assert find_env_file() is None and load_settings().resource_warn_pct == 90
    monkeypatch.chdir(real)                                             # only an explicit chdir makes them visible
    assert find_env_file() == real / ".env" and load_settings().resource_warn_pct == 1


# -- where the .env file is looked for ------------------------------------------

def test_the_env_file_in_the_working_directory_is_read(monkeypatch, tmp_path):
    write(Path.cwd() / ".env", GOOD + "SITE_ID=lab\nVERIFY_SSL=no\n")
    cfg = load_config()
    assert (cfg.controller_url, cfg.api_key, cfg.site, cfg.verify_ssl) == (
        "https://controller.example", "file-key", "lab", False)


def test_an_installed_copy_finds_the_env_file_in_the_directory_it_is_run_from():
    """The bug: the search started at the package's own directory, so only a checkout worked."""
    import unifi_sentinel.config as config_module
    package_dir = Path(config_module.__file__).resolve().parent
    assert Path.cwd().resolve() != package_dir and package_dir not in Path.cwd().resolve().parents
    write(Path.cwd() / ".env")
    assert load_config().api_key == "file-key"                          # found from the working directory alone
    assert find_env_file() == Path.cwd() / ".env"


def test_parent_directories_are_not_searched(monkeypatch, tmp_path):
    write(tmp_path / "project" / ".env")
    child = tmp_path / "project" / "sub" / "deeper"
    child.mkdir(parents=True)
    monkeypatch.chdir(child)
    assert find_env_file() is None
    with pytest.raises(ConfigError, match="CONTROLLER_URL is not set"):
        load_config()


def test_explicit_file_beats_the_environment_variable_beats_the_working_directory(monkeypatch, tmp_path):
    write(Path.cwd() / ".env", "CONTROLLER_URL=https://cwd.example\nAPI_KEY=cwd-key\n")
    via_var = write(tmp_path / "via-var.env", "CONTROLLER_URL=https://var.example\nAPI_KEY=var-key\n")
    explicit = write(tmp_path / "explicit.env", "CONTROLLER_URL=https://explicit.example\nAPI_KEY=explicit-key\n")
    assert load_config().api_key == "cwd-key"
    monkeypatch.delenv("CONTROLLER_URL"), monkeypatch.delenv("API_KEY")
    monkeypatch.setenv(ENV_FILE_VAR, str(via_var))
    assert load_config().api_key == "var-key"
    for name in ("CONTROLLER_URL", "API_KEY"):                          # load_dotenv exported the previous values
        monkeypatch.delenv(name, raising=False)
    assert load_config(explicit).api_key == "explicit-key"


def test_real_environment_variables_win_over_the_file(monkeypatch):
    write(Path.cwd() / ".env", GOOD + "SITE_ID=from-file\n")
    monkeypatch.setenv("API_KEY", "from-environment")
    monkeypatch.setenv("SITE_ID", "from-env")
    cfg = load_config()
    assert (cfg.api_key, cfg.site, cfg.controller_url) == ("from-environment", "from-env", "https://controller.example")


def test_a_named_env_file_must_exist(monkeypatch, tmp_path):
    with pytest.raises(ConfigError, match=r"env file not found: .*nope\.env \(from --env-file\)"):
        load_config(tmp_path / "nope.env")
    monkeypatch.setenv(ENV_FILE_VAR, str(tmp_path / "gone.env"))
    with pytest.raises(ConfigError, match=r"gone\.env \(from UNIFI_SENTINEL_ENV\)"):
        load_config()
    monkeypatch.setenv(ENV_FILE_VAR, "   ")                                # blank counts as unset
    assert find_env_file() is None
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path)                                               # a directory is not a file


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs POSIX permissions and a non-root user")
def test_an_unreadable_env_file_is_a_clear_error(tmp_path):
    path = write(tmp_path / "locked.env")
    path.chmod(0)
    try:
        with pytest.raises(ConfigError, match=r"cannot read env file .*locked\.env"):
            load_config(path)
    finally:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_missing_and_placeholder_settings_say_where_to_put_them():
    with pytest.raises(ConfigError, match=r"--env-file"):
        load_config()
    write(Path.cwd() / ".env", "CONTROLLER_URL=https://c.example\n")
    with pytest.raises(ConfigError, match="API_KEY is not set"):
        load_config()
    write(Path.cwd() / ".env", "CONTROLLER_URL=https://c.example\nAPI_KEY=your-api-key-here\n")
    with pytest.raises(ConfigError, match="placeholder"):
        load_config()


def test_the_controller_url_has_its_trailing_slash_removed(monkeypatch):
    monkeypatch.setenv("CONTROLLER_URL", "https://c.example///")
    monkeypatch.setenv("API_KEY", "k")
    assert load_config().controller_url == "https://c.example"


# -- VERIFY_SSL ------------------------------------------------------------------

@pytest.mark.parametrize("text", ["true", "TRUE", "True", "yes", "Yes", "1", "on", "ON", "  true  ", "\ttrue\n"])
def test_verify_ssl_true_spellings(text):
    assert parse_bool("VERIFY_SSL", text) is True


@pytest.mark.parametrize("text", ["false", "FALSE", "False", "no", "No", "0", "off", "Off", "  false  ", "\tno\n"])
def test_verify_ssl_false_spellings(text):
    assert parse_bool("VERIFY_SSL", text) is False


@pytest.mark.parametrize("text", [None, "", "   ", "\n"])
def test_unset_or_blank_uses_the_default(text):
    assert parse_bool("VERIFY_SSL", text) is True and parse_bool("X", text, default=False) is False


@pytest.mark.parametrize("text", ["off-ish", "n", "y", "disabled", "enable", "2", "-1", "none", "null", "truee", "falsee"])
def test_unknown_verify_ssl_values_are_rejected_with_the_accepted_words(text):
    with pytest.raises(ConfigError) as exc:
        parse_bool("VERIFY_SSL", text)
    message = str(exc.value)
    assert "VERIFY_SSL" in message and repr(text) in message
    assert all(word in message for word in ("true", "yes", "1", "on", "false", "no", "0", "off"))


def test_load_config_rejects_a_bad_verify_ssl_and_honours_a_good_one(monkeypatch):
    monkeypatch.setenv("CONTROLLER_URL", "https://c.example")
    monkeypatch.setenv("API_KEY", "k")
    assert load_config().verify_ssl is True
    monkeypatch.setenv("VERIFY_SSL", "  Off ")
    assert load_config().verify_ssl is False
    monkeypatch.setenv("VERIFY_SSL", "maybe")
    with pytest.raises(ConfigError, match="VERIFY_SSL must be one of"):
        load_config()


# -- SITE_ID ---------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    (None, "default"), ("", "default"), ("   ", "default"), (" lab ", "lab"), ("default", "default"),
    ("My Site", "My Site"), ("Büro Zürich", "Büro Zürich"), ("site_1-b.c", "site_1-b.c"),
    ("00000000-0000-4000-8000-000000000000", "00000000-0000-4000-8000-000000000000"),
    ("x" * MAX_SITE_LENGTH, "x" * MAX_SITE_LENGTH)])
def test_valid_site_ids(text, expected):
    assert validate_site(text) == expected


@pytest.mark.parametrize("text", ["a/b", "../x", "a\\b", "a?b=1", "a#frag", "a\nb", "a\x00b", "a\tb", "a\x1bb", "a\x7fb",
                                  "x" * (MAX_SITE_LENGTH + 1)])
def test_unsafe_or_overlong_site_ids_are_rejected(text):
    with pytest.raises(ConfigError, match="SITE_ID"):
        validate_site(text)


def test_load_config_validates_site_id(monkeypatch):
    monkeypatch.setenv("CONTROLLER_URL", "https://c.example")
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("SITE_ID", "../../admin")
    with pytest.raises(ConfigError, match="SITE_ID .* cannot be part of a site name"):
        load_config()


# -- site references in URLs --------------------------------------------------------

class Recorder:
    def __init__(self):
        self.urls = []
        self.headers = {}

    def get(self, url, params=None, verify=True, timeout=None):
        self.urls.append(url)
        return type("R", (), {"status_code": 200, "ok": True, "text": "{}", "json": lambda self: {"data": []}})()

    def post(self, url, json=None, verify=True, timeout=None):
        self.urls.append(url)
        return type("R", (), {"status_code": 200, "ok": True, "text": "{}",
                              "json": lambda self: {"data": []}})()


def test_site_references_are_percent_encoded_in_url_paths():
    client = UniFiClient("https://c.example", "k")
    client.session = Recorder()
    client.legacy_stat("a b/../c?x#y", "sta")
    client.legacy_rest("a b", "networkconf")
    client.legacy_v2("é/x", "speedtest")
    client.system_log("a/b", {})
    assert client.session.urls == [
        "https://c.example/proxy/network/api/s/a%20b%2F..%2Fc%3Fx%23y/stat/sta",
        "https://c.example/proxy/network/api/s/a%20b/rest/networkconf",
        "https://c.example/proxy/network/v2/api/site/%C3%A9%2Fx/speedtest",
        "https://c.example/proxy/network/v2/api/site/a%2Fb/system-log/all"]


def test_an_ordinary_site_reference_is_unchanged():
    client = UniFiClient("https://c.example", "k")
    client.session = Recorder()
    client.legacy_stat("default", "sta")
    assert client.session.urls == ["https://c.example/proxy/network/api/s/default/stat/sta"]


# -- the command line ------------------------------------------------------------

def test_cli_env_file_option_supplies_the_settings(monkeypatch, tmp_path, fake_client, capsys):
    captured = []
    monkeypatch.setattr(cli.UniFiClient, "from_config",
                        classmethod(lambda cls, cfg: (captured.append(cfg), fake_client)[1]))
    envfile = write(tmp_path / "lab.env", GOOD + "SITE_ID=default\nVERIFY_SSL=off\n")
    assert cli.main(["--env-file", str(envfile), "info"]) == 0
    assert (captured[0].controller_url, captured[0].api_key, captured[0].verify_ssl) == (
        "https://controller.example", "file-key", False)
    assert "Site: Default" in capsys.readouterr().out


def test_cli_env_file_errors_exit_with_the_config_code(monkeypatch, tmp_path, capsys):
    assert cli.main(["--env-file", str(tmp_path / "missing.env"), "info"]) == cli.EXIT_ERROR
    assert "env file not found" in capsys.readouterr().err
    bad = write(tmp_path / "bad.env", GOOD + "VERIFY_SSL=sometimes\n")
    assert cli.main(["--env-file", str(bad), "info"]) == cli.EXIT_ERROR
    assert "VERIFY_SSL must be one of" in capsys.readouterr().err
    unsafe = write(tmp_path / "site.env", GOOD + "SITE_ID=a/b\n")
    assert cli.main(["--env-file", str(unsafe), "info"]) == cli.EXIT_ERROR
    assert "SITE_ID" in capsys.readouterr().err


def test_cli_env_file_is_a_global_option_given_before_the_command(tmp_path):
    envfile = write(tmp_path / "x.env")
    with pytest.raises(SystemExit) as exc:
        cli.main(["info", "--env-file", str(envfile)])                       # after the command: not accepted
    assert exc.value.code == cli.EXIT_USAGE
