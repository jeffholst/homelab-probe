"""Command line interface."""

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .client import UniFiAPIError, UniFiClient
from .config import ConfigError, load_config
from .diagnose import diagnose, format_findings, stream_supports_emoji
from .export import run_export
from .query import query_rows, render
from .snapshot import collect_snapshot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
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

    query = sub.add_parser("query", help="List and filter devices and clients")
    query.add_argument("kind", nargs="?", default="all", choices=["all", "devices", "clients", "reservations"])
    query.add_argument("-s", "--search", default="",
                       help="Case-insensitive substring match on any field")
    query.add_argument("--include-offline", action="store_true",
                       help="Also list previously seen clients that are not connected")
    query.add_argument("--json", action="store_true", help="Output JSON instead of a table")

    diag = sub.add_parser("diagnose", help="Run read-only health checks (offline devices, port errors, ...)")
    diag.add_argument("--no-emoji", action="store_true",
                      help="Use text severity labels (automatic when output is not a UTF-8 terminal)")

    sub.add_parser("info", help="Show controller version and available sites")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config()
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
            rows = query_rows(snap, args.kind, args.search, args.include_offline)
            print(render(rows, args.json, args.kind))
        elif args.command == "diagnose":
            findings = diagnose(collect_snapshot(client, config.site))
            emoji = not args.no_emoji and stream_supports_emoji(sys.stdout)
            print(format_findings(findings, emoji))
    except (ConfigError, UniFiAPIError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
