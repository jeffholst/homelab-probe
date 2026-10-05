"""The command registry: one entry per subcommand, each with its arguments, checks and handler."""

import dataclasses

import pytest

from homelab_probe import cli
from homelab_probe.commands import COMMANDS, COMMANDS_BY_NAME, Command, Context

EXPECTED = ["export", "query", "new-clients", "events", "client", "topology", "snapshot", "diff", "wifi", "wan",
            "firewall", "audit", "doctor", "init", "completion", "serve", "web-user", "diagnose", "info"]


def run(fake_client, monkeypatch, argv):
    fake_client.session.fx["legacy"]["device"][0]["overheating"] = False
    monkeypatch.setenv("UNIFI_URL", "https://controller.example")
    monkeypatch.setenv("UNIFI_API_KEY", "key")
    monkeypatch.setattr(cli.UniFiClient, "from_config", classmethod(lambda cls, c: fake_client))
    return cli.main(argv)


def test_the_registry_lists_every_command_once_in_the_documented_order():
    assert [c.name for c in COMMANDS] == EXPECTED
    assert set(COMMANDS_BY_NAME) == set(EXPECTED) and len(COMMANDS_BY_NAME) == len(COMMANDS)


def test_the_parser_is_built_from_the_registry():
    parser = cli.build_parser()
    action = next(a for a in parser._actions if a.dest == "command")
    assert list(action.choices) == EXPECTED
    for command in COMMANDS:
        assert action.choices[command.name].format_help().startswith(f"usage: hlp {command.name}")
        assert command.help[0].isupper()


def test_commands_and_the_context_are_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        COMMANDS[0].name = "other"
    assert dataclasses.is_dataclass(Context) and dataclasses.is_dataclass(Command)


def test_a_command_that_does_not_override_the_hooks_gets_harmless_defaults():
    command = COMMANDS_BY_NAME["info"]
    parser = cli.build_parser()
    args = parser.parse_args(["info"])
    command.validate(parser, args)                      # accepts anything
    command.prepare(args, None)                         # checks nothing
    assert command.wants_settings(args) is False


@pytest.mark.parametrize("argv", [["client", "desktop"], ["topology"], ["wan"], ["diagnose", "--no-events"],
                                  ["query", "reservations", "--offline"]])
def test_commands_that_use_the_settings_file_fail_on_a_bad_one_before_any_request(fake_client, monkeypatch,
                                                                                    capsys, tmp_path, argv):
    bad = tmp_path / "bad.toml"
    bad.write_text("[thresholds]\nnot_a_setting = 1\n")
    assert run(fake_client, monkeypatch, [*argv, "--config", str(bad)]) == cli.EXIT_ERROR
    assert "unknown [thresholds]" in capsys.readouterr().err and fake_client.session.calls == []


@pytest.mark.parametrize("name", ["export", "new-clients", "events", "snapshot", "diff", "wifi", "info"])
def test_commands_that_do_not_use_the_settings_file_do_not_have_the_option(name):
    action = cli.build_parser()._subparsers._group_actions[0].choices[name]
    assert "--config" not in {o for a in action._actions for o in a.option_strings}
    assert COMMANDS_BY_NAME[name].wants_settings(cli.build_parser().parse_args([name])) is False


def test_validation_hooks_run_before_the_configuration_is_loaded(monkeypatch, tmp_path):
    monkeypatch.delenv("UNIFI_URL", raising=False)
    with pytest.raises(SystemExit) as caught:                 # a usage error (64), not the missing-config error (3)
        cli.main(["diff", "a", "b", "c"])
    assert caught.value.code == cli.EXIT_USAGE


def test_the_handlers_return_the_exit_code(fake_client, monkeypatch):
    assert run(fake_client, monkeypatch, ["info"]) == 0
    assert run(fake_client, monkeypatch, ["client", "nobody-has-this-name"]) == cli.EXIT_NO_MATCH
    assert run(fake_client, monkeypatch, ["diagnose", "--no-events"]) == 1


def test_cli_keeps_the_names_other_code_imports():
    assert (cli.EXIT_ERROR, cli.EXIT_NO_MATCH, cli.EXIT_USAGE) == (3, 4, 64)
    assert cli.UniFiClient is not None and callable(cli.main) and callable(cli.build_parser)


def test_no_command_prints_data_with_a_bare_print():
    """Output goes through `say`, which strips control characters; the one print in the package is inside it."""
    from pathlib import Path
    for name in ("cli.py", "commands.py"):
        source = (Path(cli.__file__).parent / name).read_text(encoding="utf-8")
        assert source.count("print(") == (1 if name == "commands.py" else 0), name
