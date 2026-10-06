"""The Dependabot configuration and the CI pins it keeps current.

There is no YAML parser in the project (and none is worth adding for this), so the checks read the files line by line.
The configuration itself was also validated against the official Dependabot 2.0 JSON schema when it was written
(`https://json.schemastore.org/dependabot-2.0.json`); GitHub's Dependabot page shows any error it still finds.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def updates():
    """{ecosystem: its block of lines} from the `updates:` list."""
    blocks, current = {}, None
    for line in CONFIG.splitlines():
        if (match := re.match(r"^  - package-ecosystem: \"([a-z-]+)\"\s*$", line)):
            current = blocks.setdefault(match.group(1), [])
        elif current is not None:
            current.append(line)
    return blocks


def test_the_ecosystems_are_configured_weekly_from_their_manifests_directory_and_grouped():
    blocks = updates()
    assert set(blocks) == {"github-actions", "uv", "npm"}
    for name, lines in blocks.items():
        text = "\n".join(lines)
        assert f'directory: "{"/web" if name == "npm" else "/"}"' in text, name
        assert 'interval: "weekly"' in text, name
        assert re.search(r'patterns: \["\*"\]', text), f"{name}: one grouped pull request, not one per package"


def test_the_version_is_2_and_nothing_else_is_at_the_top_level_but_updates():
    top = [line.split(":")[0] for line in CONFIG.splitlines() if re.match(r"^[a-z]", line)]
    assert top == ["version", "updates"] and "version: 2\n" in CONFIG


def test_every_action_in_every_workflow_is_pinned_to_an_exact_version():
    """astral-sh/setup-uv publishes only exact tags: a floating `@v10` does not exist and breaks CI (found in #89).
    Dependabot only keeps exact pins current, so a branch name or a short tag would also go unnoticed."""
    uses = [(path.name, match.group(1), match.group(2))
            for path in WORKFLOWS for match in re.finditer(r"uses:\s*([\w./-]+)@(\S+)", path.read_text())]
    assert uses and {name for _, name, _ in uses} >= {"actions/checkout", "astral-sh/setup-uv"}
    for workflow, action, ref in uses:
        if action == "astral-sh/setup-uv":
            assert re.fullmatch(r"v\d+\.\d+\.\d+", ref), f"{workflow}: {action}@{ref} is not an exact version"
        else:
            assert re.fullmatch(r"v\d+(\.\d+){0,2}", ref), f"{workflow}: {action}@{ref} is not a release tag"


def test_the_workflows_check_the_lockfile_so_a_dependency_update_cannot_leave_it_stale():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "uv lock --check" in ci and "uv sync --locked" in ci
    assert "pull_request:" in ci                                   # Dependabot pull requests run the whole suite


def test_the_npm_entry_points_at_the_directory_of_the_one_package_and_its_lockfile_and_ci_installs_from_it():
    assert (ROOT / "web" / "package.json").is_file() and (ROOT / "web" / "package-lock.json").is_file()
    assert not list((ROOT / "web").glob("yarn.lock")) and not list((ROOT / "web").glob("pnpm-lock.yaml"))
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "npm ci" in ci and "working-directory: web" in ci       # an exact install: a lock change shows in the diff


def test_dependencies_are_declared_where_dependabots_uv_ecosystem_reads_them():
    assert (ROOT / "uv.lock").is_file() and (ROOT / "pyproject.toml").is_file()
    assert "[dependency-groups]" in (ROOT / "pyproject.toml").read_text()
