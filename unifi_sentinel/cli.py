"""Command line interface."""

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .client import UniFiAPIError, UniFiClient
from .config import ConfigError, load_config
from .diagnose import (CRITICAL, INFO, WARNING, apply_ignores, diagnose, exit_code,
                       format_findings, format_ignored, stream_supports_emoji)
from .export import run_export
from .new_clients import render as render_new_clients, report as new_clients_report
from .query import query_rows, render
from .settings import load_settings
from .snapshot import collect_snapshot


EXIT_ERROR = 3  # config or connection failure; 1 and 2 are reserved for diagnose findings
EXIT_USAGE = 64


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse defaults to exit code 2, which would look like a critical finding.
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="unifi-sentinel",
        description="Query, troubleshoot and inventory a UniFi Network controller.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="Export clients, devices and switch ports to CSV")
    export.add_argument("-o", "--output-dir", type=Path, default=Path("."),
                        help="Directory for CSV files (default: current directory)")
    export.add_argument("--include-offline", action="store_true",
                        help="Also list previously seen clients that are not connected")

    query = sub.add_parser("query", help="List and filter devices, clients, reservations and switch ports")
    query.add_argument("kind", nargs="?", default="all", choices=["all", "devices", "clients", "reservations", "ports"])
    query.add_argument("-s", "--search", default="",
                       help="Case-insensitive substring match on any field")
    query.add_argument("--include-offline", action="store_true",
                       help="Also list previously seen clients that are not connected")
    query.add_argument("--json", action="store_true", help="Output JSON instead of a table")
    query.add_argument("--switch",
                       help="ports only: switch name (case-insensitive substring)")
    query.add_argument("--down", action="store_true", help="ports only: only ports that are down")
    query.add_argument("--errors", action="store_true",
                       help="ports only: only ports with rx/tx errors")

    new = sub.add_parser(
        "new-clients", help="List clients that are in no client group (all known clients)")
    new.add_argument("-s", "--search", default="",
                     help="Case-insensitive substring match on any field")
    new.add_argument("--json", action="store_true", help="Output JSON instead of a table")

    diag = sub.add_parser("diagnose", help="Run read-only health checks (offline devices, port errors, ...)")
    diag.add_argument("--fail-on", choices=[INFO, WARNING, CRITICAL], default=WARNING,
                      help="Lowest severity that gives a non-zero exit code (default: warning); "
                           "critical always exits 2")
    diag.add_argument("--config", type=Path, metavar="FILE",
                      help="TOML file with thresholds and an ignore list "
                           "(default: ./unifi-sentinel.toml if present)")
    diag.add_argument("--show-ignored", action="store_true",
                      help="Also list the findings suppressed by the ignore list")
    diag.add_argument("--no-emoji", action="store_true",
                      help="Use text severity labels (automatic when output is not a UTF-8 terminal)")

    sub.add_parser("info", help="Show controller version and available sites")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "query" and args.kind != "ports" and (
        args.switch is not None or args.down or args.errors
    ):
        parser.error("--switch, --down and --errors only apply to 'query ports'")
    try:
        config = load_config()
        # Load diagnose settings first so a bad config file fails before any API call.
        settings = load_settings(args.config) if args.command == "diagnose" else None
        client = UniFiClient.from_config(config)
        if args.command == "info":
            print(f"Application: {client.info()}")
            for s in client.sites():
                print(f"Site: {s.get('name')} ref={s.get('internalReference')} id={s.get('id')}")
        elif args.command == "export":
            run_export(collect_snapshot(client, config.site, args.include_offline), args.output_dir)
        elif args.command == "query":
            snap = collect_snapshot(
                client, config.site, args.include_offline,
                include_reservations=args.kind == "reservations")
            rows = query_rows(snap, args.kind, args.search, args.include_offline,
                              args.switch or "", args.down, args.errors)
            print(render(rows, args.json, args.kind))
        elif args.command == "new-clients":
            snap = collect_snapshot(client, config.site, include_groups=True)
            print(render_new_clients(new_clients_report(snap, args.search), args.json))
        elif args.command == "diagnose":
            findings, ignored = apply_ignores(
                diagnose(collect_snapshot(client, config.site, include_reservations=True,
                                          include_health=True), settings),
                settings.ignore)
            emoji = not args.no_emoji and stream_supports_emoji(sys.stdout)
            print(format_findings(findings, emoji, len(ignored)))
            if args.show_ignored and ignored:
                print("\n" + format_ignored(ignored))
            return exit_code(findings, args.fail_on)
    except (ConfigError, UniFiAPIError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_ERROR
    return 0


if __name__ == "__main__":
    sys.exit(main())
