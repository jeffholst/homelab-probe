"""CHANGELOG.md, the release notes tool and the release workflow.

A release is public and hard to undo, so what can go wrong is pinned here: a changelog that lacks the version or a
command, a tag that disagrees with the package, a release made on a pull request, a workflow that can write more
than it must.
"""

import re
import sys
from pathlib import Path

import pytest
from release_notes import ReleaseError, changelog_section, main, package_version, release_notes

import unifi_sentinel
from unifi_sentinel.audit import AUDIT_CODES
from unifi_sentinel.commands import COMMANDS
from unifi_sentinel.diagnose import CODES
from unifi_sentinel.firewall import FIREWALL_CODES

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")


def without_comments(text):
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


RELEASE_CODE = without_comments(RELEASE)
ALLOWED_GROUPS = {"Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"}


def version_key(text):
    return tuple(int(part) for part in text.split("."))


# -- the changelog -------------------------------------------------------------------------------------------

def test_there_is_an_entry_for_the_current_version():
    date, body = changelog_section(CHANGELOG, unifi_sentinel.__version__)
    assert date and (date == "Unreleased" or re.fullmatch(r"\d{4}-\d{2}-\d{2}", date)) and body.strip()


def test_the_entries_are_in_descending_version_order_and_the_top_one_is_the_current_version():
    versions = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", CHANGELOG, re.M)
    assert versions and versions == sorted(versions, key=version_key, reverse=True)
    top = re.findall(r"^## \[([^\]]+)\]", CHANGELOG, re.M)[0]
    assert top in (unifi_sentinel.__version__, "Unreleased")


def test_only_the_standard_groups_are_used():
    groups = set(re.findall(r"^### (.+)$", CHANGELOG, re.M))
    assert groups and groups <= ALLOWED_GROUPS


def test_the_page_says_what_scripts_can_rely_on():
    contract = CHANGELOG.split("## What scripts can rely on", 1)[1].split("\n## ", 1)[0]
    for code in ("`0`", "`1`", "`2`", "`3`", "`4`", "`64`"):
        assert code in contract, f"exit code {code}"
    for phrase in ("Finding codes", "never renamed or reused", "`version`", "`schema_version`", "Every request is a GET"):
        assert phrase in contract, phrase


def test_every_command_is_mentioned_so_a_new_one_needs_a_changelog_line():
    missing = [command.name for command in COMMANDS if f"`{command.name}`" not in CHANGELOG]
    assert not missing, f"add {missing} to CHANGELOG.md"


def test_every_finding_code_named_in_the_changelog_exists():
    known = set(CODES) | set(AUDIT_CODES) | set(FIREWALL_CODES)
    prefixes = {code.split(".")[0] for code in known}
    named = {token for token in re.findall(r"`([a-z]+\.[a-z_]+)`", CHANGELOG) if token.split(".")[0] in prefixes}
    assert named and named <= known, named - known


def test_the_readme_links_to_the_changelog_and_says_how_to_install_a_tagged_version():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "(CHANGELOG.md)" in readme and "uv tool install git+https://github.com/jeffholst/unifi-sentinel@v" in readme
    assert "pip install git+https://github.com/jeffholst/unifi-sentinel@v" in readme


# -- the release notes tool -----------------------------------------------------------------------------------

def tree(tmp_path, version="1.2.3", changelog=None):
    (tmp_path / "unifi_sentinel").mkdir()
    (tmp_path / "unifi_sentinel" / "__init__.py").write_text(f'"""x"""\n\n__version__ = "{version}"\n')
    text = changelog if changelog is not None else (
        "# Changelog\n\n## [Unreleased]\n\n### Added\n\n- next thing\n\n## [1.2.3] - 2026-10-02\n\n### Added\n\n"
        "- the thing\n\n### Fixed\n\n- the bug\n\n## [1.2.2] - 2026-09-01\n\n- older\n")
    (tmp_path / "CHANGELOG.md").write_text(text)
    return tmp_path


def test_the_notes_are_the_entry_without_its_heading_or_its_neighbors(tmp_path):
    notes = release_notes("v1.2.3", tree(tmp_path))
    assert notes == "### Added\n\n- the thing\n\n### Fixed\n\n- the bug\n"
    assert "1.2.3" not in notes and "next thing" not in notes and "older" not in notes


def test_the_last_entry_runs_to_the_end_of_the_file(tmp_path):
    assert release_notes("v1.2.2", tree(tmp_path, "1.2.2")) == "- older\n"


@pytest.mark.parametrize("tag", ["1.2.3", "v1.2.4", "V1.2.3", "v1.2", "v1.2.3-rc1", "", "latest"])
def test_a_tag_that_is_not_v_plus_the_package_version_is_refused(tmp_path, tag):
    with pytest.raises(ReleaseError, match="does not match the package version: expected 'v1.2.3'"):
        release_notes(tag, tree(tmp_path))


def test_a_version_without_a_changelog_entry_is_refused(tmp_path):
    with pytest.raises(ReleaseError, match="no entry for version 1.2.4"):
        release_notes("v1.2.4", tree(tmp_path, "1.2.4"))


@pytest.mark.parametrize("heading", ["## [1.2.3] - Unreleased", "## [1.2.3]", "## [1.2.3] - soon", "## [1.2.3] - 2026-1-2",
                                     "## [1.2.3] - 02/10/2026"])
def test_an_entry_without_a_real_date_is_refused(tmp_path, heading):
    root = tree(tmp_path, changelog=f"# Changelog\n\n{heading}\n\n### Added\n\n- x\n")
    with pytest.raises(ReleaseError, match="give it the release date"):
        release_notes("v1.2.3", root)


def test_an_empty_entry_is_refused(tmp_path):
    root = tree(tmp_path, changelog="# Changelog\n\n## [1.2.3] - 2026-10-02\n\n## [1.2.2] - 2026-09-01\n\n- older\n")
    with pytest.raises(ReleaseError, match="is empty"):
        release_notes("v1.2.3", root)


def test_a_package_without_a_version_is_refused(tmp_path):
    root = tree(tmp_path)
    (root / "unifi_sentinel" / "__init__.py").write_text('"""no version here"""\n')
    with pytest.raises(ReleaseError, match="has no __version__"):
        package_version(root)


def test_the_real_changelog_becomes_publishable_once_it_has_a_date(tmp_path):
    """Today the entry says Unreleased, which the tool refuses on purpose; dating it must be enough."""
    version = unifi_sentinel.__version__
    root = tree(tmp_path, version, CHANGELOG.replace(f"## [{version}] - Unreleased", f"## [{version}] - 2026-10-02"))
    notes = release_notes(f"v{version}", root)
    assert notes.startswith("The first tagged release") or notes.startswith("###") or notes.strip()
    assert f"[{version}]" not in notes and "## " not in notes.replace("### ", "")


def test_the_command_line_prints_the_notes_or_the_reason(tmp_path, monkeypatch, capsys):
    import release_notes as module

    monkeypatch.setattr(module, "ROOT", tree(tmp_path))
    assert main(["v1.2.3"]) == 0 and "- the thing" in capsys.readouterr().out
    assert main(["v9.9.9"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("ERROR: the tag 'v9.9.9' does not match")
    assert main([]) == 64 and main(["a", "b"]) == 64 and "usage" in capsys.readouterr().err


def test_the_tool_runs_as_a_script(tmp_path):
    import subprocess

    done = subprocess.run([sys.executable, str(ROOT / "tools" / "release_notes.py"), "v0.0.0"], capture_output=True,
                          text=True, check=False)
    assert done.returncode == 1 and "does not match the package version" in done.stderr and done.stdout == ""


# -- the release workflow -----------------------------------------------------------------------------------------

def test_the_release_runs_only_when_a_version_tag_is_pushed():
    assert re.search(r"^on:\n  push:\n    tags: \[\"v\*\"\]\n", RELEASE, re.M)
    assert "pull_request" not in RELEASE and "branches" not in RELEASE and "workflow_dispatch" not in RELEASE


def test_only_the_release_job_can_write_and_everything_else_is_read_only():
    writers = [path.name for path in sorted(WORKFLOWS.glob("*.yml"))
               if re.search(r"write", without_comments(path.read_text()))]
    assert writers == ["release.yml"]
    assert RELEASE_CODE.count("contents: write") == 1 and RELEASE_CODE.count("contents: read") == 1
    assert RELEASE_CODE.index("contents: read") < RELEASE_CODE.index("contents: write")   # read at the top, write on the job
    assert "permissions:\n  contents: read\n\njobs:" in RELEASE_CODE
    assert "contents: read" in (WORKFLOWS / "ci.yml").read_text()


def test_the_release_checks_before_it_publishes_and_in_the_right_order():
    order = ["uv lock --check", "uv sync --locked", "tools/release_notes.py", "uv run pytest -q", "uv build",
             "gh release create"]
    positions = [RELEASE_CODE.index(step) for step in order]
    assert positions == sorted(positions)
    assert '--verify-tag' in RELEASE_CODE and '--notes-file release-notes.md' in RELEASE_CODE and "dist/*" in RELEASE_CODE


def test_the_tag_reaches_the_shell_only_through_the_environment():
    """A tag name is text someone chose: interpolating it into a command line would run it."""
    run_lines = [line for line in RELEASE_CODE.splitlines() if re.match(r"\s+run:", line)]
    assert run_lines and not any("${{" in line for line in run_lines)
    assert 'TAG: ${{ github.ref_name }}' in RELEASE and run_lines and all('"$TAG"' in line or "$TAG" not in line
                                                                          for line in run_lines)


def test_the_release_uses_no_third_party_action_and_the_token_is_the_built_in_one():
    actions = set(re.findall(r"uses:\s*([\w./-]+)@", RELEASE_CODE))
    assert actions == {"actions/checkout", "astral-sh/setup-uv"}
    assert re.findall(r"GH_TOKEN: (.+)", RELEASE_CODE) == ["${{ secrets.GITHUB_TOKEN }}"]
    assert "secrets." not in RELEASE_CODE.replace("secrets.GITHUB_TOKEN", "")
    assert "enable-cache: false" in RELEASE_CODE                                           # no cache shared with other jobs
