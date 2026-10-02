"""``pytest -m live`` without credentials skips instead of failing, and a plain run never selects live tests.

Both run pytest in a subprocess inside the test's empty working directory with the configuration variables
unset (the autouse fixture in conftest.py), so no controller can be reached.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_pytest(*args):
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(ROOT / "pyproject.toml"), "--rootdir", str(ROOT),
         "-p", "no:cacheprovider", "-q", str(ROOT / "tests" / "test_live_contract.py"), *args],
        capture_output=True, text=True, check=False)


def test_live_tests_are_skipped_without_credentials():
    done = run_pytest("-m", "live", "-rs")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "no controller configured" in done.stdout
    assert "passed" not in done.stdout and "failed" not in done.stdout


def test_a_plain_run_deselects_the_live_tests():
    done = run_pytest()
    assert done.returncode == 5                                # pytest: nothing was collected to run
    assert "deselected" in done.stdout
