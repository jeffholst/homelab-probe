"""Enhanced terminal output (issue #235): grouped, colored findings with an honest summary, and tables that fit.

The renderers write through ``Presentation.print`` to stdout, so the tests read ``capsys``. Content is checked with
color off (``--color never``); the roles are checked with color forced on (that needs Rich, so those tests skip on the
base install). Handlers choose these renderers only when ``Presentation.decorations(sys.stdout)`` is true, which the
wiring tests force.
"""

import json
import re

import pytest

from homelab_probe import cli, present, pretty
from homelab_probe.present import Presentation

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def policy(columns=100, color="never", **environ):
    return Presentation(color=color, environ={"TERM": "xterm-256color", "COLUMNS": str(columns), **environ})


def finding(severity, subject, message, code="device.offline"):
    return {"severity": severity, "subject": subject, "message": message, "code": code, "mac": ""}


def document(findings=(), ignored=0, areas=None, ignored_rows=None):
    summary = {sev: sum(f["severity"] == sev for f in findings) for sev in ("critical", "warning", "info")}
    doc = {"version": 1, "areas": areas or ["devices", "health"], "summary": {**summary, "ignored": ignored},
           "findings": list(findings)}
    if ignored_rows is not None:
        doc["ignored"] = ignored_rows
    return doc


@pytest.fixture(autouse=True)
def rich_is_there(monkeypatch):
    monkeypatch.setattr(present, "rich_available", lambda: True)


# -- findings -----------------------------------------------------------------------------------------------------------

def test_findings_are_grouped_most_severe_first_and_each_keeps_its_written_label_and_code(capsys):
    doc = document([finding("info", "wlan", "a note", "health.device_subsystem"),
                    finding("warning", "Garage AP", "device is offline"),
                    finding("critical", "Gateway", "reports that it is overheating", "device.overheating"),
                    finding("warning", "Office Switch", "CPU utilization 95%", "device.cpu_high")])
    pretty.print_findings(policy(), doc, elapsed=3.24)
    out = capsys.readouterr().out
    assert out.index("Critical (1)") < out.index("Warnings (2)") < out.index("Info (1)")
    assert re.search(r"CRITICAL  Gateway  device\.overheating\n    [│|] reports that it is overheating", out)
    assert "WARNING  Garage AP  device.offline" in out and "INFO  wlan  health.device_subsystem" in out
    assert out.rstrip().splitlines()[-1] in ("1 critical, 2 warnings, 1 info · 3.2s", "1 critical, 2 warnings, 1 info - 3.2s")


def test_every_finding_is_shown_even_hundreds(capsys):
    many = [finding("warning", f"Switch {i}", f"problem {i}") for i in range(300)]
    pretty.print_findings(policy(), document(many))
    out = capsys.readouterr().out
    assert all(re.search(rf"Switch {i}  device\.offline\n    [│|] problem {i}\n", out) for i in range(300))
    assert "Warnings (300)" in out and "300 warnings" in out


def test_a_long_message_wraps_under_its_finding_and_loses_no_word(capsys):
    words = " ".join(f"word{i}" for i in range(60)) + " 2001:db8:0:0:0:0:0:1234567890abcdef"
    pretty.print_findings(policy(columns=40), document([finding("warning", "x", words)]))
    lines = capsys.readouterr().out.splitlines()
    body = [line for line in lines if line.startswith("    ")]
    assert len(body) > 3 and all(len(line) <= 40 or " " not in line[6:].strip() for line in body)
    assert " ".join(line[6:].strip() for line in body) == words                 # an address is never broken
    assert all(line[4] in "│|" for line in body)                                # every line behind the bar


def test_names_are_literal_and_cleaned(capsys):
    pretty.print_findings(policy(), document([finding("warning", "[red]x[/red]\x1b[2J", "bad\x07 name [bold]")]))
    out = capsys.readouterr().out
    assert "[red]x[/red][2J" in out and "bad name [bold]" in out and "\x1b" not in out and "\x07" not in out


def test_no_issues_is_green_only_when_every_check_could_read_what_it_needed(capsys):
    pretty.print_findings(policy(), document(), complete=True)
    assert capsys.readouterr().out.strip() == "ok No issues found"
    pretty.print_findings(policy(), document(), complete=False)
    out = capsys.readouterr().out
    assert "No issues found in what could be read, but the read was incomplete" in out and "ok " not in out


def test_findings_from_an_incomplete_read_say_there_may_be_more(capsys):
    pretty.print_findings(policy(), document([finding("warning", "x", "y")]), complete=False)
    assert "The read was incomplete: some checks could not see everything" in capsys.readouterr().out
    pretty.print_findings(policy(), document([finding("warning", "x", "y")]), complete=True)
    assert "incomplete" not in capsys.readouterr().out


def test_ignored_findings_are_counted_and_listed_when_asked(capsys):
    rows = [{**finding("warning", "Old AP", "device is offline"), "reason": "retired", "until": "2030-01-01"},
            {**finding("info", "y", "z", code=""), "reason": ""}]
    pretty.print_findings(policy(), document([finding("info", "a", "b")], ignored=2, ignored_rows=rows),
                          show_ignored=True)
    out = capsys.readouterr().out
    assert re.search("1 info [·-] 2 ignored", out) and "Ignored (2)" in out and re.search(r"    [│|] ignored\n", out)
    assert "Old AP: device is offline" in out and "device.offline ignored until 2030-01-01: retired" in out
    pretty.print_findings(policy(), document([], ignored=2, ignored_rows=rows), show_ignored=False)
    assert "Ignored (" not in capsys.readouterr().out


def test_the_checked_areas_are_reported_only_from_the_document(capsys):
    pretty.print_findings(policy(), document(areas=["devices"]), show_checked=True)
    out = capsys.readouterr().out
    assert "Checked: devices  not checked: health, wan, clients, reservations, ports, wifi, events" in out
    from homelab_probe.diagnose.areas import AREA_NAMES
    pretty.print_findings(policy(), document(areas=list(AREA_NAMES)), show_checked=True)
    out = capsys.readouterr().out
    assert "Checked: " in out and "not checked" not in out


def test_a_utf_8_terminal_gets_symbols_and_a_middle_dot(capsys, monkeypatch):
    class Utf8:
        encoding = "utf-8"

        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdout", Utf8())
    on = policy()
    assert on.unicode_ok() and on.symbol("critical") == "✗"


@pytest.mark.parametrize("seconds, text", [(0.04, "0.0s"), (3.25, "3.2s"), (12, "12s"), (65, "1m 05s"), (-1, "0.0s")])
def test_the_elapsed_time_is_short(seconds, text):
    assert pretty.elapsed_text(seconds) == text


def test_colors_follow_the_roles_and_never_carry_meaning_alone(capsys):
    pytest.importorskip("rich")
    pretty.print_findings(policy(color="always"),
                          document([finding("critical", "Gateway", "hot"), finding("warning", "AP", "down")]))
    out = capsys.readouterr().out
    assert "\x1b[1;31mCRITICAL\x1b[0m" in out and "\x1b[33mWARNING\x1b[0m" in out and "\x1b[1;36mCritical (1)" in out
    assert "\x1b[2mdevice.offline\x1b[0m" in out and "\x1b[1mGateway\x1b[0m" in out
    plain = ANSI.sub("", out)
    assert "CRITICAL" in plain and "WARNING" in plain                       # the words are there without color


# -- tables -------------------------------------------------------------------------------------------------------------

ROWS = [{"Name": "Gateway", "MAC Address": "AA:00:00:00:00:01", "Status": "Online", "Port": ""},
        {"Name": "Garage AP", "MAC Address": "AA:00:00:00:00:04", "Status": "Offline", "Port": "5"}]
COLUMNS = ["Name", "MAC Address", "Status", "Port"]


def test_a_table_that_fits_is_aligned_with_its_header(capsys):
    pretty.print_table(policy(columns=100), ROWS, COLUMNS, "2 row(s)")
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["Name", "MAC", "Address", "Status", "Port"]
    assert lines[1].startswith("Gateway    AA:00:00:00:00:01  Online") and lines[-1] == "2 row(s)"
    assert lines[1].index("AA:") == lines[2].index("AA:")


def test_a_table_too_wide_turns_into_stacked_rows_and_cuts_nothing(capsys):
    pretty.print_table(policy(columns=40), ROWS, COLUMNS, "2 row(s)")
    out = capsys.readouterr().out
    assert "Name         Gateway\nMAC Address  AA:00:00:00:00:01\nStatus       Online\n\nName         Garage AP" in out
    assert "Port         5" in out and out.count("Port") == 1                    # an empty cell is not printed
    assert out.rstrip().endswith("2 row(s)")


@pytest.mark.parametrize("columns, aligned", [(40, True), (39, False)])
def test_a_table_that_exactly_fits_stays_aligned(capsys, columns, aligned):
    rows = [{"Name": "x" * 32, "Status": "Online"}]
    pretty.print_table(policy(columns=columns), rows, ["Name", "Status"], "1 row(s)")
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == ("Name" + " " * 30 + "Status" if aligned else "Name    " + "x" * 32)


def test_wide_cells_force_a_table_to_stack_when_character_counts_would_fit(capsys):
    pytest.importorskip("rich")
    name = "\u754c" * 20
    pretty.print_table(policy(columns=40), [{"Name": name, "Status": "Online"}], ["Name", "Status"], "1 row(s)")
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Name    " + name and lines[1] == "Status  Online"


@pytest.mark.parametrize("name", ["\u754c\u754c", "e\u0301", "\U0001f600"])
def test_table_padding_uses_display_cells_for_wide_and_combining_text(capsys, name):
    pytest.importorskip("rich")
    from rich.cells import cell_len

    rows = [{"Name": name, "Status": "Online"}, {"Name": "printer", "Status": "Offline"}]
    pretty.print_table(policy(), rows, ["Name", "Status"], "2 row(s)")
    lines = capsys.readouterr().out.splitlines()
    assert cell_len(lines[0][:lines[0].index("Status")]) == 9
    assert cell_len(lines[1][:lines[1].index("Online")]) == 9
    assert cell_len(lines[2][:lines[2].index("Offline")]) == 9


def test_stacked_unicode_column_labels_are_padded_by_display_cells(capsys):
    pytest.importorskip("rich")
    columns = ["\u754c\u754c", "e\u0301"]
    pretty.print_table(policy(columns=40), [{columns[0]: "x" * 40, columns[1]: "y"}], columns, "1 row(s)")
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == columns[0] + "  " + "x" * 40
    assert lines[1] == columns[1] + " " * 5 + "y"


@pytest.mark.parametrize("name, aligned", [("printer", True), ("\u754c", False)])
def test_missing_rich_uses_ascii_widths_and_conservatively_stacks_unicode(capsys, monkeypatch, name, aligned):
    import builtins

    real_import = builtins.__import__

    def without_cells(module, *args, **kwargs):
        if module == "rich.cells":
            raise ImportError("optional extra is absent")
        return real_import(module, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_cells)
    pretty.print_table(policy(), [{"Name": name, "Status": "Online"}], ["Name", "Status"], "1 row(s)")
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == ("Name     Status" if aligned else "Name    " + name)


def test_table_cells_are_literal_and_cleaned(capsys):
    rows = [{"Name": "[red]x[/red]\x1b]0;evil\x07", "MAC Address": "AA:00:00:00:00:09", "Status": "Online", "Port": ""}]
    pretty.print_table(policy(), rows, COLUMNS, "1 row(s)")
    out = capsys.readouterr().out
    assert "[red]x[/red]" in out and "\x1b" not in out and "\x07" not in out


def test_states_are_colored_with_their_word_kept(capsys):
    pytest.importorskip("rich")
    pretty.print_table(policy(color="always"), ROWS, COLUMNS, "2 row(s)")
    out = capsys.readouterr().out
    assert "\x1b[32mOnline" in out and "\x1b[33mOffline" in out and "\x1b[1;36mName" in out
    pretty.print_table(policy(color="always", columns=40), ROWS, COLUMNS, "2 row(s)")
    stacked = capsys.readouterr().out
    assert "\x1b[33mOffline" in stacked and "\x1b[2mName" in stacked


@pytest.mark.parametrize("value, role", [("Online", "ok"), ("UP", "ok"), ("offline", "warning"), ("Down", "warning"),
                                          ("", "text"), ("Pending", "text")])
def test_state_words_map_to_roles(value, role):
    assert pretty._state_role(value) == role


# -- the commands -------------------------------------------------------------------------------------------------------

@pytest.fixture
def terminal(monkeypatch):
    """Make the handlers believe stdout is a decorated terminal; the output still goes to capsys."""
    monkeypatch.setattr(Presentation, "decorations", lambda self, stream=None: not self.plain and not self.machine)
    monkeypatch.setenv("COLUMNS", "120")


def test_diagnose_on_a_terminal_is_grouped_and_summarised_and_its_exit_code_is_unchanged(terminal, capsys):
    code = cli.main(["--demo", "diagnose"])
    out = capsys.readouterr().out
    assert "Critical (1)" in out and "Warnings (" in out and re.search(r"\d+ critical, \d+ warnings, \d+ info", out)
    plain_code = cli.main(["--demo", "--plain", "diagnose"])
    plain = capsys.readouterr().out
    assert code == plain_code == 2 and "Critical (1)" not in plain and "[CRITICAL]" in plain


def test_diagnose_json_and_plain_are_untouched_on_a_terminal(terminal, capsys):
    cli.main(["--demo", "diagnose", "--json"])
    assert json.loads(capsys.readouterr().out)["version"] == 1
    cli.main(["--demo", "diagnose", "--only", "devices"])
    assert "Checked: devices  not checked:" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["diagnose", "audit"])
def test_no_emoji_keeps_the_plain_findings_renderer_on_a_utf8_terminal(terminal, capsys, monkeypatch, command):
    from homelab_probe import commands

    monkeypatch.setattr(commands, "stream_supports_emoji", lambda stream: True)
    cli.main(["--demo", command, "--no-emoji"])
    out = capsys.readouterr().out
    assert re.search(r"\[(?:CRITICAL|WARNING|INFO)\s*\]", out)
    assert not any(symbol in out for symbol in ("\u2717", "\u26a0", "\u2022", "\U0001f6d1"))
    assert not re.search(r"^(Critical|Warnings|Info) \(", out, re.M)


def test_watch_keeps_the_plain_initial_output_on_a_decorated_terminal(terminal, capsys, monkeypatch):
    from homelab_probe import commands

    def stop(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(commands, "WATCH_SLEEP", stop)
    plain_code = cli.main(["--demo", "--plain", "diagnose", "--only", "devices"])
    expected = capsys.readouterr().out
    code = cli.main(["--demo", "diagnose", "--only", "devices", "--watch", "30"])
    captured = capsys.readouterr()
    assert code == plain_code and captured.out == expected
    assert "Watching every 30 s; only changes are printed" in captured.err and "Stopped." in captured.err


def test_audit_query_and_new_clients_use_the_enhanced_views_on_a_terminal(terminal, capsys):
    assert cli.main(["--demo", "audit"]) in (0, 1)
    assert re.search(r"(Warnings|Info) \(\d+\)", capsys.readouterr().out)
    assert cli.main(["--demo", "query", "devices"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[2].startswith("Name") and "----" not in out and out.rstrip().endswith("row(s)")
    assert cli.main(["--demo", "new-clients"]) == 0
    assert capsys.readouterr().out.rstrip().endswith("first seen in the last 7d (1 with a private MAC)")
    assert cli.main(["--demo", "query", "devices", "--csv"]) == 0
    assert capsys.readouterr().out.startswith("Type,Name,")
    assert cli.main(["--demo", "new-clients", "--json"]) == 0
    assert capsys.readouterr().out.lstrip().startswith("[")


def test_audit_says_its_read_was_incomplete_when_a_read_warned(terminal, capsys, monkeypatch):
    import dataclasses

    from homelab_probe import commands
    real = commands.audit_document

    def with_a_warning(*args, **kwargs):
        return dataclasses.replace(real(*args, **kwargs), warnings=["an optional read failed"])

    cli.main(["--demo", "audit"])
    assert "incomplete" not in capsys.readouterr().out
    monkeypatch.setattr(commands, "audit_document", with_a_warning)
    cli.main(["--demo", "audit"])
    assert "The read was incomplete" in capsys.readouterr().out
