"""Golden files: the main text renderers against stored output from the synthetic fixture.

A failure means the output of a command changed. If the change is intended, refresh the files and
review the diff like code:

    UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py

Times, ages and snapshot names are replaced by placeholders (see golden_support.normalise), so the
files do not change from day to day. The README's sample blocks are checked against these files by
tests/test_docs_drift.py.
"""

import difflib
import os

import pytest
from golden_support import CASES, GOLDEN, normalise, run_command


def expected_path(name):
    return GOLDEN / f"{name}.txt"


@pytest.mark.parametrize("name", sorted(CASES))
def test_output_matches_the_golden_file(fake_client, name):
    code, out, err = run_command(fake_client, CASES[name])
    assert code in (0, 1, 2), f"{CASES[name]} failed with exit {code}: {err}"
    actual = normalise(out)
    path = expected_path(name)
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        return
    assert path.exists(), f"no golden file {path.name}; create it with UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py"
    expected = path.read_text(encoding="utf-8")
    if actual != expected:
        diff = "\n".join(difflib.unified_diff(expected.splitlines(), actual.splitlines(),
                                              f"golden/{path.name}", "actual", lineterm=""))
        pytest.fail(f"`hlp {' '.join(CASES[name])}` changed:\n{diff}\n\n"
                    "If this is intended: UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py, review the diff, "
                    "and update the README sample if there is one.")


def test_every_golden_file_has_a_case():
    stray = {p.stem for p in GOLDEN.glob("*.txt")} - set(CASES)
    assert not stray, f"golden files without a case: {sorted(stray)}"


def test_the_normaliser_hides_only_what_changes_between_runs():
    text = ("Last: 2026-10-01 05:11 (6h ago): download 880 Mbps\n"
            "Comparing snapshot-20261001-011530Z.json (captured 2026-09-30 20:15)\n"
            "Name         Status\n-----------  -------\nold-tablet   Offline   \n\n\n")
    assert normalise(text) == ("Last: <time> (<age> ago): download 880 Mbps\n"
                               "Comparing snapshot-<stamp>.json (captured <time>)\n"
                               "Name  Status\n---  ---\nold-tablet  Offline\n")
    assert normalise("  indented  line\n") == "  indented  line\n"
    assert normalise("a     b\n") == "a  b\n"                          # a long run of padding becomes two spaces


def test_the_output_is_stable_between_two_runs(fake_client):
    from conftest import FakeSession

    from homelab_probe.client import UniFiClient
    other = UniFiClient("https://controller", "key")
    other.session = FakeSession()
    for name in ("diagnose_with_events", "wan", "events"):
        assert normalise(run_command(fake_client, CASES[name])[1]) == normalise(run_command(other, CASES[name])[1])
