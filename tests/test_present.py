"""The presentation policy (issue #233): color precedence, independent streams, machine output, literal text.

The decision tests do not need Rich: they replace ``present.rich_available`` so the matrix is the same with and
without the extra. The tests that render real escape sequences use ``pytest.importorskip("rich")`` and the base
install skips them; the tests of the missing-extra path remove Rich with ``sys.modules`` so they run everywhere.
"""

import argparse
import io
import re
import sys
from dataclasses import replace
from typing import Any, Dict, Optional

import pytest

from homelab_probe import cli, commands, present
from homelab_probe.commands import Context
from homelab_probe.present import Presentation

ANSI = re.compile(r"\x1b\[[0-9;]*m")
CLEAN_ENV: Dict[str, str] = {"TERM": "xterm-256color"}


class Stream:
    """A stand-in output stream: a terminal or not, an encoding, and whether it has a file descriptor."""

    def __init__(self, tty: bool = False, encoding: Optional[str] = "utf-8", fd: Optional[int] = None):
        self.tty, self.encoding, self.fd = tty, encoding, fd
        self.written = io.StringIO()

    def isatty(self) -> bool:
        return self.tty

    def fileno(self) -> int:
        if self.fd is None:
            raise io.UnsupportedOperation("fileno")
        return self.fd

    def write(self, text: str) -> int:
        return self.written.write(text)

    def flush(self) -> None:
        pass


def policy(environ: Optional[Dict[str, str]] = None, **options: Any) -> Presentation:
    return Presentation(environ=CLEAN_ENV if environ is None else environ, **options)


@pytest.fixture
def rich_on(monkeypatch):
    """The decision logic as if the extra were installed (whether or not it is)."""
    monkeypatch.setattr(present, "rich_available", lambda: True)


@pytest.fixture
def rich_off(monkeypatch):
    """The extra really is not importable."""
    for name in ("rich", "rich.console", "rich.text"):
        monkeypatch.setitem(sys.modules, name, None)


# -- color precedence ---------------------------------------------------------------------------------------

# (options, environment, stream is a terminal, expected reason)
PRECEDENCE = [
    (dict(plain=True, color="always"), {}, True, "plain"),                       # --plain beats --color always
    (dict(plain=True, color="always", machine=True), {}, True, "plain"),
    (dict(machine=True, color="always"), {}, True, "machine"),                   # data is never colored, even forced
    (dict(color="never"), {}, True, "never"),
    (dict(color="never"), {"NO_COLOR": "1"}, True, "never"),
    (dict(color="always"), {}, False, "always"),                                 # forced into a pipe
    (dict(color="always"), {"NO_COLOR": "1"}, True, "always"),                   # an explicit option beats NO_COLOR
    (dict(color="always"), {"TERM": "dumb"}, True, "always"),
    (dict(), {"NO_COLOR": "1"}, True, "no_color"),
    (dict(), {"NO_COLOR": "anything"}, True, "no_color"),
    (dict(), {"NO_COLOR": ""}, True, "terminal"),                                # an empty NO_COLOR does not count
    (dict(), {}, False, "not_a_terminal"),
    (dict(), {"TERM": "dumb"}, True, "dumb"),
    (dict(), {"TERM": "DUMB"}, True, "dumb"),
    (dict(), {}, True, "terminal"),
    (dict(color="auto"), {"TERM": "xterm"}, True, "terminal"),
]


@pytest.mark.parametrize(("options", "environ", "tty", "reason"), PRECEDENCE)
def test_color_precedence(rich_on, options, environ, tty, reason):
    stream = Stream(tty=tty)
    decision = Presentation(environ=environ, **options)
    assert decision.color_reason(stream) == reason
    assert decision.color_enabled(stream) is (reason in {"always", "terminal"})


@pytest.mark.parametrize("color", ["auto", "always", "never"])
def test_without_the_extra_nothing_is_ever_colored(rich_off, color):
    decision = policy(color=color)
    stream = Stream(tty=True)
    assert decision.color_reason(stream) == "no_rich" and not decision.color_enabled(stream)
    assert decision.console(stream) is None
    assert decision.style("name", "critical", stream) == "name"
    assert not decision.decorations(stream) and not decision.progress_enabled(stream)
    assert present.rich_available() is False


def test_plain_and_machine_still_win_over_a_missing_extra(rich_off):
    assert policy(plain=True).color_reason(Stream()) == "plain"
    assert policy(machine=True).color_reason(Stream()) == "machine"


def test_stdout_and_stderr_are_judged_independently(rich_on):
    decision = policy()
    out, err = Stream(tty=False), Stream(tty=True)         # stdout redirected to a file, stderr on the terminal
    assert not decision.color_enabled(out) and decision.color_enabled(err)
    assert not decision.progress_enabled(out) and decision.progress_enabled(err)
    out, err = Stream(tty=True), Stream(tty=False)         # stderr redirected, stdout on the terminal
    assert decision.color_enabled(out) and not decision.color_enabled(err)
    assert not decision.progress_enabled(err)


def test_the_default_streams_are_stdout_for_color_and_stderr_for_progress(rich_on, monkeypatch):
    monkeypatch.setattr(sys, "stdout", Stream(tty=False))
    monkeypatch.setattr(sys, "stderr", Stream(tty=True))
    decision = policy()
    assert decision.color_reason() == "not_a_terminal" and decision.progress_enabled()
    assert not decision.interactive() and decision.width() == present.DEFAULT_WIDTH
    assert decision.unicode_ok()                       # the stand-in's encoding is utf-8


def test_a_stream_that_cannot_say_whether_it_is_a_terminal_is_not_one(rich_on):
    class Bare:
        encoding = "utf-8"

    class Closed:
        def isatty(self):
            raise ValueError("I/O operation on closed file")

    for stream in (Bare(), Closed()):
        assert policy().color_reason(stream) == "not_a_terminal" and not policy().interactive(stream)


# -- width, encoding, decorations, progress ---------------------------------------------------------------------

def test_the_width_comes_from_columns_then_the_terminal_then_eighty(monkeypatch):
    assert policy({"COLUMNS": "132"}).width(Stream()) == 132
    for bad in ("", "abc", "0", "-5", "1.5"):
        assert policy({"COLUMNS": bad}).width(Stream()) == present.DEFAULT_WIDTH, bad
    monkeypatch.setattr(present.os, "get_terminal_size", lambda fd: type("S", (), {"columns": 101})())
    assert policy().width(Stream(fd=7)) == 101
    assert policy({"COLUMNS": "55"}).width(Stream(fd=7)) == 55                  # the variable wins
    monkeypatch.setattr(present.os, "get_terminal_size", lambda fd: type("S", (), {"columns": 0})())
    assert policy().width(Stream(fd=7)) == present.DEFAULT_WIDTH                  # a terminal that does not know


def test_a_stream_without_a_descriptor_or_a_terminal_size_gets_the_default(monkeypatch):
    def refuse(fd):
        raise OSError("not a terminal")

    monkeypatch.setattr(present.os, "get_terminal_size", refuse)
    assert policy().width(Stream(fd=7)) == present.DEFAULT_WIDTH
    assert policy().width(Stream(fd=None)) == present.DEFAULT_WIDTH
    assert policy().width(io.StringIO()) == present.DEFAULT_WIDTH
    assert policy().width(object()) == present.DEFAULT_WIDTH                      # not even a fileno method


@pytest.mark.parametrize(("encoding", "expected"), [
    ("utf-8", True), ("UTF-8", True), ("utf8", True), ("UTF_8", True),
    ("ascii", False), ("latin-1", False), ("", False), (None, False)])
def test_unicode_needs_a_utf8_stream(encoding, expected):
    assert policy().unicode_ok(Stream(encoding=encoding)) is expected


def test_unicode_is_off_for_plain_and_for_a_dumb_terminal():
    assert not policy(plain=True).unicode_ok(Stream())
    assert not policy({"TERM": "dumb"}).unicode_ok(Stream())


def test_decorations_need_an_interactive_wide_enough_terminal_with_the_extra(rich_on):
    stream = Stream(tty=True)
    assert policy({"TERM": "xterm", "COLUMNS": "80"}).decorations(stream)
    assert policy({"TERM": "xterm", "COLUMNS": str(present.MIN_WIDTH)}).decorations(stream)
    assert not policy({"TERM": "xterm", "COLUMNS": str(present.MIN_WIDTH - 1)}).decorations(stream)    # narrow
    assert not policy({"TERM": "dumb"}).decorations(stream)
    assert not policy().decorations(Stream(tty=False))
    assert not policy(plain=True).decorations(stream)
    assert not policy(machine=True).decorations(stream)
    assert policy({"TERM": "xterm"}, color="never").decorations(stream)       # color is a separate decision


def test_decorations_need_the_extra(rich_off):
    assert not policy().decorations(Stream(tty=True))


PROGRESS = [
    (dict(), {}, True),
    (dict(no_progress=True), {}, False),
    (dict(plain=True), {}, False),
    (dict(verbose=True), {}, False),                 # log lines and an animation would interleave
    (dict(machine=True), {}, False),
    (dict(color="never"), {}, True),                 # color is a separate decision
    (dict(), {"CI": "true"}, False),                 # an unattended run
    (dict(), {"CI": ""}, True),
    (dict(), {"TERM": "dumb"}, False),
    (dict(), {"COLUMNS": "20"}, False),              # too narrow for one stable line
]


@pytest.mark.parametrize(("options", "environ", "enabled"), PROGRESS)
def test_progress_matrix(rich_on, options, environ, enabled):
    assert Presentation(environ={"TERM": "xterm", **environ}, **options).progress_enabled(Stream(tty=True)) is enabled


def test_progress_is_never_enabled_on_a_pipe_or_a_file(rich_on):
    assert not policy().progress_enabled(Stream(tty=False))


def test_symbols_are_unicode_only_where_decoration_and_the_encoding_allow(rich_on):
    wide = {"TERM": "xterm", "COLUMNS": "80"}
    tty = Stream(tty=True)
    assert policy(wide).symbol("ok", tty) == "✓" and policy(wide).symbol("arrow", tty) == "→"
    assert policy(wide).symbol("ok", Stream(tty=True, encoding="ascii")) == "ok"
    assert policy(wide).symbol("warning", Stream(tty=True, encoding="ascii")) == "!"
    assert policy(wide, plain=True).symbol("critical", tty) == "x"
    assert policy(wide).symbol("ellipsis", Stream(tty=False)) == "..."
    assert policy({"TERM": "xterm", "COLUMNS": "10"}).symbol("bullet", tty) == "-"
    assert {name: pair[1] for name, pair in present.SYMBOLS.items()}["arrow"] == "->"
    assert all(ascii_form.isascii() for _, ascii_form in present.SYMBOLS.values())


# -- machine-readable output ----------------------------------------------------------------------------------

MACHINE = [
    (["completion", "bash"], True),
    (["export"], True),
    (["export", "--format", "json"], True),
    (["query", "clients", "--json"], True),
    (["query", "clients", "--csv"], True),
    (["diagnose", "--json"], True),
    (["events", "--json"], True),
    (["wan", "--json"], True),
    (["doctor", "--json"], True),
    (["topology", "--format", "mermaid"], True),
    (["topology", "--format", "dot"], True),
    (["topology", "--format", "text"], False),
    (["diagnose"], False),
    (["query", "clients"], False),
    (["wan"], False),
    (["topology"], False),
]


@pytest.mark.parametrize(("argv", "machine"), MACHINE)
def test_which_invocations_print_data(argv, machine):
    args = cli.build_parser().parse_args(argv)
    assert present.machine_output(args) is machine
    assert Presentation.from_args(args, environ=CLEAN_ENV).machine is machine


@pytest.mark.parametrize("argv", [argv for argv, machine in MACHINE if machine])
def test_forced_color_never_reaches_data(rich_on, argv):
    args = cli.build_parser().parse_args(["--color", "always", *argv])
    decision = Presentation.from_args(args, environ=CLEAN_ENV)
    for stream in (Stream(tty=True), Stream(tty=False)):
        assert not decision.color_enabled(stream) and decision.color_reason(stream) == "machine"
        assert not decision.decorations(stream) and not decision.progress_enabled(stream)
        assert decision.console(stream) is None
        assert decision.style("name", "critical", stream) == "name"


def test_forced_color_does_reach_human_output(rich_on):
    args = cli.build_parser().parse_args(["--color", "always", "diagnose"])
    assert Presentation.from_args(args, environ=CLEAN_ENV).color_enabled(Stream(tty=False))


def test_from_args_reads_every_option_and_tolerates_missing_ones():
    args = cli.build_parser().parse_args(["--color", "never", "--no-progress", "--plain", "--verbose", "wan"])
    decision = Presentation.from_args(args, environ={})
    assert (decision.color, decision.no_progress, decision.plain, decision.verbose) == ("never", True, True, True)
    bare = Presentation.from_args(argparse.Namespace(), environ={})
    assert bare == Presentation() and not bare.machine
    assert Presentation.from_args(argparse.Namespace()).environ is not None      # the process environment by default


# -- the command line ---------------------------------------------------------------------------------------

def test_the_options_are_global_and_default_to_automatic():
    args = cli.build_parser().parse_args(["wan"])
    assert (args.color, args.no_progress, args.plain) == ("auto", False, False)
    args = cli.build_parser().parse_args(["--color", "always", "--no-progress", "--plain", "wan"])
    assert (args.color, args.no_progress, args.plain) == ("always", True, True)


def test_a_bad_color_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--color", "purple", "wan"])
    assert stopped.value.code == 64 and "invalid choice" in capsys.readouterr().err


def test_the_options_come_before_the_command(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["wan", "--plain"])
    assert stopped.value.code == 64


def test_a_context_built_without_a_policy_is_plain(rich_on):
    context = Context(argparse.Namespace(), None, None, None)
    assert context.present == Presentation(plain=True)
    terminal = Stream(tty=True)
    assert context.present.color_reason(terminal) == "plain" and not context.present.color_enabled(terminal)
    assert not context.present.decorations(terminal) and not context.present.progress_enabled(terminal)
    assert context.present.style("x", "critical", terminal) == "x" and context.present.console(terminal) is None


def run_probe(monkeypatch, argv, environ=None):
    """Run ``cli.main`` with a handler that prints through ``ctx.present``; returns (policy seen, exit code)."""
    seen = {}

    def handler(ctx: Context) -> int:
        seen["present"] = ctx.present
        ctx.present.print(("name [red]x[/red]", "critical"))
        return 0

    for name in ("TERM", "NO_COLOR", "COLUMNS", "CI", "FORCE_COLOR"):
        monkeypatch.delenv(name, raising=False)
    for name, value in (environ or {}).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setitem(commands.COMMANDS_BY_NAME, "info", replace(commands.COMMANDS_BY_NAME["info"], run=handler))
    code = cli.main(["--demo", *argv, "info"])
    return seen["present"], code


def test_main_hands_the_policy_to_the_handler(monkeypatch, capsys):
    seen, code = run_probe(monkeypatch, ["--color", "never", "--no-progress", "--plain"])
    assert code == 0
    assert (seen.color, seen.no_progress, seen.plain, seen.verbose, seen.machine) == ("never", True, True, False, False)
    assert capsys.readouterr().out == "name [red]x[/red]\n"          # plain: exactly the text, no escape


def test_a_handler_gets_plain_text_when_stdout_is_not_a_terminal(monkeypatch, capsys):
    _, code = run_probe(monkeypatch, [])
    assert code == 0 and capsys.readouterr().out == "name [red]x[/red]\n"


def test_forced_color_reaches_a_human_handler_only_with_the_extra(monkeypatch, capsys):
    run_probe(monkeypatch, ["--color", "always"])
    out = capsys.readouterr().out
    if present.rich_available():
        assert out.startswith("\x1b[") and ANSI.sub("", out) == "name [red]x[/red]\n"
    else:
        assert out == "name [red]x[/red]\n"


def test_no_color_and_plain_beat_nothing_that_is_explicit(monkeypatch, capsys):
    # NO_COLOR alone: plain. NO_COLOR with --color always: the option is the more specific request.
    run_probe(monkeypatch, [], {"NO_COLOR": "1"})
    assert capsys.readouterr().out == "name [red]x[/red]\n"
    run_probe(monkeypatch, ["--color", "always"], {"NO_COLOR": "1"})
    forced = capsys.readouterr().out
    assert (ANSI.sub("", forced) == "name [red]x[/red]\n") and (("\x1b[" in forced) is present.rich_available())
    run_probe(monkeypatch, ["--plain", "--color", "always"], {"NO_COLOR": "1"})
    assert capsys.readouterr().out == "name [red]x[/red]\n"


# -- real Rich output ------------------------------------------------------------------------------------------

class TestWithRich:
    @pytest.fixture(autouse=True)
    def _rich(self):
        pytest.importorskip("rich")

    def test_the_extra_is_found(self):
        assert present.rich_available() is True

    def test_a_styled_role_wraps_the_text_in_escape_sequences(self):
        text = policy(color="always").style("hello", "critical", Stream())
        assert text.startswith("\x1b[") and text.endswith("\x1b[0m") and ANSI.sub("", text) == "hello"

    def test_every_role_renders_and_text_has_no_style(self):
        decision = policy(color="always")
        for role in present.ROLES:
            styled = decision.style("x", role, Stream())
            assert ANSI.sub("", styled) == "x"
            assert ("\x1b[" in styled) is bool(present.ROLES[role])

    def test_no_escape_without_color(self):
        for decision in (policy(color="never"), policy(plain=True, color="always"), policy(machine=True, color="always"),
                         policy({"NO_COLOR": "1"}), policy()):
            assert decision.style("hello", "critical", Stream(tty=False)) == "hello"

    def test_markup_in_a_name_is_printed_literally(self):
        name = "[bold red]router[/bold red] [link=https://example.invalid]x[/link] :smile: \\[x]"
        styled = policy(color="always").style(name, "subject", Stream())
        assert ANSI.sub("", styled) == name
        assert "\x1b]8" not in styled                                 # no hyperlink was made from the name
        assert policy(color="always").style(name, "text", Stream()) == name

    def test_control_characters_in_a_name_cannot_reach_the_terminal(self):
        hostile = "ap\x1b[2J\x1b]0;owned\x07\x9b31m\u202edcba\r\nnext\x00"
        for role in ("warning", "text"):
            styled = policy(color="always").style(hostile, role, Stream())
            plain = ANSI.sub("", styled)
            assert "\x1b" not in plain and "\x07" not in plain and "\x9b" not in plain and "\r" not in plain
            assert "\u202e" not in plain and "\x00" not in plain and "ap" in plain and "next" in plain
            assert "\n" in plain                                      # line breaks are kept, as ``say`` keeps them

    def test_numbers_and_addresses_are_not_highlighted(self):
        styled = policy(color="always").style("10.0.0.1 aa:bb:cc:dd:ee:ff 42 True None", "text", Stream())
        assert styled == "10.0.0.1 aa:bb:cc:dd:ee:ff 42 True None"

    def test_long_text_is_not_wrapped_or_cut(self):
        long = "word " * 200
        styled = policy({"COLUMNS": "40"}, color="always").style(long, "warning", Stream())
        assert ANSI.sub("", styled) == long and "\n" not in styled

    def test_a_multi_line_text_keeps_its_lines(self):
        styled = policy(color="always").style("one\ntwo", "ok", Stream())
        assert ANSI.sub("", styled) == "one\ntwo"

    def test_an_unknown_role_is_a_programming_error(self):
        with pytest.raises(KeyError):
            policy(color="always").style("x", "sparkly", Stream())

    def test_console_is_literal_and_styles_only_when_color_is_on(self):
        for options, colored in ((dict(color="always"), True), (dict(color="never"), False), (dict(), False)):
            stream = Stream(tty=False)
            console = policy(**options).console(stream)
            assert console is not None
            console.print("[red]10.0.0.1[/red] :smile:", style="bold")
            out = stream.written.getvalue()
            assert ANSI.sub("", out).strip() == "[red]10.0.0.1[/red] :smile:"
            assert ("\x1b[" in out) is colored, options

    def test_a_forced_pipe_console_never_animates(self):
        console = policy(color="always").console(Stream(tty=False))
        assert console is not None and not console.is_interactive
        assert console.is_terminal                          # colors are written, cursor control is not allowed
        assert console.width == present.DEFAULT_WIDTH

    def test_an_interactive_console_may_animate_and_knows_its_width(self):
        console = policy({"TERM": "xterm", "COLUMNS": "100"}).console(Stream(tty=True))
        assert console is not None and console.is_interactive and console.width == 100
        assert console.color_system is not None and not console.no_color

    def test_an_interactive_console_without_color_has_no_color_system(self):
        console = policy({"TERM": "xterm", "COLUMNS": "100"}, color="never").console(Stream(tty=True))
        assert console is not None and console.is_interactive and console.no_color and console.color_system is None

    def test_the_policy_beats_the_environment_variables_rich_reads(self, monkeypatch):
        monkeypatch.setenv("FORCE_COLOR", "1")
        monkeypatch.setenv("TTY_COMPATIBLE", "1")
        monkeypatch.setenv("TTY_INTERACTIVE", "1")
        monkeypatch.delenv("NO_COLOR", raising=False)
        stream = Stream(tty=False)
        console = policy(color="never").console(stream)
        assert console is not None and not console.is_terminal and not console.is_interactive
        console.print("x", style="bold red")
        assert stream.written.getvalue() == "x\n"
        monkeypatch.delenv("FORCE_COLOR")
        monkeypatch.setenv("NO_COLOR", "1")
        forced = Stream(tty=False)
        console = policy(color="always").console(forced)
        assert console is not None
        console.print("x", style="red")
        assert "\x1b[" in forced.written.getvalue()          # --color always beats NO_COLOR, in Rich too

    def test_console_is_none_for_plain_and_for_data(self):
        assert policy(plain=True).console(Stream()) is None and policy(machine=True).console(Stream()) is None

    def test_the_default_stream_is_stdout(self, monkeypatch):
        stream = Stream(tty=False)
        monkeypatch.setattr(sys, "stdout", stream)
        console = policy(color="always").console()
        assert console is not None
        console.print("hi")
        assert "hi" in stream.written.getvalue()
        assert policy(color="always").style("hi", "ok").startswith("\x1b[")


    def test_print_writes_styled_parts_to_the_stream_it_is_given(self):
        stream = Stream(tty=False)
        policy(color="always").print(("WARNING", "warning"), "  ", ("[b]ap[/b]", "subject"), " is down", stream=stream)
        out = stream.written.getvalue()
        assert "\x1b[" in out and ANSI.sub("", out) == "WARNING  [b]ap[/b] is down\n"

    def test_print_without_color_writes_exactly_the_cleaned_text(self):
        stream = Stream(tty=False)
        policy().print(("a\x1b[31mb", "critical"), "c", stream=stream, end="")
        assert stream.written.getvalue() == "a[31mbc"

    def test_say_would_strip_the_escape_so_styled_text_goes_through_print(self, capsys):
        styled = policy(color="always").style("x", "critical", Stream())
        commands.say(styled)
        assert "\x1b" not in capsys.readouterr().out         # the backstop is intact; print() is the styled path

    def test_literal_text_is_never_markup_in_a_console(self):
        stream = Stream(tty=False)
        decision = policy(color="always")
        console = decision.console(stream)
        assert console is not None
        console.print(decision.literal("[red]ap[/red]\x1b[2J"))
        out = stream.written.getvalue()
        assert ANSI.sub("", out) == "[red]ap[/red][2J\n" and "\x1b[2J" not in out


def test_print_defaults_to_stdout(monkeypatch):
    stream = Stream(tty=False)
    monkeypatch.setattr(sys, "stdout", stream)
    policy().print("one", ("two", "ok"))
    assert stream.written.getvalue() == "onetwo\n"


def test_literal_without_the_extra_is_the_cleaned_string(rich_off):
    assert policy().literal("a\x1bb\u202e") == "ab"
    assert policy(color="always").style("[red]x", "critical", Stream()) == "[red]x"


def test_the_missing_extra_is_reported_not_raised(rich_off):
    assert present._load_rich() is None and not present.rich_available()


def test_a_terminal_variable_in_the_env_file_is_flagged_as_doing_nothing(tmp_path):
    from homelab_probe.config import inspect_env_file

    path = tmp_path / "x.env"
    path.write_text("UNIFI_URL=u\nNO_COLOR=1\nTERM=dumb\n", encoding="utf-8")
    report = inspect_env_file(path)
    assert report.misplaced == [(2, "NO_COLOR"), (3, "TERM")] and report.unknown == []
