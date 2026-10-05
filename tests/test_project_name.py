"""The project is Homelab Probe (`hlp`); the names it had before must not come back, and the new ones must agree."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from homelab_probe import cli, completion, settings

ROOT = Path(__file__).resolve().parent.parent

# The old names are built from pieces, so this file does not contain them itself.
OLD_PROJECT = re.compile("unifi" + r"[-_ ]?" + "sentinel", re.I)
RETIRED_SETTINGS = re.compile(
    r"(?<![A-Z_])(?:" + "|".join(("CONTROLLER" + "_URL", "API" + "_KEY", "SITE" + "_ID", "VERIFY" + "_SSL",
                                  "TIME" + "OUT", "PARALLEL" + "_REQUESTS")) + r")(?![A-Z0-9_])")
# The CHANGELOG names the old spellings in the entry that announces the rename.
ALLOWED = {"CHANGELOG.md", "tests/test_project_name.py"}

needs_git = pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(),
                               reason="needs a git checkout to list the tracked files")


def tracked_text_files():
    names = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split("\n")
    for name in filter(None, names):
        if name == "uv.lock" or name in ALLOWED:
            continue
        try:
            yield name, (ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue


@needs_git
def test_no_tracked_file_uses_the_old_project_name():
    found = [f"{name}:{n}: {line.strip()[:100]}" for name, text in tracked_text_files()
             for n, line in enumerate(text.splitlines(), 1) if OLD_PROJECT.search(line)]
    assert not found, "the project is Homelab Probe / hlp / homelab_probe now:\n" + "\n".join(found[:20])


@needs_git
def test_no_tracked_file_uses_a_retired_setting_name():
    found = [f"{name}:{n}: {line.strip()[:100]}" for name, text in tracked_text_files()
             for n, line in enumerate(text.splitlines(), 1) if RETIRED_SETTINGS.search(line)]
    assert not found, "the UniFi settings carry the UNIFI_ prefix now:\n" + "\n".join(found[:20])


def test_the_old_name_patterns_do_match_what_they_guard():
    assert OLD_PROJECT.search("uni" + "fi-sent" + "inel") and OLD_PROJECT.search("UniFi " + "Sentinel")
    assert RETIRED_SETTINGS.search("CONTROLLER" + "_URL=x") and RETIRED_SETTINGS.search("\\nAPI" + "_KEY=x")
    assert not RETIRED_SETTINGS.search("UNIFI_URL X-API-KEY DEFAULT_TIME" + "OUT UNIFI_API_KEY")


def test_the_names_agree():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^name = "homelab-probe"$', text, re.M)
    assert re.search(r'^hlp = "homelab_probe\.cli:main"$', text, re.M)
    assert '"homelab_probe.server"' in text and re.search(r"^\[project\.scripts\]\nhlp = ", text, re.M)
    assert completion.PROGRAM == "hlp" == cli.build_parser().prog
    assert (ROOT / "hlp.py").is_file() and (ROOT / "hlp.example.toml").is_file()
    assert not (ROOT / "homelab_probe" / "demo" / "unifi_sentinel").exists()


def test_the_settings_file_is_hlp_toml_everywhere():
    assert settings.DEFAULT_FILENAME == "hlp.toml"
    assert "hlp.toml" in (ROOT / ".gitignore").read_text(encoding="utf-8").split()
    settings.load_settings(ROOT / "hlp.example.toml")           # the shipped example loads


def test_the_schemas_point_at_the_renamed_repository():
    base = "https://raw.githubusercontent.com/jeffholst/homelab-probe/main/docs/schemas/"
    schemas = sorted((ROOT / "docs" / "schemas").glob("*.json"))
    assert len(schemas) >= 20
    for path in schemas:
        assert json.loads(path.read_text(encoding="utf-8"))["$id"] == base + path.name, path.name
    webhook = json.loads((ROOT / "docs" / "schemas" / "webhook-payload.v1.schema.json").read_text(encoding="utf-8"))
    assert webhook["properties"]["source"] == {"const": "homelab-probe"}


def test_every_unifi_setting_the_code_reads_is_in_the_example_and_the_docs():
    config = (ROOT / "homelab_probe" / "config.py").read_text(encoding="utf-8")
    doctor = (ROOT / "homelab_probe" / "doctor.py").read_text(encoding="utf-8")
    names = set(re.findall(r'"(UNIFI_[A-Z_]+)"', config + doctor))
    assert {"UNIFI_URL", "UNIFI_API_KEY", "UNIFI_SITE_ID", "UNIFI_VERIFY_SSL", "UNIFI_TIMEOUT",
            "UNIFI_PARALLEL_REQUESTS"} <= names
    example = (ROOT / "example.env").read_text(encoding="utf-8")
    docs = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    for name in sorted(names):
        assert name in example, f"example.env does not mention {name}"
        assert f"`{name}`" in docs, f"docs/configuration.md does not list {name}"
