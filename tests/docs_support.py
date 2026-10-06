"""Shared by the tests that read the documentation: the README is the quickstart and the detail is in ``docs/``."""

import re
from pathlib import Path
from typing import Dict, List, Set, Tuple

from build_docs import anchors_of, heading_texts, plain_heading, slugify  # tools/, which pytest puts on the path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
DOCS = ROOT / "docs"


def doc_paths() -> List[Path]:
    """The README, then every page in docs/."""
    return [README, *sorted(DOCS.glob("*.md"))]


def all_docs_text() -> str:
    """Every documentation file, one after the other."""
    return "\n".join(path.read_text(encoding="utf-8") for path in doc_paths())


def headings(text: str) -> List[str]:
    """The heading texts of a Markdown file, in order, ignoring lines inside fenced code blocks."""
    return heading_texts(text)


def slug(heading: str) -> str:
    """The anchor GitHub gives a heading: lower case, punctuation dropped, spaces turned into hyphens."""
    return slugify(plain_heading(heading))


def anchors(text: str) -> Set[str]:
    """Every heading or explicit HTML anchor in a file; GitHub numbers duplicate headings with ``-1``."""
    return anchors_of(text)


def duplicate_headings(text: str) -> List[str]:
    """Anchors that more than one heading of the file would make (the later ones get a confusing ``-1``)."""
    seen: Dict[str, int] = {}
    for heading in headings(text):
        seen[slug(heading)] = seen.get(slug(heading), 0) + 1
    return sorted(anchor for anchor, count in seen.items() if count > 1)


LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)\)")


def local_links(path: Path) -> List[Tuple[Path, str, str]]:
    """(target file, anchor, as written) for every relative link in a page; links inside code are not links."""
    text = re.sub(r"```.*?```", "", path.read_text(encoding="utf-8"), flags=re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    found = []
    for target in LINK.findall(text):
        if re.match(r"[a-z][a-z0-9+.-]*:", target):          # https:, mailto:, ...
            continue
        name, _, anchor = target.partition("#")
        found.append((path if not name else (path.parent / name).resolve(), anchor, target))
    return found
