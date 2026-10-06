"""Terminal presentation: the one place that decides whether, and how, output is styled (issue #233).

Commands never look at ``isatty``, ``NO_COLOR`` or ``TERM`` themselves. ``cli.main`` builds one ``Presentation`` from
the global options (``--color``, ``--no-progress``, ``--plain``) and the environment, and hands it to the handler as
``Context.present``. Handlers ask it three kinds of question:

* ``print(*parts, stream=stream)`` prints a line whose parts are plain text or ``(text, role)`` pairs: each part is
  sanitized, and wrapped in terminal styling only when that stream may be styled. It is the only way styled text reaches
  a stream: ``commands.say`` strips escape characters (it is the backstop for plain text), so a styled string must never
  be passed to it; ``style`` is the same thing as a string;
* ``decorations(stream)``, ``symbol(name, stream)`` and ``progress_enabled(stream)`` say whether headings, symbols and
  transient progress are allowed at all;
* ``console(stream)`` is the configured Rich console for what ``style`` cannot do (tables, progress), or ``None`` when
  Rich must not be used; everything given to it from the network goes through ``literal`` first.

Rich is the optional ``pretty`` extra and only this module imports it, lazily: without it every answer is the plain one
and nothing fails. Styling is decided for stdout and for stderr separately (a redirected stdout does not switch off
a colored stderr). Machine-readable modes (``--json``, ``--csv``, ``topology --format mermaid|dot``, ``export``,
``completion``) are never styled, whatever the options say. Text from the network (device names, SSIDs, event
text) is data: it is cleaned of control characters and rendered as ``rich.text.Text``, never parsed as Rich markup.

Color precedence, first match wins (``Presentation.color_reason`` names the step that decided):
``--plain``, a machine-readable mode, Rich not installed, ``--color never``, ``--color always``, a non-empty
``NO_COLOR``, a stream that is not a terminal, ``TERM=dumb``, otherwise on.
"""

import os
import sys
from dataclasses import dataclass, field
from io import StringIO
from typing import TYPE_CHECKING, Any, Mapping, Optional, Tuple

from .util import safe_output

if TYPE_CHECKING:
    from rich.console import Console

COLOR_CHOICES = ("auto", "always", "never")
MIN_WIDTH = 40            # narrower than this: no headings, symbols or progress (text is never cut or wrapped by us)
DEFAULT_WIDTH = 80        # when the width cannot be found out (a pipe with no COLUMNS)
MACHINE_COMMANDS = frozenset({"completion", "export"})     # their stdout is data, whatever the flags
MACHINE_FLAGS = ("json", "csv")
MACHINE_FORMATS = frozenset({"mermaid", "dot"})      # `topology --format`: text to paste into another tool

# Roles are what a caller means; the style behind each is chosen here, once. The colors are the 16 standard ones, which
# a terminal theme maps to something readable on its own light or dark background. ``dim`` is for secondary text only,
# and no role may carry a meaning that is not also written (a label next to the color).
ROLES = {
    "heading": "bold cyan",
    "ok": "green",
    "warning": "yellow",
    "critical": "bold red",
    "dim": "dim",
    "subject": "bold",
    "text": "",
}

# name -> (Unicode, ASCII); the ASCII form is a word or a plain mark, so the line reads the same without the glyph.
SYMBOLS = {
    "ok": ("✓", "ok"),
    "warning": ("⚠", "!"),
    "critical": ("✗", "x"),
    "bullet": ("•", "-"),
    "arrow": ("→", "->"),
    "ellipsis": ("…", "..."),
}

# Why color is on or off (the values of ``Presentation.color_reason``).
Part = str | Tuple[str, str]          # plain text, or (text, role)
COLOR_ON = frozenset({"always", "terminal"})


def _load_rich() -> Optional[Tuple[Any, Any]]:
    """``(Console, Text)`` when the ``pretty`` extra is installed, else ``None`` (never an error)."""
    try:
        from rich.console import Console as RichConsole
        from rich.text import Text
    except ImportError:
        return None
    return RichConsole, Text


def rich_available() -> bool:
    """Whether the ``pretty`` extra (Rich) can be imported."""
    return _load_rich() is not None


def machine_output(args: Any) -> bool:
    """True when the command's stdout is data for a program (``--json``, ``--csv``, a graph format of ``topology``,
    ``export``, ``completion``)."""
    return (getattr(args, "command", None) in MACHINE_COMMANDS or getattr(args, "format", None) in MACHINE_FORMATS
            or any(getattr(args, flag, False) is True for flag in MACHINE_FLAGS))


def _is_tty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):        # no such method, or the stream is closed
        return False


@dataclass(frozen=True)
class Presentation:
    """The presentation policy of one run. ``color`` is ``--color``; ``plain``, ``no_progress`` and ``verbose`` are the
    flags of the same names; ``machine`` says the output is data; ``environ`` is the environment (injectable)."""

    color: str = "auto"
    plain: bool = False
    no_progress: bool = False
    verbose: bool = False
    machine: bool = False
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ, repr=False, compare=False)

    @classmethod
    def from_args(cls, args: Any, environ: Optional[Mapping[str, str]] = None) -> "Presentation":
        """The policy for parsed command-line ``args`` (a missing option has its default: nothing is forced)."""
        return cls(color=getattr(args, "color", "auto"), plain=bool(getattr(args, "plain", False)),
                   no_progress=bool(getattr(args, "no_progress", False)),
                   verbose=bool(getattr(args, "verbose", False)), machine=machine_output(args),
                   environ=os.environ if environ is None else environ)

    # -- what the terminal can do ---------------------------------------------------------------------------

    def dumb(self) -> bool:
        return self.environ.get("TERM", "").strip().lower() == "dumb"

    def interactive(self, stream: Any = None) -> bool:
        """A real terminal that understands control sequences (not a pipe, a file or ``TERM=dumb``)."""
        return _is_tty(sys.stdout if stream is None else stream) and not self.dumb()

    def unicode_ok(self, stream: Any = None) -> bool:
        """The stream is UTF-8 and the terminal is not dumb (``--plain`` always says no: it is ASCII)."""
        stream = sys.stdout if stream is None else stream
        encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "").replace("_", "")
        return not self.plain and not self.dumb() and encoding == "utf8"

    def width(self, stream: Any = None) -> int:
        """Columns: ``COLUMNS`` if it is a positive number, else the terminal's, else 80."""
        columns = self.environ.get("COLUMNS", "").strip()
        if columns.isdigit() and int(columns) > 0:
            return int(columns)
        try:
            found = os.get_terminal_size((sys.stdout if stream is None else stream).fileno()).columns
        except (AttributeError, OSError, ValueError):       # not a terminal, or a stream with no file descriptor
            found = 0
        return found if found > 0 else DEFAULT_WIDTH

    # -- the decisions ----------------------------------------------------------------------------------------

    def color_reason(self, stream: Any = None) -> str:
        """The step of the precedence list that decides color for ``stream``: ``plain``, ``machine``, ``no_rich``,
        ``never``, ``always``, ``no_color``, ``not_a_terminal``, ``dumb`` or ``terminal``."""
        if self.plain:
            return "plain"
        if self.machine:
            return "machine"
        if not rich_available():
            return "no_rich"
        if self.color == "never":
            return "never"
        if self.color == "always":
            return "always"
        if self.environ.get("NO_COLOR", "") != "":
            return "no_color"
        if not _is_tty(sys.stdout if stream is None else stream):
            return "not_a_terminal"
        if self.dumb():
            return "dumb"
        return "terminal"

    def color_enabled(self, stream: Any = None) -> bool:
        """Whether ``stream`` (default stdout) may carry color escapes."""
        return self.color_reason(stream) in COLOR_ON

    def decorations(self, stream: Any = None) -> bool:
        """Whether decorative headings and symbols are allowed on ``stream``: enhanced interactive output only."""
        return (not self.plain and not self.machine and rich_available() and self.interactive(stream)
                and self.width(stream) >= MIN_WIDTH)

    def progress_enabled(self, stream: Any = None) -> bool:
        """Whether transient progress may be drawn on ``stream`` (default stderr). It is only for a person at a
        terminal: not with ``--plain``, ``--no-progress``, ``--verbose`` (the log lines would interleave), a
        machine-readable mode, a narrow terminal, a non-terminal stream or ``CI`` set."""
        stream = sys.stderr if stream is None else stream
        return (not self.no_progress and not self.verbose and self.environ.get("CI", "") == ""
                and self.decorations(stream))

    def symbol(self, name: str, stream: Any = None) -> str:
        """The glyph called ``name`` when ``stream`` takes Unicode and decoration is allowed, else its ASCII form."""
        unicode_form, ascii_form = SYMBOLS[name]
        return unicode_form if self.unicode_ok(stream) and self.decorations(stream) else ascii_form

    # -- output -----------------------------------------------------------------------------------------------

    def console(self, stream: Any = None) -> Optional["Console"]:
        """A Rich console writing to ``stream`` (default stdout), configured by this policy: markup, highlighting and
        emoji codes are off, color only where ``color_enabled``, terminal behavior (cursor control, animation) only on
        an interactive stream. ``None`` when Rich is missing, ``--plain`` is given or the output is data: the caller
        then prints plain text."""
        loaded = _load_rich()
        if loaded is None or self.plain or self.machine:
            return None
        stream = sys.stdout if stream is None else stream
        color = self.color_enabled(stream)
        interactive = self.interactive(stream)
        return loaded[0](
            file=stream, force_terminal=color or interactive, force_interactive=interactive,
            color_system=(("auto" if interactive else "standard") if color else None), no_color=not color,
            markup=False, highlight=False, emoji=False, width=self.width(stream))

    def style(self, text: Any, role: str = "text", stream: Any = None) -> str:
        """``text`` as one string to print on ``stream`` (default stdout): control characters removed, and wrapped in
        the escape sequences of ``role`` only when ``color_enabled(stream)``. It is literal: ``[red]x[/red]`` stays
        exactly that. An unknown ``role`` is a programming error."""
        style = ROLES[role]
        clean = safe_output(str(text))
        loaded = _load_rich()
        if not style or loaded is None or not self.color_enabled(stream):
            return clean
        console = loaded[0](
            file=StringIO(), force_terminal=True, force_interactive=False, color_system="standard", no_color=False,
            markup=False, highlight=False, emoji=False, width=10_000, soft_wrap=True)
        with console.capture() as captured:
            console.print(loaded[1](clean, style=style), end="", soft_wrap=True, overflow="ignore", crop=False)
        return captured.get()

    def literal(self, text: Any) -> Any:
        """``text`` for ``console(...).print``: control characters removed (Rich does not strip the escape character
        itself) and, with Rich, a ``Text`` so that it is never read as markup. Without Rich: the cleaned string."""
        clean = safe_output(str(text))
        loaded = _load_rich()
        return clean if loaded is None else loaded[1](clean)

    def print(self, *parts: Part, stream: Any = None, end: str = "\n") -> None:
        """Print one line to ``stream`` (default stdout) made of ``parts``, each a string or a ``(text, role)`` pair."""
        stream = sys.stdout if stream is None else stream
        line = "".join(self.style(*part, stream=stream) if isinstance(part, tuple) else self.style(part, stream=stream)
                       for part in parts)
        print(line, end=end, file=stream)
