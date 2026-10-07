"""Line and branch coverage of 100% is a rule of this project (CLAUDE.md), so CI enforces it (issue #151).

These pin the pieces: the settings in pyproject.toml, the CI job that runs them, and the places that tell people how.
Coverage itself is measured by that job; a test cannot measure its own suite.
"""

import re
import sys

import yaml
from docs_support import ROOT

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover  (Python 3.10 only)
    import tomli as tomllib

PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
CI = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
RUN = "uv run coverage run -m pytest -q"
REPORT = "uv run coverage report"


def coverage_job():
    (job,) = [job for job in CI["jobs"].values() if "coverage" in job["name"].lower()]
    return job


def commands(job):
    return [step["run"] for step in job["steps"] if "run" in step]


def test_the_settings_measure_branches_of_the_package_and_fail_under_one_hundred():
    coverage = PYPROJECT["tool"]["coverage"]
    assert coverage["run"] == {"branch": True, "source": ["homelab_probe"]}
    assert coverage["report"]["fail_under"] == 100 and coverage["report"]["show_missing"] is True


def test_coverage_is_a_locked_dev_dependency_not_something_each_run_fetches():
    assert any(re.match(r"coverage\b", dependency) for dependency in PYPROJECT["dependency-groups"]["dev"])
    assert 'name = "coverage"' in (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert ".coverage" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()    # the data file stays out


def test_ci_has_one_job_that_runs_the_tests_under_coverage_and_then_the_report():
    job = coverage_job()
    steps = commands(job)
    # the server tests, the styling tests and their coverage need the extras
    assert "uv sync --locked --extra web --extra pretty" in steps
    assert RUN in steps and REPORT in steps and steps.index(RUN) < steps.index(REPORT)
    assert all("--cov" not in step and "--omit" not in step for step in steps)      # nothing narrows what is measured


def test_the_job_runs_on_a_supported_python_and_installs_the_shells_the_completion_tests_skip_without():
    job = coverage_job()
    setup = next(step for step in job["steps"] if str(step.get("uses", "")).startswith("astral-sh/setup-uv"))
    version = setup["with"]["python-version"]
    assert version in {matrix for matrix in CI["jobs"]["test"]["strategy"]["matrix"]["python-version"]}
    installs = " ".join(step for step in commands(job) if "apt-get install" in step)
    assert "fish" in installs and "zsh" in installs
    assert job["runs-on"] == "ubuntu-latest"                                          # util-linux flock is there


def test_the_tests_that_skip_without_a_shell_say_so_and_the_job_is_where_they_run():
    completion = (ROOT / "tests" / "test_completion.py").read_text(encoding="utf-8")
    assert 'reason="fish is not installed"' in completion and 'BASH, ZSH, FISH = shutil.which("bash")' in completion
    scheduling = (ROOT / "tests" / "test_docs_scheduling.py").read_text(encoding="utf-8")
    assert 'skipif(shutil.which("flock") is None' in scheduling


def test_the_docs_give_the_commands_ci_runs():
    for path in ("docs/development.md", "CLAUDE.md"):
        text = (ROOT / path).read_text(encoding="utf-8")
        assert "uv run coverage run -m pytest" in text and "uv run coverage report" in text, path
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "MAINTAINING.md#routine-checks" in contributing
    assert "including coverage and browser checks" in contributing
    maintaining = (ROOT / "MAINTAINING.md").read_text(encoding="utf-8")
    assert "uv run --extra web --extra pretty coverage run -m pytest" in maintaining
    assert "uv run --extra web --extra pretty coverage report" in maintaining
    development = (ROOT / "docs" / "development.md").read_text(encoding="utf-8")
    assert coverage_job()["name"] in development
