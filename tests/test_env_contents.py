"""What is inside the ``.env`` file: duplicates, misspelled names, unreadable lines, blank values and shell overrides
(``config.inspect_env_file`` and the ``config.env_contents`` check of ``doctor``, issue #196)."""

import json
import re

import pytest
from conftest import CONFIG_VARIABLES
from docs_support import ROOT

from homelab_probe import config
from homelab_probe.config import KNOWN_VARIABLES, ConfigError, EnvFileReport, inspect_env_file, load_config
from homelab_probe.doctor import FAIL, OK, SKIP, WARN, Options, render, run_checks, to_dict

MARKER = "SECRETMARK9f3a"


def write(tmp_path, text, name=".env"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def check_for(path, **options):
    found = {c.id: c for c in run_checks(Options(env_file=path, offline=True, **options))}
    return found["config.env_contents"]


# -- the file ------------------------------------------------------------------------------------------------------

def test_a_clean_file_has_nothing_to_report_and_counts_its_settings(tmp_path):
    report = inspect_env_file(write(tmp_path, "# comment\n\nUNIFI_URL=https://c.example\nexport UNIFI_API_KEY=k\n"))
    assert report == EnvFileReport(settings=2) and report.clean


def test_a_setting_on_several_lines_is_reported_with_its_lines_and_the_last_one_really_wins(tmp_path):
    path = write(tmp_path, "UNIFI_URL=https://c.example\nUNIFI_API_KEY=k\nUNIFI_SITE_ID=first\n# x\nUNIFI_SITE_ID=second\n")
    assert inspect_env_file(path).duplicates == {"UNIFI_SITE_ID": [3, 5]}
    assert load_config(path).site == "second"                    # the claim the message makes


def test_identical_duplicates_and_three_of_a_kind_are_reported_too(tmp_path):
    report = inspect_env_file(write(tmp_path, "LOG_LEVEL=INFO\nLOG_LEVEL=INFO\nexport LOG_LEVEL=INFO\nUNIFI_URL=u\nUNIFI_URL=u\n"))
    assert report.duplicates == {"LOG_LEVEL": [1, 2, 3], "UNIFI_URL": [4, 5]} and report.settings == 2


def test_a_misspelled_name_is_reported_with_the_closest_setting(tmp_path):
    report = inspect_env_file(write(tmp_path, "UNIFI_VERIFY_SLL=false\nunifi_timeout=5\nSOMETHING_ELSE=1\nSOMETHING_ELSE=2\n"))
    assert report.unknown == [(1, "UNIFI_VERIFY_SLL", "UNIFI_VERIFY_SSL"), (2, "unifi_timeout", "UNIFI_TIMEOUT"),
                              (3, "SOMETHING_ELSE", "")]                                  # a repeat is listed once
    assert report.settings == 0 and not report.clean


def test_a_key_that_is_not_a_plain_name_is_never_repeated(tmp_path):
    long_name = "A" * (config.MAX_NAME_LENGTH + 1)
    report = inspect_env_file(write(tmp_path, f"{MARKER}-key=1\n{long_name}=2\n9LIVES=3\n"))
    assert report.unknown == [(1, "", ""), (2, "", ""), (3, "", "")]


def test_hlp_env_in_the_file_is_reported_because_it_cannot_work_there(tmp_path):
    report = inspect_env_file(write(tmp_path, "HLP_ENV=other.env\nUNIFI_URL=u\n"))
    assert report.misplaced == [1] and report.unknown == [] and not report.clean


def test_a_line_the_parser_cannot_read_is_reported_by_number(tmp_path):
    report = inspect_env_file(write(tmp_path, f'UNIFI_URL=u\nUNIFI_API_KEY="{MARKER}\nnot a setting at all\n'))
    assert report.bad_lines and all(isinstance(n, int) for n in report.bad_lines) and not report.clean


def test_a_blank_value_is_reported_but_only_the_last_line_of_a_setting_counts(tmp_path):
    report = inspect_env_file(write(tmp_path, "UNIFI_TIMEOUT=\nLOG_LEVEL\nLOG_FORMAT=\nLOG_FORMAT=json\nUNIFI_SITE_ID=  \n"))
    assert report.empty == ["UNIFI_TIMEOUT", "LOG_LEVEL", "UNIFI_SITE_ID"]
    assert report.duplicates == {"LOG_FORMAT": [3, 4]}


def test_a_variable_the_environment_sets_differently_is_reported_by_name_only(tmp_path, monkeypatch):
    path = write(tmp_path, f"UNIFI_SITE_ID=from-file\nUNIFI_TIMEOUT=5\nLOG_LEVEL=${{HOME}}\nUNIFI_URL={MARKER}\nLOG_FORMAT\n")
    monkeypatch.setenv("UNIFI_SITE_ID", "from-shell")
    monkeypatch.setenv("UNIFI_TIMEOUT", "5")                     # the same value: nothing to be confused about
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")                     # refers to another variable: not compared
    monkeypatch.setenv("LOG_FORMAT", "json")                     # the file gives no value
    monkeypatch.setenv("UNIFI_URL", "")                          # an empty variable beats the file as well
    assert inspect_env_file(path).overridden == ["UNIFI_SITE_ID", "UNIFI_URL"]


def test_an_unreadable_file_is_left_to_load_config(tmp_path):
    assert inspect_env_file(tmp_path / "missing.env") == EnvFileReport()
    assert inspect_env_file(tmp_path) == EnvFileReport()                       # a directory
    bad = tmp_path / "latin.env"
    bad.write_bytes(b"UNIFI_URL=caf\xe9\n")
    assert inspect_env_file(bad) == EnvFileReport()
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.env")


# -- the check -------------------------------------------------------------------------------------------------

def test_a_clean_file_passes_the_check(tmp_path):
    found = check_for(write(tmp_path, "UNIFI_URL=https://c.example\nUNIFI_API_KEY=k\n"))
    assert found.status == OK and found.message == "2 settings, each listed once, all recognized" and found.fix == ""
    assert check_for(write(tmp_path, "UNIFI_URL=https://c.example\n", "one.env")).message.startswith("1 setting, ")


def test_every_problem_is_worded_with_names_and_lines(tmp_path):
    path = write(tmp_path, "UNIFI_VERIFY_SSL=true\nUNIFI_VERIFY_SSL=false\nUNIFI_VERIFY_SLL=false\n"
                           f"{MARKER}-key=1\nHLP_ENV=x\nUNIFI_TIMEOUT=\nUNIFI_API_KEY=\"{MARKER}\nSITE\n")
    found = check_for(path)
    assert found.status == WARN
    for text in ("UNIFI_VERIFY_SSL is listed 2 times (lines 1, 2) and the last one (line 2) is used",
                 "UNIFI_VERIFY_SLL (line 3) is not a setting, did you mean UNIFI_VERIFY_SSL?",
                 "line 4 sets a name that is not a setting", "HLP_ENV (line 5) does nothing in a .env file",
                 "UNIFI_TIMEOUT is empty, which counts as not set", "cannot be read (check quotes and the = sign)"):
        assert text in found.message, text
    for text in ("keep one line for each setting", "correct or remove the names", "fix or remove those lines",
                 "give an empty setting a value"):
        assert text in found.fix, text
    assert "unset the variable in the shell" not in found.fix


def test_a_shell_variable_that_wins_is_explained(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFI_SITE_ID", "other")
    found = check_for(write(tmp_path, "UNIFI_URL=u\nUNIFI_SITE_ID=lab\n"))
    assert found.status == WARN and "UNIFI_SITE_ID is also set in the environment with another value" in found.message
    assert "the environment wins over the .env file" in found.message and "unset the variable in the shell" in found.fix


def test_a_warning_does_not_change_the_exit_code(tmp_path):
    checks = run_checks(Options(env_file=write(tmp_path, "UNIFI_URL=u\nUNIFI_URL=u\n"), offline=True))
    assert next(c for c in checks if c.id == "config.env_contents").status == WARN
    assert "config.env_contents" not in {c.id for c in checks if c.status == FAIL}


def test_without_a_file_the_check_is_skipped(tmp_path):
    found = check_for(None)
    assert found.status == SKIP and found.message == "skipped: there is no .env file to look at"
    assert check_for(tmp_path / "nope.env").status == SKIP                      # a named file that does not exist


def test_no_value_reaches_any_output(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFI_SITE_ID", f"shell-{MARKER}")
    path = write(tmp_path, f"UNIFI_URL=https://{MARKER}.example\nUNIFI_URL={MARKER}\nUNIFI_SITE_ID={MARKER}\n"
                           f"UNIFI_VERIFY_SLL={MARKER}\n{MARKER}-key={MARKER}\nUNIFI_API_KEY=\"{MARKER}\nLOG_LEVEL=\n")
    checks = run_checks(Options(env_file=path, offline=True))
    found = next(c for c in checks if c.id == "config.env_contents")
    assert found.status == WARN and "UNIFI_URL is listed 2 times" in found.message
    assert MARKER not in render(checks) and MARKER not in json.dumps(to_dict(checks))
    assert MARKER not in found.message + found.fix


# -- the list of settings stays complete -----------------------------------------------------------------------

def test_the_known_variables_are_the_ones_documented_tested_and_read():
    example = (ROOT / "example.env").read_text(encoding="utf-8")
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z_]+)=", example, re.MULTILINE))
    assert documented == set(KNOWN_VARIABLES)
    assert set(CONFIG_VARIABLES) - {config.ENV_FILE_VAR} == set(KNOWN_VARIABLES)
    source = (ROOT / "homelab_probe" / "config.py").read_text(encoding="utf-8")
    read = set(re.findall(r'(?:getenv|get)\("((?:UNIFI|NOTIFY|LOG|ALLOW)_[A-Z_]+)"', source))
    assert read <= set(KNOWN_VARIABLES)
    assert len(KNOWN_VARIABLES) == len(set(KNOWN_VARIABLES))


def test_the_helper_that_names_a_closest_setting_ignores_distant_names():
    assert config._suggestion("UNIFI_URI") == "UNIFI_URL" and config._suggestion("COMPLETELY_DIFFERENT") == ""
