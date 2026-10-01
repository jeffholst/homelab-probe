"""Data layer: one consistent read of the controller, independent of output format."""

import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .client import UniFiAPIError, UniFiClient
from .util import printable

EVENT_PAGE_SIZE = 500   # events requested per system-log page
MAX_EVENTS = 20_000     # never read more than this many events in one run


def warn(msg: str) -> None:
    print(f"Warning: {printable(msg)}", file=sys.stderr)


@dataclass
class Snapshot:
    site: Dict[str, Any]
    devices: List[Dict[str, Any]]
    clients: List[Dict[str, Any]]
    # Legacy data supplies switch/port mapping and counters; empty if unavailable.
    legacy_devices: List[Dict[str, Any]] = field(default_factory=list)
    legacy_clients: List[Dict[str, Any]] = field(default_factory=list)
    all_users: List[Dict[str, Any]] = field(default_factory=list)
    # Integration API per-device detail and latest statistics, keyed by device id.
    device_details: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    device_stats: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Legacy rest/networkconf. Client records reference these ids, which differ
    # from the Integration API network UUIDs.
    networks: List[Dict[str, Any]] = field(default_factory=list)
    # Client group definitions (legacy v2 network-members-groups): id, name, members.
    client_groups: Optional[List[Dict[str, Any]]] = None
    # Legacy stat/health: one entry per subsystem (wlan, lan, wan, www, vpn).
    health: List[Dict[str, Any]] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
    events_truncated: bool = False
    event_window_seconds: int = 0     # how far back the events reach (0: events not collected)
    # Speedtest history (v2 speedtest), oldest first: time (ms), download_mbps, upload_mbps, latency_ms.
    speedtests: List[Dict[str, Any]] = field(default_factory=list)
    # Neighboring Wi-Fi networks seen by our APs (legacy stat/rogueap): one row per (BSSID, observing AP).
    neighbors: List[Dict[str, Any]] = field(default_factory=list)
    events_available: bool = False    # True when the event log was requested and could be read
    neighbors_available: bool = True


def _legacy_or_empty(client: UniFiClient, site_ref: str, resource: str) -> List[Dict[str, Any]]:
    try:
        return client.legacy_stat(site_ref, resource)
    except UniFiAPIError as e:
        impact = (
            "offline clients, reservations, and client-group history were skipped"
            if resource == "alluser"
            else "port mapping will be incomplete"
        )
        warn(f"legacy stat/{resource} unavailable; {impact}: {e}")
        return []


def _legacy_health_or_empty(client: UniFiClient, site_ref: str) -> List[Dict[str, Any]]:
    try:
        return client.legacy_stat(site_ref, "health")
    except UniFiAPIError as e:
        warn(f"legacy stat/health unavailable, controller health and WAN checks were skipped: {e}")
        return []


def _speedtests_or_empty(client: UniFiClient, site_ref: str) -> List[Dict[str, Any]]:
    try:
        tests = client.legacy_v2(site_ref, "speedtest")
    except UniFiAPIError as e:
        warn(f"speedtest history unavailable, speedtest results were skipped: {e}")
        return []
    def sort_time(test: Dict[str, Any]) -> float:
        value = test.get("time")
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0

    return sorted((t for t in tests if isinstance(t, dict)), key=sort_time)


def _neighbors_or_empty(client: UniFiClient, site_ref: str) -> tuple[List[Dict[str, Any]], bool]:
    try:
        return [n for n in client.legacy_stat(site_ref, "rogueap") if isinstance(n, dict)], True
    except UniFiAPIError as e:
        warn(f"neighboring networks unavailable; neighbor-based channel comparisons were skipped: {e}")
        return [], False


def _device_extras(client: UniFiClient, site_id: str, devices: List[Dict[str, Any]]):
    details: Dict[str, Dict[str, Any]] = {}
    stats: Dict[str, Dict[str, Any]] = {}
    failed = 0
    for d in devices:
        try:
            details[d["id"]] = client.device(site_id, d["id"])
            stats[d["id"]] = client.device_statistics(site_id, d["id"])
        except UniFiAPIError:
            failed += 1  # e.g. offline devices may have no statistics
    if failed:
        warn(f"detail/statistics unavailable for {failed} device(s)")
    return details, stats


def _legacy_rest_or_empty(client: UniFiClient, site_ref: str, resource: str) -> List[Dict[str, Any]]:
    try:
        return client.legacy_rest(site_ref, resource)
    except UniFiAPIError as e:
        warn(f"legacy rest/{resource} unavailable, network names may be missing: {e}")
        return []


def _legacy_v2_or_empty(
    client: UniFiClient, site_ref: str, resource: str
) -> Optional[List[Dict[str, Any]]]:
    try:
        return client.legacy_v2(site_ref, resource)
    except UniFiAPIError as e:
        warn(
            f"legacy v2 {resource} unavailable; membership cannot be validated against "
            f"deleted groups, so raw group IDs will be trusted: {e}"
        )
        return None


DEFAULT_EVENT_SECONDS = 86400


@dataclass(frozen=True)
class EventQuery:
    """What to read from the event log: how far back, and the filters the server applies."""

    since_seconds: int = DEFAULT_EVENT_SECONDS
    categories: Tuple[str, ...] = ()
    severities: Tuple[str, ...] = ()
    search: str = ""


@dataclass(frozen=True)
class Needs:
    """What a command needs from the controller. A command declares exactly this, so nothing is read
    that its output does not use (the event log, in particular, is a POST and is never read unless asked).

    ``offline``, ``reservations`` and ``groups`` all need the legacy ``stat/alluser`` list;
    ``reservations`` also reads the network configuration (names, VLANs) and ``groups`` the client
    group definitions. ``health`` is ``stat/health`` (for ``diagnose`` and ``wan``), ``speedtests``
    the speedtest history, ``neighbors`` the neighboring Wi-Fi networks, ``events`` the event log
    (None: not read).

    Degradation policy: required data fails the command, optional data warns and carries on.
    Optional is every legacy read, including ``alluser``; a command whose answer would be wrong
    without ``alluser`` (``new-clients``, ``snapshot`` and ``diff``) sets ``users_required`` and
    fails with exit code 3 instead of printing a misleading result.
    """

    offline: bool = False
    reservations: bool = False
    groups: bool = False
    health: bool = False
    speedtests: bool = False
    neighbors: bool = False
    events: Optional[EventQuery] = None
    users_required: bool = False


def _events_or_empty(
    client: UniFiClient,
    site_ref: str,
    wanted: EventQuery,
    now_ms: Optional[int] = None,
) -> tuple[List[Dict[str, Any]], bool, bool]:
    """``(events, truncated, available)``; ``available`` is False when the log could not be read."""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    query: Dict[str, Any] = {
        "timestampFrom": now - wanted.since_seconds * 1000,
        "timestampTo": now,
        "pageSize": EVENT_PAGE_SIZE,
    }
    if wanted.categories:
        query["categories"] = [c.upper() for c in wanted.categories]
    if wanted.severities:
        query["severities"] = [s.upper() for s in wanted.severities]
    if wanted.search:
        query["searchText"] = wanted.search

    events: List[Dict[str, Any]] = []
    page = 0
    total_pages = 0
    try:
        while len(events) < MAX_EVENTS:
            body = client.system_log(site_ref, {**query, "pageNumber": page})
            data = body["data"]
            total_pages = int(body.get("total_page_count") or 0)
            events.extend(data[:MAX_EVENTS - len(events)])
            page += 1
            if not data or page >= total_pages:
                return events, False, True
        return events, page < total_pages, True
    except UniFiAPIError as e:
        warn(f"event log unavailable; event history was skipped: {e}")
        return [], False, False


def describe_snapshot(snap: "Snapshot") -> str:
    """What a snapshot holds, as 'read 4 devices, 2 connected clients, ...' (empty parts left out).
    Used by --verbose to show what was actually read."""
    parts = [(snap.devices, "devices"), (snap.clients, "connected clients"),
             (snap.legacy_devices, "legacy devices"), (snap.legacy_clients, "legacy clients"),
             (snap.all_users, "known clients"), (snap.networks, "networks"),
             (snap.client_groups or [], "client groups"), (snap.health, "health subsystems"),
             (snap.speedtests, "speedtests"), (snap.neighbors, "neighbor rows"), (snap.events, "events")]
    found = [f"{len(items)} {name}" for items, name in parts if items]
    return "read " + (", ".join(found) if found else "nothing")


def collect_snapshot(
    client: UniFiClient,
    site: str,
    needs: Needs = Needs(),  # noqa: B008  (frozen, so one shared default is safe)
    now_ms: Optional[int] = None,
) -> Snapshot:
    """One read of the controller: the devices and connected clients always, and whatever ``needs``
    adds (see ``Needs``). ``now_ms`` fixes the clock of the event window, for tests."""
    site_info = client.resolve_site(site)
    site_ref = site_info.get("internalReference") or site
    events, events_truncated, events_available = _events_or_empty(
        client, site_ref, needs.events, now_ms
    ) if needs.events is not None else ([], False, False)
    neighbors, neighbors_available = (
        _neighbors_or_empty(client, site_ref) if needs.neighbors else ([], False)
    )
    devices = client.devices(site_info["id"])
    details, stats = _device_extras(client, site_info["id"], devices)
    if not (needs.groups or needs.offline or needs.reservations):
        all_users: List[Dict[str, Any]] = []
    elif needs.users_required:
        all_users = client.legacy_stat(site_ref, "alluser")
    else:
        all_users = _legacy_or_empty(client, site_ref, "alluser")
    snap = Snapshot(
        site=site_info,
        devices=devices,
        device_details=details,
        device_stats=stats,
        clients=client.clients(site_info["id"]),
        legacy_devices=_legacy_or_empty(client, site_ref, "device"),
        legacy_clients=_legacy_or_empty(client, site_ref, "sta"),
        all_users=all_users,
        networks=(
            _legacy_rest_or_empty(client, site_ref, "networkconf") if needs.reservations else []
        ),
        health=_legacy_health_or_empty(client, site_ref) if needs.health else [],
        speedtests=_speedtests_or_empty(client, site_ref) if needs.speedtests else [],
        neighbors=neighbors,
        neighbors_available=neighbors_available,
        client_groups=(
            _legacy_v2_or_empty(client, site_ref, "network-members-groups") if needs.groups else []
        ),
        events=events,
        events_truncated=events_truncated,
        event_window_seconds=needs.events.since_seconds if needs.events is not None else 0,
        events_available=events_available,
    )
    if client.trace is not None:
        client.trace(describe_snapshot(snap))
    return snap


def collect_event_snapshot(
    client: UniFiClient,
    site: str,
    wanted: EventQuery,
    now_ms: Optional[int] = None,
) -> Snapshot:
    """Only the event log (no devices or clients), for the `events` command."""
    site_info = client.resolve_site(site)
    site_ref = site_info.get("internalReference") or site
    events, truncated, available = _events_or_empty(client, site_ref, wanted, now_ms)
    snap = Snapshot(
        site=site_info,
        devices=[],
        clients=[],
        events=events,
        events_truncated=truncated,
        event_window_seconds=wanted.since_seconds,
        events_available=available,
    )
    if client.trace is not None:
        client.trace(describe_snapshot(snap))
    return snap
