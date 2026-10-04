"""Command line interface: the argument parser and ``main``.

Each command (its arguments, checks and handler) lives in ``commands.py``; ``main`` loads the configuration,
builds the client and calls the handler of the chosen command.
"""

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, List, Optional

from . import __version__, logs
from .client import UniFiAPIError, UniFiClient
from .commands import (
    COMMANDS,
    COMMANDS_BY_NAME,
    EXIT_ERROR,
    EXIT_NO_MATCH,
    EXIT_USAGE,
    Context,
    say,
    verbose,
)
from .config import ConfigError, load_config, parse_parallel, parse_timeout, validate_site
from .demo import demo_client, demo_config
from .settings import DiagnoseSettings, load_settings
from .snapshot import warn

__all__ = ["EXIT_ERROR", "EXIT_NO_MATCH", "EXIT_USAGE", "UniFiClient", "build_parser", "main"]


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse defaults to exit code 2, which would look like a critical finding.
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


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


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="unifi-sentinel",
        description="Query, troubleshoot and inventory a UniFi Network controller.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--verbose", "--debug", action="store_true", dest="verbose",
                        help="Log each request to stderr (method, path, status, milliseconds, retries) and what "
                             "was read, never the API key (before the command)")
    parser.add_argument("--demo", action="store_true",
                        help="Run the command on synthetic data: no .env or implicit settings file is read and no "
                             "controller is contacted (before the command; not with doctor, snapshot, diff or "
                             "--notify)")
    parser.add_argument("--timeout", type=_timeout, metavar="SECONDS",
                        help="Seconds to wait for each request to the controller (before the command; "
                             "default: TIMEOUT from .env, else 15)")
    parser.add_argument("--parallel", type=_parallel, metavar="N",
                        help="How many requests to make at once (before the command; 1 means one by one; "
                             "default: PARALLEL_REQUESTS from .env, else 6)")
    parser.add_argument("--site", type=_site, metavar="NAME|REF|UUID",
                        help="Which site to read: its name, internal reference (such as default) or UUID "
                             "(before the command; default: SITE_ID from .env, else default)")
    parser.add_argument("--env-file", type=Path, metavar="FILE",
                        help="Read settings from this .env file (before the command). Default: "
                             "$UNIFI_SENTINEL_ENV, else ./.env in the current directory")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in COMMANDS:
        command.add_arguments(sub.add_parser(command.name, help=command.help))
    return parser


DEMO_REFUSED = {
    "doctor": "it checks your real installation and settings",
    "snapshot": "it would save synthetic data among your own snapshots",
    "diff": "it compares against your own saved snapshots",
}


def _check_demo(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """``--demo`` runs on synthetic data and touches nothing of the user's: refuse what would read or write it."""
    if args.env_file is not None:
        parser.error("--demo does not read an environment file: drop --env-file")
    if args.command in DEMO_REFUSED:
        parser.error(f"--demo cannot be used with {args.command}: {DEMO_REFUSED[args.command]}")
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
    if command.run_local is not None:                       # needs no .env and no controller
        return command.run_local(args)
    client: Optional[UniFiClient] = None
    try:
        if args.demo:        # nothing from the machine: no .env, no environment, no settings file, no destinations
            config = demo_config(args.site or "default")
        else:
            config = load_config(args.env_file, site_override=args.site)
        if args.timeout is not None:
            config = replace(config, timeout=args.timeout)      # the command line beats .env
        if args.parallel is not None:
            config = replace(config, parallel=args.parallel)
        if not args.demo:
            logs.register_secrets(*config.secret_values())
            if config.log_format or config.log_level:
                logs.configure(config.log_format or "cli", "DEBUG" if args.verbose else config.log_level or "WARNING")
        with logs.bind(request_id=logs.new_id(), site=config.site):
            message = f"unifi-sentinel {__version__}: " + (
                "demo mode, synthetic data, no controller is contacted" if args.demo else _describe_connection(config))
            verbose(message.replace(config.api_key, "***"))
            for message in config.warnings:
                warn(message)
            if args.demo:
                say("Demo mode: synthetic data, no controller is contacted.", file=sys.stderr)
            # Load the settings first so a bad settings file fails before any API call. A demo uses the defaults
            # unless a file is named: an unifi-sentinel.toml in the current directory is the user's, not the demo's.
            wants = command.wants_settings(args)
            if wants and args.demo and getattr(args, "config", None) is None:
                settings: Optional[DiagnoseSettings] = DiagnoseSettings()
            else:
                settings = load_settings(getattr(args, "config", None)) if wants else None
            command.prepare(args, config)
            client = demo_client(config) if args.demo else UniFiClient.from_config(config)
            try:
                return command.run(Context(args, config, settings, client))
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
