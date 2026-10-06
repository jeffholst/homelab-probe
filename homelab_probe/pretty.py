"""Enhanced human output on a terminal: findings grouped and colored, inventory tables that fit (issue #235).

This is the command-line boundary only: it renders the documents the commands already build (the dict that ``--json``
prints) and never changes them, the exit codes or any machine-readable output. A handler uses it only when
``Presentation.decorations(sys.stdout)`` says yes (the ``pretty`` extra, a terminal, at least 40 columns, not
``--plain``, not a machine-readable mode); everywhere else the plain renderers print exactly what they always did.

* **Color carries no meaning on its own.** Every finding keeps its written label (``CRITICAL``, ``WARNING``,
  ``INFO``) next to its color (red, yellow, green for a healthy result, cyan for headings, dim for secondary text only:
  the code and the counts' separators). The 16 standard colors are used, which a terminal theme maps for its own light
  or dark background.
* **Nothing is dropped or cut.** Every finding is shown, in its severity group, with its subject, message and code;
  long messages wrap under their finding, a table too wide for the terminal turns into stacked rows instead of
  truncating a name or an address.
* **An honest summary.** The closing line counts what was found and how long it took, says which areas were checked
  when ``--only``/``--skip`` chose them, and a partial read is never called healthy: "no issues" is green only when
  every check could read what it needed.
* **No forged lines.** The text under a finding is behind a bar (``│``), so a message that wraps onto a new line
  cannot look like a finding of its own.
* **Names are data.** A device or SSID is printed literally (``[red]x[/red]`` stays exactly that), flattened to one
  line and cleaned of control and direction-changing characters (``util.printable``), so a name cannot forge a line.
"""

import textwrap
from typing import Any, Dict, List, Optional, Sequence

from .diagnose.areas import AREA_NAMES
from .present import Presentation
from .util import printable

SEVERITIES = (("critical", "Critical", "critical", "critical"), ("warning", "Warnings", "warning", "warning"),
              ("info", "Info", "text", "bullet"))         # (severity, heading, role, symbol)
LABELS = {"critical": "CRITICAL", "warning": "WARNING", "info": "INFO"}     # the written label next to every color
GOOD_STATES = frozenset({"online", "up", "connected", "yes"})
BAD_STATES = frozenset({"offline", "down", "disconnected"})
INDENT = "    "


def elapsed_text(seconds: float) -> str:
    """``0.8s``, ``12s`` or ``1m 05s``."""
    if seconds < 10:
        return f"{max(seconds, 0.0):.1f}s"
    if seconds < 60:
        return f"{int(seconds)}s"
    return f"{int(seconds) // 60}m {int(seconds) % 60:02d}s"


def _wrap(text: str, width: int, indent: str) -> List[str]:
    """``text`` wrapped to ``width`` with ``indent`` on every line; long words (an address) are never broken."""
    room = max(width - len(indent), 20)
    return [indent + line for line in textwrap.wrap(text, room, break_long_words=False, break_on_hyphens=False)
            or [""]]


def _print_body(present: Presentation, text: str, width: int, role: str = "text") -> None:
    """The text under a finding, wrapped, every line behind a bar: a line of a message can then never look like the
    start of a finding of its own, whatever the message says."""
    bar = "│ " if present.unicode_ok() else "| "
    for line in _wrap(text, width - len(bar), INDENT):
        present.print(line[:len(INDENT)], (bar, "dim"), (line[len(INDENT):], role))


def print_findings(present: Presentation, document: Dict[str, Any], *, elapsed: Optional[float] = None,
                   complete: bool = True, show_ignored: bool = False, show_checked: bool = False) -> None:
    """``diagnose`` and ``audit`` on a terminal: the findings grouped by severity (most severe first), each with its
    label, subject, code and wrapped message, then the closing summary and, when asked, the ignored ones."""
    width = present.width()
    findings: List[Dict[str, Any]] = document["findings"]
    for severity, heading, role, symbol in SEVERITIES:
        group = [f for f in findings if f["severity"] == severity]
        if not group:
            continue
        present.print((f"{heading} ({len(group)})", "heading"))
        for finding in group:
            present.print("  ", (present.symbol(symbol), role), " ", (LABELS[severity], role), "  ",
                          (printable(finding["subject"]), "subject"), "  ",
                          (printable(finding.get("code") or ""), "dim"))
            _print_body(present, printable(finding["message"]), width)
        present.print("")
    _print_summary(present, document, elapsed, complete, show_checked)
    if show_ignored and document.get("ignored"):
        present.print("")
        present.print((f"Ignored ({len(document['ignored'])})", "heading"))
        for row in document["ignored"]:
            until = f" until {row['until']}" if row.get("until") else ""
            present.print("  ", (printable(row["subject"]), "subject"), ": ", printable(row["message"]))
            reason = f": {row['reason']}" if row.get("reason") else ""
            _print_body(present, printable(f"{row.get('code') or ''} ignored{until}{reason}".strip()), width, "dim")


def _print_summary(present: Presentation, document: Dict[str, Any], elapsed: Optional[float], complete: bool,
                   show_checked: bool) -> None:
    summary = document["summary"]
    parts: List[Any] = []
    for severity, _, role, _ in SEVERITIES:
        count = summary.get(severity, 0)
        if count:
            word = "warnings" if severity == "warning" and count != 1 else severity
            parts += [(f"{count} {word}", role), ", "]
    found = bool(parts)
    if found:
        parts.pop()
    elif complete:
        parts = [(present.symbol("ok"), "ok"), " ", ("No issues found", "ok")]
    else:
        parts = [(present.symbol("warning"), "warning"), " ",
                 ("No issues found in what could be read, but the read was incomplete", "warning")]
    if summary.get("ignored"):
        parts += [(" · " if present.unicode_ok() else " - ", "dim"), f"{summary['ignored']} ignored"]
    if elapsed is not None:
        parts += [(" · " if present.unicode_ok() else " - ", "dim"), (elapsed_text(elapsed), "dim")]
    present.print(*parts)
    if found and not complete:
        present.print((present.symbol("warning"), "warning"), " ",
                      ("The read was incomplete: some checks could not see everything (see the warnings above), so "
                       "there may be more.", "warning"))
    if show_checked:
        checked = document.get("areas") or []
        skipped = [a for a in AREA_NAMES if a not in checked]
        line: List[Any] = [("Checked: ", "dim"), ", ".join(checked) or "nothing"]
        if skipped:
            line += [("  not checked: ", "dim"), ", ".join(skipped)]
        present.print(*line)


def _state_role(value: Any) -> str:
    word = str(value).strip().lower()
    return "ok" if word in GOOD_STATES else "warning" if word in BAD_STATES else "text"


def print_table(present: Presentation, rows: Sequence[Dict[str, Any]], columns: Sequence[str], footer: str,
                state_columns: Sequence[str] = ("Status",)) -> None:
    """An inventory table on a terminal: aligned columns with a highlighted header and colored states where it fits
    the terminal, stacked ``Column: value`` rows where it does not (nothing is cut). ``footer`` is the count line."""
    cells = [{c: printable(r.get(c, "")) for c in columns} for r in rows]       # one line each, cleaned of controls
    widths = {c: max([len(c)] + [len(r[c]) for r in cells]) for c in columns}
    total = sum(widths.values()) + 2 * (len(columns) - 1)
    if total < present.width():
        present.print(*_cells(columns, {c: c for c in columns}, widths, "heading"))
        for row in cells:
            present.print(*_cells(columns, row, widths, None, state_columns))
    else:
        label_width = max(len(c) for c in columns)
        for index, row in enumerate(cells):
            if index:
                present.print("")
            for column in columns:
                if row[column]:
                    role = _state_role(row[column]) if column in state_columns else "text"
                    present.print((column.ljust(label_width), "dim"), "  ", (row[column], role))
    present.print("")
    present.print((footer, "dim"))


def _cells(columns: Sequence[str], values: Dict[str, str], widths: Dict[str, int], role: Optional[str],
           state_columns: Sequence[str] = ()) -> List[Any]:
    parts: List[Any] = []
    last = len(columns) - 1
    for i, column in enumerate(columns):
        text = values[column] if i == last else values[column].ljust(widths[column])
        cell_role = role or (_state_role(values[column]) if column in state_columns else "text")
        parts.append((text.rstrip() if i == last else text, cell_role))
        if i != last:
            parts.append("  ")
    return parts
