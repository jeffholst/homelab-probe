"""Ignoring findings, exit codes and rendering (text and JSON)."""

import json
from collections.abc import Sequence
from typing import Any, Dict, List, Optional, Tuple

from ..settings import IgnoreRule
from ..util import printable
from .areas import AREA_NAMES
from .model import (
    CRITICAL,
    EMOJI,
    EXIT_CRITICAL,
    EXIT_OK,
    EXIT_WARNING,
    INFO,
    SEVERITY_ORDER,
    WARNING,
    Finding,
)


def apply_ignores(
    findings: List[Finding], rules: Tuple[IgnoreRule, ...]
) -> Tuple[List[Finding], List[Tuple[Finding, IgnoreRule]]]:
    """Split findings into (kept, [(ignored finding, the rule that matched)])."""
    kept: List[Finding] = []
    ignored: List[Tuple[Finding, IgnoreRule]] = []
    for f in findings:
        rule = next((r for r in rules if r.matches(f.subject, f.message)), None)
        if rule:
            ignored.append((f, rule))
        else:
            kept.append(f)
    return kept, ignored


def exit_code(findings: List[Finding], fail_on: str = WARNING) -> int:
    """Exit code for a set of findings.

    Critical findings always give EXIT_CRITICAL. Warnings (and info) give
    EXIT_WARNING only when ``fail_on`` is at or below their severity; otherwise 0.
    """
    if not findings:
        return EXIT_OK
    worst = min(SEVERITY_ORDER[f.severity] for f in findings)
    if worst == SEVERITY_ORDER[CRITICAL]:
        return EXIT_CRITICAL
    return EXIT_WARNING if worst <= SEVERITY_ORDER[fail_on] else EXIT_OK


def format_findings(findings: List[Finding], emoji: bool = True, ignored: int = 0) -> str:
    """Render findings. ``emoji=False`` uses text labels (logs, pipes, old terminals).
    ``ignored`` is how many findings the ignore list suppressed (noted in the summary)."""
    note = f" ({ignored} ignored)" if ignored else ""
    if not findings:
        return "No issues found." + note

    def label(severity: str) -> str:
        return EMOJI[severity] if emoji else f"[{severity.upper():8}]"

    lines = [f"{label(f.severity)} {printable(f.subject)}: {printable(f.message)}" for f in findings]
    counts = {sev: sum(f.severity == sev for f in findings) for sev in SEVERITY_ORDER}
    words = {CRITICAL: "critical", WARNING: "warning", INFO: "info"}
    parts = []
    for sev in SEVERITY_ORDER:
        if counts[sev]:
            word = words[sev] + ("s" if sev == WARNING and counts[sev] != 1 else "")
            prefix = f"{EMOJI[sev]} " if emoji else ""
            parts.append(f"{prefix}{counts[sev]} {word}")
    return "\n".join(lines) + "\n\n" + ", ".join(parts) + note


def format_ignored(ignored: List[Tuple[Finding, IgnoreRule]]) -> str:
    """The findings the ignore list suppressed, with each rule's reason."""
    lines = [f"  {printable(f.subject)}: {printable(f.message)}  (ignored: {printable(r.reason)})"
             for f, r in ignored]
    return f"Ignored ({len(ignored)}):\n" + "\n".join(lines)


JSON_VERSION = 1


def findings_json(findings: List[Finding], ignored: List[Tuple[Finding, IgnoreRule]],
                  show_ignored: bool = False, areas: Optional[Sequence[str]] = None) -> str:
    """``diagnose --json``: the findings with their stable codes, a severity summary and the
    number the ignore list suppressed. The ``ignored`` list (each with its rule's reason) is
    only included with ``show_ignored``, as in the text output. Names are raw here, which is
    safe: JSON escapes control characters itself. ``areas`` are the areas of checks that ran (all of them by
    default), so a consumer can tell "nothing found" from "not looked at"."""
    document: Dict[str, Any] = {
        "version": JSON_VERSION,
        "areas": list(AREA_NAMES if areas is None else areas),
        "summary": {**{sev: sum(f.severity == sev for f in findings) for sev in SEVERITY_ORDER},
                    "ignored": len(ignored)},
        "findings": [f.to_dict() for f in findings],
    }
    if show_ignored:
        document["ignored"] = [{**f.to_dict(), "reason": rule.reason} for f, rule in ignored]
    return json.dumps(document, indent=2)


def stream_supports_emoji(stream: Any) -> bool:
    """True for an interactive UTF-8 terminal; otherwise text labels are safer."""
    encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
    return bool(getattr(stream, "isatty", lambda: False)()) and encoding == "utf8"
