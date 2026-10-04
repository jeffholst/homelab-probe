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

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import logs
from .client import UniFiClient
from .doctor import Check
from .doctor import to_dict as doctor_dict
from .settings import DiagnoseSettings
from .snapshot import Needs, collect_snapshot
from .wan import DEFAULT_DAYS, build_wan
from .wan import JSON_VERSION as WAN_JSON_VERSION


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
