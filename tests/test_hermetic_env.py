"""Guards for conventions that review kept finding broken: the hermetic environment and errors that escape main."""

import dataclasses
import re
from pathlib import Path

import pytest
from conftest import CONFIG_VARIABLES

from unifi_sentinel import cli
from unifi_sentinel.commands import COMMANDS_BY_NAME

PACKAGE = Path(__file__).resolve().parent.parent / "unifi_sentinel"


def variables_read():
    """The environment variables the package reads, from its source."""
    found = set()
    for path in PACKAGE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        found |= set(re.findall(r'(?:getenv|environ\.get|environ\[)\(?"([A-Z][A-Z0-9_]+)"', text))
        found |= set(re.findall(r'^[A-Z_]*ENV[A-Z_]*VAR\w*\s*=\s*"([A-Z][A-Z0-9_]+)"', text, re.M))
    return found


def test_every_variable_the_package_reads_is_cleared_for_each_test():
    """A new setting that is not in conftest.CONFIG_VARIABLES lets a developer's own environment change the
    tests (found in review twice). Add it there."""
    missing = variables_read() - set(CONFIG_VARIABLES)
    assert not missing, f"add {sorted(missing)} to CONFIG_VARIABLES in tests/conftest.py"
    assert variables_read() >= {"CONTROLLER_URL", "API_KEY", "UNIFI_SENTINEL_ENV", "PARALLEL_REQUESTS"}   # the scan works


def test_no_variable_is_cleared_that_nothing_reads():
    assert set(CONFIG_VARIABLES) - variables_read() == set()


def run(fake_client, monkeypatch, argv):
    monkeypatch.setenv("CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    return cli.main(argv)


def test_a_file_that_cannot_be_written_is_exit_3_with_a_reason_not_a_traceback(fake_client, monkeypatch, capsys,
                                                                                tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory is needed")
    assert run(fake_client, monkeypatch, ["snapshot", "--dir", str(blocker / "sub")]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "Traceback" not in err and err.strip().splitlines()[-1].startswith("ERROR: Not a directory (")
    assert "blocker" in err


@pytest.mark.parametrize("error, message", [
    (PermissionError(13, "Permission denied", "/some/file"), "ERROR: Permission denied (/some/file)"),
    (OSError(28, "No space left on device"), "ERROR: No space left on device"),
    (OSError(), "ERROR: OSError"),
])
def test_any_os_error_from_a_command_is_reported_the_same_way(fake_client, monkeypatch, capsys, error, message):
    def broken(ctx):
        raise error

    patched = dataclasses.replace(COMMANDS_BY_NAME["info"], run=broken)
    monkeypatch.setitem(COMMANDS_BY_NAME, "info", patched)
    assert run(fake_client, monkeypatch, ["info"]) == cli.EXIT_ERROR
    assert capsys.readouterr().err.strip() == message
