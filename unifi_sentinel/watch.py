"""`diagnose --watch`: repeat the checks and print only what changed since the last pass.

What counts as a change is exactly what a notification would announce (``notify.plan``): a finding that is new, one
that got worse, a critical one still unresolved after ``notify_repeat_hours``, and one that is gone. A finding is the
same finding when its code and subject are the same, whatever its wording says, so a changing count prints nothing.
The state is kept in memory for the length of the run; nothing is written, sent or remembered afterwards (that is what
``--notify`` and cron are for).
"""

import time
from collections.abc import Collection
from typing import Any, Dict, List, Optional, Tuple

from .diagnose import INFO, Finding
from .notify import baseline, plan, render_text

MIN_SECONDS = 10          # each pass reads the controller (about ten requests); do not hammer it
MAX_SECONDS = 86_400


def start(findings: List[Finding], now: float, areas: Optional[Collection[str]] = None) -> Dict[str, Any]:
    """The state after the first pass: everything found so far counts as already seen."""
    return baseline(findings, now, INFO, areas)


def changes(findings: List[Finding], state: Dict[str, Any], now: float, repeat_hours: float = 24,
            areas: Optional[Collection[str]] = None) -> Tuple[List[str], Dict[str, Any]]:
    """``(lines, new state)``: one line per change since ``state``, each starting with the time, and none when
    nothing changed. Findings of every severity are followed; ``areas`` are the areas that ran, so a partial
    ``--only``/``--skip`` run never reports the findings of the others as gone."""
    events, new_state = plan(findings, state, now, INFO, repeat_hours, areas)
    if not events:
        return [], new_state
    stamp = time.strftime("%H:%M:%S", time.localtime(now))
    _, body = render_text(events)
    return [f"{stamp}  {line}" for line in body.splitlines()], new_state
