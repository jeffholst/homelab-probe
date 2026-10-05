"""`hlp web-user`: the accounts of the web interface from the command line (issue #182)."""

import getpass
import io
import json
import os
import sys

import pytest

from homelab_probe import accounts, cli
from homelab_probe.accounts import AccountStore, ScryptParams

PASSWORD = "correct horse battery"
OTHER = "another long password"


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch):
    monkeypatch.setattr(accounts, "PARAMS", ScryptParams(n=16, r=8, p=1))


@pytest.fixture
def data(tmp_path):
    return tmp_path / "data"


def web_user(capsys, data, *argv, stdin=None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = cli.main(["web-user", *argv, "--data-dir", str(data)])
    out = capsys.readouterr()
    return code, out.out, out.err


def audit_events(data):
    return [json.loads(line) for line in (data / "audit.log").read_text().splitlines()]


@pytest.fixture
def admin(capsys, data, monkeypatch):
    assert web_user(capsys, data, "add", "root", "--role", "admin", "--password-stdin", stdin=PASSWORD + "\n",
                    monkeypatch=monkeypatch)[0] == 0
    return "root"


# -- add and list ----------------------------------------------------------------------------------------------

def test_no_users_yet_says_how_to_add_one(capsys, data):
    code, out, err = web_user(capsys, data, "list")
    assert code == 0 and "No users yet. Add one with: hlp web-user add NAME --role admin" in out and err == ""
    assert not data.exists()                                       # listing creates nothing


def test_adding_with_the_password_on_stdin_then_listing(capsys, data, monkeypatch):
    code, out, err = web_user(capsys, data, "add", "Alice", "--role", "admin", "--password-stdin",
                              stdin=PASSWORD + "\r\n", monkeypatch=monkeypatch)
    assert (code, out, err) == (0, "Added the admin alice.\n", "")
    code, out, _ = web_user(capsys, data, "add", "bob", "--password-stdin", stdin=OTHER, monkeypatch=monkeypatch)
    assert code == 0 and out == "Added the viewer bob.\n"                    # the role defaults to viewer
    code, out, _ = web_user(capsys, data, "list")
    lines = out.splitlines()
    assert lines[0].split() == ["Username", "Role", "Status", "Created", "Last", "login"]
    assert lines[2].split()[:3] == ["alice", "admin", "active"] and lines[3].split()[:3] == ["bob", "viewer", "active"]
    store = AccountStore(data)
    assert accounts.verify_password(PASSWORD, store.get("alice").password_hash)      # the CRLF was not part of it


def test_the_password_can_be_asked_for_twice_with_nothing_echoed(capsys, data, monkeypatch):
    asked = []
    answers = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": asked.append(prompt) or next(answers))
    code, out, _ = web_user(capsys, data, "add", "alice", "--role", "admin")
    assert code == 0 and asked == ["Password: ", "Repeat the password: "] and PASSWORD not in out
    assert accounts.verify_password(PASSWORD, AccountStore(data).get("alice").password_hash)


def test_two_passwords_that_differ_change_nothing(capsys, data, monkeypatch):
    answers = iter([PASSWORD, OTHER])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    code, out, err = web_user(capsys, data, "add", "alice")
    assert code == 3 and "the two passwords differ" in err and out == "" and AccountStore(data).users() == []


@pytest.mark.parametrize("stdin", ["", "\n", "\r\n"])
def test_an_empty_stdin_is_an_error_not_an_empty_password(capsys, data, monkeypatch, stdin):
    code, _, err = web_user(capsys, data, "add", "alice", "--password-stdin", stdin=stdin, monkeypatch=monkeypatch)
    assert code == 3 and "no password was given on standard input" in err


def test_a_short_password_and_a_duplicate_are_errors_with_exit_3(capsys, data, monkeypatch, admin):
    code, _, err = web_user(capsys, data, "add", "bob", "--password-stdin", stdin="short\n", monkeypatch=monkeypatch)
    assert code == 3 and err.strip() == "ERROR: the password must have at least 12 characters"
    code, _, err = web_user(capsys, data, "add", "ROOT", "--password-stdin", stdin=PASSWORD, monkeypatch=monkeypatch)
    assert code == 3 and "already exists" in err
    assert [u.username for u in AccountStore(data).users()] == ["root"]


# -- the other actions -----------------------------------------------------------------------------------------

def test_set_role_disable_enable_reset_password_and_delete(capsys, data, monkeypatch, admin):
    web_user(capsys, data, "add", "bob", "--password-stdin", stdin=OTHER, monkeypatch=monkeypatch)
    assert web_user(capsys, data, "set-role", "bob", "--role", "admin")[1] == "bob is now a admin.\n"
    assert web_user(capsys, data, "disable", "bob")[1] == "bob is disabled.\n"
    assert AccountStore(data).get("bob").disabled
    assert web_user(capsys, data, "enable", "BOB")[1] == "bob is enabled.\n"
    code, out, _ = web_user(capsys, data, "reset-password", "bob", "--password-stdin", stdin="a brand new password",
                            monkeypatch=monkeypatch)
    assert code == 0 and out == "The password of bob was changed.\n"
    assert accounts.verify_password("a brand new password", AccountStore(data).get("bob").password_hash)
    assert web_user(capsys, data, "delete", "bob")[1] == "Deleted bob.\n"
    assert [u.username for u in AccountStore(data).users()] == ["root"]


def test_every_change_is_in_the_audit_log_without_a_password(capsys, data, monkeypatch, admin):
    web_user(capsys, data, "add", "bob", "--password-stdin", stdin=OTHER, monkeypatch=monkeypatch)
    web_user(capsys, data, "set-role", "bob", "--role", "admin")
    web_user(capsys, data, "disable", "bob")
    web_user(capsys, data, "enable", "bob")
    web_user(capsys, data, "reset-password", "bob", "--password-stdin", stdin=PASSWORD + "!", monkeypatch=monkeypatch)
    web_user(capsys, data, "delete", "bob")
    events = audit_events(data)
    assert [e["event"] for e in events] == ["user.added", "user.added", "user.role_changed", "user.disabled",
                                            "user.enabled", "user.password_reset", "user.deleted"]
    assert all(e["actor"].startswith("cli") and e["user"] in ("root", "bob") for e in events)
    assert events[2]["role"] == "admin" and events[1]["role"] == "viewer"
    text = (data / "audit.log").read_text() + (data / "users.json").read_text()
    assert PASSWORD not in text and OTHER not in text


def test_an_audit_write_failure_is_reported_and_rolls_back_the_account_change(capsys, data, monkeypatch):
    def fail_open(self):
        raise OSError("write failed")

    monkeypatch.setattr(accounts._PrivateRotatingFileHandler, "_open", fail_open)
    code, out, err = web_user(capsys, data, "add", "alice", "--role", "admin", "--password-stdin",
                              stdin=PASSWORD + "\n", monkeypatch=monkeypatch)
    assert code == 3 and out == ""
    assert "audit log could not be written; the account change was rolled back" in err
    assert AccountStore(data).users() == []


def test_the_last_administrator_is_protected_from_the_command_line(capsys, data, admin):
    for argv in (("delete", "root"), ("disable", "root"), ("set-role", "root", "--role", "viewer")):
        code, _, err = web_user(capsys, data, *argv)
        assert code == 3 and "the last administrator cannot be deleted, demoted or disabled" in err
    assert len(audit_events(data)) == 1                                  # a refused change is not recorded as done


def test_an_unknown_user_is_an_error(capsys, data, admin):
    for argv in (("delete", "ghost"), ("disable", "ghost"), ("enable", "ghost"), ("set-role", "ghost", "--role", "admin")):
        code, _, err = web_user(capsys, data, *argv)
        assert code == 3 and "there is no user ghost" in err


# -- usage errors (exit 64) --------------------------------------------------------------------------------------

@pytest.mark.parametrize("argv, message", [
    (["list", "alice"], "list takes no NAME"),
    (["add"], "add needs the NAME of a user"),
    (["delete"], "delete needs the NAME of a user"),
    (["add", "ab", "--password-stdin"], "a username has 3 to 64 characters"),
    (["add", "alice", "--role", "root"], "invalid choice"),
    (["disable", "alice", "--role", "admin"], "--role only applies to add and set-role"),
    (["list", "--role", "admin"], "--role only applies to add and set-role"),
    (["set-role", "alice"], "set-role needs --role"),
    (["delete", "alice", "--password-stdin"], "--password-stdin only applies to add and reset-password"),
    (["list", "--password-stdin"], "--password-stdin only applies to add and reset-password"),
    (["frobnicate"], "invalid choice"),
])
def test_bad_arguments_are_usage_errors(argv, message, capsys, data):
    with pytest.raises(SystemExit) as caught:
        cli.main(["web-user", *argv, "--data-dir", str(data)])
    assert caught.value.code == 64 and message in capsys.readouterr().err
    assert not data.exists()


def test_a_password_is_never_an_argument():
    parser = cli.build_parser()
    action = next(a for a in parser._actions if a.dest == "command")
    options = {flag for a in action.choices["web-user"]._actions for flag in a.option_strings}
    assert options == {"-h", "--help", "--role", "--password-stdin", "--data-dir"}


def test_the_demo_refuses_it(capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["--demo", "web-user", "list"])
    assert caught.value.code == 64 and "your own accounts file" in capsys.readouterr().err


# -- the data directory and the environment ----------------------------------------------------------------------

def test_the_current_directory_is_the_default_data_directory(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(PASSWORD))
    assert cli.main(["web-user", "add", "alice", "--role", "admin", "--password-stdin"]) == 0
    assert (tmp_path / "users.json").is_file() and (tmp_path / "audit.log").is_file()


def test_it_needs_no_env_file_and_no_controller(capsys, monkeypatch, data):
    monkeypatch.delenv("UNIFI_URL", raising=False)
    assert web_user(capsys, data, "list")[0] == 0 and not (os.getcwd() and os.path.exists(".env"))


def test_the_audit_limits_come_from_the_environment_and_a_bad_one_is_an_error(capsys, data, monkeypatch, admin):
    monkeypatch.setenv("AUDIT_LOG_MAX_MB", "0")
    code, _, err = web_user(capsys, data, "disable", "root")
    assert code == 3 and "AUDIT_LOG_MAX_MB must be between 1 and 1024" in err
    monkeypatch.setenv("AUDIT_LOG_MAX_MB", "2")
    monkeypatch.setenv("AUDIT_LOG_FILES", "1")
    code, _, err = web_user(capsys, data, "disable", "root")
    assert code == 3 and "AUDIT_LOG_FILES must be between 2 and 1000" in err
    monkeypatch.setenv("AUDIT_LOG_FILES", "3")
    assert web_user(capsys, data, "list")[0] == 0


def test_a_damaged_accounts_file_is_an_error_with_exit_3_not_a_traceback(capsys, data):
    data.mkdir()
    (data / "users.json").write_text("{broken")
    for argv in (("list",), ("disable", "alice")):
        code, _, err = web_user(capsys, data, *argv)
        assert code == 3 and "not valid JSON" in err and "Traceback" not in err


def test_a_data_directory_that_cannot_be_written_is_an_error_with_the_path(capsys, tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory is needed")
    monkeypatch.setattr(sys, "stdin", io.StringIO(PASSWORD))
    code = cli.main(["web-user", "add", "alice", "--password-stdin", "--data-dir", str(blocker / "sub")])
    assert code == 3 and "blocker" in capsys.readouterr().err


def test_the_actor_is_the_os_user_or_plain_cli(capsys, data, monkeypatch, admin):
    monkeypatch.setattr(getpass, "getuser", lambda: "jeff")
    web_user(capsys, data, "add", "bob", "--password-stdin", stdin=OTHER, monkeypatch=monkeypatch)

    def nobody():
        raise KeyError("no login name")

    monkeypatch.setattr(getpass, "getuser", nobody)
    web_user(capsys, data, "disable", "bob")
    assert [e["actor"] for e in audit_events(data)][1:] == ["cli:jeff", "cli"]


def test_a_username_with_control_characters_in_a_hand_edited_file_is_refused_not_printed(capsys, data):
    data.mkdir()
    (data / "users.json").write_text(json.dumps({"version": 1, "users": [
        {"username": "evil\x1b[31m", "role": "admin", "password": "x"}]}))
    code, out, err = web_user(capsys, data, "list")
    assert code == 3 and "\x1b" not in out + err and "damaged" in err
