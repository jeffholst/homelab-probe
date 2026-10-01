"""Command line interface."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Optional

from . import __version__
from .client import UniFiAPIError, UniFiClient
from .client_view import build_client_detail, find_clients, render_candidates, render_detail, to_json
from .config import ConfigError, load_config
from .diagnose import (
    CRITICAL,
    INFO,
    WARNING,
    apply_ignores,
    diagnose,
    exit_code,
    format_findings,
    format_ignored,
    stream_supports_emoji,
)
from .events import DEFAULT_LIMIT, DEFAULT_SINCE, SEVERITIES, fetch_events, make_filter, parse_duration, render_events
from .export import run_export
from .history import (
    DEFAULT_DIR,
    capture,
    diff_snapshots,
    label_for,
    list_snapshots,
    load_snapshot,
    prune,
    render_diff,
    resolve,
    save_snapshot,
)
from .new_clients import render as render_new_clients
from .new_clients import report as new_clients_report
from .query import query_rows, render
from .settings import load_settings
from .snapshot import collect_event_snapshot, collect_snapshot, warn
from .topology import build_topology
from .topology import render_text as render_topology
from .topology import to_json as topology_json
from .util import printable, safe_output
from .wan import DEFAULT_DAYS, build_wan
from .wan import render_text as render_wan
from .wan import to_json as wan_json
from .wifi import DEFAULT_MIN_SIGNAL, build_wifi, parse_band
from .wifi import render_text as render_wifi
from .wifi import to_json as wifi_json

EXIT_ERROR = 3  # config or connection failure; 1 and 2 are reserved for diagnose findings
EXIT_NO_MATCH = 4  # `client` found no client, or several (it lists them)
EXIT_USAGE = 64


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse defaults to exit code 2, which would look like a critical finding.
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def _say(text: Any = "", file: Any = None) -> None:
    """Print ``text`` with control characters removed (names come from devices on the network,
    and an escape sequence in one must not reach the terminal). Line breaks are kept."""
    print(safe_output(str(text)), file=file)


def _duration(text: str) -> int:
    try:
        return parse_duration(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def _non_negative(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = -1
    if value < 0:
        raise argparse.ArgumentTypeError(f"invalid value {text!r}: use a whole number, 0 or more")
    return value


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"invalid value {text!r}: use a whole number, 1 or more")
    return value


def _band(text: str) -> str:
    try:
        return parse_band(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def _signal(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = 1.0
    if not -120 <= value <= 0:
        raise argparse.ArgumentTypeError(f"invalid signal {text!r}: use dBm between -120 and 0")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="unifi-sentinel",
        description="Query, troubleshoot and inventory a UniFi Network controller.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--env-file", type=Path, metavar="FILE",
                        help="Read settings from this .env file (before the command). Default: "
                             "$UNIFI_SENTINEL_ENV, else ./.env in the current directory")
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

    ev = sub.add_parser(
        "events", help="Event history from the controller log: disconnects, roams, IP conflicts...")
    ev.add_argument("--since", type=_duration, default=_duration(DEFAULT_SINCE), metavar="DURATION",
                    help=f"How far back to look, e.g. 90m, 24h, 7d, 2w (default: {DEFAULT_SINCE})")
    ev.add_argument("--category", action="append", default=[], metavar="NAME",
                    help="Only this category, e.g. CLIENT_DEVICES, UNIFI_DEVICES, INTERNET_AND_WAN, "
                         "AUDIT (repeatable)")
    ev.add_argument("--severity", action="append", default=[], type=str.lower, choices=SEVERITIES,
                    help="Only this severity (repeatable)")
    ev.add_argument("--event", default="", metavar="TEXT",
                    help="Only event types containing TEXT, e.g. roam, disconnected, ip_conflict")
    ev.add_argument("--client", default="", metavar="NAME|MAC|IP",
                    help="Only events about this client (name, hostname, IP or part of a MAC)")
    ev.add_argument("--device", default="", metavar="NAME|IP",
                    help="Only events about this UniFi device (e.g. an AP or switch)")
    ev.add_argument("-s", "--search", default="", help="Text search done by the controller")
    ev.add_argument("--limit", type=_non_negative, default=DEFAULT_LIMIT,
                    help=f"Most events to show, newest first (default {DEFAULT_LIMIT}; 0 for all)")
    ev.add_argument("--summary", action="store_true",
                    help="Counts by type and the noisiest clients/devices over the whole window "
                         "(ignores --limit) instead of a list")
    ev.add_argument("--json", action="store_true", help="Output JSON instead of a table")

    cview = sub.add_parser(
        "client", help="Troubleshoot one client: where it attaches, link quality and related findings")
    cview.add_argument("query", help="Client name, MAC address or IP address")
    cview.add_argument("--json", action="store_true", help="Output JSON instead of text")
    cview.add_argument("--config", type=Path, metavar="FILE",
                       help="TOML file with diagnose thresholds and ignore list "
                            "(default: ./unifi-sentinel.toml if present)")
    cview.add_argument("--no-events", action="store_true",
                       help="Skip the recent-events section, which otherwise sends the one approved "
                            "read-only event-log query")
    cview.add_argument("--since", type=_duration, default=_duration(DEFAULT_SINCE), metavar="DURATION",
                       help=f"How far back the recent events reach, e.g. 12h, 7d (default: {DEFAULT_SINCE})")
    cview.add_argument("--no-emoji", action="store_true",
                       help="Use text severity labels (automatic when output is not a UTF-8 terminal)")

    topo = sub.add_parser(
        "topology", help="Draw the uplink tree: gateway, switches and APs with ports, speeds and problems")
    topo.add_argument("--clients", action="store_true",
                      help="Also list the wired clients under each device")
    topo.add_argument("--json", action="store_true", help="Output nested JSON instead of a tree")
    topo.add_argument("--config", type=Path, metavar="FILE",
                      help="TOML file with diagnose thresholds and ignore list "
                           "(default: ./unifi-sentinel.toml if present)")
    topo.add_argument("--no-emoji", action="store_true",
                      help="Use ASCII drawing and text severity labels (automatic when output is "
                           "not a UTF-8 terminal)")

    snapcmd = sub.add_parser(
        "snapshot", help="Save the current inventory to a local JSON file, for `diff` later")
    snapcmd.add_argument("-o", "--output", type=Path, metavar="FILE",
                         help="Write to this file instead of a timestamped one in --dir")
    snapcmd.add_argument("--dir", type=Path, default=Path(DEFAULT_DIR), metavar="DIR",
                         help=f"Directory for timestamped snapshots (default: ./{DEFAULT_DIR}/)")
    snapcmd.add_argument("--keep", type=_positive, metavar="N",
                         help="Afterwards delete the oldest snapshots in --dir, keeping the newest N")
    snapcmd.add_argument("--force", action="store_true", help="Allow -o to replace an existing file")

    diffcmd = sub.add_parser(
        "diff", help="What changed: compare saved snapshots, or a snapshot against the live network")
    diffcmd.add_argument("refs", nargs="*", metavar="OLD [NEW]",
                         help="Snapshot files (or names inside --dir). One file is compared with the live "
                              "network; none uses the newest saved snapshot")
    diffcmd.add_argument("--dir", type=Path, default=Path(DEFAULT_DIR), metavar="DIR",
                         help=f"Snapshot directory (default: ./{DEFAULT_DIR}/)")
    diffcmd.add_argument("--last-two", action="store_true",
                         help="Compare the two newest saved snapshots (no controller needed)")
    diffcmd.add_argument("--all", action="store_true",
                         help="List every client connect/disconnect and IP change instead of the first "
                              "few")
    diffcmd.add_argument("--json", action="store_true", help="Output JSON instead of text")

    wifi = sub.add_parser(
        "wifi", help="Wireless report: each AP's radios and a channel plan from the neighboring networks")
    wifi.add_argument("--band", type=_band, default=None, metavar="2.4|5|6", help="Only this band")
    wifi.add_argument("--ap", default="", metavar="NAME",
                      help="Only this AP (name or part of it), and only neighbors it hears")
    wifi.add_argument("--min-signal", type=_signal, default=DEFAULT_MIN_SIGNAL, metavar="DBM",
                      help=f"Neighbors weaker than this are counted but not named or compared "
                           f"(default {DEFAULT_MIN_SIGNAL})")
    wifi.add_argument("--all", action="store_true",
                      help="Name every strong neighbor on a channel, not just the first few")
    wifi.add_argument("--json", action="store_true", help="Output JSON instead of text")

    wancmd = sub.add_parser(
        "wan", help="Internet health: current state, 24-hour monitoring and speedtest history")
    wancmd.add_argument("--days", type=_positive, default=DEFAULT_DAYS, metavar="N",
                        help=f"How many days of speedtests to summarize (default: {DEFAULT_DAYS})")
    wancmd.add_argument("--json", action="store_true", help="Output JSON instead of text")
    wancmd.add_argument("--config", type=Path, metavar="FILE",
                        help="TOML file with the thresholds (default: ./unifi-sentinel.toml if present)")

    diag = sub.add_parser("diagnose", help="Run read-only health checks (offline devices, port errors, ...)")
    diag.add_argument("--fail-on", choices=[INFO, WARNING, CRITICAL], default=WARNING,
                      help="Lowest severity that gives a non-zero exit code (default: warning); "
                           "critical always exits 2")
    diag.add_argument("--config", type=Path, metavar="FILE",
                      help="TOML file with thresholds and an ignore list "
                           "(default: ./unifi-sentinel.toml if present)")
    diag.add_argument("--no-events", action="store_true",
                      help="Skip the event-log checks (repeated disconnects, IP conflicts, ...), "
                           "which otherwise send the one approved read-only POST")
    diag.add_argument("--since", type=_duration, default=_duration(DEFAULT_SINCE), metavar="DURATION",
                      help=f"How far back the event checks look, e.g. 12h, 7d (default: {DEFAULT_SINCE})")
    diag.add_argument("--show-ignored", action="store_true",
                      help="Also list the findings suppressed by the ignore list")
    diag.add_argument("--no-emoji", action="store_true",
                      help="Use text severity labels (automatic when output is not a UTF-8 terminal)")

    sub.add_parser("info", help="Show controller version and available sites")
    return parser


def _live_inventory(client: UniFiClient, config: Any) -> dict:
    """The network as it is right now, as a snapshot record."""
    try:
        version = str(client.info().get("applicationVersion") or "")
    except UniFiAPIError:
        version = ""
    return capture(collect_snapshot(client, config.site, include_reservations=True, include_groups=True),
                   version)


def _run_history(client: UniFiClient, config: Any, args: argparse.Namespace) -> int:
    if args.command == "snapshot":
        record = _live_inventory(client, config)
        path = save_snapshot(record, args.output, args.dir, args.force)
        _say(f"Saved {len(record['devices'])} devices, {len(record['clients'])} clients and "
              f"{len(record['reservations'])} reservations to {path}")
        if args.keep:
            for gone in prune(args.dir, args.keep, protect=path):
                _say(f"Removed old snapshot {gone}")
        return 0

    if args.last_two:
        saved = list_snapshots(args.dir)
        if len(saved) < 2:
            raise ConfigError(f"need at least two saved snapshots in {args.dir}/ (found {len(saved)}); "
                              "run `snapshot` first")
        old_path, new_path = saved[-2], saved[-1]
    else:
        if args.refs:
            old_path = resolve(args.refs[0], args.dir)
        else:
            saved = list_snapshots(args.dir)
            if not saved:
                raise ConfigError(f"no saved snapshots in {args.dir}/; run `snapshot` first")
            old_path = saved[-1]
        new_path = resolve(args.refs[1], args.dir) if len(args.refs) == 2 else None

    old = load_snapshot(old_path)
    if new_path:
        new = load_snapshot(new_path)
        new_label = label_for(new, new_path.name)
    else:
        new, new_label = _live_inventory(client, config), "the network right now"
    result = diff_snapshots(old, new)
    _say(json.dumps(result, indent=2) if args.json
          else render_diff(result, label_for(old, old_path.name), new_label, args.all))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "diff" and (len(args.refs) > 2 or (args.last_two and args.refs)):
        parser.error("give at most two snapshots, and none with --last-two")
    if args.command == "query" and args.kind != "ports" and (
        args.switch is not None or args.down or args.errors
    ):
        parser.error("--switch, --down and --errors only apply to 'query ports'")
    try:
        config = load_config(args.env_file)
        for message in config.warnings:
            warn(message)
        # Load diagnose settings first so a bad config file fails before any API call.
        settings = load_settings(args.config) if args.command in ("diagnose", "client", "topology", "wan") else None
        client = UniFiClient.from_config(config)
        if args.command == "info":
            _say(f"Application: {client.info()}")
            for s in client.sites():
                _say(f"Site: {printable(s.get('name'))} ref={printable(s.get('internalReference'))} "
                 f"id={printable(s.get('id'))}")
        elif args.command == "export":
            run_export(collect_snapshot(client, config.site, args.include_offline), args.output_dir)
        elif args.command == "query":
            snap = collect_snapshot(
                client, config.site, args.include_offline,
                include_reservations=args.kind == "reservations")
            rows = query_rows(snap, args.kind, args.search, args.include_offline,
                              args.switch or "", args.down, args.errors)
            _say(render(rows, args.json, args.kind))
        elif args.command == "new-clients":
            snap = collect_snapshot(client, config.site, include_groups=True)
            _say(render_new_clients(new_clients_report(snap, args.search), args.json))
        elif args.command == "events":
            snap = collect_event_snapshot(
                client, config.site, args.since, categories=args.category,
                severities=args.severity, search=args.search)
            events, more = fetch_events(
                snap, predicate=make_filter(args.client, args.device, args.event),
                limit=0 if args.summary else args.limit)  # a summary counts the whole window
            _say(render_events(events, more, args.json, args.summary, snap.events_truncated))
        elif args.command in ("snapshot", "diff"):
            return _run_history(client, config, args)
        elif args.command == "wifi":
            snap = collect_snapshot(client, config.site, include_neighbors=True)
            report = build_wifi(snap, args.min_signal, args.band or "", args.ap)
            _say(wifi_json(report) if args.json else render_wifi(report, args.all, args.ap))
        elif args.command == "wan":
            snap = collect_snapshot(client, config.site, include_health=True, include_speedtests=True)
            report = build_wan(snap, args.days, settings)
            _say(wan_json(report) if args.json else render_wan(report))
        elif args.command == "topology":
            snap = collect_snapshot(client, config.site)
            tree = build_topology(snap, settings, with_clients=args.clients)
            emoji = not args.no_emoji and stream_supports_emoji(sys.stdout)
            _say(topology_json(tree) if args.json else render_topology(tree, emoji, args.clients))
        elif args.command == "client":
            snap = collect_snapshot(client, config.site, include_reservations=True,
                                    include_groups=True, include_events=not args.no_events,
                                    event_since_seconds=args.since)
            matches = find_clients(snap, args.query)
            if len(matches) != 1:
                _say(render_candidates(args.query, matches), file=sys.stderr)
                return EXIT_NO_MATCH
            detail = build_client_detail(snap, matches[0], settings)
            emoji = not args.no_emoji and stream_supports_emoji(sys.stdout)
            _say(to_json(detail) if args.json else render_detail(detail, emoji))
        elif args.command == "diagnose":
            findings, ignored = apply_ignores(
                diagnose(collect_snapshot(client, config.site, include_reservations=True,
                                          include_health=True, include_speedtests=True,
                                          include_events=not args.no_events,
                                          event_since_seconds=args.since), settings),
                settings.ignore)
            emoji = not args.no_emoji and stream_supports_emoji(sys.stdout)
            _say(format_findings(findings, emoji, len(ignored)))
            if args.show_ignored and ignored:
                _say("\n" + format_ignored(ignored))
            return exit_code(findings, args.fail_on)
    except (ConfigError, UniFiAPIError) as e:
        _say(f"ERROR: {e}", file=sys.stderr)
        return EXIT_ERROR
    return 0


if __name__ == "__main__":
    sys.exit(main())
