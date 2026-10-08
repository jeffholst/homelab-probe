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


def test_nothing_in_the_release_shortcuts_pushes():
    script = (ROOT / "tools" / "release_check.sh").read_text(encoding="utf-8")
    for text in (MAKEFILE, script):
        for line in text.splitlines():
            if "git push" in line:
                assert line.strip().startswith(("echo ", "#")), f"runs a push: {line.strip()}"
    assert "git tag -a" in script and "git tag -a" not in MAKEFILE
    assert "--tag" in dry_run("tag") and "--tag" not in dry_run("release-check")


# The release preflight, run in a throwaway repository with its own origin, a stand-in for the release notes and a
# stand-in `gh`, so it never touches this repository, the network or GitHub.
needs_git = pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None, reason="git or bash missing")


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def release_repo(tmp_path):
    origin, work, bin_dir = tmp_path / "origin.git", tmp_path / "work", tmp_path / "bin"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    (work / "tools").mkdir(parents=True)
    bin_dir.mkdir()
    shutil.copy(ROOT / "tools" / "release_check.sh", work / "tools" / "release_check.sh")
    (work / "tools" / "release_notes.py").write_text(
        "import sys\nsys.exit(1 if sys.argv[1] == 'v9.9.9' else print('- the notes of ' + sys.argv[1]))\n")
    git(work, "init", "-q", "-b", "main")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "release commit")
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-q", "origin", "main")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, capture_output=True, text=True).stdout.strip()
    gh = bin_dir / "gh"
    gh.write_text(f"#!/bin/sh\nprintf '%s\\n' \"${{FAKE_CI:-{sha}\tcompleted\tsuccess}}\"\n")
    gh.chmod(0o755)
    return work, origin, bin_dir


def run_check(repo, *args, env=None):
    work, _origin, bin_dir = repo
    import os
    environment = {**os.environ, "PYTHON": "python3", "PATH": f"{bin_dir}:{os.environ['PATH']}", **(env or {})}
    return subprocess.run(["bash", "tools/release_check.sh", *args], cwd=work, env=environment, capture_output=True, text=True)


@needs_git
def test_release_check_passes_prints_the_notes_and_creates_nothing(release_repo):
    result = run_check(release_repo, "1.2.3")
    assert result.returncode == 0, result.stderr
    assert "the notes of v1.2.3" in result.stdout and "make tag VERSION=1.2.3" in result.stdout
    assert subprocess.run(["git", "tag", "--list"], cwd=release_repo[0], capture_output=True, text=True).stdout == ""


@needs_git
def test_tag_creates_the_local_annotated_tag_and_does_not_push_it(release_repo):
    work, origin, _ = release_repo
    result = run_check(release_repo, "1.2.3", "--tag")
    assert result.returncode == 0, result.stderr
    kind = subprocess.run(["git", "cat-file", "-t", "v1.2.3"], cwd=work, capture_output=True, text=True).stdout.strip()
    assert kind == "tag"                                    # annotated, not lightweight
    assert subprocess.run(["git", "tag", "--list"], cwd=origin, capture_output=True, text=True).stdout == ""
    assert "git push origin v1.2.3" in result.stdout
    again = run_check(release_repo, "1.2.3", "--tag")       # a second run refuses: the tag exists
    assert again.returncode == 1 and "already exists locally" in again.stderr


def dirty(work):
    (work / "stray.txt").write_text("x")


def off_main(work):
    git(work, "switch", "-q", "-c", "topic")


def behind_origin(work):
    git(work, "commit", "-q", "--allow-empty", "-m", "local only")


def tagged_here(work):
    git(work, "tag", "v1.2.3")


def tagged_on_origin(work):
    git(work, "tag", "v1.2.3")
    git(work, "push", "-q", "origin", "v1.2.3")
    git(work, "tag", "-d", "v1.2.3")


@needs_git
@pytest.mark.parametrize("setup, version, reason", [
    (dirty, "1.2.3", "not clean"),
    (off_main, "1.2.3", "not on main"),
    (behind_origin, "1.2.3", "is not origin/main"),
    (tagged_here, "1.2.3", "already exists locally"),
    (tagged_on_origin, "1.2.3", "already exists on origin"),
    (lambda work: None, "9.9.9", "CHANGELOG.md entry"),
    (lambda work: None, "1.2", "X.Y.Z"),
    (lambda work: None, "", "X.Y.Z"),
])
def test_release_check_stops_for_each_problem(release_repo, setup, version, reason):
    setup(release_repo[0])
    result = run_check(release_repo, version)
    assert result.returncode == 1 and reason in result.stderr, result.stderr
    assert run_check(release_repo, version, "--tag").returncode == 1
    assert subprocess.run(["git", "tag", "--list", "v9.9.9"], cwd=release_repo[0], capture_output=True,
                          text=True).stdout == ""


@needs_git
@pytest.mark.parametrize("ci", ["deadbeef\tcompleted\tsuccess", "{sha}\tcompleted\tfailure", "{sha}\tin_progress\t"])
def test_release_check_wants_a_green_ci_run_for_this_commit(release_repo, ci):
    work = release_repo[0]
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, capture_output=True, text=True).stdout.strip()
    result = run_check(release_repo, "1.2.3", "--tag", env={"FAKE_CI": ci.format(sha=sha)})
    assert result.returncode == 1 and "CI run on main" in result.stderr
    assert subprocess.run(["git", "tag", "--list"], cwd=work, capture_output=True, text=True).stdout == ""
