#!/usr/bin/env python3
"""Builds the documentation bundle for the web interface's Docs page (a development tool, not part of the package).

    python tools/build_docs.py OUT_DIR --tag v0.4.0

It reads ``README.md``, ``docs/*.md`` and ``docs/schemas/*.json`` and writes ``OUT_DIR/docs.json`` plus copies of the
images and schema files under ``OUT_DIR/docs/``. The documentation in the repository is the only copy of the content;
nothing is fetched and nothing outside the repository is read.

``docs.json`` (format 1) holds, in a fixed order so two runs give the same bytes:

* ``version`` (``homelab_probe.__version__``), ``tag`` and ``repository``;
* ``groups`` (id, title, page ids) and ``pages`` in group order. A page has ``id``, ``title``, ``group``, ``source``,
  ``markdown`` (the source without its inert HTML, see below), ``headings`` (a tree of ``level``, ``text``,
  ``anchor``, ``line``, ``children``; anchors are the ones GitHub makes, duplicates numbered ``-1``, ``-2``),
  ``anchors`` (every anchor a link may name), ``search`` (plain text), ``links`` and ``images``;
* ``schemas`` and ``images``: the copied files with their size and SHA-256.

A link is ``{"kind": "page", "page", "anchor"}`` (a route inside the bundle, also for ``#anchor`` on the same page),
``{"kind": "file", "path"}`` (a bundled schema or image), ``{"kind": "repository", "href"}`` (anything else that exists
in the repository, at the given tag) or ``{"kind": "external", "href"}`` (``http``, ``https`` and ``mailto`` only).

The Markdown subset is the approved one: ATX headings, paragraphs, bullet and numbered lists (nested, with fenced
code and tables inside), tables, fenced code (backticks), block quotes, links, images, emphasis and inline code. It
is parsed only to classify and check it, never rendered. The build **fails**, naming the file and the line, on:

* raw HTML (an open or closing tag, a ``<!DOCTYPE``, a ``<?``, an angle-bracket autolink) outside code. Two inert
  forms are allowed and removed from the bundled Markdown: a comment (``<!-- ... -->``) and an explicit anchor
  ``<a id="name"></a>`` (the README keeps its old anchors that way; ``name`` joins the page's ``anchors``);
* a construct outside the subset: setext headings, horizontal rules, ``~~~`` fences, indented code blocks, reference
  links and their definitions, ``<...>`` link destinations, tabs in indentation, a list item or block quote continued
  by an unindented line, and control, zero-width or direction-changing characters;
* an image that is missing, is not a ``png``/``jpg``/``jpeg``/``gif``/``webp``/``svg`` file, is remote, or is outside
  the repository (so is any link whose target resolves outside it, symbolic links included);
* an internal link to a page, file or anchor that does not exist, or a destination that is not relative, ``http``,
  ``https`` or ``mailto`` (``javascript:`` and ``data:`` fail);
* a page without a ``# Title`` or two pages with the same id, a schema that is not JSON, a ``--tag`` that is not
  ``v`` plus the package version (other names such as ``main`` are fine) and an output directory that is the
  repository or holds something that is not a previous build.
"""

import argparse
import hashlib
import json
import re
import shutil
import string
import sys
import tempfile
from functools import cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import quote, unquote

ROOT = Path(__file__).resolve().parent.parent
REPOSITORY = "https://github.com/jeffholst/homelab-probe"
FORMAT = 1
README = "README.md"

# (id, title, page ids in the order of the README's Documentation paragraph). A page that is in no group goes to a
# last group, "More", so a new page is bundled without touching this table; ``tests/test_build_docs.py`` keeps the
# order in step with the README and fails on a name that no longer exists.
GROUPS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("overview", "Overview", ("readme",)),
    ("using", "Using the tool", ("configuration", "diagnose", "inventory", "network")),
    ("running", "Running it", ("web", "notifications", "scheduling", "docker", "logging")),
    ("reference", "Reference", ("schemas", "examples", "features", "development")),
)
OTHER_GROUP = ("more", "More")

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"})
EXTERNAL_SCHEMES = frozenset({"http", "https", "mailto"})
PAGE_ID = re.compile(r"[a-z0-9][a-z0-9_-]*")
TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
RELEASE_TAG = re.compile(r"v\d+\.\d+\.\d+.*")
REPOSITORY_URL = re.compile(r"https://[A-Za-z0-9.-]+/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")

HIDDEN = re.compile(
    "[\x00\x01\x02\x03\x04\x05\x06\x07\x08\x0b\x0c\x0d\x0e\x0f\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19"
    "\x1a\x1b\x1c\x1d\x1e\x1f\x7f\u200b-\u200f\u2028\u2029\u202a-\u202e\u2060-\u2069\ufeff]"
)
SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*):")
ANCHOR_TAG = re.compile(r'<a id="([A-Za-z0-9_-]+)"></a>')
FENCE_OPEN = re.compile(r"^( *)(`{3,})([^`]*)$")
TILDE_FENCE = re.compile(r"^ {0,3}~{3,}")
LOOSE_FENCE = re.compile(r"^\s*(`{3,})[^`]*$")
HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*))?$")
THEMATIC = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
SETEXT = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
LIST_ITEM = re.compile(r"^( {0,3})([-*+]|(\d{1,9})([.)]))(?:( +)(.*))?$")
QUOTE = re.compile(r"^ {0,3}>")
DEFINITION = re.compile(r"^ {0,3}\[[^\]]+\]:")
TABLE_DELIMITER = re.compile(r"^ {0,3}\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")
HTML_TAG = re.compile(
    r"<(?:/[A-Za-z][A-Za-z0-9-]*\s*"
    r"|[A-Za-z][A-Za-z0-9-]*(?:\s+[A-Za-z_:][\w:.-]*(?:\s*=\s*(?:[^\s\"'=<>`]+|'[^']*'|\"[^\"]*\"))?)*\s*/?)>")
HTML_OTHER = re.compile(r"<(?:[!?]|/[A-Za-z])")
AUTOLINK = re.compile(r"<[A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*>")
PUNCTUATION = frozenset(string.punctuation)


class BuildError(Exception):
    """The bundle cannot be built; the message says why (``problems`` are ``file:line: reason`` lines)."""

    def __init__(self, problems: Sequence[str]):
        super().__init__("\n".join(problems))
        self.problems = list(problems)


# -- GitHub's anchors (shared with tests/docs_support.py) ----------

def slugify(text: str) -> str:
    """The anchor GitHub makes from heading text that is already plain: lower case, punctuation dropped, a hyphen for
    each space. Letters, digits, hyphens and underscores stay (``unifi_clients.csv`` is ``unifi_clientscsv``)."""
    return re.sub(r"[^\w\s-]", "", text.strip().lower()).replace(" ", "-")


class Slugger:
    """Numbers repeated anchors the way GitHub does: ``a``, ``a-1``, ``a-2`` (and skips a number a heading has)."""

    def __init__(self) -> None:
        self.seen: Dict[str, int] = {}

    def anchor(self, plain_text: str) -> str:
        base = slugify(plain_text)
        slug = base
        while slug in self.seen:
            self.seen[base] += 1
            slug = f"{base}-{self.seen[base]}"
        self.seen[slug] = 0
        return slug


def plain_heading(markup: str) -> str:
    """The text of a heading with its Markdown taken out (code ticks, emphasis, link and image syntax)."""
    return normalize_space(PageParser("<heading>", Problems()).scan(markup, 1))


def heading_texts(text: str) -> List[str]:
    """The raw heading texts of Markdown, in order, ignoring lines inside fenced code (no checks are made)."""
    return headings_and_anchors(text)[0]


def headings_and_anchors(text: str) -> Tuple[List[str], List[str]]:
    """``(raw heading texts, explicit <a id> names)`` of the text left after comments and anchor tags are taken out,
    the one cleaning path for both (so a heading inside a comment makes no anchor)."""
    parser = PageParser("<text>", Problems())
    cleaned = parser.clean(text.replace("\r\n", "\n").split("\n"))
    found, fence = [], 0
    for line in cleaned:
        if fence:
            closing = re.match(r"^\s*(`{3,})[ \t]*$", line)
            if closing and len(closing.group(1)) >= fence:
                fence = 0
        elif opening := LOOSE_FENCE.match(line):
            fence = len(opening.group(1))
        elif match := HEADING.match(line):
            found.append(strip_closing_hashes(match.group(2) or ""))
    return found, parser.explicit_anchors


def anchors_of(text: str) -> set:
    """Every anchor of a Markdown file: its headings (numbered like GitHub) and its explicit ``<a id>`` anchors."""
    slugger = Slugger()
    headings, explicit = headings_and_anchors(text)
    found = {slugger.anchor(plain_heading(heading)) for heading in headings}
    found.update(explicit)
    return found


def strip_closing_hashes(content: str) -> str:
    content = content.strip()
    if re.fullmatch(r"#+", content):
        return ""
    return re.sub(r"[ \t]+#+$", "", content)


def normalize_space(text: str) -> str:
    return " ".join(text.split())


# -- problems ----------

class Problems:
    """Collects ``file:line: reason`` lines so one run reports everything wrong, not only the first thing."""

    def __init__(self) -> None:
        self.items: List[Tuple[str, int, str]] = []

    def add(self, path: str, line: int, message: str) -> None:
        self.items.append((path, line, message))

    def lines(self) -> List[str]:
        return [f"{path}:{line}: {message}" if line else f"{path}: {message}"
                for path, line, message in sorted(set(self.items))]

    def raise_if_any(self) -> None:
        if self.items:
            raise BuildError(self.lines())


class Stop(Exception):
    """A block-level problem after which the rest of the page cannot be read reliably."""


# -- the parser: classifies blocks, checks the subset, and collects headings, links, images and search text ----------

Line = Tuple[int, str]          # (line number in the file, text)


def indent_of(text: str) -> int:
    return len(text) - len(text.lstrip(" "))


def is_blank(text: str) -> bool:
    return not text.strip()


@cache
def closing_run(length: int) -> "re.Pattern[str]":
    return re.compile(rf"(?<!`)`{{{length}}}(?!`)")


def code_spans(line: str) -> List[Tuple[int, int]]:
    """(start, end) of the inline code spans that open and close on one line."""
    spans, i = [], 0
    while i < len(line):
        if line[i] != "`":
            i += 1
            continue
        j = i
        while j < len(line) and line[j] == "`":
            j += 1
        match = closing_run(j - i).search(line, j)
        if match:
            spans.append((i, match.end()))
            i = match.end()
        else:
            i = j
    return spans


def find_bracket_end(text: str, start: int) -> Optional[int]:
    """Index of the ``]`` that closes the ``[`` at ``start``, skipping code spans and escapes."""
    depth, i = 0, start
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if char == "`":
            j = i
            while j < len(text) and text[j] == "`":
                j += 1
            match = closing_run(j - i).search(text, j)
            i = match.end() if match else j
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def split_row(line: str) -> List[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", text)]


def starts_table(lines: List[Line], i: int) -> bool:
    return (i + 1 < len(lines) and "|" in lines[i][1] and "|" in lines[i + 1][1]
            and bool(TABLE_DELIMITER.match(lines[i + 1][1])))


def starts_block(lines: List[Line], i: int) -> bool:
    """Would the line at ``i`` end a paragraph (a heading, fence, quote, list item or table starts there)?"""
    text = lines[i][1]
    if HEADING.match(text) or FENCE_OPEN.match(text) or QUOTE.match(text) or starts_table(lines, i):
        return True
    item = LIST_ITEM.match(text)
    if item and not THEMATIC.match(text):
        ordered_start = item.group(3)
        return bool((item.group(6) or "").strip()) and (ordered_start is None or int(ordered_start) == 1)
    return False


class PageParser:
    """One page: ``parse`` fills ``headings``, ``links``, ``images``, ``search`` and reports problems as it goes."""

    def __init__(self, path: str, problems: Problems) -> None:
        self.path = path
        self.problems = problems
        self.slugger = Slugger()
        self.headings: List[Dict[str, Any]] = []
        self.links: List[Tuple[int, str, str]] = []          # (line, text, destination)
        self.images: List[Tuple[int, str, str]] = []         # (line, alt, destination)
        self.explicit_anchors: List[str] = []
        self.search: List[str] = []
        self.link_depth = 0

    def problem(self, line: int, message: str) -> None:
        self.problems.add(self.path, line, message)

    # -- the line pass: hidden characters, tabs, comments and anchor tags ----------

    def clean(self, raw: List[str]) -> List[str]:
        """The lines without their inert HTML (comments, ``<a id>`` anchors), the line count unchanged."""
        out: List[str] = []
        fence = 0
        i = 0
        while i < len(raw):
            line = raw[i]
            number = i + 1
            if HIDDEN.search(line):
                self.problem(number, "control, zero-width or direction-changing character; remove it or write it as an "
                                     "escape in a code span")
            if fence:
                closing = re.match(r"^\s*(`{3,})[ \t]*$", line)
                if closing and len(closing.group(1)) >= fence:
                    fence = 0
                out.append(line)
                i += 1
                continue
            opening = LOOSE_FENCE.match(line)
            if opening:
                fence = len(opening.group(1))
                out.append(line)
                i += 1
                continue
            if re.match(r" *\t", line):
                self.problem(number, "a tab in the indentation; use spaces")
            lines, used = self.strip_comments(raw, i)
            out.extend(self.strip_anchors(text) for text in lines)
            i += used
        return out

    def strip_comments(self, raw: List[str], index: int) -> Tuple[List[str], int]:
        """``raw[index]`` without its comments, as lines, and how many source lines that used (a comment may span)."""
        line = raw[index]
        spans = code_spans(line)
        pieces: List[str] = []
        position = 0
        while True:
            comment = line.find("<!--", position)
            while comment != -1 and any(start <= comment < end for start, end in spans):
                comment = line.find("<!--", comment + 1)
            if comment == -1:
                pieces.append(line[position:])
                return ["".join(pieces)], 1
            pieces.append(line[position:comment])
            end = line.find("-->", comment + 4)
            if end != -1:
                position = end + 3
                continue
            if "".join(pieces).strip():
                self.problem(index + 1, "a comment that spans lines must start its own line")
                return [line], 1
            last = index + 1
            while last < len(raw) and "-->" not in raw[last]:
                last += 1
            if last == len(raw):
                self.problem(index + 1, "this comment is never closed")
                return [""] * (len(raw) - index), len(raw) - index
            if raw[last].split("-->", 1)[1].strip():
                self.problem(last + 1, "text after the end of a multi-line comment; end the line with -->")
            return [""] * (last - index + 1), last - index + 1

    def strip_anchors(self, line: str) -> str:
        """The line without its ``<a id="name"></a>`` anchors (those in code spans stay); the names are recorded."""
        spans = code_spans(line)

        def drop(match: "re.Match[str]") -> str:
            if any(start <= match.start() < end for start, end in spans):
                return match.group(0)
            self.explicit_anchors.append(match.group(1))
            return ""
        return ANCHOR_TAG.sub(drop, line)

    # -- blocks ----------

    def parse(self, raw: List[str]) -> str:
        """Parses the page and returns its Markdown without the inert HTML."""
        cleaned = self.clean(raw)
        try:
            self.parse_blocks(list(enumerate(cleaned, 1)))
        except Stop:
            pass
        return "\n".join(cleaned) + "\n"

    def parse_blocks(self, lines: List[Line]) -> None:
        i = 0
        while i < len(lines):
            number, text = lines[i]
            if is_blank(text):
                i += 1
            elif TILDE_FENCE.match(text):
                self.problem(number, "a ~~~ fence is not supported; use backticks")
                raise Stop()
            elif indent_of(text) >= 4:
                self.problem(number, "an indented code block is not supported; use a fenced block")
                raise Stop()
            elif FENCE_OPEN.match(text):
                i = self.parse_fence(lines, i)
            elif heading := HEADING.match(text):
                self.parse_heading(number, heading)
                i += 1
            elif THEMATIC.match(text):
                self.problem(number, "a horizontal rule is not supported")
                i += 1
            elif DEFINITION.match(text):
                self.problem(number, "a reference link definition is not supported; write the link inline")
                i += 1
            elif QUOTE.match(text):
                i = self.parse_quote(lines, i)
            elif starts_table(lines, i):
                i = self.parse_table(lines, i)
            elif LIST_ITEM.match(text):
                i = self.parse_list(lines, i)
            else:
                i = self.parse_paragraph(lines, i)

    def parse_fence(self, lines: List[Line], i: int) -> int:
        number, text = lines[i]
        match = FENCE_OPEN.match(text)
        assert match is not None
        length = len(match.group(2))
        body: List[str] = []
        j = i + 1
        while j < len(lines):
            closing = re.match(r"^ {0,3}(`{3,})[ \t]*$", lines[j][1])
            if closing and len(closing.group(1)) >= length:
                self.search.append("\n".join(body))
                return j + 1
            body.append(lines[j][1])
            j += 1
        self.problem(number, "this code fence is never closed")
        raise Stop()

    def parse_heading(self, number: int, match: "re.Match[str]") -> None:
        content = strip_closing_hashes(match.group(2) or "")
        text = normalize_space(self.scan(content, number))
        if not text:
            self.problem(number, "an empty heading")
            return
        self.headings.append({"level": len(match.group(1)), "text": text, "anchor": self.slugger.anchor(text),
                              "line": number})
        self.search.append(text)

    def parse_paragraph(self, lines: List[Line], i: int) -> int:
        start = lines[i][0]
        parts = [lines[i][1].strip()]
        j = i + 1
        while j < len(lines) and not is_blank(lines[j][1]) and not starts_block(lines, j):
            if SETEXT.match(lines[j][1]):
                self.problem(lines[j][0], "a setext heading (underlined text) is not supported; use # headings")
            elif THEMATIC.match(lines[j][1]):
                self.problem(lines[j][0], "a horizontal rule is not supported")
            parts.append(lines[j][1].strip())
            j += 1
        self.search.append(normalize_space(self.scan("\n".join(parts), start)))
        return j

    def parse_quote(self, lines: List[Line], i: int) -> int:
        inner: List[Line] = []
        j = i
        while j < len(lines) and not is_blank(lines[j][1]):
            number, text = lines[j]
            if not QUOTE.match(text):
                self.problem(number, "a block quote continued by a line without > is not supported")
                break
            body = text.lstrip(" ")[1:]
            inner.append((number, body[1:] if body.startswith(" ") else body))
            j += 1
        self.parse_blocks(inner)
        return j

    def parse_table(self, lines: List[Line], i: int) -> int:
        header = split_row(lines[i][1])
        delimiter = split_row(lines[i + 1][1])
        if len(header) != len(delimiter):
            self.problem(lines[i][0], f"the table header has {len(header)} columns and its delimiter row "
                                      f"{len(delimiter)}")
        rows = [header]
        j = i + 2
        while j < len(lines) and "|" in lines[j][1] and not is_blank(lines[j][1]):
            rows.append(split_row(lines[j][1]))
            j += 1
        for offset, row in enumerate(rows):
            number = lines[i][0] if offset == 0 else lines[i + 1 + offset][0]
            self.search.append(normalize_space(" | ".join(self.scan(cell, number) for cell in row)))
        return j

    def parse_list(self, lines: List[Line], i: int) -> int:
        first = LIST_ITEM.match(lines[i][1])
        assert first is not None
        kind = (first.group(2) if first.group(3) is None else first.group(4))
        while True:
            match = LIST_ITEM.match(lines[i][1])
            if not match or THEMATIC.match(lines[i][1]) or (
                    match.group(2) if match.group(3) is None else match.group(4)) != kind:
                break
            marker_end = len(match.group(1)) + len(match.group(2))
            spaces = len(match.group(5) or "")
            rest = match.group(6) or ""
            content_indent = marker_end + (spaces if rest.strip() and spaces <= 4 else 1)
            if rest.strip() and spaces > 4:
                rest = " " * (spaces - 1) + rest
            item: List[Line] = [(lines[i][0], rest)]
            i += 1
            while i < len(lines):
                number, text = lines[i]
                if is_blank(text):
                    j = i
                    while j < len(lines) and is_blank(lines[j][1]):
                        j += 1
                    if j < len(lines) and indent_of(lines[j][1]) >= content_indent:
                        item.extend((n, "") for n, _ in lines[i:j])
                        i = j
                        continue
                    break
                if indent_of(text) >= content_indent:
                    item.append((number, text[content_indent:]))
                    i += 1
                elif LIST_ITEM.match(text) or HEADING.match(text) or FENCE_OPEN.match(text) or QUOTE.match(text):
                    break
                else:
                    self.problem(number, "a line that continues a list item must be indented under it")
                    raise Stop()
            self.parse_blocks(item)
            j = i
            while j < len(lines) and is_blank(lines[j][1]):
                j += 1
            if j < len(lines) and LIST_ITEM.match(lines[j][1]) and not THEMATIC.match(lines[j][1]):
                i = j
            else:
                break
        return i

    # -- inline: plain text, links, images, raw HTML ----------

    def scan(self, text: str, line: int) -> str:
        """The plain text of inline Markdown; records its links and images and reports raw HTML."""
        out: List[str] = []
        n = len(text)
        i = 0

        def at(position: int) -> int:
            return line + text.count("\n", 0, position)

        while i < n:
            char = text[i]
            if char == "\\" and i + 1 < n and text[i + 1] in PUNCTUATION:
                out.append(text[i + 1])
                i += 2
            elif char == "`":
                j = i
                while j < n and text[j] == "`":
                    j += 1
                match = closing_run(j - i).search(text, j)
                if match:
                    code = text[j:match.start()].replace("\n", " ")
                    if len(code) > 2 and code[0] == " " and code[-1] == " " and code.strip():
                        code = code[1:-1]
                    out.append(code)
                    i = match.end()
                else:
                    out.append(text[i:j])
                    i = j
            elif char == "!" and text[i + 1:i + 2] == "[":
                i = self.scan_bracket(text, i + 1, True, out, at)
            elif char == "[":
                i = self.scan_bracket(text, i, False, out, at)
            elif char == "<":
                rest = text[i:]
                if HTML_TAG.match(rest) or HTML_OTHER.match(rest):
                    shown = rest.split(">", 1)[0][:40]
                    self.problem(at(i), f"raw HTML is not supported ({shown}>); use Markdown or a code span")
                elif AUTOLINK.match(rest):
                    self.problem(at(i), "an angle-bracket link is not supported; write [text](url)")
                out.append("<")
                i += 1
            elif char in "*_":
                j = i
                while j < n and text[j] == char:
                    j += 1
                before = text[i - 1] if i else " "
                after = text[j] if j < n else " "
                if char == "*":
                    keep = before.isspace() and after.isspace()
                else:
                    keep = (before.isalnum() and after.isalnum()) or (before.isspace() and after.isspace())
                if keep:
                    out.append(text[i:j])
                i = j
            else:
                out.append(char)
                i += 1
        return "".join(out)

    def scan_bracket(self, text: str, start: int, is_image: bool, out: List[str], at: Any) -> int:
        """A ``[`` at ``start``: a link, an image (``is_image``) or just a bracket. Returns where to go on."""
        end = find_bracket_end(text, start)
        mark = start - 1 if is_image else start
        if end is None or text[end + 1:end + 2] not in ("(", "["):
            out.append(("!" if is_image else "") + "[")
            return start + 1
        if text[end + 1] == "[":
            self.problem(at(mark), "a reference link ([text][label]) is not supported; write the link inline")
            out.append("[")
            return start + 1
        destination, after = self.parse_destination(text, end + 2)
        if destination is None:
            self.problem(at(mark), "a link or image destination could not be read; check its parentheses")
            out.append(("!" if is_image else "") + "[")
            return start + 1
        number = at(mark)
        inner = text[start + 1:end]
        if is_image:
            alt = normalize_space(self.scan(inner, at(start + 1)))
            self.images.append((number, alt, destination))
            out.append(alt)
        else:
            if self.link_depth:
                self.problem(number, "a link inside a link")
            self.link_depth += 1
            label = normalize_space(self.scan(inner, at(start + 1)))
            self.link_depth -= 1
            self.links.append((number, label, destination))
            out.append(label)
        return after

    def parse_destination(self, text: str, i: int) -> Tuple[Optional[str], int]:
        """``(destination, index after the closing parenthesis)``, or ``(None, i)`` when it is not well formed."""
        n = len(text)
        while i < n and text[i] in " \t\n":
            i += 1
        if i < n and text[i] == "<":
            return None, i
        start, depth = i, 0
        while i < n:
            char = text[i]
            if char == "\\" and i + 1 < n:
                i += 2
                continue
            if char in " \t\n":
                break
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    break
                depth -= 1
            i += 1
        destination = re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", text[start:i])
        while i < n and text[i] in " \t\n":
            i += 1
        if i < n and text[i] in "\"'":
            close = text.find(text[i], i + 1)
            if close == -1:
                return None, i
            i = close + 1
            while i < n and text[i] in " \t\n":
                i += 1
        if i >= n or text[i] != ")" or not destination:
            return None, i
        return destination, i + 1


def heading_tree(flat: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    tree: List[Dict[str, Any]] = []
    stack: List[Dict[str, Any]] = []
    for heading in flat:
        node = {**heading, "children": []}
        while stack and stack[-1]["level"] >= node["level"]:
            stack.pop()
        (stack[-1]["children"] if stack else tree).append(node)
        stack.append(node)
    return tree


# -- the bundle ----------

class Page:
    def __init__(self, page_id: str, source: str, path: Path) -> None:
        self.id = page_id
        self.source = source
        self.path = path
        self.parser: Optional[PageParser] = None
        self.markdown = ""
        self.title = ""
        self.anchors: List[str] = []


class Bundle:
    """What ``build_bundle`` made: the ``docs.json`` value and the files to copy (relative path to source)."""

    def __init__(self, data: Dict[str, Any], files: Dict[str, Path]) -> None:
        self.data = data
        self.files = files


def read_version(root: Path) -> str:
    path = root / "homelab_probe" / "__init__.py"
    real = contained(root.resolve(), path)
    if real is None:
        raise BuildError(["homelab_probe/__init__.py: points outside the repository"])
    try:
        text = real.read_text(encoding="utf-8")
    except OSError:
        raise BuildError([f"{root}: no homelab_probe/__init__.py to read the version from"]) from None
    match = re.search(r'^__version__ = "([^"]+)"', text, re.M)
    if not match:
        raise BuildError(["homelab_probe/__init__.py: no __version__"])
    return match.group(1)


def check_tag(tag: str, version: str) -> None:
    if not TAG.fullmatch(tag) or ".." in tag:
        raise BuildError([f"--tag {tag!r}: use a tag or branch name such as v{version}"])
    if RELEASE_TAG.fullmatch(tag) and tag != f"v{version}":
        raise BuildError([f"--tag {tag}: the package version is {version}, so a release tag is v{version}"])


def contained(root: Path, candidate: Path) -> Optional[Path]:
    """The real path of ``candidate`` when it is inside ``root`` (links followed), else None."""
    try:
        real = candidate.resolve()
        real.relative_to(root)
    except (ValueError, OSError, RuntimeError):
        return None
    return real


def rel(root: Path, real: Path) -> str:
    return real.relative_to(root).as_posix()


def source_pages(root: Path, problems: Problems) -> List[Page]:
    pages: List[Page] = []
    docs = sorted(path.relative_to(root).as_posix() for path in (root / "docs").glob("*.md"))
    names = [README, *docs]
    seen: Dict[str, str] = {}
    for source in names:
        path = root / source
        page_id = "readme" if source == README else Path(source).stem.lower()
        if not path.is_file():
            problems.add(source, 0, "the page does not exist")
            continue
        if contained(root, path) is None:
            problems.add(source, 0, "the page is a link to a file outside the repository")
            continue
        if not PAGE_ID.fullmatch(page_id):
            problems.add(source, 0, f"the page id {page_id!r} is not lower case letters, digits, - and _")
        if page_id in seen:
            problems.add(source, 0, f"the page id {page_id!r} is already used by {seen[page_id]}")
            continue
        seen[page_id] = source
        pages.append(Page(page_id, source, path))
    return pages


def parse_pages(pages: List[Page], problems: Problems) -> None:
    for page in pages:
        try:
            text = page.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            problems.add(page.source, 0, f"cannot be read as UTF-8 text ({type(error).__name__})")
            continue
        parser = PageParser(page.source, problems)
        lines = text.replace("\r\n", "\n").split("\n")
        if lines[-1] == "":
            lines.pop()
        page.markdown = parser.parse(lines)
        page.parser = parser
        titles = [h for h in parser.headings if h["level"] == 1]
        if not titles:
            problems.add(page.source, 0, "the page has no '# Title' heading")
        else:
            page.title = titles[0]["text"]
        page.anchors = sorted({h["anchor"] for h in parser.headings} | set(parser.explicit_anchors))


def group_pages(pages: List[Page]) -> List[Dict[str, Any]]:
    by_id = {page.id: page for page in pages}
    groups: List[Dict[str, Any]] = []
    placed: Set[str] = set()
    for group_id, title, ids in GROUPS:
        members = [by_id[page_id] for page_id in ids if page_id in by_id]
        placed.update(page.id for page in members)
        if members:
            groups.append({"id": group_id, "title": title, "pages": [page.id for page in members]})
    rest = sorted(page_id for page_id in by_id if page_id not in placed)
    if rest:
        groups.append({"id": OTHER_GROUP[0], "title": OTHER_GROUP[1], "pages": rest})
    return groups


class Resolver:
    """Turns the destinations written in a page into route-ready links, and finds the files the bundle must carry."""

    def __init__(self, root: Path, pages: List[Page], tag: str, repository: str, problems: Problems) -> None:
        self.root = root
        self.tag = tag
        self.repository = repository
        self.problems = problems
        self.by_path = {page.path.resolve(): page for page in pages}
        self.files: Dict[str, Path] = {}
        self.schemas = self.schema_paths()      # every one is inside the repository, or a problem was reported
        self.markdown_anchors: Dict[Path, set] = {}

    def schema_paths(self) -> List[Path]:
        """The schema files, resolved; a folder or file that leads outside the repository fails the build."""
        folder = self.root / "docs" / "schemas"
        if not folder.is_dir():
            return []
        if contained(self.root, folder) is None:
            self.problems.add("docs/schemas", 0, "the folder is a link to a place outside the repository")
            return []
        found = []
        for path in sorted(folder.glob("*.json")):
            if not path.is_file():
                continue
            real = contained(self.root, path)
            if real is None:
                self.problems.add(f"docs/schemas/{path.name}", 0, "the schema is a link to a file outside the "
                                                                   "repository")
            else:
                found.append(real)
        return sorted(found)

    def local_target(self, page: Page, line: int, destination: str) -> Optional[Tuple[Path, str]]:
        """``(real path inside the repository, anchor)`` of a relative destination, or None after a problem."""
        name, _, anchor = destination.partition("#")
        if re.search(r"[\x00-\x1f\\?]", unquote(name)) or destination.startswith("/"):
            self.problems.add(page.source, line, f"'{destination}': use a relative path with forward slashes and "
                                                 "no query string")
            return None
        decoded = unquote(name)
        real = contained(self.root, (page.path.parent / decoded) if decoded else page.path)
        if real is None:
            self.problems.add(page.source, line, f"'{destination}' points outside the repository")
            return None
        if not real.exists():
            self.problems.add(page.source, line, f"'{destination}': no such file or directory")
            return None
        return real, unquote(anchor)

    def add_file(self, real: Path) -> str:
        path = rel(self.root, real)
        self.files[path] = real
        return path

    def anchors_in(self, real: Path) -> set:
        if real not in self.markdown_anchors:
            self.markdown_anchors[real] = anchors_of(real.read_text(encoding="utf-8"))
        return self.markdown_anchors[real]

    def link(self, page: Page, line: int, label: str, destination: str) -> Optional[Dict[str, Any]]:
        base = {"text": label, "target": destination, "line": line}
        scheme = SCHEME.match(destination)
        if scheme:
            if scheme.group(1).lower() not in EXTERNAL_SCHEMES:
                self.problems.add(page.source, line, f"'{destination}': only http, https and mailto links are "
                                                     "allowed")
                return None
            return {**base, "kind": "external", "href": destination}
        if destination.startswith("//"):
            self.problems.add(page.source, line, f"'{destination}': write the scheme (https://) of an external link")
            return None
        if destination.startswith("#"):
            anchor = unquote(destination[1:])
            return self.page_link(base, page, page, line, anchor, destination)
        target = self.local_target(page, line, destination)
        if target is None:
            return None
        real, anchor = target
        if real in self.by_path:
            return self.page_link(base, page, self.by_path[real], line, anchor, destination)
        if real in self.schemas or (real.is_file() and real.suffix.lower() in IMAGE_SUFFIXES):
            if anchor:
                self.problems.add(page.source, line, f"'{destination}': a file has no anchors")
            return {**base, "kind": "file", "path": self.add_file(real)}
        if real.suffix.lower() == ".md" and anchor and real.is_file() and anchor not in self.anchors_in(real):
            self.problems.add(page.source, line, f"'{destination}': no heading makes the anchor #{anchor}")
            return None
        where = rel(self.root, real) if real != self.root else ""
        kind = "tree" if real.is_dir() else "blob"
        href = f"{self.repository}/{kind}/{self.tag}" + (f"/{quote(where)}" if where else "")
        if anchor:
            href += f"#{quote(anchor)}"
        return {**base, "kind": "repository", "href": href}

    def page_link(self, base: Dict[str, Any], page: Page, target: Page, line: int, anchor: str,
                  written: str) -> Optional[Dict[str, Any]]:
        if written.endswith("#") or (anchor and anchor not in target.anchors):
            reason = "an empty anchor" if not anchor else f"{target.source} has no heading or anchor #{anchor}"
            self.problems.add(page.source, line, f"'{written}': {reason}")
            return None
        return {**base, "kind": "page", "page": target.id, "anchor": anchor}

    def image(self, page: Page, line: int, alt: str, destination: str) -> Optional[Dict[str, Any]]:
        if SCHEME.match(destination) or destination.startswith("//"):
            self.problems.add(page.source, line, f"'{destination}': a remote image is not bundled; keep the image in "
                                                 "docs/images and link it by a relative path")
            return None
        target = self.local_target(page, line, destination)
        if target is None:
            return None
        real, anchor = target
        if anchor or not real.is_file() or real.suffix.lower() not in IMAGE_SUFFIXES:
            self.problems.add(page.source, line, f"'{destination}': an image must be a "
                                                 f"{'/'.join(sorted(s[1:] for s in IMAGE_SUFFIXES))} file with no "
                                                 "anchor")
            return None
        return {"alt": alt, "target": destination, "line": line, "path": self.add_file(real)}


def file_entry(root: Path, real: Path) -> Dict[str, Any]:
    data = real.read_bytes()
    return {"path": rel(root, real), "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def build_bundle(root: Path, tag: str, repository: str = REPOSITORY) -> Bundle:
    """Reads the documentation under ``root`` and returns the bundle, or raises ``BuildError`` listing every problem."""
    root = root.resolve()
    version = read_version(root)
    check_tag(tag, version)
    if not REPOSITORY_URL.fullmatch(repository):
        raise BuildError([f"--repository {repository!r}: expected https://HOST/OWNER/NAME"])
    problems = Problems()
    pages = source_pages(root, problems)
    parse_pages(pages, problems)
    resolver = Resolver(root, pages, tag, repository, problems)
    groups = group_pages(pages)
    group_of = {page_id: group["id"] for group in groups for page_id in group["pages"]}
    order = {page_id: n for n, page_id in enumerate(page_id for group in groups for page_id in group["pages"])}
    records = []
    for page in sorted(pages, key=lambda p: order[p.id]):
        parser = page.parser
        if parser is None:
            continue
        links = [link for line, label, destination in parser.links
                 if (link := resolver.link(page, line, label, destination))]
        images = [image for line, alt, destination in parser.images
                  if (image := resolver.image(page, line, alt, destination))]
        records.append({
            "id": page.id, "title": page.title, "group": group_of[page.id], "source": page.source,
            "markdown": page.markdown, "headings": heading_tree(parser.headings), "anchors": page.anchors,
            "search": "\n".join(part for part in parser.search if part), "links": links, "images": images})
    schemas = []
    for path in resolver.schemas:
        try:
            title = json.loads(path.read_text(encoding="utf-8")).get("title", "")
        except (OSError, ValueError, AttributeError):
            problems.add(rel(root, path), 0, "the schema is not a JSON object")
            continue
        schemas.append({"id": path.name.removesuffix(".schema.json").removesuffix(".json"),
                        "title": title if isinstance(title, str) else "", **file_entry(root, path)})
        resolver.files[rel(root, path)] = path
    problems.raise_if_any()
    images = [file_entry(root, real) for path, real in sorted(resolver.files.items())
              if real.suffix.lower() in IMAGE_SUFFIXES]
    data = {"version": version, "format": FORMAT, "tag": tag, "repository": repository, "groups": groups,
            "pages": records, "schemas": schemas, "images": images}
    return Bundle(data, dict(sorted(resolver.files.items())))


def prepare_output(out_dir: Path, root: Path, paths: Sequence[str] = ()) -> Path:
    """The output directory, created, and cleared of the previous build's files; nothing else is ever removed.

    Everything is checked first, ``paths`` (what will be written) included, so a refusal leaves the old build alone."""
    out = out_dir.resolve()
    if out == root or out in root.parents:
        raise BuildError([f"{out_dir}: the output directory may not be the repository or contain it"])
    if out.exists():
        if not out.is_dir():
            raise BuildError([f"{out_dir}: not a directory"])
        if any(out.iterdir()):
            if not (out / "docs.json").is_file():
                raise BuildError([f"{out_dir}: not empty and not a previous docs bundle (no docs.json); use a new "
                                  "or empty directory"])
            if (out / "docs").is_symlink():
                raise BuildError([f"{out_dir}/docs: a symbolic link; remove it or use a new directory"])
    for path in paths:
        if Path(path).parts[0] != "docs":              # what is under docs/ is removed below, links included
            unlinked_destination(out, path)
    if out.exists() and any(out.iterdir()):
        (out / "docs.json").unlink()
        shutil.rmtree(out / "docs", ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    return out


def unlinked_destination(out: Path, path: str) -> Path:
    """``out/path`` after checking that neither the file nor any folder on the way to it is a symbolic link, which a
    copy would follow out of the output directory (files an earlier run or someone else left there are not trusted)."""
    current = out
    for part in Path(path).parts:
        current = current / part
        if current.is_symlink():
            raise BuildError([f"{current.relative_to(out).as_posix()}: a symbolic link in the output directory; "
                              "remove it or use a new directory"])
    return current


def write_bundle(bundle: Bundle, out_dir: Path, root: Path) -> Path:
    out = prepare_output(out_dir, root.resolve(), [*bundle.files, "docs.json"])
    for path, source in bundle.files.items():
        target = unlinked_destination(out, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    text = json.dumps(bundle.data, indent=2, ensure_ascii=False) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=out, prefix=".docs-", suffix=".tmp",
                                     delete=False) as handle:
        handle.write(text)
    destination = unlinked_destination(out, "docs.json")
    Path(handle.name).replace(destination)
    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_docs.py", description="Build the documentation bundle (docs.json and the files it references) "
                                          "for the web interface's Docs page.")
    parser.add_argument("out_dir", metavar="OUT_DIR", type=Path,
                        help="directory for docs.json and docs/ (created; a previous build in it is replaced)")
    parser.add_argument("--tag", required=True, help="the tag the repository links point at, vX.Y.Z for a release "
                                                     "(must be v plus the package version) or a branch such as main")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository to read (default: this checkout)")
    parser.add_argument("--repository", default=REPOSITORY, help="repository URL for links that leave the bundle "
                                                                  f"(default: {REPOSITORY})")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        bundle = build_bundle(args.root, args.tag, args.repository)
        destination = write_bundle(bundle, args.out_dir, args.root)
    except BuildError as error:
        print("build_docs: the documentation bundle was not built:", file=sys.stderr)
        for problem in error.problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"build_docs: {error.strerror or type(error).__name__}: {error.filename}", file=sys.stderr)
        return 1
    print(f"{destination}: {len(bundle.data['pages'])} pages, {len(bundle.data['schemas'])} schemas, "
          f"{len(bundle.data['images'])} images (version {bundle.data['version']}, tag {args.tag})")
    return 0


if __name__ == "__main__":      # pragma: no cover  (tests/test_build_docs.py runs the script in a subprocess)
    sys.exit(main())
