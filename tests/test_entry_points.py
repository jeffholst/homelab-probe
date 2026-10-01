"""The ways the program is started: the launcher script, the installed console script and
`python -m`, each in a subprocess, from a directory that is not the project root."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from unifi_sentinel import __version__, cli

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "unifi-sentinel.py"


def console_script():
    name = "unifi-sentinel.exe" if sys.platform.startswith("win") else "unifi-sentinel"
    path = Path(sys.executable).parent / name
    return path if path.exists() else None


ENTRY_POINTS = {
    "launcher": [sys.executable, str(LAUNCHER)],
    "module": [sys.executable, "-m", "unifi_sentinel.cli"],
}
if console_script():
    ENTRY_POINTS["console script"] = [str(console_script())]


def start(entry, args, cwd, **env):
    environment = {k: v for k, v in os.environ.items()
                   if k not in ("CONTROLLER_URL", "API_KEY", "SITE_ID", "VERIFY_SSL", "TIMEOUT", "UNIFI_SENTINEL_ENV")}
    environment.update(env)
    return subprocess.run(ENTRY_POINTS[entry] + args, cwd=cwd, env=environment, capture_output=True, text=True,
                          timeout=60)


@pytest.fixture(params=sorted(ENTRY_POINTS))
def entry(request):
    return request.param


def test_version_is_the_package_version(entry, tmp_path):
    result = start(entry, ["--version"], tmp_path)
    assert result.returncode == 0 and result.stdout.strip() == __version__ and result.stderr == ""


def test_help_lists_every_command(entry, tmp_path):
    result = start(entry, ["--help"], tmp_path)
    assert result.returncode == 0 and result.stdout.startswith("usage: unifi-sentinel")
    for command in ("export", "query", "new-clients", "events", "client", "topology", "snapshot", "diff", "wifi",
                    "wan", "diagnose", "info"):
        assert re.search(rf"\b{command}\b", result.stdout), command


def test_every_command_has_its_own_help(entry, tmp_path):
    for command in ("query", "diagnose", "snapshot"):
        result = start(entry, [command, "--help"], tmp_path)
        assert result.returncode == 0 and result.stdout.startswith(f"usage: unifi-sentinel {command}"), command


@pytest.mark.parametrize("args", [["nonsense"], ["diagnose", "--fail-on", "bad"], ["query", "devices", "--down"],
                                  ["--timeout", "0", "info"], []])
def test_a_usage_error_is_exit_64_with_the_usage_on_stderr(entry, tmp_path, args):
    result = start(entry, args, tmp_path)
    assert result.returncode == cli.EXIT_USAGE == 64
    assert "usage: unifi-sentinel" in result.stderr and "error:" in result.stderr and result.stdout == ""


def test_a_missing_configuration_is_exit_3_without_touching_a_network(entry, tmp_path):
    result = start(entry, ["info"], tmp_path)
    assert result.returncode == cli.EXIT_ERROR == 3
    assert "ERROR: CONTROLLER_URL is not set" in result.stderr and result.stdout == ""


def test_the_env_file_is_read_from_the_directory_it_is_run_in_not_the_project(entry, tmp_path):
    (tmp_path / ".env").write_text("CONTROLLER_URL=http://insecure.invalid\nAPI_KEY=k\n")
    result = start(entry, ["info"], tmp_path)          # a bad URL proves the file in the cwd was found and validated
    assert result.returncode == 3 and "CONTROLLER_URL uses http://" in result.stderr


def test_it_works_from_any_directory_and_in_a_directory_with_spaces(entry, tmp_path):
    odd = tmp_path / "a directory with spaces"
    odd.mkdir()
    assert start(entry, ["--version"], odd).stdout.strip() == __version__


def test_the_launcher_is_a_thin_wrapper():
    source = LAUNCHER.read_text(encoding="utf-8")
    assert "from unifi_sentinel.cli import main" in source and len(source.splitlines()) <= 15


def test_the_console_script_is_declared_in_pyproject():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'unifi-sentinel = "unifi_sentinel.cli:main"' in text
