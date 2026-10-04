"""Documents: what a command knows, as data, before any of it is printed.

A command used to build, render and print in one handler. A *document* is the part in the middle that a command
line, a script and (later) an API all want: the **dict that ``--json`` prints** (with its ``version``, when the
command has one) and the **warnings** of the read that produced it. The text renderer renders from that dict,
``--json`` is ``json.dumps`` of it, and nothing here prints.

The pattern for a command ``x``:

* ``build_x(snapshot, params, settings)`` in its own module is a pure function over a ``Snapshot``;
* ``x_document(client, site, params, settings)`` here reads the snapshot with the command's ``Needs`` (a constant
  here, so a caller cannot read more or less than the command does) inside ``logs.collect_warnings``, and returns
  a ``Document``;
* the handler in ``commands.py`` parses the arguments, calls ``x_document``, renders ``document.data`` and prints.

``echo`` says what happens to the warnings besides being returned: with ``echo=True`` (the command line) they are
also shown as ``Warning: ...`` as they always were; with ``echo=False`` (an API) they are only in the document and
in a DEBUG record. This module imports only the standard library and this package.
"""

import datetime
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import logs
from .audit import AUDIT_AREAS, audit
from .client import UniFiAPIError, UniFiClient
from .client_view import build_client_detail, client_data, find_clients
from .diagnose import apply_ignores, findings_document
from .doctor import Check
from .doctor import to_dict as doctor_dict
from .events import DEFAULT_LIMIT, DEFAULT_SINCE, events_data, fetch_events, make_filter, parse_duration
from .firewall import build_firewall
from .new_clients import new_clients_data
from .new_clients import report as new_clients_report
from .query import query_data, query_rows
from .settings import DiagnoseSettings
from .snapshot import EventQuery, Needs, collect_event_snapshot, collect_snapshot, extend_snapshot
from .topology import JSON_VERSION as TOPOLOGY_JSON_VERSION
from .topology import build_topology
from .wan import DEFAULT_DAYS, build_wan
from .wan import JSON_VERSION as WAN_JSON_VERSION
from .wifi import DEFAULT_MIN_SIGNAL, build_wifi
from .wifi import JSON_VERSION as WIFI_JSON_VERSION


@dataclass(frozen=True)
class Document:
    """``data`` is exactly what ``--json`` prints (JSON-serializable: a dict, or a list for the commands whose
    ``--json`` is a bare array); ``warnings`` are the messages of the degraded reads behind it, in the order they
    were issued. ``meta`` holds what the text view needs and ``--json`` does not print (a note that the limit cut
    the list); it is not part of the JSON."""

    name: str
    data: Any
    warnings: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        """The text ``--json`` prints."""
        return json.dumps(self.data, indent=2)


@dataclass(frozen=True)
class FirewallDocument(Document):
    """Firewall JSON data plus the zone names needed only by its text renderer."""

    zone_names: Dict[str, str] = field(default_factory=dict)


# -- wan ----------------------------------------------------------------------------------------------

WAN_NEEDS = Needs(health=True, speedtests=True)


def wan_document(client: UniFiClient, site: str, days: int = DEFAULT_DAYS,
                 settings: Optional[DiagnoseSettings] = None, echo: bool = True,
                 now_ms: Optional[int] = None) -> Document:
    """Internet health: read what ``wan`` needs and build its report."""
    with logs.collect_warnings(quiet=not echo) as warnings:
        snap = collect_snapshot(client, site, WAN_NEEDS)
        report = build_wan(snap, days, settings, now_ms)
    return Document("wan", {"version": WAN_JSON_VERSION, **report}, [logs.scrub(w) for w in warnings])


# -- wifi ---------------------------------------------------------------------------------------------

WIFI_NEEDS = Needs(neighbors=True)


def wifi_document(client: UniFiClient, site: str, min_signal: float = DEFAULT_MIN_SIGNAL, band: str = "",
                  ap: str = "", echo: bool = True) -> Document:
    """Radios, neighboring networks and the channel plan."""
    with logs.collect_warnings(quiet=not echo) as warnings:
        snap = collect_snapshot(client, site, WIFI_NEEDS)
        report = build_wifi(snap, min_signal, band, ap)
    return Document("wifi", {"version": WIFI_JSON_VERSION, **report}, [logs.scrub(w) for w in warnings])


# -- topology -----------------------------------------------------------------------------------------

TOPOLOGY_NEEDS = Needs()


def topology_document(client: UniFiClient, site: str, settings: Optional[DiagnoseSettings] = None,
                      with_clients: bool = False, echo: bool = True) -> Document:
    """The uplink tree, with the findings of ``diagnose`` that flag a device."""
    with logs.collect_warnings(quiet=not echo) as warnings:
        snap = collect_snapshot(client, site, TOPOLOGY_NEEDS)
        tree = build_topology(snap, settings, with_clients=with_clients)
    return Document("topology", {"version": TOPOLOGY_JSON_VERSION, **tree}, [logs.scrub(w) for w in warnings])


# -- firewall -----------------------------------------------------------------------------------------

FIREWALL_NEEDS = Needs(firewall=True, reservations=True)


def firewall_document(client: UniFiClient, site: str, show_all: bool = False, search: str = "",
                      echo: bool = True) -> FirewallDocument:
    """Zone-based firewall policies, zones, the zone matrix and port forwards, with their findings."""
    with logs.collect_warnings(quiet=not echo) as warnings:
        snap = collect_snapshot(client, site, FIREWALL_NEEDS)
        report = build_firewall(snap, show_all, search)
    zone_names = report.pop("_zone_names")
    return FirewallDocument("firewall", report, [logs.scrub(w) for w in warnings], zone_names=zone_names)


# -- audit --------------------------------------------------------------------------------------------

AUDIT_NEEDS = Needs(offline=True, wlans=True, legacy_devices=False, device_extras=False)


def audit_document(client: UniFiClient, site: str, settings: Optional[DiagnoseSettings] = None,
                   show_ignored: bool = False, echo: bool = True,
                   today: Optional[datetime.date] = None) -> Document:
    """Configuration findings after the ignore list (``today`` decides which rules have expired)."""
    settings = settings or DiagnoseSettings()
    with logs.collect_warnings(quiet=not echo) as warnings:
        snap = collect_snapshot(client, site, AUDIT_NEEDS)
        findings, ignored = apply_ignores(audit(snap), settings.ignore, today)
    return Document("audit", findings_document(findings, ignored, show_ignored, AUDIT_AREAS),
                    [logs.scrub(w) for w in warnings])


# -- events -----------------------------------------------------------------------------------------

def events_document(client: UniFiClient, site: str, wanted: EventQuery, who: str = "", device: str = "",
                    event: str = "", limit: int = DEFAULT_LIMIT, summary: bool = False,
                    echo: bool = True) -> Document:
    """Events from the log (the one read-only POST) filtered by who they are about. ``meta`` has ``more`` (the limit
    cut the list) and ``cap_truncated`` (the read hit its cap), which the text view mentions."""
    with logs.collect_warnings(quiet=not echo) as warnings:
        snap = collect_event_snapshot(client, site, wanted)
        events, more = fetch_events(snap, predicate=make_filter(who, device, event),
                                    limit=0 if summary else limit)       # a summary counts the whole window
    return Document("events", events_data(events, more, summary), [logs.scrub(w) for w in warnings],
                    {"more": more, "cap_truncated": snap.events_truncated})


# -- client -----------------------------------------------------------------------------------------

# Look the client up in the cheap data first (the devices, the connected clients and the client history); the rest
# is read only for a client that matched.
CLIENT_LOOKUP_NEEDS = Needs(offline=True, device_extras=False, legacy_devices=False)


def client_document(client: UniFiClient, site: str, query: str, settings: Optional[DiagnoseSettings] = None,
                    since: int = parse_duration(DEFAULT_SINCE), events: bool = True, echo: bool = True) -> Document:
    """The detail of the one client that ``query`` names. When it names none or several, ``data`` is empty and
    ``meta["matches"]`` has the candidates (the command lists them and exits with 4)."""
    with logs.collect_warnings(quiet=not echo) as warnings:
        snap = collect_snapshot(client, site, CLIENT_LOOKUP_NEEDS)
        matches = find_clients(snap, query)
        data: Dict[str, Any] = {}
        if len(matches) == 1:
            extend_snapshot(client, snap, Needs(reservations=True, groups=True, device_extras=True,
                                                legacy_devices=True, events=EventQuery(since) if events else None))
            data = client_data(build_client_detail(snap, matches[0], settings))
    return Document("client", data, [logs.scrub(w) for w in warnings], {"matches": matches})


# -- query and new-clients --------------------------------------------------------------------------

def query_needs(kind: str, include_offline: bool = False, network: Optional[str] = None) -> Needs:
    """What ``query KIND`` reads: networks and Wi-Fi networks only the configuration, the rest the inventory."""
    if kind in ("networks", "wlans"):
        return Needs(networks=True, wlans=kind == "wlans", devices=False, clients=False,
                     device_extras=False, legacy_devices=False)
    return Needs(offline=include_offline, reservations=kind == "reservations", networks=network is not None)


def query_document(client: UniFiClient, site: str, kind: str = "all", search: str = "",
                   include_offline: bool = False, switch: str = "", down: bool = False, errors: bool = False,
                   offline: bool = False, settings: Optional[DiagnoseSettings] = None,
                   network: Optional[str] = None, ssid: Optional[str] = None, ap: Optional[str] = None,
                   echo: bool = True) -> Document:
    """Rows of ``kind`` after the filters. The whole answer is that data, so a failed read of it raises instead of
    giving an empty list that would look like "there are none"."""
    with logs.collect_warnings(quiet=not echo) as warnings:
        snap = collect_snapshot(client, site, query_needs(kind, include_offline, network))
        if kind == "networks" and not snap.networks_available:
            raise UniFiAPIError("no networks were returned (legacy rest/networkconf could not be read)")
        if kind == "wlans" and snap.wlans is None:
            raise UniFiAPIError("the Wi-Fi networks could not be read (legacy rest/wlanconf); see the warning above")
        if any(value is not None for value in (network, ssid, ap)) and not snap.legacy_clients_available:
            raise UniFiAPIError("--network, --ssid and --ap need the connected-client details (legacy stat/sta), "
                                "which could not be read; see the warning above")
        offline_days = settings.reserved_offline_warn_days if settings is not None and offline else None
        rows = query_rows(snap, kind, search, include_offline, switch, down, errors, offline_days,
                          network or "", ssid or "", ap or "")
    return Document("query", query_data(rows, kind, offline), [logs.scrub(w) for w in warnings],
                    {"kind": kind, "offline": offline})


NEW_CLIENTS_NEEDS = Needs(groups=True, users_required=True)


def new_clients_document(client: UniFiClient, site: str, search: str = "", echo: bool = True) -> Document:
    """Known clients that are in no client group."""
    with logs.collect_warnings(quiet=not echo) as warnings:
        snap = collect_snapshot(client, site, NEW_CLIENTS_NEEDS)
        rows = new_clients_report(snap, search)
    return Document("new-clients", new_clients_data(rows), [logs.scrub(w) for w in warnings])


# -- info ---------------------------------------------------------------------------------------------

def info_document(client: UniFiClient) -> Document:
    """The controller's application info and its sites, as read (names are raw: a renderer cleans them)."""
    data = {"application": client.info(),
            "sites": [{"name": s.get("name"), "ref": s.get("internalReference"), "id": s.get("id")}
                      for s in client.sites()]}
    return Document("info", data)


# -- doctor -------------------------------------------------------------------------------------------

def doctor_document(checks: List[Check]) -> Document:
    """The checks of ``doctor`` as its ``--json`` document. ``doctor`` reads no snapshot, so it has no warnings."""
    return Document("doctor", doctor_dict(checks))
