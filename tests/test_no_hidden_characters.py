"""No invisible or direction-changing characters in the repository's text files.

A text-direction override or a zero-width character in source or docs can make code read differently from how it
runs (a "Trojan Source" attack) and is almost always an accident in a test written to contain one. Write such a
character as an escape (``"\\u202e"``) in a test, never raw.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HIDDEN = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")
SUFFIXES = {".py", ".md", ".toml", ".yml", ".yaml", ".json", ".txt", ".example"}


def tracked_text_files():
    listed = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                            capture_output=True, text=True, check=False).stdout.split()
    return [ROOT / name for name in listed if Path(name).suffix in SUFFIXES and (ROOT / name).is_file()]


def test_no_file_contains_a_hidden_or_direction_changing_character():
    offenders = []
    for path in tracked_text_files():
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if HIDDEN.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}")
    assert not offenders, "write these as escapes, not raw characters: " + ", ".join(offenders)


def test_the_scan_sees_the_files_and_catches_a_character():
    assert any(path.name == "pyproject.toml" for path in tracked_text_files())
    assert HIDDEN.search("a\u202eb") and HIDDEN.search("a\u200bb") and not HIDDEN.search("plain text, emoji \U0001F6D1")
