"""Transient progress on stderr for the commands that read the controller (issue #234).

A person at a terminal who runs a slow command sees one restrained line, redrawn in place: a spinner, what the tool is
really waiting for (``Reading devices``) and how long it has been going (``3.2s``). Nothing more is claimed: the total
is not known, so there is no percentage, no bar and no estimate.

* **Real stages.** The work calls ``client.stage("...")`` at its real boundaries (``snapshot.collect_snapshot`` and the
  documents); ``client.note("retrying")`` adds a word to the current stage while a request is backing off. Both are
  no-ops unless a ``Progress`` is running. Labels are fixed words, never text from the network.
* **No flash.** Nothing is drawn until ``DELAY`` seconds have passed, so a quick command prints no escape sequence at
  all, and no artificial delay is added.
* **One stable line.** It is cut to the terminal width (the label is shortened, the spinner and the elapsed time stay)
  and never wraps. Unicode spinner frames where the stream takes UTF-8, ``|/-\\`` otherwise.
* **Only when it is welcome.** ``Presentation.progress_enabled(stream)`` decides: a terminal on stderr, and not
  ``--plain``, ``--no-progress``, ``--verbose``, ``CI``, a machine-readable mode or a narrow terminal. ``ELIGIBLE``
  names the commands that show it, and ``--watch`` and ``--notify`` runs never do. Redirected stderr never gets an
  escape sequence.
* **Never in the way.** The line is erased before anything else is written to the terminal: a warning or log line
  (``logs.STDERR_GUARD``) and the first output of the command itself (``finish``, called by ``commands.say`` and
  ``Presentation.print``), after which progress ends for good. The cursor is hidden only while a line is drawn and is
  restored on success, on an error, on Ctrl+C and at exit.
"""

import atexit
import contextlib
import sys
import threading
import time
from typing import Any, Callable, Iterator, Optional

from . import logs
from .present import Presentation

ELIGIBLE = frozenset({"audit", "client", "diagnose", "diff", "events", "firewall", "info", "new-clients", "query",
                      "snapshot", "topology", "wan", "wifi"})
INITIAL_STAGE = "Contacting the controller"
DELAY = 0.2                 # seconds of work before anything is drawn
INTERVAL = 0.1              # seconds between redraws
SPINNER_UNICODE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_ASCII = "|/-\\"
HIDE_CURSOR, SHOW_CURSOR, ERASE_LINE = "\x1b[?25l", "\x1b[?25h", "\r\x1b[K"

current: Optional["Progress"] = None
_outer_guard: Optional[Callable[[], Any]] = None      # what ``logs.STDERR_GUARD`` was before the first progress started


@contextlib.contextmanager
def _guard() -> Iterator[None]:
    """``logs.STDERR_GUARD`` while any progress runs: it always asks the one that is current, so a progress that was
    replaced or stopped can never be left behind as the guard."""
    running = current
    if running is None:
        yield
    else:
        with running.hold():
            yield


def eligible(args: Any) -> bool:
    """Does this run show progress at all? The command must be one of ``ELIGIBLE``; ``diagnose --watch`` and
    ``--notify`` are long-running or unattended and never do."""
    if getattr(args, "command", None) not in ELIGIBLE:
        return False
    return not (getattr(args, "watch", None) or getattr(args, "notify", False))


def format_elapsed(seconds: float) -> str:
    """``3.2s`` under ten seconds, ``42s`` under a minute, ``1m 05s`` after."""
    if seconds < 10:
        return f"{max(seconds, 0.0):.1f}s"
    if seconds < 60:
        return f"{int(seconds)}s"
    return f"{int(seconds) // 60}m {int(seconds) % 60:02d}s"


def finish() -> None:
    """End the running progress (if any) and leave the terminal clean: called before the first real output."""
    running = current
    if running is not None:
        running.stop()


class Progress:
    """One transient progress line. Use it as a context manager around the work; ``enabled`` False (the command is not
    eligible, or the terminal cannot take it) makes every method a no-op. ``clock`` and ``threaded`` are for tests: with
    ``threaded=False`` nothing runs on its own and the test calls ``tick()``."""

    def __init__(self, present: Presentation, enabled: bool = True, stream: Any = None,
                 clock: Callable[[], float] = time.monotonic, delay: Optional[float] = None,
                 interval: Optional[float] = None, threaded: bool = True) -> None:
        self.present = present
        self.stream = sys.stderr if stream is None else stream
        self.enabled = enabled and present.progress_enabled(self.stream)
        self._clock, self._threaded = clock, threaded
        self._delay = DELAY if delay is None else delay        # read now, so a test can change the module values
        self._interval = INTERVAL if interval is None else interval
        self._lock = threading.RLock()
        self._label = INITIAL_STAGE      # never a blank label: a read nobody named is still "contacting the controller"
        self._note = ""
        self._started: Optional[float] = None
        self._drawn = False          # a line is on the screen now
        self._hidden = False         # the cursor was hidden (and must be shown again)
        self._stopped = False
        self._frame = 0
        self._done = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- what the work says -------------------------------------------------------------------------------

    def stage(self, label: str) -> None:
        """The work moved on to ``label`` (a fixed phrase); a note about the previous stage is dropped."""
        with self._lock:
            self._label, self._note = label, ""

    def note(self, text: str) -> None:
        """Add a word to the current stage (for example ``retrying``)."""
        with self._lock:
            self._note = text

    # -- lifetime -----------------------------------------------------------------------------------------

    def start(self) -> None:
        global current
        with self._lock:
            if not self.enabled or self._started is not None:
                return
            self._started = self._clock()
            current = self
            global _outer_guard
            if _outer_guard is None:
                _outer_guard = logs.STDERR_GUARD
            logs.STDERR_GUARD = _guard
            atexit.register(self.stop)
        if self._threaded:
            self._thread = threading.Thread(target=self._run, name="hlp-progress", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Erase the line, show the cursor and stop; safe to call again."""
        global current
        with self._lock:
            if self._stopped or self._started is None:
                return
            self._stopped = True
            self._erase()
            if self._hidden:
                self._write(SHOW_CURSOR)
                self._hidden = False
            if current is self:
                current = None
                global _outer_guard
                # the last one out puts back what was there before
                logs.STDERR_GUARD = _outer_guard or contextlib.nullcontext
                _outer_guard = None
        self._done.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        atexit.unregister(self.stop)

    def __enter__(self) -> "Progress":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- drawing ------------------------------------------------------------------------------------------

    @contextlib.contextmanager
    def hold(self) -> Iterator[None]:
        """Erase the line and keep the renderer from drawing while the caller writes to the terminal."""
        with self._lock:
            self._erase()
            yield

    def tick(self) -> None:
        """Draw (or redraw) the line when the delay has passed and the work is still going."""
        with self._lock:
            if self._stopped or self._started is None or not self.enabled:
                return
            elapsed = self._clock() - self._started
            if elapsed < self._delay:
                return
            line = self._line(elapsed)
            if not self._hidden:
                self._write(HIDE_CURSOR)
                self._hidden = True
            self._write(ERASE_LINE + line)
            self._drawn = True

    def _run(self) -> None:
        while not self._done.wait(self._interval):
            self.tick()

    def _line(self, elapsed: float) -> str:
        frames = SPINNER_UNICODE if self.present.unicode_ok(self.stream) else SPINNER_ASCII
        spinner = frames[self._frame % len(frames)]
        self._frame += 1
        text = self._label + (f" ({self._note})" if self._note else "")
        clock = format_elapsed(elapsed)
        ellipsis = "…" if self.present.unicode_ok(self.stream) else "..."
        room = self.present.width(self.stream) - 1 - len(spinner) - len(clock) - 2    # never reach the last column
        if len(text) > room:
            text = text[:max(room - len(ellipsis), 0)] + ellipsis
        style = self.present.style
        return (style(spinner, "heading", self.stream) + " " + style(text, "text", self.stream) + " "
                + style(clock, "dim", self.stream))

    def _erase(self) -> None:
        if self._drawn:
            self._write(ERASE_LINE)
            self._drawn = False

    def _write(self, text: str) -> None:
        try:
            self.stream.write(text)
            self.stream.flush()
        except (OSError, ValueError):          # a closed or broken terminal must never take the command down
            self.enabled = False
