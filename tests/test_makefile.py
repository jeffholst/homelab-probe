"""The maintainer's shortcuts (Makefile) run what CI runs and never touch the data kept next to the code."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")
PROTECTED = (".env", "hlp.toml", "users.json", "audit.log", "snapshots", "certs", "recovery", "git clean")

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")


def targets() -> list:
    return re.findall(r"^([a-z0-9-]+):.*## .+$", MAKEFILE, flags=re.M)


def dry_run(target: str) -> str:
    return subprocess.run(["make", "-n", "-C", str(ROOT), target], capture_output=True, text=True, check=True).stdout


def test_every_target_is_phony_and_has_a_help_line():
    phony = re.search(r"^\.PHONY:(.*)$", MAKEFILE, flags=re.M).group(1).split()
    assert sorted(phony) == sorted(targets())
    shown = subprocess.run(["make", "-C", str(ROOT), "help"], capture_output=True, text=True, check=True).stdout
    for name in targets():
        assert re.search(rf"^\s+{name}\s", shown, flags=re.M), name


def test_ci_runs_the_commands_the_ci_workflow_runs():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    plan = dry_run("ci")
    for command, in_make in (("uv lock --check", "uv lock --check"), ("uv run ruff check .", "ruff check ."),
                             ("uv run mypy", "python -m mypy"), ("uv run coverage report", "coverage report"),
                             ("npm run check:types", "npm --prefix web run check")):
        assert command in ci and in_make in plan, command
    assert "pytest" in dry_run("test") and "coverage run -m pytest" in plan


def test_clean_names_only_disposable_paths():
    for target in ("clean", "clean-all"):
        plan = dry_run(target)
        for name in PROTECTED:
            assert name not in plan, f"make {target} mentions {name}"
        assert "git" not in plan and " * " not in plan
    assert ".venv" in dry_run("clean-all") and ".venv" not in dry_run("clean").replace("./.venv/", "")
