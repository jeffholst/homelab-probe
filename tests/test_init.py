"""`hlp init`: the guided setup from the terminal (issue #159)."""

import builtins
import getpass
import io
import os
import stat
import sys

import pytest
from conftest import FakeSession

from homelab_probe import cli
from homelab_probe.client import UniFiClient
from homelab_probe.config import build_config, read_env_values

KEY = "the-api-key-0123456789abcdef"
URL = "https://192.168.1.1"


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


def init(capsys, monkeypatch, *argv, stdin="", answers=(), key=None):
    """Run `init` with the given standard input (the API key) and typed answers to its questions."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    typed = iter(answers)
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(typed))
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": key if key is not None else KEY)
    code = cli.main(["init", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def written(home):
    return read_env_values(home / ".env")


# -- without questions -----------------------------------------------------------------------------------------

def test_the_options_and_a_key_on_stdin_write_the_files_and_say_nothing_was_sent(capsys, monkeypatch, home):
    code, out, err = init(capsys, monkeypatch, "--dir", str(home), "--url", URL, "--api-key-stdin", stdin=KEY + "\n")
    assert code == 0 and err == ""
    assert "created" in out and "Nothing was sent to the controller" in out and KEY not in out
    assert written(home) == {"UNIFI_URL": URL, "UNIFI_SITE_ID": "default", "UNIFI_API_KEY": KEY}
    assert stat.S_IMODE(os.stat(home / ".env").st_mode) == 0o600 and (home / "snapshots").is_dir()
    assert (home / "hlp.toml").is_file()


def test_the_site_and_the_verify_option_are_written(capsys, monkeypatch, home):
    ca = home.parent / "ca.pem"
    ca.write_text("")
    init(capsys, monkeypatch, "--dir", str(home), "--url", URL, "--site", "Lab", "--verify", str(ca),
         "--api-key-stdin", stdin=KEY)
    values = written(home)
    assert values["UNIFI_SITE_ID"] == "Lab" and values["UNIFI_VERIFY_SSL"] == str(ca)
    assert build_config(values).verify_ssl == str(ca)


def test_a_rerun_without_questions_keeps_the_settings_it_is_not_told_about(capsys, monkeypatch, home):
    home.mkdir()
    (home / ".env").write_text(f"# mine\nUNIFI_URL=https://old\nUNIFI_API_KEY={KEY}\nUNIFI_VERIFY_SSL=false\nLOG_LEVEL=INFO\n")
    code, out, _ = init(capsys, monkeypatch, "--dir", str(home), "--url", URL, "--no-input")
    assert code == 0 and "updated" in out and ".env.bak" in out
    values = written(home)
    assert values["UNIFI_URL"] == URL and values["UNIFI_API_KEY"] == KEY and values["UNIFI_VERIFY_SSL"] == "false"
    assert values["LOG_LEVEL"] == "INFO" and "# mine" in (home / ".env").read_text()
    assert (home / ".env.bak").read_text().startswith("# mine")


def test_no_input_without_a_key_anywhere_is_an_error(capsys, monkeypatch, home):
    code, _, err = init(capsys, monkeypatch, "--dir", str(home), "--url", URL, "--no-input")
    assert code == 3 and "no API key to keep" in err and not home.exists()
    home.mkdir()
    (home / ".env").write_text("UNIFI_API_KEY=your-api-key-here\n")
    assert init(capsys, monkeypatch, "--dir", str(home), "--url", URL, "--no-input")[0] == 3


@pytest.mark.parametrize("argv, message", [
    (["--api-key-stdin"], "--api-key-stdin needs --url"),
    (["--no-input"], "--no-input needs --url"),
    (["--no-input", "--url", URL, "--bogus"], "unrecognized arguments"),
])
def test_options_that_cannot_work_together_are_usage_errors(argv, message, capsys, monkeypatch, home):
    with pytest.raises(SystemExit) as caught:
        init(capsys, monkeypatch, "--dir", str(home), *argv)
    assert caught.value.code == 64 and message in capsys.readouterr().err and not home.exists()


@pytest.mark.parametrize("argv, message", [
    (["--url", "ftp://c"], "UNIFI_URL"), (["--url", URL, "--verify", "maybe"], "UNIFI_VERIFY_SSL"),
    (["--url", URL, "--site", "a/b"], "UNIFI_SITE_ID"), (["--url", "http://192.168.1.1"], "http://"),
])
def test_values_the_commands_would_refuse_are_refused_before_anything_is_written(argv, message, capsys, monkeypatch,
                                                                                 home):
    code, _, err = init(capsys, monkeypatch, "--dir", str(home), *argv, "--api-key-stdin", stdin=KEY)
    assert code == 3 and message in err and KEY not in err and not home.exists()


def test_an_empty_key_on_stdin_is_an_error(capsys, monkeypatch, home):
    code, _, err = init(capsys, monkeypatch, "--dir", str(home), "--url", URL, "--api-key-stdin", stdin="\n")
    assert code == 3 and "no API key was given" in err


# -- with questions --------------------------------------------------------------------------------------------

def test_the_questions_are_asked_and_the_key_is_not_echoed(capsys, monkeypatch, home):
    code, out, err = init(capsys, monkeypatch, "--dir", str(home), answers=[URL, "Lab", "yes"])
    assert code == 0 and KEY not in out + err
    assert written(home) == {"UNIFI_URL": URL, "UNIFI_SITE_ID": "Lab", "UNIFI_API_KEY": KEY, "UNIFI_VERIFY_SSL": "true"}


def test_an_address_that_is_not_acceptable_is_asked_again_three_times(capsys, monkeypatch, home):
    code, out, err = init(capsys, monkeypatch, "--dir", str(home), answers=["nonsense", "ftp://x", URL, "", "yes"])
    assert code == 0 and err.count("UNIFI_URL") >= 2 and written(home)["UNIFI_URL"] == URL
    code, _, err = init(capsys, monkeypatch, "--dir", str(home / "x"), answers=["a", "b", "c"])
    assert code == 3 and "still not acceptable" in err and not (home / "x").exists()


def test_turning_certificate_checking_off_needs_a_yes_and_says_why_it_matters(capsys, monkeypatch, home):
    code, _, err = init(capsys, monkeypatch, "--dir", str(home), answers=[URL, "", "no", "n"])
    assert code == 3 and "sent to whatever answers" in err and "nothing was written" in err and not home.exists()
    code, _, _ = init(capsys, monkeypatch, "--dir", str(home), answers=[URL, "", "no", "y"])
    assert code == 0 and written(home)["UNIFI_VERIFY_SSL"] == "false"


def test_force_skips_the_questions_about_risk_and_replacing(capsys, monkeypatch, home):
    init(capsys, monkeypatch, "--dir", str(home), answers=[URL, "", "yes"])
    code, out, err = init(capsys, monkeypatch, "--dir", str(home), "--force", answers=[URL, "", "no"])
    assert code == 0 and "sent to whatever answers" not in err and written(home)["UNIFI_VERIFY_SSL"] == "false"


def test_replacing_an_existing_env_asks_first_and_declining_changes_nothing(capsys, monkeypatch, home):
    init(capsys, monkeypatch, "--dir", str(home), answers=[URL, "", "yes"])
    before = (home / ".env").read_text()
    code, _, err = init(capsys, monkeypatch, "--dir", str(home), answers=["https://10.0.0.1", "", "yes", "n"])
    assert code == 3 and "everything else kept" in err and (home / ".env").read_text() == before
    code, out, _ = init(capsys, monkeypatch, "--dir", str(home), answers=["https://10.0.0.1", "", "yes", "y"])
    assert code == 0 and written(home)["UNIFI_URL"] == "https://10.0.0.1" and "updated" in out


def test_the_current_values_are_the_defaults_and_an_empty_key_keeps_the_current_one(capsys, monkeypatch, home):
    init(capsys, monkeypatch, "--dir", str(home), answers=[URL, "Lab", "yes"])
    code, _, _ = init(capsys, monkeypatch, "--dir", str(home), answers=["", "", "", "y"], key="")
    assert code == 0 and written(home)["UNIFI_API_KEY"] == KEY and written(home)["UNIFI_SITE_ID"] == "Lab"


def test_a_ca_file_can_be_given_at_the_prompt(capsys, monkeypatch, home):
    ca = home.parent / "ca.pem"
    ca.write_text("")
    init(capsys, monkeypatch, "--dir", str(home), answers=[URL, "", str(ca)])
    assert written(home)["UNIFI_VERIFY_SSL"] == str(ca)


def test_the_input_ending_early_is_an_error_not_a_traceback(capsys, monkeypatch, home):
    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr(builtins, "input", eof)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": KEY)
    assert cli.main(["init", "--dir", str(home)]) == 3
    assert "the input ended" in capsys.readouterr().err and not home.exists()


# -- the optional check ------------------------------------------------------------------------------------------

def test_check_reads_the_controller_with_what_was_just_written(capsys, monkeypatch, home):
    monkeypatch.setattr(UniFiClient, "from_config", classmethod(lambda cls, config: _fake(config)))
    code, out, _ = init(capsys, monkeypatch, "--dir", str(home), "--url", URL, "--api-key-stdin", "--check", stdin=KEY)
    assert code == 0 and "Controller and API key" in out and "answered" in out and KEY not in out
    assert "Nothing was sent to the controller" not in out


def test_a_failing_check_gives_exit_3_and_explains(capsys, monkeypatch, home):
    def broken(config):
        client = _fake(config)
        client.session.status = 401
        return client

    monkeypatch.setattr(UniFiClient, "from_config", classmethod(lambda cls, config: broken(config)))
    code, out, _ = init(capsys, monkeypatch, "--dir", str(home), "--url", URL, "--api-key-stdin", "--check", stdin=KEY)
    assert code == 3 and "FAIL" in out and KEY not in out and (home / ".env").exists()     # the files were written anyway


def _fake(config):
    client = UniFiClient(config.controller_url, config.api_key)
    client.session = FakeSession()
    return client


# -- safety ----------------------------------------------------------------------------------------------------

def test_the_demo_refuses_it(capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["--demo", "init"])
    assert caught.value.code == 64 and "writes your own" in capsys.readouterr().err


def test_a_key_is_never_an_argument():
    parser = cli.build_parser()
    action = next(a for a in parser._actions if a.dest == "command")
    options = {flag for a in action.choices["init"]._actions for flag in a.option_strings}
    assert not {"--api-key", "--key", "--password", "--token"} & options and "--api-key-stdin" in options


def test_an_unwritable_directory_is_an_error_with_the_path(capsys, monkeypatch, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory is needed")
    code, _, err = init(capsys, monkeypatch, "--dir", str(blocker / "sub"), "--url", URL, "--api-key-stdin", stdin=KEY)
    assert code == 3 and "blocker" in err and KEY not in err


def test_init_needs_no_env_file_and_no_controller(capsys, monkeypatch, home):
    monkeypatch.delenv("UNIFI_URL", raising=False)
    assert init(capsys, monkeypatch, "--dir", str(home), "--url", URL, "--api-key-stdin", stdin=KEY)[0] == 0


def test_a_setup_step_that_fails_after_the_checks_is_an_error_not_a_traceback(capsys, monkeypatch, home):
    home.mkdir()
    real = home.parent / "real.env"
    real.write_text("KEEP=1\n")
    (home / ".env").symlink_to(real)
    code, _, err = init(capsys, monkeypatch, "--dir", str(home), "--url", URL, "--api-key-stdin", stdin=KEY)
    assert code == 3 and "symbolic link" in err and "Traceback" not in err and KEY not in err
    assert real.read_text() == "KEEP=1\n"
