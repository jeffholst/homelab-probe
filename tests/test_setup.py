"""The setup engine behind `hlp init` and the web wizard: .env merging, private files, validation (issues #159, #185)."""

import os
import stat
import sys

import pytest
from dotenv import dotenv_values

from homelab_probe import accounts, setup
from homelab_probe.accounts import AccountStore
from homelab_probe.config import KNOWN_VARIABLES, ConfigError, build_config, layered_values, read_env_values
from homelab_probe.settings import load_settings
from homelab_probe.setup import SetupError, StepResult

KEY = "the-api-key-0123456789abcdef"
GOOD = {"UNIFI_URL": "https://192.168.1.1", "UNIFI_API_KEY": KEY, "UNIFI_SITE_ID": "default"}
posix = pytest.mark.skipif(sys.platform == "win32", reason="file modes mean little on Windows")


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


# -- writing a value -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["plain", "has space", "a#b", "a # b", "it's", 'say "hi"', "back\\slash", "dollar$sign",
                                   "$HOME", "tab\there", "=eq", "'", '"', "trailing ", " leading", "ünï", "a;b|c&d",
                                   "x" * 300, "%s", "https://u:p@h:8443/p?q=1#f", "/path/to/ca.pem"])
def test_every_value_reads_back_exactly_as_it_was_written(value, tmp_path):
    path = tmp_path / ".env"
    path.write_text(setup.render_env({"UNIFI_API_KEY": value}))
    assert dotenv_values(path)["UNIFI_API_KEY"] == value


def test_a_safe_value_is_written_bare_and_anything_else_single_quoted():
    assert setup.format_value("UNIFI_URL", "https://192.168.1.1:443/x") == "https://192.168.1.1:443/x"
    assert setup.format_value("UNIFI_API_KEY", "has space") == "'has space'"
    assert setup.format_value("UNIFI_API_KEY", "it's") == "'it\\'s'"


@pytest.mark.parametrize("value, why", [("", "empty"), ("a\nb", "line break"), ("a\rb", "line break"), ("a\0b", "NUL"),
                                        ("x${HOME}y", r"\$\{")])
def test_a_value_that_a_dotenv_file_cannot_hold_is_refused_with_the_setting_name(value, why):
    with pytest.raises(SetupError, match=f"UNIFI_API_KEY.*{why}|UNIFI_API_KEY is empty"):
        setup.format_value("UNIFI_API_KEY", value)


# -- merging into an existing .env -----------------------------------------------------------------------------

def test_a_new_setting_is_added_at_the_end_after_a_note_and_a_blank_line():
    text = setup.render_env({"UNIFI_URL": "https://c"}, "# mine\nLOG_LEVEL=INFO\n")
    assert text == "# mine\nLOG_LEVEL=INFO\n\n# Added by hlp init\nUNIFI_URL=https://c\n"
    assert setup.render_env({"UNIFI_URL": "https://c"}) == "# Added by hlp init\nUNIFI_URL=https://c\n"


def test_a_setting_that_is_there_is_replaced_where_it_stands_and_its_repeats_are_dropped():
    existing = "# top\nUNIFI_URL=https://old\nLOG_LEVEL=INFO\nexport UNIFI_URL=https://older\n\n# end\n"
    text = setup.render_env({"UNIFI_URL": "https://new"}, existing)
    assert text == "# top\nUNIFI_URL=https://new\nLOG_LEVEL=INFO\n\n# end\n"
    assert dotenv_values(stream=__import__("io").StringIO(text))["UNIFI_URL"] == "https://new"


def test_none_removes_a_setting_and_everything_else_stays_byte_for_byte():
    existing = "# note\nUNIFI_URL=https://c\n   \nNOTIFY_NTFY_URL='https://ntfy.example/x'\nUNIFI_VERIFY_SSL=/ca.pem # why\n"
    text = setup.render_env({"UNIFI_VERIFY_SSL": None, "UNIFI_SITE_ID": "lab"}, existing)
    assert text == ("# note\nUNIFI_URL=https://c\n   \nNOTIFY_NTFY_URL='https://ntfy.example/x'\n\n"
                    "# Added by hlp init\nUNIFI_SITE_ID=lab\n")


def test_a_last_line_without_a_newline_and_a_line_nobody_can_parse_survive():
    text = setup.render_env({"UNIFI_URL": "https://c"}, "this is not a setting\nLOG_LEVEL=INFO")
    assert text.startswith("this is not a setting\nLOG_LEVEL=INFO\n") and "UNIFI_URL=https://c\n" in text


def test_only_settings_of_this_tool_can_be_written():
    with pytest.raises(SetupError, match="PATH is not a setting"):
        setup.render_env({"PATH": "/bin"})
    with pytest.raises(SetupError, match="HLP_ENV, PATH are not settings"):
        setup.render_env({"PATH": "/bin", "HLP_ENV": "x"})


def test_a_nothing_to_change_merge_reproduces_the_file():
    existing = "UNIFI_URL=https://c\n# c\nLOG_LEVEL=INFO\n"
    assert setup.render_env({"UNIFI_URL": "https://c"}, existing) == existing
    assert setup.render_env({}, existing) == existing


# -- private files ---------------------------------------------------------------------------------------------

@posix
def test_a_file_is_created_owner_only_and_a_second_identical_write_changes_nothing(tmp_path):
    path = tmp_path / "sub" / ".env"
    first = setup.write_private_file("setup.env", path, "A=1\n")
    assert first.status == "created" and mode(path) == 0o600 and mode(path.parent) == 0o700
    os.chmod(path, 0o644)
    again = setup.write_private_file("setup.env", path, "A=1\n")
    assert again.status == "kept" and mode(path) == 0o600 and not (tmp_path / "sub" / ".env.bak").exists()


@posix
def test_replacing_a_file_keeps_the_old_one_as_a_private_backup(tmp_path):
    path = tmp_path / ".env"
    path.write_text("OLD=1\n")
    os.chmod(path, 0o644)
    result = setup.write_private_file("setup.env", path, "NEW=1\n")
    assert result.status == "updated" and ".env.bak" in result.message
    assert path.read_text() == "NEW=1\n" and (tmp_path / ".env.bak").read_text() == "OLD=1\n"
    assert mode(path) == 0o600 and mode(tmp_path / ".env.bak") == 0o600
    setup.write_private_file("setup.env", path, "NEWER=1\n", backup=False)
    assert (tmp_path / ".env.bak").read_text() == "OLD=1\n"                   # no new backup was asked for


def test_a_crash_while_writing_leaves_the_old_file_and_no_temporary_file(tmp_path, monkeypatch):
    tmp_path = tmp_path / "home"
    tmp_path.mkdir()
    path = tmp_path / ".env"
    path.write_text("OLD=1\n")

    def crash(source, target):
        raise OSError("disk gone")

    monkeypatch.setattr(setup.os, "replace", crash)
    with pytest.raises(OSError, match="disk gone"):
        setup.write_private_file("setup.env", path, "NEW=1\n", backup=False)
    monkeypatch.undo()
    assert path.read_text() == "OLD=1\n" and sorted(p.name for p in tmp_path.iterdir()) == [".env"]


def test_a_file_that_is_a_symbolic_link_is_never_followed_or_replaced(tmp_path):
    target = tmp_path / "real.env"
    target.write_text("KEEP=1\n")
    link = tmp_path / ".env"
    link.symlink_to(target)
    with pytest.raises(SetupError, match="symbolic link"):
        setup.write_private_file("setup.env", link, "NEW=1\n")
    assert target.read_text() == "KEEP=1\n" and link.is_symlink()
    (tmp_path / "hlp.toml").symlink_to(target)
    with pytest.raises(SetupError, match="symbolic link"):
        setup.write_settings_stub(tmp_path / "hlp.toml")
    (tmp_path / "snapshots").symlink_to(tmp_path)
    with pytest.raises(SetupError, match="symbolic link"):
        setup.ensure_private_dir("setup.snapshots", tmp_path / "snapshots")
    backup = tmp_path / "x" / ".env"
    backup.parent.mkdir()
    backup.write_text("OLD=1\n")
    (backup.parent / ".env.bak").symlink_to(target)
    with pytest.raises(SetupError, match="symbolic link"):
        setup.write_private_file("setup.env", backup, "NEW=1\n")


def test_a_file_that_cannot_be_read_is_not_overwritten_blindly(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes(b"UNIFI_URL=caf\xe9\n")
    with pytest.raises(SetupError, match="cannot read"):
        setup.write_private_file("setup.env", path, "NEW=1\n")
    assert path.read_bytes() == b"UNIFI_URL=caf\xe9\n"


@posix
def test_the_settings_stub_is_valid_commented_and_never_replaces_the_users_own(tmp_path):
    path = tmp_path / "hlp.toml"
    assert setup.write_settings_stub(path).status == "created" and mode(path) == 0o600
    settings = load_settings(path)
    assert settings.ignore == () and "# [thresholds]" in path.read_text()
    path.write_text('[[ignore]]\ncode = "device.offline"\nreason = "spare"\n')
    assert setup.write_settings_stub(path).status == "kept" and "device.offline" in path.read_text()


def test_the_private_directory_is_made_once(tmp_path):
    first = setup.ensure_private_dir("setup.snapshots", tmp_path / "snapshots")
    again = setup.ensure_private_dir("setup.snapshots", tmp_path / "snapshots")
    assert (first.status, again.status) == ("created", "kept")


# -- validation ------------------------------------------------------------------------------------------------

def test_good_values_have_no_problems():
    assert setup.validate_values(GOOD) == [] and setup.validate_field(GOOD, "UNIFI_URL") is None
    assert setup.validate_field(GOOD, "NOT_A_SETTING") is None


@pytest.mark.parametrize("name, value", [
    ("UNIFI_URL", "ftp://c"), ("UNIFI_URL", "http://c"), ("UNIFI_URL", "not a url"), ("UNIFI_URL", ""),
    ("UNIFI_API_KEY", ""), ("UNIFI_API_KEY", setup.PLACEHOLDER_KEY), ("UNIFI_SITE_ID", "a/b"),
    ("UNIFI_VERIFY_SSL", "maybe"), ("UNIFI_VERIFY_SSL", "/no/such/ca.pem"), ("UNIFI_TIMEOUT", "0"),
    ("UNIFI_PARALLEL_REQUESTS", "99"), ("ALLOW_INSECURE_HTTP", "perhaps"), ("LOG_LEVEL", "LOUD"), ("LOG_FORMAT", "xml"),
    ("AUDIT_LOG_MAX_MB", "0"), ("AUDIT_LOG_FILES", "1"), ("SESSION_IDLE_MINUTES", "0"), ("SESSION_MAX_HOURS", "9999"),
    ("NOTIFY_NTFY_URL", "http://ntfy.example/t"), ("NOTIFY_WEBHOOK_URL", "not a url"),
    ("NOTIFY_SMTP_HOST", "mail.example"),
])
def test_each_setting_is_checked_on_its_own_and_named(name, value):
    values = {**GOOD, name: value}
    problems = dict(setup.validate_values(values))
    assert name in problems or name.startswith("NOTIFY_") and "NOTIFY_*" in problems, (name, problems)
    assert setup.validate_field(values, name if name in problems else "NOTIFY_*")


def test_the_problems_never_repeat_the_api_key():
    values = {**GOOD, "UNIFI_SITE_ID": f"{KEY}/x"}               # a validator that quotes the value it refuses
    from homelab_probe.config import validate_site

    with pytest.raises(ConfigError) as raw:
        validate_site(values["UNIFI_SITE_ID"])
    assert KEY in str(raw.value)                                    # so the scrub has something to do
    problems = setup.validate_values(values)
    assert problems and "***" in problems[0][1] and KEY not in " ".join(message for _, message in problems)
    assert KEY not in (setup.validate_field(values, "UNIFI_SITE_ID") or "")


def test_settings_that_are_each_fine_but_wrong_together_are_reported_as_a_whole(monkeypatch):
    monkeypatch.setattr(setup, "CHECKS", [])
    problems = setup.validate_values({"UNIFI_API_KEY": KEY})
    assert problems and problems[0][0] == "" and "UNIFI_URL is not set" in problems[0][1]


# -- apply -----------------------------------------------------------------------------------------------------

@posix
def test_apply_sets_a_directory_up_and_the_result_loads(tmp_path, monkeypatch):
    steps = setup.apply({**GOOD, "UNIFI_VERIFY_SSL": "false"}, tmp_path / "home")
    assert [(s.id, s.status) for s in steps] == [("setup.env", "created"), ("setup.settings", "created"),
                                                 ("setup.snapshots", "created")]
    directory = tmp_path / "home"
    assert mode(directory / ".env") == 0o600 and mode(directory / "snapshots") == 0o700
    values = read_env_values(directory / ".env")
    config = build_config(values)
    assert (config.controller_url, config.verify_ssl, config.api_key) == ("https://192.168.1.1", False, KEY)
    assert all(isinstance(step, StepResult) and KEY not in step.message for step in steps)


def test_apply_checks_everything_before_it_writes_anything(tmp_path):
    for values, text in (({**GOOD, "UNIFI_URL": "ftp://c"}, "UNIFI_URL"), ({**GOOD, "LOG_LEVEL": "LOUD"}, "LOG_LEVEL"),
                         ({**GOOD, "UNIFI_API_KEY": "line\nbreak"}, "UNIFI_API_KEY")):
        with pytest.raises(SetupError, match=text):
            setup.apply(values, tmp_path / "home")
        assert not (tmp_path / "home").exists()
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / ".env").write_text("OLD=1\n")
    with pytest.raises(SetupError):
        setup.apply({**GOOD, "UNIFI_URL": "ftp://c"}, tmp_path / "home")
    assert (tmp_path / "home" / ".env").read_text() == "OLD=1\n" and not (tmp_path / "home" / ".env.bak").exists()


def test_apply_keeps_what_the_user_already_had(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("# mine\nLOG_LEVEL=INFO\nUNIFI_URL=https://old\n")
    (home / "hlp.toml").write_text("[thresholds]\nwan_latency_warn_ms = 50\n")
    steps = setup.apply(GOOD, home)
    assert [s.status for s in steps] == ["updated", "kept", "created"]
    text = (home / ".env").read_text()
    assert "# mine\nLOG_LEVEL=INFO\nUNIFI_URL=https://192.168.1.1\n" in text and (home / ".env.bak").exists()
    assert "wan_latency_warn_ms = 50" in (home / "hlp.toml").read_text()


def test_apply_can_make_the_first_administrator(tmp_path):
    steps = setup.apply(GOOD, tmp_path, admin=("Alice", "correct horse battery"))
    assert steps[-1].id == "setup.admin" and "alice" in steps[-1].message and "correct horse" not in steps[-1].message
    user = AccountStore(tmp_path).get("alice")
    assert user.role == "admin" and accounts.verify_password("correct horse battery", user.password_hash)


def test_a_refused_administrator_is_a_setup_error(tmp_path):
    with pytest.raises(SetupError, match="at least 12"):
        setup.apply(GOOD, tmp_path, admin=("alice", "short"))
    with pytest.raises(SetupError, match="3 to 64"):
        setup.apply(GOOD, tmp_path / "other", admin=("x", "correct horse battery"))


# -- reading without touching the environment --------------------------------------------------------------------

def test_reading_a_dotenv_does_not_change_the_environment(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("UNIFI_URL=https://c\nUNIFI_API_KEY=k\nNOT_OURS=1\nEMPTY=\n")
    monkeypatch.delenv("UNIFI_URL", raising=False)
    assert read_env_values(tmp_path / ".env") == {"UNIFI_URL": "https://c", "UNIFI_API_KEY": "k", "NOT_OURS": "1",
                                                  "EMPTY": ""}
    assert "UNIFI_URL" not in os.environ and "NOT_OURS" not in os.environ


def test_the_environment_wins_over_the_file_and_only_for_the_names_the_tool_knows(tmp_path):
    (tmp_path / ".env").write_text("UNIFI_URL=https://file\nLOG_LEVEL=INFO\nNOT_OURS=file\n")
    values = layered_values(tmp_path / ".env", {"UNIFI_URL": "https://env", "NOT_OURS": "env", "HOME": "/h"})
    assert values == {"UNIFI_URL": "https://env", "LOG_LEVEL": "INFO", "NOT_OURS": "file"}
    assert layered_values(None, {"UNIFI_URL": "https://env", "HOME": "/h"}) == {"UNIFI_URL": "https://env"}


def test_a_file_that_cannot_be_read_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="cannot read env file"):
        read_env_values(tmp_path / "missing.env")
    (tmp_path / "bad.env").write_bytes(b"A=\xff\xfe\n")
    with pytest.raises(ConfigError, match="cannot read env file"):
        read_env_values(tmp_path / "bad.env")


def test_every_setting_the_engine_may_write_is_one_the_tool_reads():
    names = {name for name, _ in setup.CHECKS if name != "NOTIFY_*"}
    assert names <= set(KNOWN_VARIABLES)


def test_a_temporary_file_that_cannot_be_removed_does_not_hide_the_real_error(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()

    def crash(source, target):
        raise OSError("disk gone")

    def stuck(path):
        raise OSError("cannot remove")

    monkeypatch.setattr(setup.os, "replace", crash)
    monkeypatch.setattr(setup.os, "unlink", stuck)
    with pytest.raises(OSError, match="disk gone"):                      # not "cannot remove"
        setup.write_private_file("setup.env", home / ".env", "NEW=1\n")


# -- fixes from the review of this change ----------------------------------------------------------------------

def test_a_file_without_a_final_newline_is_kept_byte_for_byte_unless_something_is_appended():
    for existing in ("A=1", "# note", "UNIFI_URL=https://c\n# end", "LOG_LEVEL=INFO"):
        assert setup.render_env({}, existing) == existing
        assert setup.render_env({"UNIFI_URL": "https://c"} if "UNIFI_URL" in existing else {}, existing) == existing
    assert setup.render_env({"UNIFI_SITE_ID": "lab"}, "# note") == "# note\n\n# Added by hlp init\nUNIFI_SITE_ID=lab\n"
    assert setup.render_env({"UNIFI_SITE_ID": "lab"}, "A=1") == "A=1\n\n# Added by hlp init\nUNIFI_SITE_ID=lab\n"
    assert setup.render_env({"LOG_LEVEL": "DEBUG"}, "# a\nLOG_LEVEL=INFO") == "# a\nLOG_LEVEL=DEBUG\n"


def test_applying_nothing_new_to_a_file_without_a_final_newline_leaves_it_alone(tmp_path):
    (tmp_path / ".env").write_text(f"UNIFI_URL=https://192.168.1.1\nUNIFI_API_KEY={KEY}")
    steps = setup.apply({"UNIFI_URL": "https://192.168.1.1"}, tmp_path)
    assert steps[0].status == "kept" and not (tmp_path / ".env.bak").exists()
    assert (tmp_path / ".env").read_text() == f"UNIFI_URL=https://192.168.1.1\nUNIFI_API_KEY={KEY}"


@posix
def test_an_existing_directory_that_others_can_read_is_made_private(tmp_path):
    directory = tmp_path / "snapshots"
    directory.mkdir()
    os.chmod(directory, 0o755)
    result = setup.ensure_private_dir("setup.snapshots", directory)
    assert result.status == "updated" and "readable by others" in result.message and mode(directory) == 0o700
    assert setup.ensure_private_dir("setup.snapshots", directory).status == "kept"


@posix
def test_a_directory_that_cannot_be_made_private_is_an_error_not_a_silent_success(tmp_path, monkeypatch):
    directory = tmp_path / "snapshots"
    directory.mkdir()
    os.chmod(directory, 0o755)

    def refuse(path, mode):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(setup.os, "chmod", refuse)
    with pytest.raises(SetupError, match="could not be made private"):
        setup.ensure_private_dir("setup.snapshots", directory)


def test_what_is_checked_is_the_whole_file_that_would_result_not_only_the_new_values(tmp_path):
    (tmp_path / ".env").write_text("LOG_LEVEL=LOUD\nNOTIFY_NTFY_URL=http://ntfy.example/t\nUNIFI_URL=https://old\n")
    with pytest.raises(SetupError) as raised:
        setup.apply(GOOD, tmp_path)
    message = str(raised.value)
    assert "LOG_LEVEL" in message and "already in the existing .env" in message and "NOTIFY_" in message
    assert "UNIFI_URL" not in message                                         # the new value is fine
    assert (tmp_path / ".env").read_text().startswith("LOG_LEVEL=LOUD") and not (tmp_path / ".env.bak").exists()
    assert not (tmp_path / "hlp.toml").exists() and not (tmp_path / "snapshots").exists()


def test_a_bad_setting_that_the_new_values_replace_or_remove_is_not_held_against_them(tmp_path):
    (tmp_path / ".env").write_text("LOG_LEVEL=LOUD\nUNIFI_URL=ftp://old\n")
    steps = setup.apply({**GOOD, "LOG_LEVEL": "INFO"}, tmp_path)
    assert steps[0].status == "updated"
    (tmp_path / ".env").write_text("LOG_LEVEL=LOUD\n")
    assert setup.apply({**GOOD, "LOG_LEVEL": None}, tmp_path)[0].status == "updated"
    assert "LOG_LEVEL" not in read_env_values(tmp_path / ".env")


def test_settings_that_are_only_in_the_environment_are_not_part_of_the_file_check(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "LOUD")
    assert setup.apply(GOOD, tmp_path)[0].status == "created"


def test_the_existing_file_is_read_through_a_link_never(tmp_path):
    target = tmp_path / "real.env"
    target.write_text("UNIFI_API_KEY=leaked-key-0123456789\n")
    link = tmp_path / ".env"
    link.symlink_to(target)
    with pytest.raises(SetupError, match="symbolic link"):
        setup.read_existing_env(link)
    assert setup.read_existing_env(tmp_path / "missing.env") == ""
    (tmp_path / "latin.env").write_bytes(b"A=caf\xe9\n")
    with pytest.raises(SetupError, match="cannot read"):
        setup.read_existing_env(tmp_path / "latin.env")
    assert setup.existing_values("UNIFI_URL=https://c\nNOT_OURS=1\nLOG_LEVEL=\n") == {"UNIFI_URL": "https://c",
                                                                                  "LOG_LEVEL": ""}


@pytest.mark.parametrize("key", ["a", "ab", "abc", "abcd", KEY])
def test_a_key_of_any_length_is_kept_out_of_the_messages(key):
    values = {**GOOD, "UNIFI_API_KEY": key, "UNIFI_SITE_ID": f"{key}/x"}
    problems = setup.validate_values(values)
    shown = " ".join(message for _, message in problems)
    assert problems and (key not in shown if len(key) >= 4 else
                         "would have repeated the API key" in shown and "/x" not in shown)
    assert key not in (setup.validate_field(values, "UNIFI_SITE_ID") or "") or len(key) < 4


def test_a_file_that_already_ends_with_a_blank_line_gets_no_second_one():
    assert setup.render_env({"UNIFI_SITE_ID": "lab"}, "A=1\n\n") == "A=1\n\n# Added by hlp init\nUNIFI_SITE_ID=lab\n"
    assert setup.render_env({"UNIFI_SITE_ID": "lab"}, "A=1\n   \n") == "A=1\n   \n# Added by hlp init\nUNIFI_SITE_ID=lab\n"


def test_apply_takes_the_locks_a_restore_takes_and_is_refused_when_they_are_busy(tmp_path, monkeypatch):
    from homelab_probe.util import file_lock

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(setup, "LOCK_WAIT", 0.2)
    for name in (".env.lock", "hlp.toml.lock"):
        with file_lock(home / name), pytest.raises(SetupError, match="another change"):
            setup.apply(GOOD, home)
        assert not (home / ".env").exists()
    assert setup.apply(GOOD, home)[0].status == "created"
