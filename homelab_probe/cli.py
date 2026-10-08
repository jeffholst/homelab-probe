"""Command line interface: the argument parser and ``main``.

Each command (its arguments, checks and handler) lives in ``commands.py``; ``main`` loads the configuration,
builds the client and calls the handler of the chosen command.
"""

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, List, NoReturn, Optional

from . import __version__, logs
from .client import UniFiAPIError, UniFiClient
from .commands import (
    COMMANDS,
    COMMANDS_BY_NAME,
    EXIT_ERROR,
    EXIT_NO_MATCH,
    EXIT_USAGE,
    Context,
    apply_logging,
    say,
    verbose,
)
from .config import ConfigError, load_config, parse_parallel, parse_timeout, validate_site
from .demo import demo_client, demo_config
from .present import COLOR_CHOICES, Presentation
from .progress import Progress, eligible
from .settings import DiagnoseSettings, load_settings
from .snapshot import warn

__all__ = ["EXIT_ERROR", "EXIT_NO_MATCH", "EXIT_USAGE", "UniFiClient", "build_parser", "main"]


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse defaults to exit code 2, which would look like a critical finding.
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


class StrictParseError(ValueError):
    """Invalid untrusted arguments; the message never contains submitted input."""


class _StrictParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, terminal_scope: str = "", **kwargs: Any) -> None:
        add_help = kwargs.pop("add_help", True)
        kwargs.update(allow_abbrev=False, fromfile_prefix_chars=None, add_help=False)
        super().__init__(*args, **kwargs)
        self.terminal_scope = terminal_scope
        self._provided: set[argparse.Action] = set()
        if add_help:
            self.add_argument("-h", "--help", action="store_true", help="show this help message and exit")

    def error(self, message: str) -> NoReturn:
        raise StrictParseError("The command arguments are not valid.") from None

    def exit(self, status: int = 0, message: Optional[str] = None) -> NoReturn:
        raise StrictParseError("The command arguments are not valid.") from None

    def _get_values(self, action, arg_strings):
        if any(value.startswith("@") for value in arg_strings):
            self.error("response files are unavailable")
        # argparse otherwise silently overwrites repeated scalar options, including aliases and --flag=value.
        if action.option_strings:
            if action in self._provided and not isinstance(action, argparse._AppendAction):
                self.error("duplicate option")
            self._provided.add(action)
        return super()._get_values(action, arg_strings)

    def parse_known_args(self, args=None, namespace=None):
        if args is None or any(not isinstance(token, str) or token.startswith("@") for token in args):
            self.error("explicit tokens required; response files are unavailable")
        self._provided = set()
        parsed, extras = super().parse_known_args(args, namespace)
        provided = dict(getattr(parsed, "_terminal_options", {}))
        provided[self.terminal_scope] = tuple(
            flag for action in self._provided for flag in action.option_strings)
        parsed._terminal_options = provided
        return parsed, extras

    def parse_args(self, args=None, namespace=None):
        parsed = super().parse_args(args, namespace)
        COMMANDS_BY_NAME[parsed.command].validate(self, parsed)
        return parsed


def _describe_connection(config: Any) -> str:
    verify = {True: "on", False: "off"}.get(config.verify_ssl) if isinstance(config.verify_ssl, bool) \
        else f"CA bundle {config.verify_ssl}"
    source = str(config.env_file) if config.env_file else "environment variables only"
    return (f"settings from {source}; controller {config.controller_url}, site {config.site}, "
            f"timeout {config.timeout:g} s, TLS verification {verify}, up to {config.parallel} requests at once")


def _timeout(text: str) -> float:
    try:
        return parse_timeout(text)
    except ConfigError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def _parallel(text: str) -> int:
    try:
        return parse_parallel(text)
    except ConfigError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def _site(text: str) -> str:
    if not text.strip():
        raise argparse.ArgumentTypeError("--site needs a site name, internal reference or UUID")
    try:
        return validate_site(text, "--site")
    except ConfigError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def build_parser(*, strict: bool = False) -> argparse.ArgumentParser:
    """The CLI grammar; strict mode raises safe errors and runs pure cross-option validation."""
    parser = (_StrictParser if strict else _Parser)(
        prog="hlp",
        description="Query, troubleshoot and inventory a UniFi Network controller.",
        add_help=False,
    )
    general = parser.add_argument_group("general")
    connection = parser.add_argument_group("connection")
    presentation = parser.add_argument_group("presentation")
    general.add_argument("-h", "--help", action="store_true" if strict else "help",
                         help="show this help message and exit")
    if strict:
        general.add_argument("--version", action="store_true", help="show program's version number and exit")
    else:
        general.add_argument("--version", action="version", version=__version__)
    general.add_argument("--verbose", "--debug", action="store_true", dest="verbose",
                        help="Log each request to stderr (method, path, status, milliseconds, retries) and what "
                             "was read, never the API key (before the command)")
    general.add_argument("--demo", action="store_true",
                        help="Run the command on synthetic data: no .env or implicit settings file is read and no "
                             "controller is contacted (before the command; not with doctor, snapshot, diff or "
                             "--notify)")
    connection.add_argument("--timeout", type=_timeout, metavar="SECONDS",
                        help="Seconds to wait for each request to the controller (before the command; "
                             "default: UNIFI_TIMEOUT from .env, else 15)")
    connection.add_argument("--parallel", type=_parallel, metavar="N",
                        help="How many requests to make at once (before the command; 1 means one by one; "
                             "default: UNIFI_PARALLEL_REQUESTS from .env, else 6)")
    connection.add_argument("--site", type=_site, metavar="NAME|REF|UUID",
                        help="Which site to read: its name, internal reference (such as default) or UUID "
                             "(before the command; default: UNIFI_SITE_ID from .env, else default)")
    connection.add_argument("--env-file", type=Path, metavar="FILE",
                        help="Read settings from this .env file (before the command). Default: "
                             "$HLP_ENV, else ./.env in the current directory")
    presentation.add_argument("--color", choices=COLOR_CHOICES, default="auto", metavar="WHEN",
                        help="Terminal colors: auto (a terminal that supports them, unless NO_COLOR is set), always "
                             "(also when piped, but never in --json/--csv/export/completion output) or never "
                             "(before the command; needs the pretty extra; default: auto)")
    presentation.add_argument("--no-progress", action="store_true", dest="no_progress",
                        help="Never show transient progress on stderr (before the command; progress is also off "
                             "when stderr is not a terminal, with --plain, --verbose or machine-readable output)")
    presentation.add_argument("--plain", action="store_true",
                        help="Plain text only: no color, animation, decorative headings or symbols, whatever --color "
                             "says (before the command)")
    sub = parser.add_subparsers(dest="command", required=True, title="commands", metavar="COMMAND")
    for command in sorted(COMMANDS, key=lambda command: command.name):
        options: dict[str, Any] = {"terminal_scope": command.name} if strict else {}
        command.add_arguments(sub.add_parser(command.name, help=command.help, **options))
    return parser


DEMO_REFUSED = {
    "doctor": "it checks your real installation and settings",
    "snapshot": "it would save synthetic data among your own snapshots",
    "diff": "it compares against your own saved snapshots",
    "web-user": "it reads and writes your own accounts file",
    "init": "it writes your own .env and settings files",
}


def _check_demo(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """``--demo`` runs on synthetic data and touches nothing of the user's: refuse what would read or write it."""
    if args.env_file is not None:
        parser.error("--demo does not read an environment file: drop --env-file")
    if args.command in DEMO_REFUSED:
        parser.error(f"--demo cannot be used with {args.command}: {DEMO_REFUSED[args.command]}")
    if getattr(args, "scheduler", False):
        parser.error("--demo cannot be combined with --scheduler: a demo has nothing to schedule")
    if any(getattr(args, name, False) for name in ("notify", "notify_dry_run", "notify_baseline")):
        parser.error("--demo cannot be combined with --notify: nothing is ever sent from a demo")


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = COMMANDS_BY_NAME[args.command]
    command.validate(parser, args)
    if args.demo:
        _check_demo(parser, args)
    logs.reset()                                            # a fresh logger state for this run
    logs.configure("cli", "DEBUG" if args.verbose else "WARNING")
    client: Optional[UniFiClient] = None
    try:
        if command.run_local is not None:                   # needs no .env and no controller
            return command.run_local(args)
        if args.demo:        # nothing from the machine: no .env, no environment, no settings file, no destinations
            config = demo_config(args.site or "default")
        else:
            config = load_config(args.env_file, site_override=args.site)
        if args.timeout is not None:
            config = replace(config, timeout=args.timeout)      # the command line beats .env
        if args.parallel is not None:
            config = replace(config, parallel=args.parallel)
        if not args.demo:
            apply_logging(config, args.verbose)
        with logs.bind(request_id=logs.new_id(), site=config.site):
            message = f"hlp {__version__}: " + (
                "demo mode, synthetic data, no controller is contacted" if args.demo else _describe_connection(config))
            verbose(message.replace(config.api_key, "***"))
            for message in config.warnings:
                warn(message)
            if args.demo:
                say("Demo mode: synthetic data, no controller is contacted.", file=sys.stderr)
            # Load the settings first so a bad settings file fails before any API call. A demo uses the defaults
            # unless a file is named: an hlp.toml in the current directory is the user's, not the demo's.
            wants = command.wants_settings(args)
            if wants and args.demo and getattr(args, "config", None) is None:
                settings: Optional[DiagnoseSettings] = DiagnoseSettings()
            else:
                settings = load_settings(getattr(args, "config", None)) if wants else None
            command.prepare(args, config)
            client = demo_client(config) if args.demo else UniFiClient.from_config(config)
            present = Presentation.from_args(args)
            try:
                with Progress(present, enabled=eligible(args)) as progress:     # a transient line, only where welcome
                    client.stage, client.note = progress.stage, progress.note
                    return command.run(Context(args, config, settings, client, present))
            finally:
                if client is not None and client.attempts_made:
                    verbose(client.summary(), "run.summary")
    except (ConfigError, UniFiAPIError) as e:
        say(f"ERROR: {e}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as e:        # a file the command could not read or write: say which, never a traceback
        say(f"ERROR: {e.strerror or type(e).__name__}" + (f" ({e.filename})" if e.filename else ""), file=sys.stderr)
        return EXIT_ERROR

if __name__ == "__main__":
    sys.exit(main())
