"""The commands: for each one its arguments, its checks and its handler, in one place.

``cli.main`` looks the command up in ``COMMANDS`` and calls its handler with a ``Context``; adding a
command means adding a section here and a row to ``COMMANDS``. Fetching stays in ``snapshot.py`` (each
handler declares what it reads with a ``Needs``), analysis and rendering in the feature modules.
"""

import argparse
import datetime
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import logs
from .client import UniFiAPIError, UniFiClient
from .client_view import render_candidates, render_detail
from .completion import SHELLS
from .completion import script as completion_script
from .config import Config, ConfigError
from .diagnose import (
    AREA_NAMES,
    CRITICAL,
    INFO,
    WARNING,
    apply_ignores,
    diagnose,
    exit_code,
    findings_from_document,
    findings_json,
    format_findings,
    format_ignored,
    needs_for,
    parse_areas,
    render_findings,
    stream_supports_emoji,
)
from .doctor import Options as DoctorOptions
from .doctor import exit_failed, run_checks
from .doctor import render as render_doctor
from .documents import (
    audit_document,
    client_document,
    doctor_document,
    events_document,
    firewall_document,
    info_document,
    new_clients_document,
    query_document,
    topology_document,
    wan_document,
    wifi_document,
)
from .events import DEFAULT_LIMIT, DEFAULT_SINCE, SEVERITIES, parse_duration, render_events_text
from .export import EXPORT_FORMATS, JSON_FILENAME, run_export
from .firewall import render_text as render_firewall
from .history import (
    DEFAULT_DIR,
    SnapshotRecord,
    capture,
    diff_json,
    diff_snapshots,
    label_for,
    list_snapshots,
    load_snapshot,
    prune,
    render_diff,
    resolve,
    save_snapshot,
)
from .new_clients import render_table as render_new_clients
from .notify import (
    DEFAULT_STATE_FILE,
    baseline,
    destinations_from_config,
    load_state,
    plan,
    render_text,
    save_state,
    send,
)
from .query import render_csv, render_table
from .settings import DiagnoseSettings, expired_rules
from .snapshot import EventQuery, Needs, collect_snapshot, warn
from .topology import render_text as render_topology
from .util import printable, safe_output
from .wan import DEFAULT_DAYS
from .wan import render_text as render_wan
from .watch import MAX_SECONDS, MIN_SECONDS
from .watch import changes as watch_changes
from .watch import start as watch_start
from .wifi import DEFAULT_MIN_SIGNAL, parse_band
from .wifi import render_text as render_wifi

_log = logging.getLogger(__name__)

EXIT_ERROR = 3  # config or connection failure; 1 and 2 are reserved for diagnose findings
EXIT_NO_MATCH = 4  # `client` found no client, or several (it lists them)
EXIT_USAGE = 64


@dataclass(frozen=True)
class Context:
    """What a handler gets: the parsed arguments, the configuration, the diagnose settings (loaded only
    for commands that want them) and the controller client."""

    args: argparse.Namespace
    config: Config
    settings: Optional[DiagnoseSettings]
    client: UniFiClient


def _no_check(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """The default ``Command.validate``: nothing to reject beyond what argparse already did."""


def _no_prepare(args: argparse.Namespace, config: Config) -> None:
    """The default ``Command.prepare``: nothing to check before the controller is contacted."""


def _never(args: argparse.Namespace) -> bool:
    return False


@dataclass(frozen=True)
class Command:
    """One subcommand. ``validate`` rejects bad argument combinations (a usage error, exit 64), ``wants_settings``
    says whether the diagnose settings file is loaded first (so a bad file fails before any request), ``prepare``
    checks anything else that must fail before the controller is contacted, and ``run`` returns the exit code."""

    name: str
    help: str
    add_arguments: Callable[[argparse.ArgumentParser], None]
    run: Callable[[Context], int]
    validate: Callable[[argparse.ArgumentParser, argparse.Namespace], None] = _no_check
    wants_settings: Callable[[argparse.Namespace], bool] = _never
    prepare: Callable[[argparse.Namespace, Config], None] = _no_prepare
    # A command that needs neither the configuration nor the controller (``completion``) is run with just its
    # arguments, before any ``.env`` is read; ``run`` is then never called.
    run_local: Optional[Callable[[argparse.Namespace], int]] = None


def _not_run(ctx: "Context") -> int:
    raise AssertionError("this command runs without a controller (Command.run_local)")


def say(text: Any = "", file: Any = None) -> None:
    """Print ``text`` with control characters removed (names come from devices on the network,
    and an escape sequence in one must not reach the terminal). Line breaks are kept."""
    print(safe_output(str(text)), file=file)


def verbose(message: str, event: str = "run.settings") -> None:
    """A ``--verbose`` line: a DEBUG record that the command-line log format shows as ``[verbose] ...``."""
    logs.verbose(message, event)


def _duration(text: str) -> int:
    try:
        return parse_duration(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def _watch_interval(text: str) -> int:
    try:
        seconds = int(text)
    except ValueError:
        seconds = 0
    if not MIN_SECONDS <= seconds <= MAX_SECONDS:
        raise argparse.ArgumentTypeError(
            f"invalid value {text!r}: use a whole number of seconds from {MIN_SECONDS} to {MAX_SECONDS}")
    return seconds


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


# -- info -------------------------------------------------------------------------------------------

def _add_info(parser: argparse.ArgumentParser) -> None:
    """`info` takes no arguments."""


def _run_info(ctx: Context) -> int:
    document = info_document(ctx.client)
    say(f"Application: {document.data['application']}")
    for s in document.data["sites"]:
        say(f"Site: {printable(s['name'])} ref={printable(s['ref'])} id={printable(s['id'])}")
    return 0


# -- export -----------------------------------------------------------------------------------------

def _add_export(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("."),
                        help="Directory for the files (default: current directory)")
    parser.add_argument("--format", choices=EXPORT_FORMATS, default="csv",
                        help="csv: unifi_clients.csv and one CSV per switch (default); json: one file, "
                             f"{JSON_FILENAME}, with the same data")
    parser.add_argument("--include-offline", action="store_true",
                        help="Also list previously seen clients that are not connected")


def _run_export(ctx: Context) -> int:
    snap = collect_snapshot(ctx.client, ctx.config.site, Needs(offline=ctx.args.include_offline))
    run_export(snap, ctx.args.output_dir, ctx.args.format)
    return 0


# -- query ------------------------------------------------------------------------------------------

def _add_query(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("kind", nargs="?", default="all",
                        choices=["all", "devices", "clients", "reservations", "ports", "networks", "wlans"])
    parser.add_argument("-s", "--search", default="",
                        help="Case-insensitive substring match on any field")
    parser.add_argument("--include-offline", action="store_true",
                        help="Also list previously seen clients that are not connected")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of a table")
    parser.add_argument("--csv", action="store_true",
                        help="Output CSV instead of a table (the columns of --json; text that a spreadsheet would "
                             "run as a formula gets a leading ')")
    parser.add_argument("--switch",
                        help="ports only: switch name (case-insensitive substring)")
    parser.add_argument("--down", action="store_true", help="ports only: only ports that are down")
    parser.add_argument("--errors", action="store_true",
                        help="ports only: only ports with rx/tx errors")
    parser.add_argument("--network", metavar="NAME",
                        help="clients only: connected clients on a network (name, case-insensitive substring)")
    parser.add_argument("--ssid", metavar="NAME",
                        help="clients only: connected clients on a Wi-Fi network (SSID, case-insensitive substring)")
    parser.add_argument("--ap", metavar="NAME",
                        help="clients only: connected clients on an access point (name, case-insensitive substring)")
    parser.add_argument("--offline", action="store_true",
                        help="reservations only: only clients offline long enough for `diagnose` to report "
                             "them (reserved_offline_warn_days), or never seen")
    parser.add_argument("--config", type=Path, metavar="FILE",
                        help="reservations --offline only: TOML file with the threshold "
                             "(default: ./hlp.toml if present)")


def _check_query(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.json and args.csv:
        parser.error("--json and --csv cannot be combined")
    if args.kind != "ports" and (args.switch is not None or args.down or args.errors):
        parser.error("--switch, --down and --errors only apply to 'query ports'")
    filters = {"--network": args.network, "--ssid": args.ssid, "--ap": args.ap}
    if args.kind != "clients" and any(value is not None for value in filters.values()):
        parser.error("--network, --ssid and --ap only apply to 'query clients'")
    for flag, value in filters.items():
        if value is not None and not value.strip():
            parser.error(f"{flag} needs a name (an empty one would match everything)")
    if args.kind != "reservations" and (args.offline or args.config is not None):
        parser.error("--offline and --config only apply to 'query reservations'")
    if args.config is not None and not args.offline:
        parser.error("--config only applies with --offline")


def _run_query(ctx: Context) -> int:
    args = ctx.args
    document = query_document(ctx.client, ctx.config.site, args.kind, args.search, args.include_offline,
                              args.switch or "", args.down, args.errors, args.offline, ctx.settings,
                              args.network, args.ssid, args.ap)
    if args.json:
        say(document.to_json())
    elif args.csv:
        say(render_csv(document.data, args.kind, args.offline))
    else:
        say(render_table(document.data, args.kind, args.offline))
    return 0


# -- new-clients ------------------------------------------------------------------------------------

def _add_new_clients(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-s", "--search", default="",
                        help="Case-insensitive substring match on any field")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of a table")


def _run_new_clients(ctx: Context) -> int:
    document = new_clients_document(ctx.client, ctx.config.site, ctx.args.search)
    say(document.to_json() if ctx.args.json else render_new_clients(document.data))
    return 0


# -- events -----------------------------------------------------------------------------------------

def _add_events(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--since", type=_duration, default=_duration(DEFAULT_SINCE), metavar="DURATION",
                        help=f"How far back to look, e.g. 90m, 24h, 7d, 2w (default: {DEFAULT_SINCE})")
    parser.add_argument("--category", action="append", default=[], metavar="NAME",
                        help="Only this category, e.g. CLIENT_DEVICES, UNIFI_DEVICES, INTERNET_AND_WAN, "
                             "AUDIT (repeatable)")
    parser.add_argument("--severity", action="append", default=[], type=str.lower, choices=SEVERITIES,
                        help="Only this severity (repeatable)")
    parser.add_argument("--event", default="", metavar="TEXT",
                        help="Only event types containing TEXT, e.g. roam, disconnected, ip_conflict")
    parser.add_argument("--client", default="", metavar="NAME|MAC|IP",
                        help="Only events about this client (name, hostname, IP or part of a MAC)")
    parser.add_argument("--device", default="", metavar="NAME|IP",
                        help="Only events about this UniFi device (e.g. an AP or switch)")
    parser.add_argument("-s", "--search", default="", help="Text search done by the controller")
    parser.add_argument("--limit", type=_non_negative, default=DEFAULT_LIMIT,
                        help=f"Most events to show, newest first (default {DEFAULT_LIMIT}; 0 for all)")
    parser.add_argument("--summary", action="store_true",
                        help="Counts by type and the noisiest clients/devices over the whole window "
                             "(ignores --limit) instead of a list")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of a table")


def _run_events(ctx: Context) -> int:
    args = ctx.args
    document = events_document(
        ctx.client, ctx.config.site,
        EventQuery(args.since, tuple(args.category or ()), tuple(args.severity or ()), args.search),
        args.client, args.device, args.event, args.limit, args.summary)
    say(document.to_json() if args.json
        else render_events_text(document.data, args.summary, document.meta["more"], document.meta["cap_truncated"]))
    return 0


# -- client -----------------------------------------------------------------------------------------

def _add_client(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("query", help="Client name, MAC address or IP address")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of text")
    parser.add_argument("--config", type=Path, metavar="FILE",
                        help="TOML file with diagnose thresholds and ignore list "
                             "(default: ./hlp.toml if present)")
    parser.add_argument("--no-events", action="store_true",
                        help="Skip the recent-events section, which otherwise sends the one approved "
                             "read-only event-log query")
    parser.add_argument("--since", type=_duration, default=_duration(DEFAULT_SINCE), metavar="DURATION",
                        help=f"How far back the recent events reach, e.g. 12h, 7d (default: {DEFAULT_SINCE})")
    parser.add_argument("--no-emoji", action="store_true",
                        help="Use text severity labels (automatic when output is not a UTF-8 terminal)")


def _run_client(ctx: Context) -> int:
    args = ctx.args
    document = client_document(ctx.client, ctx.config.site, args.query, ctx.settings, args.since,
                               not args.no_events)
    if not document.data:
        say(render_candidates(args.query, document.meta["matches"]), file=sys.stderr)
        return EXIT_NO_MATCH
    emoji = not args.no_emoji and stream_supports_emoji(sys.stdout)
    say(document.to_json() if args.json else render_detail(document.data, emoji))
    return 0


# -- topology ---------------------------------------------------------------------------------------

def _add_topology(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--clients", action="store_true",
                        help="Also list the wired clients under each device")
    parser.add_argument("--json", action="store_true", help="Output nested JSON instead of a tree")
    parser.add_argument("--config", type=Path, metavar="FILE",
                        help="TOML file with diagnose thresholds and ignore list "
                             "(default: ./hlp.toml if present)")
    parser.add_argument("--no-emoji", action="store_true",
                        help="Use ASCII drawing and text severity labels (automatic when output is "
                             "not a UTF-8 terminal)")


def _run_topology(ctx: Context) -> int:
    args = ctx.args
    document = topology_document(ctx.client, ctx.config.site, ctx.settings, args.clients)
    emoji = not args.no_emoji and stream_supports_emoji(sys.stdout)
    say(document.to_json() if args.json else render_topology(document.data, emoji, args.clients))
    return 0


# -- snapshot and diff --------------------------------------------------------------------------------

# `snapshot` and `diff` record offline clients, reservations and groups, and must not save or compare a
# record that silently lacks them, so the client history is required.
INVENTORY_NEEDS = Needs(reservations=True, groups=True, users_required=True)


def _live_inventory(client: UniFiClient, config: Any) -> SnapshotRecord:
    """The network as it is right now, as a snapshot record."""
    try:
        version = str(client.info().get("applicationVersion") or "")
    except UniFiAPIError:
        version = ""
    return capture(collect_snapshot(client, config.site, INVENTORY_NEEDS), version)


def _add_snapshot(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-o", "--output", type=Path, metavar="FILE",
                        help="Write to this file instead of a timestamped one in --dir")
    parser.add_argument("--dir", type=Path, default=Path(DEFAULT_DIR), metavar="DIR",
                        help=f"Directory for timestamped snapshots (default: ./{DEFAULT_DIR}/)")
    parser.add_argument("--keep", type=_positive, metavar="N",
                        help="Afterwards delete the oldest snapshots in --dir, keeping the newest N")
    parser.add_argument("--force", action="store_true", help="Allow -o to replace an existing file")


def _run_snapshot(ctx: Context) -> int:
    args = ctx.args
    record = _live_inventory(ctx.client, ctx.config)
    path = save_snapshot(record, args.output, args.dir, args.force)
    say(f"Saved {len(record['devices'])} devices, {len(record['clients'])} clients and "
        f"{len(record['reservations'])} reservations to {path}")
    if args.keep:
        for gone in prune(args.dir, args.keep, protect=path):
            say(f"Removed old snapshot {gone}")
    return 0


def _add_diff(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("refs", nargs="*", metavar="OLD [NEW]",
                        help="Snapshot files (or names inside --dir). One file is compared with the live "
                             "network; none uses the newest saved snapshot")
    parser.add_argument("--dir", type=Path, default=Path(DEFAULT_DIR), metavar="DIR",
                        help=f"Snapshot directory (default: ./{DEFAULT_DIR}/)")
    parser.add_argument("--last-two", action="store_true",
                        help="Compare the two newest saved snapshots (no controller needed)")
    parser.add_argument("--all", action="store_true",
                        help="List every client connect/disconnect and IP change instead of the first "
                             "few")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of text")


def _check_diff(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if len(args.refs) > 2 or (args.last_two and args.refs):
        parser.error("give at most two snapshots, and none with --last-two")


def _run_diff(ctx: Context) -> int:
    args = ctx.args
    new_path: Optional[Path]
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
        new, new_label = _live_inventory(ctx.client, ctx.config), "the network right now"
    result = diff_snapshots(old, new)
    say(diff_json(result) if args.json
        else render_diff(result, label_for(old, old_path.name), new_label, args.all))
    return 0


# -- wifi -------------------------------------------------------------------------------------------

def _add_wifi(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--band", type=_band, default=None, metavar="2.4|5|6", help="Only this band")
    parser.add_argument("--ap", default="", metavar="NAME",
                        help="Only this AP (name or part of it), and only neighbors it hears")
    parser.add_argument("--min-signal", type=_signal, default=DEFAULT_MIN_SIGNAL, metavar="DBM",
                        help=f"Neighbors weaker than this are counted but not named or compared "
                             f"(default {DEFAULT_MIN_SIGNAL})")
    parser.add_argument("--all", action="store_true",
                        help="Name every strong neighbor on a channel, not just the first few")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of text")


def _run_wifi(ctx: Context) -> int:
    args = ctx.args
    document = wifi_document(ctx.client, ctx.config.site, args.min_signal, args.band or "", args.ap)
    say(document.to_json() if args.json else render_wifi(document.data, args.all, args.ap))
    return 0


# -- wan --------------------------------------------------------------------------------------------

def _add_wan(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--days", type=_positive, default=DEFAULT_DAYS, metavar="N",
                        help=f"How many days of speedtests to summarize (default: {DEFAULT_DAYS})")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of text")
    parser.add_argument("--config", type=Path, metavar="FILE",
                        help="TOML file with the thresholds (default: ./hlp.toml if present)")


def _run_wan(ctx: Context) -> int:
    document = wan_document(ctx.client, ctx.config.site, ctx.args.days, ctx.settings)
    say(document.to_json() if ctx.args.json else render_wan(document.data))
    return 0


# -- firewall ---------------------------------------------------------------------------------------

def _add_firewall(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--all", action="store_true",
                        help="Also list the built-in policies (by default only the ones you defined)")
    parser.add_argument("--zones", action="store_true",
                        help="Also show each zone's networks and the zone matrix")
    parser.add_argument("--search", default="", metavar="TEXT",
                        help="Only policies and port forwards with this text in any column")
    parser.add_argument("--no-emoji", action="store_true",
                        help="Use text severity labels (automatic when output is not a UTF-8 terminal)")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of text")


def _run_firewall(ctx: Context) -> int:
    args = ctx.args
    document = firewall_document(ctx.client, ctx.config.site, args.all, args.search)
    emoji = not args.no_emoji and stream_supports_emoji(sys.stdout)
    say(document.to_json() if args.json else
        render_firewall(document.data, args.zones, emoji, zone_names=document.zone_names))
    return 0


# -- audit ------------------------------------------------------------------------------------------

def _add_audit(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--fail-on", choices=[INFO, WARNING], default=WARNING,
                        help="Lowest severity that gives a non-zero exit code (default: warning)")
    parser.add_argument("--config", type=Path, metavar="FILE",
                        help="TOML file with an ignore list (default: ./hlp.toml if present)")
    parser.add_argument("--show-ignored", action="store_true",
                        help="Also list the findings suppressed by the ignore list")
    parser.add_argument("--no-emoji", action="store_true",
                        help="Use text severity labels (automatic when output is not a UTF-8 terminal)")
    parser.add_argument("--json", action="store_true", help="Print the findings as JSON (with a stable code each)")


def _warn_expired_rules(settings: DiagnoseSettings, today: Optional[datetime.date] = None) -> None:
    """One stderr line per ignore rule whose ``until`` date has passed: its findings are back, and the rule is
    stale. Only a warning: it never changes an exit code or what is on stdout."""
    for rule, until in expired_rules(settings.ignore, today):
        say(f"Warning: the ignore rule for {rule.describe()} expired on {until.isoformat()} and no longer "
            f"applies; delete it or give it a later until date ({printable(rule.reason)})", file=sys.stderr)


def _run_audit(ctx: Context) -> int:
    args = ctx.args
    settings = ctx.settings or DiagnoseSettings()
    today = datetime.date.today()
    _warn_expired_rules(settings, today)
    document = audit_document(ctx.client, ctx.config.site, settings, args.show_ignored, today=today)
    if args.json:
        say(document.to_json())
    else:
        emoji = not args.no_emoji and stream_supports_emoji(sys.stdout)
        say(render_findings(document.data, emoji, args.show_ignored))
    return exit_code(findings_from_document(document.data), args.fail_on)


# -- doctor -----------------------------------------------------------------------------------------

def _add_doctor(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--offline", action="store_true",
                        help="Only check the installation and the settings files; do not contact the controller")
    parser.add_argument("--no-events", action="store_true",
                        help="Skip the event-log check (the one read-only POST)")
    parser.add_argument("--config", type=Path, metavar="FILE",
                        help="TOML settings file to check (default: ./hlp.toml if present)")
    parser.add_argument("--json", action="store_true", help="Print the checks as JSON")


def _run_doctor(args: argparse.Namespace) -> int:
    """Runs without the usual configuration step: a broken setup is what it is there to report, not to stop at."""
    checks = run_checks(DoctorOptions(env_file=args.env_file, site=args.site, timeout=args.timeout,
                                      parallel=args.parallel, config=args.config, offline=args.offline,
                                      events=not args.no_events))
    say(doctor_document(checks).to_json() if args.json else render_doctor(checks))
    return EXIT_ERROR if exit_failed(checks) else 0


# -- completion -------------------------------------------------------------------------------------

def _add_completion(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("shell", choices=SHELLS, help="The shell to print the completion script for")


def _run_completion(args: argparse.Namespace) -> int:
    from .cli import build_parser  # the parser is what the script is generated from

    say(completion_script(args.shell, build_parser()).rstrip("\n"))
    return 0


# -- diagnose ---------------------------------------------------------------------------------------

def _add_diagnose(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--fail-on", choices=[INFO, WARNING, CRITICAL], default=WARNING,
                        help="Lowest severity that gives a non-zero exit code (default: warning); "
                             "critical always exits 2")
    parser.add_argument("--config", type=Path, metavar="FILE",
                        help="TOML file with thresholds and an ignore list "
                             "(default: ./hlp.toml if present)")
    parser.add_argument("--only", action="append", default=[], metavar="AREA[,AREA...]",
                        help=f"Run only the checks of these areas ({', '.join(AREA_NAMES)}); repeatable. "
                             "Only the data they need is read")
    parser.add_argument("--skip", action="append", default=[], metavar="AREA[,AREA...]",
                        help="Run every check except those of these areas (same names); repeatable. "
                             "Skipping events sends no event-log request")
    parser.add_argument("--no-events", action="store_true",
                        help="Skip the event-log checks (repeated disconnects, IP conflicts, ...), "
                             "which otherwise send the one approved read-only POST; same as --skip events")
    parser.add_argument("--since", type=_duration, default=_duration(DEFAULT_SINCE), metavar="DURATION",
                        help=f"How far back the event checks look, e.g. 12h, 7d (default: {DEFAULT_SINCE})")
    parser.add_argument("--show-ignored", action="store_true",
                        help="Also list the findings suppressed by the ignore list")
    parser.add_argument("--no-emoji", action="store_true",
                        help="Use text severity labels (automatic when output is not a UTF-8 terminal)")
    parser.add_argument("--json", action="store_true",
                        help="Print the findings as JSON (with a stable code per check); exit codes are unchanged")
    parser.add_argument("--watch", type=_watch_interval, default=None, metavar="SECONDS",
                        help=f"Run the checks again every SECONDS ({MIN_SECONDS} to {MAX_SECONDS}) and print only "
                             "what changed (new, worse, fixed) until Ctrl-C; not with --json or --notify")
    parser.add_argument("--notify", action="store_true",
                        help="Send a notification (ntfy, a webhook and/or email, set in .env) when findings are new, "
                             "worse or fixed since the last notified run; this is the only thing that sends "
                             "data off this machine")
    parser.add_argument("--notify-min", choices=[INFO, WARNING, CRITICAL], default=None, metavar="SEVERITY",
                        help="with --notify: lowest severity to notify about (default: warning)")
    parser.add_argument("--notify-redact", action="store_true",
                        help="with --notify: send only the generic description of each check, no names, "
                             "addresses or MACs")
    parser.add_argument("--notify-dry-run", action="store_true",
                        help="with --notify: print what would be sent to stderr, send nothing, keep the state")
    parser.add_argument("--notify-baseline", action="store_true",
                        help="with --notify: record the current findings as already reported and send nothing "
                             "(avoids a first message about everything)")
    parser.add_argument("--notify-state", type=Path, default=None, metavar="FILE",
                        help=f"with --notify: where reported findings are remembered (default: {DEFAULT_STATE_FILE})")


def diagnose_areas(args: argparse.Namespace) -> Optional[List[str]]:
    """The areas ``diagnose`` runs, in the canonical order (None: all of them), from ``--only``, ``--skip`` and
    ``--no-events``. Raises ``ValueError`` with the reason when the options cannot be combined."""
    only, unknown_only = parse_areas(args.only)
    skip, unknown_skip = parse_areas(args.skip)
    unknown = unknown_only + unknown_skip
    if unknown:
        raise ValueError(f"unknown area {', '.join(repr(u) for u in unknown)}; the areas are {', '.join(AREA_NAMES)}")
    if args.only and args.skip:
        raise ValueError("--only and --skip cannot be combined")
    if args.only:
        if args.no_events and "events" in only:
            raise ValueError("--no-events contradicts --only events")
        wanted = set(only)
    elif args.skip or args.no_events:
        wanted = set(AREA_NAMES) - set(skip) - ({"events"} if args.no_events else set())
    else:
        return None
    chosen = [a for a in AREA_NAMES if a in wanted]
    if not chosen:
        raise ValueError("no checks are left to run: " + ("--only names no area" if args.only
                                                          else "every area is skipped"))
    return None if len(chosen) == len(AREA_NAMES) else chosen


def _check_diagnose(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    try:
        args.areas = diagnose_areas(args)
    except ValueError as e:
        parser.error(str(e))
    if not args.notify and (args.notify_min or args.notify_redact or args.notify_dry_run or args.notify_baseline
                            or args.notify_state is not None):
        parser.error("the --notify-* options only apply together with --notify")
    if args.notify_dry_run and args.notify_baseline:
        parser.error("--notify-dry-run and --notify-baseline cannot be combined")
    if args.watch is not None and args.json:
        parser.error("--watch prints lines for a person to read and cannot be combined with --json")
    if args.watch is not None and args.notify:
        parser.error("--watch cannot be combined with --notify: run `diagnose --notify` from cron to be told "
                     "about changes without a long-running process")


def _prepare_diagnose(args: argparse.Namespace, config: Config) -> None:
    if args.notify and not args.notify_dry_run and not args.notify_baseline and not destinations_from_config(config):
        raise ConfigError("--notify needs a destination: set NOTIFY_NTFY_URL, NOTIFY_WEBHOOK_URL and/or "
                          "NOTIFY_SMTP_HOST in .env (see https://github.com/jeffholst/homelab-probe/blob/main/"
                          "docs/notifications.md); nothing was sent")


def _notify(findings: List[Any], config: Any, settings: Any, args: argparse.Namespace) -> bool:
    """Run the notification step of ``diagnose --notify``. True when a message had to be sent and
    every destination failed (the caller turns that into exit code 3 if nothing else applies)."""
    state_path = args.notify_state or Path(DEFAULT_STATE_FILE)
    minimum = args.notify_min or WARNING
    state, problem = load_state(state_path)
    if problem:
        warn(problem)
    now = time.time()
    if args.notify_baseline:
        saved = baseline(findings, now, minimum, args.areas, state)
        try:
            save_state(state_path, saved)
        except OSError as e:
            raise ConfigError(
                f"the notification baseline could not be saved to {state_path}: {e.strerror or e}"
            ) from e
        say(f"Notification baseline saved: {len(saved['active'])} current finding(s) count as already reported",
             file=sys.stderr)
        return False
    events, new_state = plan(findings, state, now, minimum, settings.notify_repeat_hours, args.areas)
    if not events:
        if new_state != state:
            try:
                save_state(state_path, new_state)          # e.g. a finding improved but is still reported
            except OSError as e:
                raise ConfigError(
                    f"the notification state could not be saved to {state_path}: {e.strerror or e}"
                ) from e
        say("Notification: nothing new, worse or fixed since the last notified run", file=sys.stderr)
        return False
    if args.notify_dry_run:
        title, body = render_text(events, args.notify_redact)
        say(f"Notification dry run (nothing sent, state unchanged): {title}\n{body}", file=sys.stderr)
        return False
    results = send(destinations_from_config(config), events, args.notify_redact, config.timeout)
    for kind, delivered, reason in results:
        say(f"Notification to {kind}: " + ("sent" if delivered else f"FAILED ({reason})"), file=sys.stderr)
    delivered_somewhere = any(ok for _, ok, _ in results)
    if delivered_somewhere:
        try:
            save_state(state_path, new_state)
        except OSError as e:
            raise ConfigError(f"the notification was sent but its state could not be saved to {state_path}: "
                              f"{e.strerror or e}; it will be sent again next run") from e
    return not delivered_somewhere


WATCH_SLEEP = time.sleep          # looked up when used, so a test can replace the wait between passes


def _diagnose_once(
    ctx: Context, settings: DiagnoseSettings, today: Optional[datetime.date] = None
) -> Tuple[List[Any], List[Any], bool]:
    """One pass of the checks: the findings that remain and those the ignore list suppressed."""
    areas = ctx.args.areas
    snap = collect_snapshot(ctx.client, ctx.config.site, needs_for(areas, ctx.args.since))
    findings, ignored = apply_ignores(diagnose(snap, settings, areas=areas), settings.ignore, today)
    return findings, ignored, not snap.degraded


def _watch_diagnose(ctx: Context, settings: DiagnoseSettings, first: List[Any], first_complete: bool) -> int:
    """Repeat the checks every ``--watch`` seconds and print what changed, until Ctrl-C. Failed or degraded reads
    are retried without changing the last complete state. The exit code is the one the last complete pass gave."""
    args = ctx.args
    findings = first
    state: Optional[Dict[str, Any]] = watch_start(findings, time.time(), args.areas) if first_complete else None
    say(f"Watching every {args.watch} s; only changes are printed (Ctrl-C to stop).", file=sys.stderr)
    try:
        while True:
            WATCH_SLEEP(args.watch)
            try:
                next_findings, _ignored, complete = _diagnose_once(ctx, settings)
            except (UniFiAPIError, OSError) as e:
                reason = str(e) if isinstance(e, UniFiAPIError) else (e.strerror or type(e).__name__)
                logs.log_event(_log, logging.WARNING, "watch.unavailable", "could not read the controller",
                               reason=e.kind if isinstance(e, UniFiAPIError) and e.kind else "error",
                               retry_s=args.watch)
                say(f"{time.strftime('%H:%M:%S')}  could not read the controller ({reason}); "
                    f"trying again in {args.watch} s", file=sys.stderr)
                continue
            logs.log_event(_log, logging.INFO if complete else logging.WARNING,
                           "watch.pass" if complete else "watch.unavailable",
                           "pass finished" if complete else "optional controller data unavailable",
                           findings=len(next_findings), complete=complete, retry_s=args.watch)
            if not complete:
                status = "waiting for a complete baseline" if state is None else "keeping the last complete watch state"
                say(f"{time.strftime('%H:%M:%S')}  optional controller data unavailable; {status} "
                    f"and trying again in {args.watch} s", file=sys.stderr)
                continue
            if state is None:
                findings = next_findings
                state = watch_start(findings, time.time(), args.areas)
                continue
            findings = next_findings
            lines, state = watch_changes(findings, state, time.time(), settings.notify_repeat_hours, args.areas)
            for line in lines:
                say(line)
    except KeyboardInterrupt:
        say("Stopped.", file=sys.stderr)
    return exit_code(findings, args.fail_on)


def _run_diagnose(ctx: Context) -> int:
    args = ctx.args
    settings = ctx.settings or DiagnoseSettings()          # loaded for this command, so never None
    today = datetime.date.today()
    _warn_expired_rules(settings, today)                   # once, also before a --watch loop
    areas = args.areas
    findings, ignored, complete = _diagnose_once(ctx, settings, today)
    if args.json:
        say(findings_json(findings, ignored, args.show_ignored, areas))
    else:
        emoji = not args.no_emoji and stream_supports_emoji(sys.stdout)
        say(format_findings(findings, emoji, len(ignored)))
        if areas is not None and (args.only or args.skip):       # --no-events alone prints what it always did
            say(f"Checked: {', '.join(areas)} (not checked: {', '.join(a for a in AREA_NAMES if a not in areas)})")
        if args.show_ignored and ignored:
            say("\n" + format_ignored(ignored))
    if args.watch is not None:
        return _watch_diagnose(ctx, settings, findings, complete)
    code = exit_code(findings, args.fail_on)
    if args.notify and _notify(findings, ctx.config, settings, args) and code == 0:
        return EXIT_ERROR              # the message could not be delivered and nothing else says so
    return code


# -- the registry -------------------------------------------------------------------------------------

def _always(args: argparse.Namespace) -> bool:
    return True


COMMANDS: List[Command] = [
    Command("export", "Export clients, devices and switch ports to CSV", _add_export, _run_export),
    Command("query", "List and filter devices, clients, reservations, switch ports, networks and Wi-Fi networks",
            _add_query, _run_query,
            validate=_check_query, wants_settings=lambda args: bool(args.offline)),
    Command("new-clients", "List clients that are in no client group (all known clients)", _add_new_clients,
            _run_new_clients),
    Command("events", "Event history from the controller log: disconnects, roams, IP conflicts...", _add_events,
            _run_events),
    Command("client", "Troubleshoot one client: where it attaches, link quality and related findings",
            _add_client, _run_client, wants_settings=_always),
    Command("topology", "Draw the uplink tree: gateway, switches and APs with ports, speeds and problems",
            _add_topology, _run_topology, wants_settings=_always),
    Command("snapshot", "Save the current inventory to a local JSON file, for `diff` later", _add_snapshot,
            _run_snapshot),
    Command("diff", "What changed: compare saved snapshots, or a snapshot against the live network", _add_diff,
            _run_diff, validate=_check_diff),
    Command("wifi", "Wireless report: each AP's radios and a channel plan from the neighboring networks",
            _add_wifi, _run_wifi),
    Command("wan", "Internet health: current state, 24-hour monitoring and speedtest history", _add_wan, _run_wan,
            wants_settings=_always),
    Command("firewall", "Firewall policies, port forwards and the zone matrix, with what looks wrong",
            _add_firewall, _run_firewall),
    Command("audit", "Configuration audit: settings that are probably not what you want", _add_audit, _run_audit,
            wants_settings=_always),
    Command("doctor", "Check the installation and the settings, and that the controller answers", _add_doctor, _not_run,
            run_local=_run_doctor),
    Command("completion", "Print a shell completion script (bash, zsh or fish)", _add_completion, _not_run,
            run_local=_run_completion),
    Command("diagnose", "Run read-only health checks (offline devices, port errors, ...)", _add_diagnose,
            _run_diagnose, validate=_check_diagnose, wants_settings=_always, prepare=_prepare_diagnose),
    Command("info", "Show controller version and available sites", _add_info, _run_info),
]
COMMANDS_BY_NAME = {command.name: command for command in COMMANDS}
