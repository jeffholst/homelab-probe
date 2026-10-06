"""Transient progress on stderr (issue #234): delay, stages, elapsed time, cleanup, and every way it stays off.

The tests use a fake clock and call ``tick()`` themselves (``threaded=False``) so nothing depends on timing; one test
runs the real thread. Rich is not needed: ``present.rich_available`` is replaced, as in ``test_present.py``.
"""

import argparse
import io
import threading

import pytest

from homelab_probe import cli, commands, logs, present, progress
from homelab_probe import client as client_module
from homelab_probe.client import UniFiClient
from homelab_probe.demo import demo_client, demo_config
from homelab_probe.present import Presentation
from homelab_probe.progress import ERASE_LINE, HIDE_CURSOR, SHOW_CURSOR, Progress

TERM = {"TERM": "xterm-256color"}


class Terminal(io.StringIO):
    """A stderr that is a terminal (or not), of a given encoding."""

    def __init__(self, tty=True, encoding="utf-8"):
        super().__init__()
        self.tty, self._encoding = tty, encoding

    def isatty(self):
        return self.tty

    @property
    def encoding(self):
        return self._encoding


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def rich_is_there(monkeypatch):
    monkeypatch.setattr(present, "rich_available", lambda: True)
    monkeypatch.setattr(progress, "current", None)
    monkeypatch.setattr(logs, "STDERR_GUARD", logs.STDERR_GUARD)


def make(stream=None, environ=None, columns="80", **options):
    stream = Terminal() if stream is None else stream
    env = {**TERM, "COLUMNS": columns, **(environ or {})}
    policy = Presentation(environ=env, color=options.pop("color", "never"),
                          **{k: options.pop(k) for k in ("plain", "no_progress", "verbose", "machine") if k in options})
    clock = Clock()
    return Progress(policy, stream=stream, clock=clock, threaded=False, **options), stream, clock


# -- the elapsed time -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("seconds, text", [(0, "0.0s"), (0.04, "0.0s"), (3.24, "3.2s"), (9.96, "10.0s"), (10, "10s"),
                                           (59.9, "59s"), (60, "1m 00s"), (65, "1m 05s"), (3725, "62m 05s"),
                                           (-1, "0.0s")])
def test_the_elapsed_time_is_short_and_has_one_form_per_range(seconds, text):
    assert progress.format_elapsed(seconds) == text


# -- nothing for a quick command ------------------------------------------------------------------------------------

def test_a_command_that_ends_before_the_delay_writes_nothing_at_all():
    shown, stream, clock = make()
    with shown:
        shown.stage("Reading devices")
        clock.now += 0.19
        shown.tick()
    assert stream.getvalue() == ""                       # no spinner, no cursor control, no erase


def test_after_the_delay_one_line_is_drawn_with_the_stage_the_spinner_and_the_elapsed_time():
    shown, stream, clock = make()
    with shown:
        shown.stage("Reading devices")
        clock.now += 0.2
        shown.tick()
        assert stream.getvalue() == HIDE_CURSOR + ERASE_LINE + "⠋ Reading devices 0.2s"
        clock.now += 3.0
        shown.tick()
        assert stream.getvalue().endswith(ERASE_LINE + "⠙ Reading devices 3.2s")
        assert stream.getvalue().count(HIDE_CURSOR) == 1       # hidden once, not at every redraw


def test_with_color_on_only_the_spinner_and_the_time_are_styled_and_the_words_are_plain():
    pytest.importorskip("rich")                  # the styling itself is Rich's; the base install skips this one
    shown, stream, clock = make(color="always")
    with shown:
        clock.now += 0.2
        shown.stage("Reading devices")
        shown.tick()
    assert "\x1b[1;36m⠋\x1b[0m Reading devices \x1b[2m0.2s\x1b[0m" in stream.getvalue()


def test_the_stage_changes_with_the_work_and_a_note_belongs_to_one_stage():
    shown, stream, clock = make()
    with shown:
        clock.now += 1
        shown.stage("Reading devices")
        shown.note("retrying, attempt 2 of 3")
        shown.tick()
        assert stream.getvalue().endswith("Reading devices (retrying, attempt 2 of 3) 1.0s")
        shown.stage("Reading clients")
        shown.tick()
        assert stream.getvalue().endswith("Reading clients 1.0s")


def test_stages_come_from_the_threads_of_a_parallel_read_without_trouble():
    shown, stream, clock = make()
    with shown:
        clock.now += 1
        workers = [threading.Thread(target=lambda n=n: [shown.stage(f"Reading {n}") for _ in range(200)])
                   for n in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        shown.tick()
    assert "Reading " in stream.getvalue()


# -- one line that fits ---------------------------------------------------------------------------------------------

def test_the_line_never_reaches_the_last_column_and_only_the_label_is_shortened():
    shown, stream, clock = make(columns="40")
    with shown:
        clock.now += 12.3
        shown.stage("Reading the details of every device that the controller lists for this site")
        shown.tick()
    line = stream.getvalue().split(ERASE_LINE)[1]
    assert len(line) <= 39 and line.startswith("⠋ Reading the details") and line.endswith("… 12s")


def test_a_terminal_that_is_not_utf_8_gets_an_ascii_spinner_and_an_ascii_ellipsis():
    shown, stream, clock = make(Terminal(encoding="latin-1"), columns="40")
    with shown:
        clock.now += 1
        shown.stage("Reading the details of every device that the controller lists for this site")
        shown.tick()
        shown.tick()
    lines = stream.getvalue().split(ERASE_LINE)[1:]
    assert lines[0].startswith("| ") and lines[1].startswith("/ ") and "..." in lines[0]
    assert stream.getvalue().isascii() or "\x1b" in stream.getvalue()
    assert all(ch.isascii() for ch in "".join(lines))


# -- cleanup --------------------------------------------------------------------------------------------------------

def test_stopping_erases_the_line_shows_the_cursor_and_can_be_repeated():
    shown, stream, clock = make()
    shown.start()
    clock.now += 1
    shown.stage("Reading devices")
    shown.tick()
    shown.stop()
    assert stream.getvalue().endswith(ERASE_LINE + SHOW_CURSOR)
    size = len(stream.getvalue())
    shown.stop()
    shown.tick()
    assert len(stream.getvalue()) == size and progress.current is None


@pytest.mark.parametrize("error", [RuntimeError("boom"), KeyboardInterrupt()])
def test_an_error_or_ctrl_c_restores_the_cursor_and_the_error_goes_on(error):
    shown, stream, clock = make()
    with pytest.raises(type(error)):
        with shown:
            clock.now += 1
            shown.tick()
            raise error
    assert stream.getvalue().endswith(ERASE_LINE + SHOW_CURSOR)


def test_it_registers_for_exit_and_unregisters_when_stopped(monkeypatch):
    calls = []
    monkeypatch.setattr(progress.atexit, "register", lambda function: calls.append(("register", function)))
    monkeypatch.setattr(progress.atexit, "unregister", lambda function: calls.append(("unregister", function)))
    shown, _, _ = make()
    shown.start()
    shown.start()                                    # a second start changes nothing
    shown.stop()
    assert [name for name, _ in calls] == ["register", "unregister"]


def test_a_broken_terminal_turns_progress_off_and_never_raises():
    class Broken(Terminal):
        def write(self, text):
            raise OSError("gone")

    shown, stream, clock = make(Broken())
    with shown:
        clock.now += 1
        shown.tick()
        assert shown.enabled is False
        shown.tick()


# -- coordination with everything else on the terminal --------------------------------------------------------------

def test_a_warning_is_written_on_a_clean_line_and_the_spinner_comes_back_after_it():
    shown, stream, clock = make()
    with shown:
        clock.now += 1
        shown.tick()
        logs.reset()
        logs.configure("cli", "WARNING")
        import sys
        saved, sys.stderr = sys.stderr, stream
        try:
            logs.warn("something degraded")
        finally:
            sys.stderr = saved
        text = stream.getvalue()
        assert ERASE_LINE + "Warning: something degraded\n" in text
        shown.tick()
        assert stream.getvalue().endswith("0.0s") or "1.0s" in stream.getvalue()
    logs.reset()


def test_the_guard_is_installed_while_progress_runs_and_put_back_after():
    before = logs.STDERR_GUARD
    shown, _, _ = make()
    with shown:
        assert logs.STDERR_GUARD == shown.hold and progress.current is shown
    assert logs.STDERR_GUARD == before and progress.current is None


def test_hold_erases_the_line_and_the_renderer_waits_for_it():
    shown, stream, clock = make()
    with shown:
        clock.now += 1
        shown.tick()
        with shown.hold():
            assert stream.getvalue().endswith(ERASE_LINE)
        shown.tick()
        assert stream.getvalue().endswith("1.0s")


def test_the_first_output_of_the_command_ends_progress_for_good(capsys):
    shown, stream, clock = make()
    shown.start()
    clock.now += 1
    shown.tick()
    commands.say("the result")
    assert stream.getvalue().endswith(ERASE_LINE + SHOW_CURSOR) and progress.current is None
    size = len(stream.getvalue())
    shown.tick()
    assert len(stream.getvalue()) == size
    assert capsys.readouterr().out == "the result\n"


def test_presentation_print_ends_progress_too(capsys):
    shown, stream, clock = make()
    shown.start()
    clock.now += 1
    shown.tick()
    Presentation(environ=TERM).print("a line")
    assert stream.getvalue().endswith(SHOW_CURSOR) and progress.current is None


def test_finish_with_nothing_running_does_nothing():
    progress.finish()
    assert progress.current is None


# -- the real thread ------------------------------------------------------------------------------------------------

def test_the_thread_redraws_by_itself_and_stops_when_asked():
    drawn = threading.Event()

    class Signalling(Terminal):
        def write(self, text):
            result = super().write(text)
            if "Reading devices" in text:
                drawn.set()
            return result

    stream = Signalling()
    shown = Progress(Presentation(environ={**TERM, "COLUMNS": "80"}), stream=stream, delay=0.0, interval=0.01)
    with shown:
        shown.stage("Reading devices")
        assert drawn.wait(5)
    assert stream.getvalue().endswith(ERASE_LINE + SHOW_CURSOR)
    assert not any(t.name == "hlp-progress" and t.is_alive() for t in threading.enumerate())


# -- every way it stays off -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("options", [
    {"stream": Terminal(tty=False)}, {"plain": True}, {"no_progress": True}, {"verbose": True}, {"machine": True},
    {"environ": {"CI": "true"}}, {"environ": {"TERM": "dumb"}}, {"columns": "39"}, {"enabled": False}])
def test_it_writes_nothing_where_progress_is_not_welcome(options):
    stream = options.pop("stream", Terminal())
    shown, stream, clock = make(stream, **options)
    with shown:
        shown.stage("Reading devices")
        clock.now += 60
        shown.tick()
    assert stream.getvalue() == "" and shown.enabled is False and progress.current is None
    assert logs.STDERR_GUARD != shown.hold


@pytest.mark.parametrize("args, expected", [
    (argparse.Namespace(command="diagnose"), True), (argparse.Namespace(command="diagnose", watch=60), False),
    (argparse.Namespace(command="diagnose", notify=True), False), (argparse.Namespace(command="wifi"), True),
    (argparse.Namespace(command="serve"), False), (argparse.Namespace(command="export"), False),
    (argparse.Namespace(command="doctor"), False), (argparse.Namespace(command="web-user"), False),
    (argparse.Namespace(command="completion"), False), (argparse.Namespace(command="init"), False)])
def test_only_the_commands_that_read_the_controller_for_a_person_show_progress(args, expected):
    assert progress.eligible(args) is expected


def test_every_eligible_command_exists():
    assert progress.ELIGIBLE <= set(commands.COMMANDS_BY_NAME)


# -- the work says where it is --------------------------------------------------------------------------------------

def test_the_client_has_silent_stage_and_note_hooks_until_someone_listens():
    client = UniFiClient("https://c.example", "k" * 20)
    client.stage("anything")
    client.note("anything")
    assert client.stage is client_module._ignore


def test_a_retry_adds_a_note_with_the_attempt_and_never_the_url(monkeypatch):
    client = UniFiClient("https://c.example", "k" * 20)
    notes = []
    client.note = notes.append
    client._sleep = lambda seconds: None
    client._back_off("GET /proxy/network/integration/v1/sites", 1, 3)
    assert notes == ["retrying, attempt 2 of 3"]


def snapshot_stages(needs):
    from homelab_probe.snapshot import collect_snapshot
    client = demo_client(demo_config("default"))
    stages = []
    client.stage = stages.append
    collect_snapshot(client, "default", needs)
    return stages


def test_a_collection_names_its_real_boundaries_in_order():
    from homelab_probe.snapshot import Needs
    assert snapshot_stages(Needs()) [:3] == ["Connecting to the controller", "Reading devices", "Reading clients"]
    assert snapshot_stages(Needs())[-2:] == ["Reading network details", "Reading device details"] or \
        "Reading network details" in snapshot_stages(Needs())


def test_the_documents_name_their_own_stage_after_the_read():
    from homelab_probe import documents
    client = demo_client(demo_config("default"))
    stages = []
    client.stage = stages.append
    documents.diagnose_document(client, "default", echo=False)
    assert stages[-1] == "Running the checks"
    stages.clear()
    documents.wifi_document(client, "default", echo=False)
    assert stages[-1] == "Checking Wi-Fi"
    stages.clear()
    documents.snapshot_document(client, "default", echo=False)
    assert stages[-1] == "Preparing the snapshot"


def test_the_event_command_names_its_read():
    from homelab_probe.snapshot import EventQuery, collect_event_snapshot
    client = demo_client(demo_config("default"))
    stages = []
    client.stage = stages.append
    collect_event_snapshot(client, "default", EventQuery(since_seconds=3600))
    assert stages == ["Connecting to the controller", "Reading the event log"]


# -- the whole command line ---------------------------------------------------------------------------------------------

def run(argv, stderr, monkeypatch, capsys):
    monkeypatch.setattr("sys.stderr", stderr)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("CI", raising=False)
    code = cli.main(argv)
    return code, capsys.readouterr().out


def test_a_real_command_on_a_terminal_draws_progress_on_stderr_only_and_leaves_stdout_alone(monkeypatch, capsys):
    monkeypatch.setattr(progress, "DELAY", 0.0)
    original = Progress.stage

    def stage_and_draw(self, label):                 # draw at every stage, so the test does not depend on the thread
        original(self, label)
        self.tick()

    monkeypatch.setattr(Progress, "stage", stage_and_draw)
    terminal = Terminal()
    code, shown = run(["--demo", "wifi"], terminal, monkeypatch, capsys)
    code_plain, plain = run(["--demo", "--plain", "wifi"], Terminal(), monkeypatch, capsys)
    assert code == code_plain == 0 and shown and shown.startswith(plain.split("\n")[0][:10])
    text = terminal.getvalue()
    assert HIDE_CURSOR in text and "Connecting to the controller" in text and "Checking Wi-Fi" in text
    assert text.rstrip().endswith(SHOW_CURSOR) or text.endswith(ERASE_LINE + SHOW_CURSOR)
    assert "\x1b" not in shown                         # nothing of it reached stdout


def test_json_output_and_a_redirected_stderr_never_carry_an_escape(monkeypatch, capsys):
    monkeypatch.setattr(progress, "DELAY", 0.0)
    redirected = Terminal(tty=False)
    code, out = run(["--demo", "wifi", "--json"], redirected, monkeypatch, capsys)
    assert code == 0 and out.lstrip().startswith("{") and "\x1b" not in out
    assert "\x1b" not in redirected.getvalue()
    on_a_terminal = Terminal()
    code, out = run(["--demo", "wifi", "--json"], on_a_terminal, monkeypatch, capsys)
    assert code == 0 and "\x1b" not in out and HIDE_CURSOR not in on_a_terminal.getvalue()   # machine output: no progress


def test_an_error_on_a_terminal_clears_the_line_before_the_message(monkeypatch, capsys):
    monkeypatch.setattr(progress, "DELAY", 0.0)
    original = Progress.stage
    monkeypatch.setattr(Progress, "stage", lambda self, label: (original(self, label), self.tick()))
    terminal = Terminal()
    monkeypatch.setattr("sys.stderr", terminal)
    monkeypatch.setenv("TERM", "xterm-256color")

    def failing(self, site):
        self.stage("Connecting to the controller")
        raise client_module.UniFiAPIError("the controller said no", kind="http")

    monkeypatch.setattr(UniFiClient, "resolve_site", failing)
    monkeypatch.setattr("homelab_probe.demo.demo_client", lambda config: UniFiClient("https://c.example", "k" * 20))
    monkeypatch.setattr(cli, "demo_client", lambda config: UniFiClient("https://c.example", "k" * 20))
    assert cli.main(["--demo", "wifi"]) == 3
    text = terminal.getvalue()
    assert ERASE_LINE + SHOW_CURSOR + "ERROR: the controller said no\n" in text and text.endswith("\n")


def test_a_progress_that_was_replaced_does_not_undo_the_one_that_replaced_it():
    first, _, _ = make()
    second, _, _ = make()
    first.start()
    second.start()
    first.stop()
    assert progress.current is second and logs.STDERR_GUARD == second.hold
    second.stop()
    assert progress.current is None


def test_the_command_line_asks_for_progress_only_for_the_eligible_commands(monkeypatch, capsys):
    made = []

    class Spy(Progress):
        def __init__(self, present_, enabled=True, **kwargs):
            made.append(enabled)
            super().__init__(present_, enabled=enabled, **kwargs)

    monkeypatch.setattr(cli, "Progress", Spy)
    assert cli.main(["--demo", "wifi"]) == 0
    assert cli.main(["--demo", "export", "--output-dir", "out"]) == 0
    assert made == [True, False]
