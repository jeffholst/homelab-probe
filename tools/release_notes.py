#!/usr/bin/env python3
"""The release checks and notes for a version tag (a development tool, not part of the package).

    python tools/release_notes.py v0.3.0 > notes.md

Used by ``.github/workflows/release.yml``. It stops (exit 1, with the reason on stderr) unless the tag is
``v`` plus ``homelab_probe.__version__`` and ``CHANGELOG.md`` has a dated entry for that version, then prints that
entry (without its heading) as the release notes. A release is public and hard to undo, so nothing else is allowed
through: not a tag that disagrees with the package, not a missing entry, not one still marked Unreleased.
"""

import re
import sys
from datetime import date
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\](?: - (?P<date>.+?))?\s*$", re.M)
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class ReleaseError(Exception):
    """The release cannot go ahead; the message says why."""


def package_version(root: Optional[Path] = None) -> str:
    root = root or ROOT                    # looked up when called, so a caller (or a test) can point it elsewhere
    match = re.search(r'^__version__ = "([^"]+)"', (root / "homelab_probe" / "__init__.py").read_text(), re.M)
    if not match:
        raise ReleaseError("homelab_probe/__init__.py has no __version__")
    return match.group(1)


def changelog_section(text: str, version: str) -> tuple[Optional[str], str]:
    """``(date, body)`` of the entry for ``version`` (the date is None when the heading has none)."""
    headings = list(HEADING.finditer(text))
    for i, heading in enumerate(headings):
        if heading.group("version") == version:
            end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            return heading.group("date"), text[heading.end():end].strip("\n")
    raise ReleaseError(f"CHANGELOG.md has no entry for version {version}")


def release_notes(tag: str, root: Optional[Path] = None) -> str:
    root = root or ROOT
    version = package_version(root)
    if tag != f"v{version}":
        raise ReleaseError(f"the tag {tag!r} does not match the package version: expected 'v{version}'")
    release_date, body = changelog_section((root / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    if not release_date or not DATE.fullmatch(release_date.strip()):
        raise ReleaseError(f"the CHANGELOG.md entry for {version} is dated {release_date!r}: give it the release date "
                           "(YYYY-MM-DD) before tagging")
    try:
        date.fromisoformat(release_date.strip())
    except ValueError:
        raise ReleaseError(f"the CHANGELOG.md entry for {version} is dated {release_date!r}: give it a real "
                           "release date (YYYY-MM-DD) before tagging") from None
    if not body.strip():
        raise ReleaseError(f"the CHANGELOG.md entry for {version} is empty")
    return body + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: release_notes.py vX.Y.Z", file=sys.stderr)
        return 64
    try:
        sys.stdout.write(release_notes(args[0]))
    except ReleaseError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
