"""Helpers shared by the analysis and rendering modules.

Output safety comes first. It is used by every renderer.

Names on a network (client hostnames, device names, SSIDs, event text) are chosen by whoever
owns the device, so they are untrusted when printed or exported:

* in a terminal, an escape sequence or a newline can recolour, move the cursor, or forge a
  line that looks like a finding;
* in a spreadsheet, a cell that starts with ``=``, ``+``, ``-`` or ``@`` runs as a formula.

``printable`` cleans one value, ``safe_output`` is a backstop for a whole block of text, and
``csv_safe`` protects one CSV cell. JSON output is not touched: ``json`` already escapes
control characters.

The value helpers further down (``number``, ``plural``, ``normalize_mac``, ``format_time``, ...) used to be
copied into several modules; they live here once.
"""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

# Controls that are never wanted in a name: C0 (except tab, newline, CR, handled separately),
# DEL and C1. Also removed: bidirectional overrides and isolates (they reorder text to disguise
# it) and invisible characters (zero-width space, word joiner, BOM) that make two different
# names look identical. Kept: the zero-width joiner and non-joiner (emoji and Persian need
# them), the weak direction marks LRM, RLM and ALM, and the letters of right-to-left scripts.
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_HIDDEN = re.compile("[\u200b\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
_LINE_BREAKS = re.compile(r"[\t\r\n\u0085\u2028\u2029]+")
_BLOCK_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def printable(value: Any, limit: int = 0) -> str:
    """``value`` as one line of text that is safe to print.

    Tabs and line breaks become a single space; other control characters, bidirectional
    overrides and invisible characters are removed. ``limit`` (when above zero) cuts a long value with an ellipsis.
    """
    text = "" if value is None else str(value)
    text = _LINE_BREAKS.sub(" ", text)
    text = _HIDDEN.sub("", _CONTROLS.sub("", text)).strip()
    if limit and len(text) > limit:
        text = text[:limit - 1] + "…"
    return text


def safe_output(text: str) -> str:
    """A block of already rendered text with control characters (including the escape that
    starts a terminal sequence) removed. Line breaks are kept; this is the last line of
    defence at the print boundary, not a replacement for ``printable`` on individual names."""
    return _HIDDEN.sub("", _BLOCK_CONTROLS.sub("", text.replace("\t", " ").replace("\r", "")))


def csv_safe(value: Any) -> Any:
    """A CSV cell that a spreadsheet will not run as a formula.

    Text starting with ``=``, ``+``, ``-``, ``@``, a tab or a carriage return gets a leading
    apostrophe, which spreadsheets show as plain text. Numbers, booleans and None are left
    alone (a negative number is a number, not a formula). A string that merely looks numeric,
    such as ``"-67"``, is text and is prefixed too, so a column of numbers must hold numbers.
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


def describe_age(seconds: int) -> str:
    """'45m', '18h' or '12d' style text for how long ago something was (no 'ago')."""
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    if seconds < 2 * 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


_MAC_SEPARATORS = re.compile(r"[:\-.\s]")


def is_randomized_mac(mac: Any) -> bool:
    """True for a locally administered unicast MAC address, the kind phones, tablets and laptops
    generate for "private" or "randomized" Wi-Fi addresses.

    The second-lowest bit of the first octet (0x02) marks an address as locally administered;
    the lowest bit (0x01) must be clear, because a set bit means multicast, which no client
    uses. In hex the second digit is 2, 6, A or E. It is a hint, not proof: virtual machines,
    containers, bridges and some IoT devices also use locally administered addresses.
    Anything that is not 12 hex digits (any usual separator or case) is False.
    """
    if not isinstance(mac, str):
        return False
    digits = _MAC_SEPARATORS.sub("", mac)
    if len(digits) != 12 or any(c not in "0123456789abcdefABCDEF" for c in digits):
        return False
    return int(digits[:2], 16) & 0x03 == 0x02


# -- small value helpers shared by the analysis and rendering modules --------------------------

def number(value: Any) -> Optional[float]:
    """``value`` as a float when it is a real number (not a bool, not text), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def number_or_zero(value: Any) -> float:
    """Like ``number`` but a missing or non-numeric value counts as 0."""
    found = number(value)
    return 0.0 if found is None else found


def known_percent(value: Any) -> Optional[float]:
    """A 0-100 quality value, or None when missing or unknown (the controller uses -1)."""
    found = number(value)
    return None if found is None or found < 0 else found


def plural(n: int, word: str) -> str:
    """'1 time', '2 times': the count with the word, pluralised with an s."""
    return f"{n} {word}" + ("" if n == 1 else "s")


def normalize_mac(value: Optional[str]) -> str:
    """A MAC address as the upper-case text used to match records from different sources."""
    return (value or "").upper()


def hex_digits(text: Any) -> str:
    """Only the hex digits of a MAC address or fragment, lower case, whatever the separators."""
    return re.sub(r"[:\-.\s]", "", str(text)).lower()


def epoch_text(value: Any) -> str:
    """A Unix timestamp in seconds as local 'YYYY-MM-DD HH:MM:SS', or '' when it is missing or zero."""
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S") if value else ""


def format_time(value: Optional[str]) -> str:
    """An ISO-8601 timestamp (the Integration API's form) as local 'YYYY-MM-DD HH:MM:SS'; text that is
    not a timestamp is returned as it came, and nothing gives ''."""
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def search_rows(rows: List[Dict[str, Any]], search: str) -> List[Dict[str, Any]]:
    """The rows with a field that contains ``search`` (case-insensitive); all rows when it is empty."""
    if not search:
        return rows
    needle = search.lower()
    return [r for r in rows if any(needle in str(v).lower() for v in r.values())]


def record_for(table: Dict[str, Dict[str, Any]], key: Any) -> Dict[str, Any]:
    """``table[key]`` for a record keyed by device id, or ``{}`` when the key is missing or unknown.
    The id of a record may itself be missing (None), which is not a valid key, so it is checked here once."""
    return (table.get(key) if isinstance(key, str) else None) or {}
