"""Command line interface."""

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .client import UniFiAPIError, UniFiClient
from .config import ConfigError, load_config
from .export import run_export


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
            run_export(client, config.site, args.output_dir, args.include_offline)
    except (ConfigError, UniFiAPIError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
