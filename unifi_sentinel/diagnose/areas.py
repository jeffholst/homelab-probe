"""Areas: the groups of checks ``diagnose --only`` and ``--skip`` choose between.

An area is a set of finding-code prefixes (``port`` and ``link`` make the ``ports`` area), so the area of any
finding follows from its code and a new code needs no extra registration, as long as its prefix is listed here
(``tests/test_diagnose_areas.py`` fails otherwise). A check function can emit codes of more than one area (the
health check also reports devices waiting for adoption), so ``CHECKS`` lists, for each function, every area it
can emit; it runs when any of them is selected and its findings are then filtered to the selected areas.

``needs_for`` is what a selection has to read from the controller, so ``--only ports`` reads no speedtests and
``--skip events`` sends no event-log request. The tests prove that reading less never changes the findings of the
selected areas.
"""

from collections.abc import Callable, Iterable
from typing import List, Optional, Tuple

from ..settings import DiagnoseSettings
from ..snapshot import EventQuery, Needs, Snapshot
from .addresses import _client_ip_findings, _duplicate_ip_findings, _private_mac_findings
from .devices import _offline_device_findings, _overheating_findings, _resource_findings
from .event_checks import _event_findings
from .health import _health_findings, _wan_findings
from .model import CODES, Finding
from .ports import (
    _legacy_unavailable_findings,
    _port_basic_findings,
    _port_health_findings,
    _uplink_speed_findings,
)
from .reserved import _offline_reservation_findings, _pool_findings, _reservation_findings
from .wireless import _wifi_findings

# area -> code prefixes, in the order the areas are listed and reported
AREAS = {
    "devices": ("device", "controller"),
    "health": ("health", "internet"),
    "wan": ("wan",),
    "clients": ("client", "ip"),
    "reservations": ("reservation",),
    "ports": ("port", "link"),
    "wifi": ("wifi",),
    "events": ("event",),
}
AREA_NAMES = tuple(AREAS)
_AREA_OF_PREFIX = {prefix: area for area, prefixes in AREAS.items() for prefix in prefixes}


def area_of(code: str) -> Optional[str]:
    """The area of a finding code (``port.errors`` is ``ports``), or None for a code that is not in any area."""
    return _AREA_OF_PREFIX.get(code.partition(".")[0]) if code else None


def codes_of(area: str) -> List[str]:
    return sorted(code for code in CODES if area_of(code) == area)


Check = Callable[[Snapshot, DiagnoseSettings, Optional[float]], List[Finding]]

# (check, the areas it can emit), in the order the checks run: that order is part of the output
CHECKS: List[Tuple[Check, Tuple[str, ...]]] = [
    (lambda snap, settings, now: _offline_device_findings(snap), ("devices",)),
    (lambda snap, settings, now: _resource_findings(snap, settings), ("devices",)),
    (lambda snap, settings, now: _overheating_findings(snap), ("devices",)),
    (lambda snap, settings, now: _health_findings(snap, settings), ("health", "devices")),
    (lambda snap, settings, now: _wan_findings(snap, settings), ("wan",)),
    (lambda snap, settings, now: _client_ip_findings(snap), ("clients",)),
    (lambda snap, settings, now: _reservation_findings(snap), ("reservations",)),
    (lambda snap, settings, now: _pool_findings(snap), ("reservations",)),
    (lambda snap, settings, now: _offline_reservation_findings(snap, settings, now), ("reservations",)),
    (lambda snap, settings, now: _private_mac_findings(snap), ("reservations", "clients")),
    (lambda snap, settings, now: _duplicate_ip_findings(snap), ("clients", "reservations")),
    (lambda snap, settings, now: _legacy_unavailable_findings(snap), ("devices",)),
    (lambda snap, settings, now: _port_basic_findings(snap, settings), ("ports",)),
    (lambda snap, settings, now: _port_health_findings(snap, settings), ("ports",)),
    (lambda snap, settings, now: _uplink_speed_findings(snap), ("ports",)),
    (lambda snap, settings, now: _wifi_findings(snap, settings), ("wifi",)),
    (lambda snap, settings, now: _event_findings(snap, settings), ("events",)),
]


def parse_areas(values: Iterable[str]) -> Tuple[List[str], List[str]]:
    """``(areas, unknown)`` from the values of a repeatable, comma-separated option, in the order given,
    without repeats. Names are compared without regard to case or surrounding spaces."""
    areas: List[str] = []
    unknown: List[str] = []
    for value in values:
        for name in value.split(","):
            name = name.strip().lower()
            if not name:
                continue
            if name not in AREAS:
                unknown.append(name)
            elif name not in areas:
                areas.append(name)
    return areas, unknown


def needs_for(areas: Optional[Iterable[str]], since_seconds: int) -> Needs:
    """What the selected areas (None: all of them) read from the controller."""
    chosen = set(AREA_NAMES if areas is None else areas)
    return Needs(
        devices=bool(chosen & {"devices", "clients", "reservations", "ports", "wifi", "events"}),
        clients=bool(chosen & {"clients", "reservations"}),
        reservations=bool(chosen & {"reservations", "events"}),      # an IP conflict names who holds the reservation
        health=bool(chosen & {"health", "wan", "devices"}),
        speedtests="wan" in chosen,
        events=EventQuery(since_seconds) if "events" in chosen else None,
        device_extras=bool(chosen & {"devices", "ports"}),
        device_stats="devices" in chosen,
        legacy_devices=bool(chosen & {"devices", "ports", "wifi"}),
        legacy_clients=bool(chosen & {"clients", "wifi"}),
    )
