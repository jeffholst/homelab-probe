"""Output safety helpers shared by every renderer.

Names on a network (client hostnames, device names, SSIDs, event text) are chosen by whoever
owns the device, so they are untrusted when printed or exported:

* in a terminal, an escape sequence or a newline can recolour, move the cursor, or forge a
  line that looks like a finding;
* in a spreadsheet, a cell that starts with ``=``, ``+``, ``-`` or ``@`` runs as a formula.

``printable`` cleans one value, ``safe_output`` is a backstop for a whole block of text, and
``csv_safe`` protects one CSV cell. JSON output is not touched: ``json`` already escapes
control characters.
"""

import re
from typing import Any

# Controls that are never wanted in a name: C0 (except tab, newline, CR, handled separately),
# DEL and C1. Bidirectional overrides/isolates can reorder text to disguise it; the zero-width
# joiner used by emoji and the plain right-to-left letters of Hebrew or Arabic are kept.
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_BIDI = re.compile("[؜‎‏‪-‮⁦-⁩]")
_LINE_BREAKS = re.compile(r"[\t\r\n\u0085  ]+")
_BLOCK_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def printable(value: Any, limit: int = 0) -> str:
    """``value`` as one line of text that is safe to print.

    Tabs and line breaks become a single space, other control characters and bidirectional
    overrides are removed. ``limit`` (when above zero) cuts a long value with an ellipsis.
    """
    text = "" if value is None else str(value)
    text = _LINE_BREAKS.sub(" ", text)
    text = _BIDI.sub("", _CONTROLS.sub("", text)).strip()
    if limit and len(text) > limit:
        text = text[:limit - 1] + "…"
    return text


def safe_output(text: str) -> str:
    """A block of already rendered text with control characters (including the escape that
    starts a terminal sequence) removed. Line breaks are kept; this is the last line of
    defence at the print boundary, not a replacement for ``printable`` on individual names."""
    return _BIDI.sub("", _BLOCK_CONTROLS.sub("", text.replace("\t", " ").replace("\r", "")))


def csv_safe(value: Any) -> Any:
    """A CSV cell that a spreadsheet will not run as a formula.

    Text starting with ``=``, ``+``, ``-``, ``@``, a tab or a carriage return gets a leading
    apostrophe, which spreadsheets show as plain text. Numbers, booleans and None are left
    alone (a negative number is a number, not a formula).
    """
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


def clean_data(value: Any) -> Any:
    """A copy of a report (nested dicts, lists and tuples) with every text value passed through
    ``printable``. Text renderers call this once on their input so that no name, however deep
    in the report, can carry a control character or a line break into the output."""
    if isinstance(value, str):
        return printable(value)
    if isinstance(value, dict):
        return {k: clean_data(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_data(v) for v in value]
    if isinstance(value, tuple):
        return tuple(clean_data(v) for v in value)
    return value
