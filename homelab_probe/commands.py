"""The commands: for each one its arguments, its checks and its handler, in one place.

``cli.main`` looks the command up in ``COMMANDS`` and calls its handler with a ``Context``; adding a
command means adding a section here and a row to ``COMMANDS``. Fetching stays in ``snapshot.py`` (each
handler declares what it reads with a ``Needs``), analysis and rendering in the feature modules.
"""

import argparse
import datetime
import getpass
import logging
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import accounts, logs, setup
from . import config as config_module
from .backup import BackupError
from .client import UniFiAPIError, UniFiClient
from .client_view import render_candidates, render_detail
from .completion import SHELLS
from .completion import script as completion_script
from .config import Config, ConfigError, parse_audit_log_files, parse_audit_log_mb
from .demo import demo_config
from .diagnose import (
    AREA_NAMES,
    CRITICAL,
    INFO,
    WARNING,
    Finding,
    exit_code,
    findings_from_document,
    parse_areas,
    render_findings,
    stream_supports_emoji,
)
from .doctor import Options as DoctorOptions
from .doctor import check_controller, exit_failed, run_checks
from .doctor import render as render_doctor
from .documents import (
    Document,
    audit_document,
    client_document,
    diagnose_document,
    diff_document,
    doctor_document,
    events_document,
    export_document,
    firewall_document,
    info_document,
    new_clients_document,
    query_document,
    snapshot_document,
    topology_document,
    wan_document,
    wifi_document,
)
from .events import DEFAULT_LIMIT, DEFAULT_SINCE, SEVERITIES, parse_duration, render_events_text
from .export import EXPORT_FORMATS, JSON_FILENAME, write_export
from .firewall import render_text as render_firewall
from .history import (
    DEFAULT_DIR,
    label_for,
    list_snapshots,
    load_snapshot,
    prune,
    render_diff,
    resolve,
    save_snapshot,
    site_dir,
    site_snapshots,
)
from .new_clients import render_table as render_new_clients
from .notify import STATE_DIR, destinations_from_config, process
from .present import Presentation
from .query import format_table, render_csv, render_table
from .restore import recover as recover_restore
from .settings import DiagnoseSettings, expired_rules, load_settings, server_settings_path
from .snapshot import EventQuery, warn
from .topology import render_text as render_topology
from .topology_graph import GRAPH_RENDERERS, TOPOLOGY_FORMATS
from .util import check_bind, parse_allowed_host, parse_forwarded_ips, printable, safe_output
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
    for commands that want them), the controller client and the presentation policy (``present``: whether and how
    this run may style its output, see ``present.py``; an explicitly plain policy, never styled whatever is installed,
    when a caller builds a context without one)."""

    args: argparse.Namespace
    config: Config
    settings: Optional[DiagnoseSettings]
    client: UniFiClient
    present: Presentation = field(default_factory=lambda: Presentation(plain=True))


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
    document = export_document(ctx.client, ctx.config.site, ctx.args.include_offline)
    write_export(document.data, document.meta["rows"], document.meta["switches"], document.meta["site"],
                 document.meta["connected"], ctx.args.output_dir, ctx.args.format)
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
    parser.add_argument("--format", choices=TOPOLOGY_FORMATS, default="text",
                        help="text: the tree (default); mermaid: a Mermaid flowchart; dot: a Graphviz digraph. "
                             "The graph formats cannot be combined with --json")
    parser.add_argument("--json", action="store_true", help="Output nested JSON instead of a tree")
    parser.add_argument("--config", type=Path, metavar="FILE",
                        help="TOML file with diagnose thresholds and ignore list "
                             "(default: ./hlp.toml if present)")
    parser.add_argument("--no-emoji", action="store_true",
                        help="Use ASCII drawing and text severity labels (automatic when output is "
                             "not a UTF-8 terminal); no effect on the graph formats")


def _check_topology(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.json and args.format != "text":
        parser.error(f"--json and --format {args.format} cannot be combined: --json prints the tree as data, "
                     "--format draws it as a graph")


def _run_topology(ctx: Context) -> int:
    args = ctx.args
    document = topology_document(ctx.client, ctx.config.site, ctx.settings, args.clients)
    emoji = not args.no_emoji and stream_supports_emoji(sys.stdout)
    if args.json:
        say(document.to_json())
    elif args.format == "text":
        say(render_topology(document.data, emoji, args.clients))
    else:
        say(GRAPH_RENDERERS[args.format](document.data))
    return 0


# -- snapshot and diff --------------------------------------------------------------------------------

def _add_snapshot(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-o", "--output", type=Path, metavar="FILE",
                        help="Write to this file instead of a timestamped one in --dir")
    parser.add_argument("--dir", type=Path, default=None, metavar="DIR",
                        help=f"Directory for timestamped snapshots (default: ./{DEFAULT_DIR}/<site id>/, one "
                             "directory per site)")
    parser.add_argument("--keep", type=_positive, metavar="N",
                        help="Afterwards delete the oldest snapshots in --dir (of this site, by default), keeping "
                             "the newest N")
    parser.add_argument("--force", action="store_true", help="Allow -o to replace an existing file")


def _run_snapshot(ctx: Context) -> int:
    args = ctx.args
    record = snapshot_document(ctx.client, ctx.config.site).data
    base = Path(DEFAULT_DIR)
    directory = args.dir if args.dir is not None else site_dir(base, record["site"])
    path = save_snapshot(record, args.output, directory, args.force)
    say(f"Saved {len(record['devices'])} devices, {len(record['clients'])} clients and "
        f"{len(record['reservations'])} reservations to {path}")
    if args.keep:
        # In the default place the snapshots counted are this site's (also the older ones saved straight into
        # snapshots/); a directory given with --dir is counted as it is.
        files = site_snapshots(base, record["site"]) if args.dir is None else None
        for gone in prune(directory, args.keep, protect=path, files=files):
            say(f"Removed old snapshot {gone}")
    return 0


def _add_diff(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("refs", nargs="*", metavar="OLD [NEW]",
                        help="Snapshot files (or names inside --dir). One file is compared with the live "
                             "network; none uses the newest saved snapshot")
    parser.add_argument("--dir", type=Path, default=None, metavar="DIR",
                        help=f"Snapshot directory (default: this site's directory in ./{DEFAULT_DIR}/; finding it "
                             "asks the controller which site is meant, so give --dir to work offline)")
    parser.add_argument("--last-two", action="store_true",
                        help="Compare the two newest saved snapshots (no controller needed)")
    parser.add_argument("--all", action="store_true",
                        help="List every client connect/disconnect and IP change instead of the first "
                             "few")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of text")


def _check_diff(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if len(args.refs) > 2 or (args.last_two and args.refs):
        parser.error("give at most two snapshots, and none with --last-two")


def _snapshot_places(ctx: Context) -> Tuple[Path, Sequence[Path], Callable[[], List[Path]]]:
    """(the directory to look in, other places for a name, the saved snapshots oldest first) for ``diff``. With
    ``--dir`` that directory is everything. Without it the place is this site's own directory (plus the older
    snapshots saved straight into ``snapshots/`` that name this site), found by asking the controller which site is
    meant; that is done only when a snapshot has to be looked for."""
    args = ctx.args
    if args.dir is not None:
        return args.dir, (), lambda: list_snapshots(args.dir)
    base = Path(DEFAULT_DIR)
    try:
        site = ctx.client.resolve_site(ctx.config.site)
    except UniFiAPIError as e:
        raise ConfigError(f"{e}; the snapshots are kept per site, so the controller is asked which site is meant "
                          "(give --dir DIR to work without it)") from e
    return site_dir(base, site), (base,), lambda: site_snapshots(base, site)


def _run_diff(ctx: Context) -> int:
    args = ctx.args
    new_path: Optional[Path]
    names = [ref for ref in args.refs if not Path(ref).is_file()]
    if args.last_two or not args.refs or names:
        directory, also, saved_list = _snapshot_places(ctx)
    else:
        directory, also, saved_list = Path(DEFAULT_DIR), (), list
    if args.last_two:
        saved = saved_list()
        if len(saved) < 2:
            raise ConfigError(f"need at least two saved snapshots in {directory}/ (found {len(saved)}); "
                              "run `snapshot` first")
        old_path, new_path = saved[-2], saved[-1]
    else:
        if args.refs:
            old_path = resolve(args.refs[0], directory, also)
        else:
            saved = saved_list()
            if not saved:
                raise ConfigError(f"no saved snapshots in {directory}/; run `snapshot` first")
            old_path = saved[-1]
        new_path = resolve(args.refs[1], directory, also) if len(args.refs) == 2 else None

    old = load_snapshot(old_path)
    new = load_snapshot(new_path) if new_path else None
    new_label = label_for(new, new_path.name) if new is not None and new_path else "the network right now"
    document = diff_document(ctx.client, ctx.config.site, old, new)
    say(document.to_json() if args.json
        else render_diff(document.data, label_for(old, old_path.name), new_label, args.all))
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


# -- web-user ---------------------------------------------------------------------------------------

WEB_USER_ACTIONS = ("add", "list", "set-role", "disable", "enable", "delete", "reset-password")


def _username(text: str) -> str:
    try:
        return accounts.check_username(text)
    except accounts.AccountError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def _add_web_user(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("action", choices=WEB_USER_ACTIONS, help="What to do")
    parser.add_argument("name", nargs="?", type=_username, metavar="NAME",
                        help="The user (everything except list)")
    parser.add_argument("--role", choices=accounts.ROLES,
                        help="add: the role of the new user (default viewer); set-role: the new role")
    parser.add_argument("--password-stdin", action="store_true",
                        help="add and reset-password: read the password from one line of standard input instead of "
                             "asking for it (a password is never an argument)")
    parser.add_argument("--data-dir", type=Path, default=Path("."), metavar="DIR",
                        help="Where users.json and audit.log are kept (default: the current directory)")


def _check_web_user(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.action == "list":
        if args.name is not None:
            parser.error("list takes no NAME")
    elif args.name is None:
        parser.error(f"{args.action} needs the NAME of a user")
    if args.role is not None and args.action not in ("add", "set-role"):
        parser.error("--role only applies to add and set-role")
    if args.action == "set-role" and args.role is None:
        parser.error("set-role needs --role viewer or --role admin")
    if args.password_stdin and args.action not in ("add", "reset-password"):
        parser.error("--password-stdin only applies to add and reset-password")


def _read_password(args: argparse.Namespace) -> str:
    """The password from one line of standard input, or asked for twice with nothing echoed."""
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
        if not password:
            raise ConfigError("no password was given on standard input")
        return password
    first = getpass.getpass("Password: ")
    if getpass.getpass("Repeat the password: ") != first:
        raise ConfigError("the two passwords differ; nothing was changed")
    return first


def _actor() -> str:
    try:
        return f"cli:{getpass.getuser()}"
    except (KeyError, OSError, ImportError):
        return "cli"


def _list_users(store: accounts.AccountStore) -> None:
    users = store.users()
    if not users:
        say("No users yet. Add one with: hlp web-user add NAME --role admin")
        return
    say(format_table([{"Username": u.username, "Role": u.role, "Status": "disabled" if u.disabled else "active",
                       "Created": u.created_at, "Last login": u.last_login or ""} for u in users],
                     ["Username", "Role", "Status", "Created", "Last login"]))


def _run_web_user(args: argparse.Namespace) -> int:
    store = accounts.AccountStore(args.data_dir)
    name = args.name
    try:
        if args.action == "list":
            _list_users(store)
            return 0
        with accounts.AuditLog(args.data_dir, parse_audit_log_mb(os.environ.get("AUDIT_LOG_MAX_MB")),
                               parse_audit_log_files(os.environ.get("AUDIT_LOG_FILES"))) as audit:
            actor = _actor()
            def record_change(event: str, include_role: bool = False) -> Callable[[accounts.User], None]:
                def record(user: accounts.User) -> None:
                    fields = {"user": user.username}
                    if include_role:
                        fields["role"] = user.role
                    audit.write(event, actor, **fields)
                return record

            if args.action == "add":
                user = store.add(name, args.role or "viewer", _read_password(args),
                                 on_change=record_change("user.added", include_role=True))
                say(f"Added the {user.role} {user.username}.")
            elif args.action == "set-role":
                user = store.set_role(name, args.role, on_change=record_change("user.role_changed", include_role=True))
                say(f"{user.username} is now a {user.role}.")
            elif args.action in ("disable", "enable"):
                user = store.set_disabled(name, args.action == "disable",
                                          on_change=record_change("user.disabled" if args.action == "disable"
                                                                  else "user.enabled"))
                say(f"{user.username} is {'disabled' if user.disabled else 'enabled'}.")
            elif args.action == "delete":
                user = store.remove(name, on_change=record_change("user.deleted", include_role=True))
                say(f"Deleted {user.username}.")
            else:                                                       # reset-password
                user = store.reset_password(name, _read_password(args),
                                            on_change=record_change("user.password_reset"))
                say(f"The password of {user.username} was changed.")
    except accounts.AccountError as e:
        raise ConfigError(str(e)) from e
    return 0


# -- init -------------------------------------------------------------------------------------------

def _add_init(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dir", type=Path, default=Path("."), metavar="DIR",
                        help="Where to write .env, hlp.toml and snapshots/ (default: the current directory)")
    parser.add_argument("--url", metavar="URL", help="The controller's address, such as https://192.168.1.1")
    parser.add_argument("--site", metavar="NAME", help="The site to use (default: default)")
    parser.add_argument("--verify", metavar="true|false|FILE",
                        help="Check the controller's certificate (true, the default), do not (false), or against this "
                             "CA file")
    parser.add_argument("--api-key-stdin", action="store_true",
                        help="Read the API key from one line of standard input and ask nothing else (needs --url); "
                             "a key is never an argument")
    parser.add_argument("--no-input", action="store_true",
                        help="Ask nothing: use only the options and what the existing .env holds")
    parser.add_argument("--force", action="store_true",
                        help="Do not ask before replacing the settings in an existing .env")
    parser.add_argument("--check", action="store_true",
                        help="Afterwards read the controller once to check the address, the key and the site")


def _check_init(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.api_key_stdin and not args.url:
        parser.error("--api-key-stdin needs --url (it asks nothing else)")
    if args.no_input and not args.url and not args.api_key_stdin:
        parser.error("--no-input needs --url, and the API key on standard input with --api-key-stdin")


def _ask(prompt: str, default: str = "") -> str:
    try:
        answer = input(f"{prompt}" + (f" [{default}]" if default else "") + ": ").strip()
    except EOFError:
        raise ConfigError("the input ended before the questions were answered; use the options and "
                          "--no-input") from None
    return answer or default


def _confirm(question: str, default: bool = False) -> bool:
    answer = _ask(f"{question} [{'Y/n' if default else 'y/N'}]").lower()
    return default if not answer else answer.startswith("y")


def _ask_valid(name: str, prompt: str, values: Dict[str, Optional[str]], default: str = "") -> str:
    """Ask until ``name`` is acceptable (three tries), showing why it was not."""
    for _ in range(3):
        values[name] = _ask(prompt, default)
        problem = setup.validate_field(values, name)
        if not problem:
            return str(values[name])
        say(f"  {problem}", file=sys.stderr)
    raise ConfigError(f"{name} is still not acceptable; run `hlp init` again when you have it")


def _verify_choice(args: argparse.Namespace, existing: Dict[str, str], interactive: bool) -> Optional[str]:
    """The ``UNIFI_VERIFY_SSL`` to write: from ``--verify``, else asked, else what is there."""
    if args.verify is not None:
        return args.verify
    if not interactive:
        return existing.get("UNIFI_VERIFY_SSL")
    answer = _ask("Check the controller's certificate? yes, no, or the path of its CA file",
                  "yes" if existing.get("UNIFI_VERIFY_SSL", "true").lower() in ("true", "1", "yes", "on") else "no")
    if answer.lower() in ("yes", "y", "true"):
        return "true"
    if answer.lower() in ("no", "n", "false"):
        return "false"
    return answer


def _read_api_key(args: argparse.Namespace, existing: Dict[str, str], interactive: bool) -> str:
    if args.api_key_stdin:
        key = sys.stdin.readline().rstrip("\r\n")
        if not key:
            raise ConfigError("no API key was given on standard input")
        return key
    kept = existing.get("UNIFI_API_KEY", "")
    if not interactive:
        if not kept or kept == setup.PLACEHOLDER_KEY:
            raise ConfigError("there is no API key to keep: give it on standard input with --api-key-stdin")
        return kept
    key = getpass.getpass("API key (Settings > Control Plane > Integrations; nothing is shown as you type"
                          + ("; empty keeps the current one" if kept else "") + "): ").strip()
    return key or kept


def _run_init(args: argparse.Namespace) -> int:
    directory: Path = args.dir
    env_path = directory / setup.ENV_FILE
    try:
        existing = setup.existing_values(setup.read_existing_env(env_path))
    except setup.SetupError as e:
        raise ConfigError(str(e)) from e
    interactive = not (args.no_input or args.api_key_stdin)
    values: Dict[str, Optional[str]] = {}
    if interactive:
        values["UNIFI_URL"] = _ask_valid("UNIFI_URL", "Controller address (for example https://192.168.1.1)", values,
                                         args.url or existing.get("UNIFI_URL", ""))
        values["UNIFI_SITE_ID"] = _ask("Site (name, reference or UUID)", args.site or existing.get("UNIFI_SITE_ID",
                                                                                                 "default"))
    else:
        values["UNIFI_URL"] = args.url or existing.get("UNIFI_URL")
        values["UNIFI_SITE_ID"] = args.site or existing.get("UNIFI_SITE_ID") or "default"
    values["UNIFI_API_KEY"] = _read_api_key(args, existing, interactive)
    verify = _verify_choice(args, existing, interactive)
    values["UNIFI_VERIFY_SSL"] = verify
    if verify and verify.lower() in ("false", "0", "no", "off") and interactive and not args.force:
        say("Without certificate checking the API key is sent to whatever answers at that address.", file=sys.stderr)
        if not _confirm("Turn certificate checking off anyway?"):
            raise ConfigError("nothing was written; run `hlp init` again (a self-signed controller certificate can be "
                              "trusted with UNIFI_VERIFY_SSL=/path/to/its-certificate.pem instead)")
    problems = setup.validate_values(values)
    if problems:
        raise ConfigError("; ".join(f"{name}: {message}" if name else message for name, message in problems))
    if env_path.is_file() and interactive and not args.force:
        say(f"{env_path} exists: its UNIFI_* settings will be replaced and everything else kept; the old file is saved "
            f"as {setup.ENV_FILE}.bak.", file=sys.stderr)
        if not _confirm("Replace them?"):
            raise ConfigError(f"nothing was written; {env_path} is as it was")
    try:
        steps = setup.apply(values, directory)
    except setup.SetupError as e:
        raise ConfigError(str(e)) from e
    for step in steps:
        say(step.message)
    if args.check:
        checks = check_controller(config_module.build_config(values))
        say("\n" + render_doctor(checks))
        return EXIT_ERROR if exit_failed(checks) else 0
    say("Next: `hlp doctor` checks the setup and `hlp diagnose` looks at your network. "
        "Nothing was sent to the controller.")
    return 0


# -- serve ------------------------------------------------------------------------------------------

DEFAULT_PORT = 8787


def _port(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"invalid port {text!r}: use a whole number from 1 to 65535")
    return value


def _allowed_host(text: str) -> str:
    try:
        return parse_allowed_host(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def _forwarded_ips(text: str) -> str:
    try:
        return parse_forwarded_ips(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def _add_serve(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default="127.0.0.1", metavar="ADDRESS",
                        help="The address to listen on (default 127.0.0.1, this machine only). Another address "
                             "makes the server reachable from the network: logins then need TLS in front "
                             "(see --forwarded-allow-ips)")
    parser.add_argument("--allowed-host", action="append", default=[], type=_allowed_host, metavar="NAME",
                        help="A name the server is reached by, such as hlp.lan or 192.168.1.5 (repeatable). A "
                             "request with another Host header is refused. Required with --host 0.0.0.0 or ::; "
                             "wildcards are not accepted")
    parser.add_argument("--forwarded-allow-ips", type=_forwarded_ips, metavar="IPS",
                        help="Believe X-Forwarded-For and X-Forwarded-Proto from these reverse proxies "
                             "(addresses or networks, comma-separated); by default from none. * and 0.0.0.0/0 are "
                             "not accepted")
    parser.add_argument("--allow-public-controller", action="store_true",
                        help="Let the guided setup (a server with no settings) connect to a controller on a public "
                             "address; by default it only connects to addresses on your own network")
    parser.add_argument("--read-only", action="store_true",
                        help="Write no file on this machine: the settings editor and the setup's files answer 403 "
                             "(logging in and the audit log still work)")
    parser.add_argument("--scheduler", action="store_true",
                        help="Run diagnose (and notify) and take snapshots on a timer inside the server "
                             "(SCHEDULER_DIAGNOSE_MINUTES, SCHEDULER_SNAPSHOT_HOURS, SCHEDULER_SNAPSHOT_KEEP); it "
                             "writes files, so not with --read-only")
    parser.add_argument("--port", type=_port, default=DEFAULT_PORT, metavar="PORT",
                        help=f"The port to listen on (default {DEFAULT_PORT})")
    parser.add_argument("--data-dir", type=Path, default=Path("."), metavar="DIR",
                        help="Where the server keeps its own files (default: the current directory)")
    parser.add_argument("--config", type=Path, metavar="FILE",
                        help="TOML file with diagnose thresholds and ignore list "
                             "(default: ./hlp.toml if present)")


def _check_serve(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.scheduler and args.read_only:
        parser.error("--scheduler writes files (snapshots, the notification state) and cannot be combined with "
                     "--read-only")
    try:
        check_bind(args.host, args.allowed_host)
    except ValueError as e:
        parser.error(str(e))


def apply_logging(config: Config, verbose_flag: bool) -> None:
    """Hide the secrets of ``config`` from every log record and apply its ``LOG_LEVEL`` and ``LOG_FORMAT``."""
    logs.register_secrets(*config.secret_values())
    if config.log_format or config.log_level:
        logs.configure(config.log_format or "cli", "DEBUG" if verbose_flag else config.log_level or "WARNING")


def finish_interrupted_restore(directory: Path, settings_file: Path) -> None:
    """Before the settings are read: complete or undo a restore a crash interrupted (``restore.recover``); a state that
    can be neither is a start-up error, never a silent mixture."""
    try:
        outcome = recover_restore(directory, settings_file)
    except BackupError as e:
        raise ConfigError(str(e)) from e
    if outcome:
        warn(f"An unfinished restore of a backup was found at start-up and was {outcome.replace('_', ' ')}.")


def _run_serve(args: argparse.Namespace) -> int:
    """Runs before any ``.env`` is read (``Command.run_local``): a server whose settings are missing is not an error
    but the guided setup, so it resolves its own configuration (``server.runner.resolve_config``)."""
    try:
        from .server.runner import resolve_config, run  # only here: the command line never needs the web extra
    except ImportError as e:
        raise ConfigError("`hlp serve` needs the web extra: uv run --extra web hlp.py serve "
                          "(from the project checkout), or python -m pip install 'homelab-probe[web]' "
                          f"(missing: {e.name or 'a module'})") from e

    def overridden(config: Config) -> Config:
        if args.timeout is not None:
            config = replace(config, timeout=args.timeout)          # the command line beats .env
        if args.parallel is not None:
            config = replace(config, parallel=args.parallel)
        return config

    def reload() -> Config:
        """The configuration after the guided setup saved the settings: read again the way it was at start."""
        fresh, again, _ = resolve_config(args.env_file, args.data_dir, args.site, args.allow_public_controller)
        if again is not None:
            raise ConfigError("the settings are still missing")
        return overridden(fresh)

    if not args.demo:
        finish_interrupted_restore(args.data_dir, server_settings_path(args.config, args.data_dir))
    setup_state = None
    if args.demo:
        config = demo_config(args.site or "default")
        say("Demo mode: synthetic data, no controller is contacted.", file=sys.stderr)
    else:
        config, setup_state, problem = resolve_config(args.env_file, args.data_dir, args.site,
                                                      args.allow_public_controller)
        if setup_state is not None:
            say(f"Not configured ({problem.split('. ')[0].rstrip('.')}).", file=sys.stderr)   # the first sentence
    config = overridden(config)
    if not args.demo:
        apply_logging(config, args.verbose)
        for message in config.warnings:
            warn(message)
    # A bad settings file fails now, not at the first request. It is the file the server will use: the one named with
    # --config, else hlp.toml in the data directory (a demo has a data directory of its own and no file in it).
    settings_file = server_settings_path(args.config, args.data_dir)
    if args.config is not None or (not args.demo and settings_file.is_file()):
        load_settings(settings_file)
    host = f"[{args.host}]" if ":" in args.host else args.host
    say(f"Serving on http://{host}:{args.port} (Ctrl-C to stop)."
        + ("" if setup_state is not None else " Log in with an account made by `hlp web-user`."), file=sys.stderr)
    run(config, args.host, args.port, args.config, args.data_dir, demo=args.demo,
        announce=lambda message: say(message, file=sys.stderr), allowed=args.allowed_host,
        forwarded_allow_ips=args.forwarded_allow_ips, setup=setup_state, reload=reload,
        read_only=args.read_only, scheduler=args.scheduler,
        env_named=args.env_file is not None or bool(os.environ.get(config_module.ENV_FILE_VAR, "").strip()))
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
                        help=f"with --notify: where reported findings are remembered (default: "
                             f"{STATE_DIR}/<site id>/notify-state.json, one file per site)")


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


def _notify(findings: List[Any], config: Any, settings: Any, args: argparse.Namespace, site: Dict[str, str]) -> bool:
    """Run the notification step of ``diagnose --notify``. True when a message had to be sent and
    every destination failed (the caller turns that into exit code 3 if nothing else applies). ``site`` is the site
    that was read: its findings are remembered apart from every other site's. The step is ``notify.process``, which
    the scheduler of ``serve`` uses too, under one lock on the state."""
    outcome = process(findings, config=config, settings=settings, site=site, state_file=args.notify_state,
                      minimum=args.notify_min or WARNING, areas=args.areas, redact=args.notify_redact,
                      dry_run=args.notify_dry_run, baseline_only=args.notify_baseline,
                      report=lambda message: say(message, file=sys.stderr), warn=warn)
    return outcome.undelivered


WATCH_SLEEP = time.sleep          # looked up when used, so a test can replace the wait between passes


def _diagnose_once(
    ctx: Context, settings: DiagnoseSettings, today: Optional[datetime.date] = None
) -> Tuple[List[Finding], Document, bool]:
    """One pass of the checks: the findings that remain, the document they are in (which also says what the ignore
    list suppressed) and whether every optional read worked."""
    args = ctx.args
    document = diagnose_document(ctx.client, ctx.config.site, settings, args.areas, args.since, args.show_ignored,
                                 today=today)
    return findings_from_document(document.data), document, document.meta["complete"]


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
    findings, document, complete = _diagnose_once(ctx, settings, today)
    if args.json:
        say(document.to_json())
    else:
        emoji = not args.no_emoji and stream_supports_emoji(sys.stdout)
        checked_note = areas is not None and bool(args.only or args.skip)    # --no-events alone: as it always did
        say(render_findings(document.data, emoji, args.show_ignored, checked_note))
    if args.watch is not None:
        return _watch_diagnose(ctx, settings, findings, complete)
    code = exit_code(findings, args.fail_on)
    if args.notify and _notify(findings, ctx.config, settings, args, document.meta["site"]) and code == 0:
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
            _add_topology, _run_topology, validate=_check_topology,
            wants_settings=_always),
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
    Command("init", "Guided first-time setup: write .env, hlp.toml and snapshots/ for you", _add_init, _not_run,
            validate=_check_init, run_local=_run_init),
    Command("completion", "Print a shell completion script (bash, zsh or fish)", _add_completion, _not_run,
            run_local=_run_completion),
    Command("serve", "Serve the read-only web API on this machine (needs the web extra)", _add_serve, _not_run,
            validate=_check_serve, run_local=_run_serve),
    Command("web-user", "Manage the accounts of the web interface: users, roles and passwords", _add_web_user,
            _not_run, validate=_check_web_user, run_local=_run_web_user),
    Command("diagnose", "Run read-only health checks (offline devices, port errors, ...)", _add_diagnose,
            _run_diagnose, validate=_check_diagnose, wants_settings=_always, prepare=_prepare_diagnose),
    Command("info", "Show controller version and available sites", _add_info, _run_info),
]
COMMANDS_BY_NAME = {command.name: command for command in COMMANDS}
