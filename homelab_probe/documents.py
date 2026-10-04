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
from .client import UniFiClient
from .diagnose import apply_ignores, findings_document
from .doctor import Check
from .doctor import to_dict as doctor_dict
from .firewall import build_firewall
from .settings import DiagnoseSettings
from .snapshot import Needs, collect_snapshot
from .topology import JSON_VERSION as TOPOLOGY_JSON_VERSION
from .topology import build_topology
from .wan import DEFAULT_DAYS, build_wan
from .wan import JSON_VERSION as WAN_JSON_VERSION
from .wifi import DEFAULT_MIN_SIGNAL, build_wifi
from .wifi import JSON_VERSION as WIFI_JSON_VERSION


@dataclass(frozen=True)
class Document:
    """``data`` is exactly what ``--json`` prints (a JSON-serializable dict); ``warnings`` are the messages of the
    degraded reads behind it, in the order they were issued."""

    name: str
    data: Dict[str, Any]
    warnings: List[str] = field(default_factory=list)

    def to_json(self) -> str:
        """The text ``--json`` prints."""
        return json.dumps(self.data, indent=2)


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
                      echo: bool = True) -> Document:
    """Zone-based firewall policies, zones, the zone matrix and port forwards, with their findings."""
    with logs.collect_warnings(quiet=not echo) as warnings:
        snap = collect_snapshot(client, site, FIREWALL_NEEDS)
        report = build_firewall(snap, show_all, search)
    return Document("firewall", report, [logs.scrub(w) for w in warnings])


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
