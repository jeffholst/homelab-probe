"""The terminal's deny-by-default grammar, without any endpoint or controller execution."""

import argparse
import shlex
from dataclasses import replace
from pathlib import Path

import pytest

from homelab_probe import cli
from homelab_probe.cli import StrictParseError, build_parser
from homelab_probe.commands import COMMANDS
from homelab_probe.completion import Command, Option, spec
from homelab_probe.server import terminal_policy as policy

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = [
    ["info"], ["diagnose", "--only", "wifi"], ["audit", "--show-ignored"], ["wan", "--days", "7"],
    ["wifi", "--band", "5"], ["topology", "--clients"], ["firewall", "--zones"],
    ["events", "--category", "one", "--category", "two", "--severity", "high"],
    ["query", "clients", "--ssid", "Guest Wi-Fi"], ["new-clients", "--json"], ["client", "Example Laptop"],
]


def fail(*args, **kwargs):
    pytest.fail("parsing must not read files, run a handler, or print")


@pytest.mark.parametrize("argv", EXAMPLES)
def test_supported_examples_only_parse_and_validate(argv, monkeypatch, capsys):
    monkeypatch.setattr("builtins.open", fail)
    monkeypatch.setattr(Path, "open", fail)
    monkeypatch.setattr(cli, "load_config", fail)
    for command in COMMANDS:
        monkeypatch.setitem(cli.COMMANDS_BY_NAME, command.name, replace(command, run=fail, run_local=fail))
    result = policy.parse_command(argv)
    assert result.operation == argv[0]
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("argv", [
    [], ["--help"], ["-h"], ["--version"], ["--jso"], ["query", "--jso"],
    ["info", "--unknown"], ["wifi", "--band"], ["wifi", "--band", "7"],
    ["@file"], ["client", "@file"], ["--site", "@file", "info"],
    ["query", "--search=@file"], ["--env-file=@file", "info"],
    ["query", "--json", "--json"], ["query", "-s", "one", "--search=two"],
    ["--site=one", "--site", "two", "info"], ["query", "--json", "--csv"],
    ["query", "devices", "--ssid", "Guest"], ["topology", "--json", "--format", "dot"],
    ["diagnose", "--only", "wifi", "--skip", "ports"], ["diagnose", "--only", "unknown"],
    ["diagnose", "--notify-min", "warning"], ["wan", "--days", "0"],
    ["client", "one", "two"], ["wifi", "--band", "\x1b[2J"], ["info", "secret" * 10000],
    ["--site", "../../secret", "info"], ["query", "--search"],
])
def test_strict_errors_never_exit_print_echo_input_or_read_files(argv, monkeypatch, capsys):
    parser = build_parser(strict=True)
    monkeypatch.setattr("builtins.open", fail)
    with pytest.raises(StrictParseError, match=r"^The command arguments are not valid\.$"):
        parser.parse_args(argv)
    assert capsys.readouterr() == ("", "")


def test_strict_parser_requires_explicit_tokens_and_cannot_exit(capsys):
    parser = build_parser(strict=True)
    for invoke in (parser.parse_args, lambda: parser.parse_args([123]), parser.exit):
        with pytest.raises(StrictParseError):
            invoke()
    assert capsys.readouterr() == ("", "")


def test_every_subparser_is_strict_and_help_is_an_inert_flag(capsys):
    parser = build_parser(strict=True)
    subparsers = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    assert not parser.allow_abbrev and parser.fromfile_prefix_chars is None
    for name, sub in subparsers.choices.items():
        required = {"client": ["Example"], "completion": ["bash"], "web-user": ["list"]}.get(name, [])
        assert not sub.allow_abbrev and sub.fromfile_prefix_chars is None
        assert sub.parse_known_args([*required, "--help"])[0].help
        assert sub.parse_known_args([*required, "--hel"])[1] == ["--hel"]
        with pytest.raises(StrictParseError):
            sub.parse_known_args([*required, "--help", "-h"])
    assert capsys.readouterr() == ("", "")


def test_parser_reuse_resets_duplicate_tracking_and_preserves_repeatable_options():
    parser = build_parser(strict=True)
    argv = ["diagnose", "--only", "wifi", "--only", "ports"]
    for _ in range(2):
        args = parser.parse_args(argv)
        assert args.only == ["wifi", "ports"] and args.areas == ["ports", "wifi"]
    args = parser.parse_args(["events", "--severity", "LOW", "--severity", "high"])
    assert args.severity == ["low", "high"]


@pytest.mark.parametrize("argv, operation, topic", [
    (["help"], "help", None), (["--help"], "help", None), (["-h"], "help", None),
    (["hlp", "help", "info"], "help", "info"), (["info", "--help"], "help", "info"),
    (["client", "--help"], "help", "client"), (["--help", "client"], "help", "client"),
    (["client", "-h"], "help", "client"), (["client", "Example", "--help"], "help", "client"),
    (["--help", "info"], "help", "info"), (["version"], "version", None),
    (["hlp", "--version"], "version", None), (["hlp", "info"], "info", None),
])
def test_static_operations_and_optional_prefix(argv, operation, topic):
    parsed = policy.parse_command(argv)
    assert parsed.operation == operation
    assert getattr(parsed.args, "topic", None) == topic


@pytest.mark.parametrize("argv", [
    ["snapshot"], ["export"], ["doctor"], ["diff"], ["completion", "bash"], ["serve"],
    ["init"], ["web-user", "list"], ["help", "serve"], ["help", "missing"],
    ["serve", "--help"],
    ["--env-file", "secret.env", "info"], ["--debug", "info"], ["--demo", "info"],
    ["--timeout", "1", "info"], ["--parallel", "1", "info"], ["--color", "never", "info"],
    ["--plain", "info"], ["--no-progress", "info"], ["--version", "info"],
    ["diagnose", "--notify"], ["diagnose", "--watch", "10"], ["audit", "--config", "secret.toml"],
    ["query", "--csv"], ["topology", "--format", "mermaid"],
    ["--debug", "info", "--help"],
])
def test_unavailable_capabilities_cannot_be_enabled_by_help_or_parsing(argv, monkeypatch, capsys):
    monkeypatch.setattr("builtins.open", fail)
    monkeypatch.setattr(Path, "open", fail)
    monkeypatch.setattr(cli, "load_config", fail)
    with pytest.raises(policy.UnsupportedCapability, match="unavailable in the browser"):
        policy.parse_command(argv)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("argv", [[], [123], ["hlp"], ["python", "info"]])
def test_malformed_terminal_input(argv):
    with pytest.raises(StrictParseError):
        policy.parse_command(argv)


def test_literal_shell_characters_have_no_execution_semantics():
    text = "Example; | $(whoami) `id` > output $HOME *.env"
    assert policy.parse_command(["query", "-s", text]).args.search == text
    assert policy.parse_command(["--site", "default", "info"]).args.site == "default"


def test_exact_registry_parity_and_metadata():
    assert policy.compatibility_errors(spec(build_parser())) == ()
    for capability in policy.CAPABILITIES.values():
        assert capability.minimum_role == "viewer"
        for entry in (capability.policy, *capability.options.values()):
            assert entry.status in {"supported", "restricted", "unavailable"}
            assert entry.reason.endswith(".")
    assert {cap.operation for cap in policy.CAPABILITIES.values() if cap.operation} == {argv[0] for argv in EXAMPLES}


def test_parity_detects_new_and_stale_commands_options_and_aliases():
    grammar = spec(build_parser())
    fake_option = Option(("--unreviewed",), "Not reviewed", False)
    changed = replace(grammar, commands=(*grammar.commands, Command("unreviewed", "New", ())))
    assert policy.compatibility_errors(changed) == ("Command classifications do not match the CLI.",)
    changed = replace(grammar, commands=grammar.commands[1:])
    assert policy.compatibility_errors(changed)
    changed = replace(grammar, options=(*grammar.options, fake_option))
    assert policy.compatibility_errors(changed) == ("Option classifications do not match: global.",)
    first = grammar.commands[0]
    for options in ((*first.options, fake_option), first.options[1:],
                    (replace(first.options[0], flags=("-h", "--help", "--assist")), *first.options[1:])):
        changed = replace(grammar, commands=(replace(first, options=options), *grammar.commands[1:]))
        assert policy.compatibility_errors(changed) == ("Option classifications do not match: audit.",)
    stale = dict(policy.GLOBAL_OPTIONS, **{"--removed": policy.READ})
    assert policy.compatibility_errors(grammar, global_options=stale)
    stale_commands = dict(policy.CAPABILITIES, removed=policy.CAPABILITIES["info"])
    assert policy.compatibility_errors(grammar, capabilities=stale_commands)


@pytest.mark.parametrize("name", ["query", "completion", "web-user"])
@pytest.mark.parametrize("change", ["added", "removed"])
def test_parity_detects_new_and_stale_positional_choices(name, change):
    grammar = spec(build_parser())
    command = next(command for command in grammar.commands if command.name == name)
    choices = (*command.choices, "unreviewed") if change == "added" else command.choices[:-1]
    changed = replace(grammar, commands=tuple(
        replace(item, choices=choices) if item.name == name else item for item in grammar.commands))
    assert policy.compatibility_errors(changed) == (f"Positional classifications do not match: {name}.",)


@pytest.mark.parametrize("name", ["info", "diff"])
def test_parity_detects_added_and_removed_file_positional_metadata(name):
    grammar = spec(build_parser())
    changed = replace(grammar, commands=tuple(
        replace(command, files=not command.files) if command.name == name else command
        for command in grammar.commands))
    assert policy.compatibility_errors(changed) == (f"Positional classifications do not match: {name}.",)


@pytest.mark.parametrize("file_argument", [False, True])
def test_runtime_refuses_unreviewed_positional_grammar_even_if_the_parser_accepts_it(monkeypatch, file_argument):
    parser = build_parser(strict=True)
    sub = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    if file_argument:
        sub.choices["info"].add_argument("source", nargs="?", type=Path)
        argv = ["info", "secret.json"]
    else:
        kind = next(action for action in sub.choices["query"]._actions if action.dest == "kind")
        kind.choices = (*kind.choices, "unreviewed")
        argv = ["query", "unreviewed"]
    assert parser.parse_args(argv).command == argv[0]
    assert policy.compatibility_errors(spec(parser))
    monkeypatch.setattr(policy, "build_parser", lambda **kwargs: parser)
    monkeypatch.setattr("builtins.open", fail)
    monkeypatch.setattr(Path, "open", fail)
    with pytest.raises(policy.UnsupportedCapability, match="unreviewed changes"):
        policy.parse_command(argv)


def test_registry_is_the_allowlist_even_when_grammar_changes(monkeypatch):
    parser = build_parser(strict=True)
    parser.add_argument("--unreviewed", action="store_true")
    sub = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    sub.choices["info"].add_argument("--unreviewed", action="store_true")
    new_command = sub.add_parser("unreviewed", terminal_scope="unreviewed")
    monkeypatch.setitem(cli.COMMANDS_BY_NAME, "unreviewed", replace(COMMANDS[0], name="unreviewed"))
    assert new_command is not None
    monkeypatch.setattr(policy, "build_parser", lambda **kwargs: parser)
    for argv in (["--unreviewed", "info"], ["info", "--unreviewed"], ["unreviewed"]):
        with pytest.raises(policy.UnsupportedCapability):
            policy.parse_command(argv)
    # The registry must still deny unknown capabilities independently of the grammar-drift guard.
    monkeypatch.setattr(policy, "compatibility_errors", lambda grammar: ())
    for argv in (["--unreviewed", "info"], ["info", "--unreviewed"], ["unreviewed"]):
        with pytest.raises(policy.UnsupportedCapability):
            policy.parse_command(argv)


def test_matrix_and_limits_are_documented_from_the_registry():
    docs = (ROOT / "docs/web.md").read_text(encoding="utf-8")
    matrix = docs.split("<!-- terminal-matrix:start -->\n", 1)[1].split("<!-- terminal-matrix:end -->", 1)[0]
    assert matrix == policy.compatibility_markdown() + "\n"
    assert (policy.BODY_BYTES, policy.TOKEN_COUNT, policy.TOKEN_CHARS, policy.USER_CONCURRENCY,
            policy.USER_RATE, policy.GLOBAL_CONCURRENCY, policy.OUTPUT_BYTES) == (16384, 64, 256, 1, 30, 4, 262144)
    assert "stopped waiting" in docs and "16 KiB" in docs and "256 KiB" in docs
    for argv in EXAMPLES:
        assert "hlp " + shlex.join(argv) in docs


def test_default_parser_and_completion_metadata_are_unchanged(capsys):
    parser = build_parser()
    assert parser.allow_abbrev
    assert parser.parse_args(["query", "--jso"]).json
    assert parser.parse_args(["query", "--search", "one", "--search", "two"]).search == "two"
    with pytest.raises(SystemExit) as caught:
        parser.parse_args(["--help"])
    assert caught.value.code == 0 and "Query, troubleshoot" in capsys.readouterr().out
    assert spec(parser) == spec(build_parser(strict=True))
