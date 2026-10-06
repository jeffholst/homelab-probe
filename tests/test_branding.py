"""Compact CLI identity: literal resolved sites, bounded headings, and no decoration in script output."""

import argparse
import dataclasses
from pathlib import Path

import pytest

from homelab_probe import cli, commands, present, pretty
from homelab_probe.present import Presentation


@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.setattr(present, "rich_available", lambda: True)
    monkeypatch.setattr(present, "_is_tty", lambda stream: True)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setenv("COLUMNS", "80")


@pytest.mark.parametrize("command,title", [("diagnose", "Diagnose"), ("audit", "Audit"),
                                           ("query", "Query"), ("new-clients", "New Clients")])
def test_selected_commands_print_one_resolved_site_heading(terminal, monkeypatch, capsys, command, title):
    builder = getattr(commands, command.replace("-", "_") + "_document")

    def resolved(*args, **kwargs):
        doc = builder(*args, **kwargs)
        return dataclasses.replace(doc, meta={**doc.meta, "site": {"ref": "resolved-site", "name": "Other name"}})

    monkeypatch.setattr(commands, command.replace("-", "_") + "_document", resolved)
    cli.main(["--demo", "--color", "never", command])
    out = capsys.readouterr().out
    assert out.startswith(f"Homelab Probe | {title} | resolved-site\n\n")
    assert out.count("Homelab Probe") == 1 and "Other name" not in out


@pytest.mark.parametrize("site,suffix", [({"ref": "default", "name": "Home"}, " | default"),
                                        ({"name": "Home"}, " | Home"), ({"id": "site-id"}, " | site-id"),
                                        ({}, ""), ({"ref": "[red]Lab[/red]\n\x07"}, " | [red]Lab[/red]")])
def test_heading_snapshot_and_safe_identity(terminal, capsys, site, suffix):
    pretty.print_heading(Presentation(color="never"), "Diagnose", site)
    assert capsys.readouterr().out == f"Homelab Probe | Diagnose{suffix}\n\n"


@pytest.mark.parametrize("rich", [True, False])
@pytest.mark.parametrize("width,site", [(40, "A" * 128), (40, "Home lab " * 20),
                                      (80, "Lab"), (40, "\u7db2\u7d61" * 40)])
def test_long_headings_wrap_without_losing_site_text(terminal, monkeypatch, capsys, rich, width, site):
    policy = Presentation(color="never", environ={"TERM": "xterm", "COLUMNS": str(width)})
    if not rich:
        monkeypatch.setattr(present, "_load_rich", lambda: None)
    else:
        pytest.importorskip("rich")
    pretty.print_heading(policy, "Diagnose", {"name": site})
    lines = capsys.readouterr().out.strip().splitlines()
    assert "".join("".join(lines).split()) == "".join(f"Homelab Probe | Diagnose | {site}".split())
    if rich:
        from rich.cells import cell_len
        assert all(cell_len(line) <= width for line in lines)
    else:
        assert all(len(line) <= width for line in lines)


@pytest.mark.parametrize("mode", ["plain", "json", "csv", "redirect", "narrow", "dumb", "ci", "base"])
def test_no_heading_in_disabled_modes(terminal, monkeypatch, capsys, mode):
    argv = ["--demo", "--color", "always"]
    if mode == "plain":
        argv += ["--plain"]
    if mode == "redirect":
        monkeypatch.setattr(present, "_is_tty", lambda stream: False)
    if mode == "narrow":
        monkeypatch.setenv("COLUMNS", "39")
    if mode == "dumb":
        monkeypatch.setenv("TERM", "dumb")
    if mode == "ci":
        monkeypatch.setenv("CI", "true")
    if mode == "base":
        monkeypatch.setattr(present, "rich_available", lambda: False)
    argv += ["query", "devices"]
    if mode in ("json", "csv"):
        argv += ["--" + mode]
    cli.main(argv)
    assert "Homelab Probe" not in capsys.readouterr().out


@pytest.mark.parametrize("policy", [Presentation(plain=True), Presentation(machine=True)])
def test_heading_itself_obeys_policy(terminal, capsys, policy):
    pretty.print_heading(policy, "Diagnose", {"ref": "default"})
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("background", ["0;15", "15;0"])
def test_heading_uses_standard_theme_colors_and_keeps_written_identity(terminal, capsys, background):
    pytest.importorskip("rich")
    policy = Presentation(color="always", environ={"TERM": "xterm", "COLORFGBG": background})
    pretty.print_heading(policy, "Query", {"ref": "default"})
    assert capsys.readouterr().out == "\x1b[1;36mHomelab Probe | Query | default\x1b[0m\n\n"


def test_no_color_and_no_progress_keep_the_heading(terminal, monkeypatch, capsys):
    monkeypatch.setenv("NO_COLOR", "1")
    cli.main(["--demo", "--no-progress", "query", "devices"])
    out = capsys.readouterr().out
    assert out.startswith("Homelab Probe | Query | default\n\n") and "\x1b" not in out


@pytest.mark.parametrize("command", ["diagnose", "audit", "query", "new-clients"])
def test_selected_site_name_is_resolved_to_its_reference(terminal, capsys, command):
    cli.main(["--demo", "--color", "never", "--site", "Default", command])
    out = capsys.readouterr().out
    assert out.splitlines()[0].endswith(" | default") and " | Default" not in out


def test_a_failed_read_does_not_print_a_heading(terminal, monkeypatch, capsys):
    from homelab_probe.client import UniFiAPIError

    def failed(*args, **kwargs):
        raise UniFiAPIError("site could not be resolved")

    monkeypatch.setattr(commands, "query_document", failed)
    assert cli.main(["--demo", "query"]) == cli.EXIT_ERROR
    captured = capsys.readouterr()
    assert captured.out == "" and "site could not be resolved" in captured.err


@pytest.mark.parametrize("argv", [["info"], ["wifi"], ["wan"], ["topology"], ["firewall"], ["events"],
                                   ["client", "AA:00:00:00:00:11"], ["export"], ["completion", "bash"],
                                   ["--version"], ["diagnose", "--no-emoji"], ["audit", "--no-emoji"]])
def test_other_commands_stay_concise(terminal, capsys, argv):
    if argv == ["--version"]:
        with pytest.raises(SystemExit) as exited:
            cli.main(argv)
        assert exited.value.code == 0
    else:
        cli.main(["--demo", "--no-progress", *argv])
    assert "Homelab Probe" not in capsys.readouterr().out


def test_help_snapshot_and_plain_help_match(terminal, monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "80")
    expected = (Path(__file__).parent / "golden" / "help" / "main.txt").read_text()
    assert cli.build_parser().format_help() == expected
    for argv in (["--help"], ["--plain", "--help"]):
        with pytest.raises(SystemExit) as exited:
            cli.main(argv)
        assert exited.value.code == 0 and capsys.readouterr().out == expected
    parser = cli.build_parser()
    sub = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    assert list(sub.choices) == sorted(commands.COMMANDS_BY_NAME)


@pytest.mark.parametrize("command", ["diagnose", "audit", "query", "new-clients"])
def test_branding_adds_no_controller_requests(terminal, monkeypatch, capsys, command):
    from homelab_probe.demo import demo_client

    monkeypatch.setattr("homelab_probe.snapshot.time.time", lambda: 1_800_000_000)
    clients = []

    def remember(config):
        client = demo_client(config)
        clients.append(client)
        return client

    monkeypatch.setattr(cli, "demo_client", remember)
    enhanced_code = cli.main(["--demo", "--color", "never", command])
    enhanced = capsys.readouterr().out
    plain_code = cli.main(["--demo", "--plain", command])
    plain = capsys.readouterr().out
    assert enhanced.count("Homelab Probe") == 1 and "Homelab Probe" not in plain
    assert enhanced_code == plain_code
    assert sorted(clients[0].session.calls) == sorted(clients[1].session.calls)
    assert clients[0].session.posts == clients[1].session.posts
