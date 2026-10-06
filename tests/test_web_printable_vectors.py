"""The web app's ``safeText`` is a port of ``util.printable``: both are checked against the same vectors.

``web/src/lib/printable-vectors.json`` holds inputs (written with escapes, so no file has a hidden character in it) and
the answers this module's ``printable`` gives; ``web/src/lib/safeText.test.tsx`` asserts the TypeScript function gives
the same. When ``printable`` changes on purpose, regenerate the answers and port the change.
"""

import json

from docs_support import ROOT

from homelab_probe.util import printable

VECTORS = ROOT / "web" / "src" / "lib" / "printable-vectors.json"


def test_the_vectors_are_what_printable_gives_today():
    vectors = json.loads(VECTORS.read_text(encoding="utf-8"))
    assert len(vectors) >= 20
    for vector in vectors:
        assert printable(vector["input"], vector["limit"]) == vector["expected"], vector["name"]


def test_the_vectors_cover_the_cases_that_matter_and_are_written_with_escapes():
    text = VECTORS.read_text(encoding="utf-8")
    names = " ".join(vector["name"] for vector in json.loads(text))
    for word in ("override", "zero-width", "escape", "limit", "line", "kept"):
        assert word in names
    assert text.isascii()          # a raw hidden character would defeat the point of the file
