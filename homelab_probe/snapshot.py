"""Data layer: one consistent read of the controller, independent of output format."""

import contextvars
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Dict, List, Optional, Tuple

from .client import UniFiAPIError, UniFiClient
from .logs import warn

EVENT_PAGE_SIZE = 500   # events requested per system-log page
MAX_EVENTS = 20_000     # never read more than this many events in one run


@dataclass
class FirewallData:
    """What the controller says about its firewall, as read. Each part is ``None`` when its read failed
    (a controller on the classic firewall has no zone-based policies, for one), and an empty list when the
    controller answered with nothing. Zone-based: v2 ``firewall-policies``, ``firewall/zone`` and
    ``firewall/zone-matrix``; port forwards: legacy ``rest/portforward``."""

    policies: Optional[List[Dict[str, Any]]] = None
    zones: Optional[List[Dict[str, Any]]] = None
    matrix: Optional[List[Dict[str, Any]]] = None
    port_forwards: Optional[List[Dict[str, Any]]] = None


@dataclass
class Snapshot:
    site: Dict[str, Any]
    devices: List[Dict[str, Any]]
    clients: List[Dict[str, Any]]
    # Legacy data supplies switch/port mapping and counters; empty if unavailable.
    legacy_devices: List[Dict[str, Any]] = field(default_factory=list)
    legacy_devices_available: bool = True
    legacy_clients: List[Dict[str, Any]] = field(default_factory=list)
    legacy_clients_available: bool = True
    all_users: List[Dict[str, Any]] = field(default_factory=list)
    # False when the client history (legacy stat/alluser) was requested and could not be read, so an empty
    # ``all_users`` is "unknown", not "no clients"; True otherwise (also when it was not requested).
    all_users_available: bool = True
    # Integration API per-device detail and latest statistics, keyed by device id.
    device_details: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    device_stats: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Legacy rest/networkconf. Client records reference these ids, which differ
    # from the Integration API network UUIDs.
    networks: List[Dict[str, Any]] = field(default_factory=list)
    networks_available: bool = True
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
    degraded: bool = False            # True when requested optional data may be incomplete
    firewall: Optional[FirewallData] = None     # set when the firewall was requested (Needs.firewall)
    # Wi-Fi network settings (legacy rest/wlanconf), when requested (Needs.wlans); None: not requested or unreadable.
    wlans: Optional[List[Dict[str, Any]]] = None


def _legacy_or_empty(client: UniFiClient, site_ref: str, resource: str, notes: List[str]) -> List[Dict[str, Any]]:
    try:
        return client.legacy_stat(site_ref, resource)
    except UniFiAPIError as e:
        impact = (
            "offline clients, reservations, client-group history and the new-device check (client.new_device) "
            "were skipped"
            if resource == "alluser"
            else "port mapping will be incomplete"
        )
        notes.append(f"legacy stat/{resource} unavailable; {impact}: {e}")
        return []


def _legacy_users_or_empty(
    client: UniFiClient, site_ref: str, notes: List[str]
) -> tuple[List[Dict[str, Any]], bool]:
    """``(client history, available)``: an empty list with ``available`` True is an answer, one with False is the
    failure path."""
    before = len(notes)
    users = _legacy_or_empty(client, site_ref, "alluser", notes)
    return users, len(notes) == before


def _legacy_clients_or_empty(
    client: UniFiClient, site_ref: str, notes: List[str]
) -> tuple[List[Dict[str, Any]], bool]:
    try:
        return client.legacy_stat(site_ref, "sta"), True
    except UniFiAPIError as e:
        notes.append(f"legacy stat/sta unavailable; port mapping will be incomplete: {e}")
        return [], False


def _legacy_devices_or_empty(
    client: UniFiClient, site_ref: str, notes: List[str]
) -> tuple[List[Dict[str, Any]], bool]:
    try:
        return client.legacy_stat(site_ref, "device"), True
    except UniFiAPIError as e:
        notes.append(
            f"legacy stat/device unavailable; port, overheating, storage and recent-reboot checks were skipped: {e}"
        )
        return [], False


def _legacy_health_or_empty(client: UniFiClient, site_ref: str, notes: List[str]) -> List[Dict[str, Any]]:
    try:
        return client.legacy_stat(site_ref, "health")
    except UniFiAPIError as e:
        notes.append(f"legacy stat/health unavailable, controller health and WAN checks were skipped: {e}")
        return []


def _speedtests_or_empty(client: UniFiClient, site_ref: str, notes: List[str]) -> List[Dict[str, Any]]:
    try:
        tests = client.legacy_v2(site_ref, "speedtest")
    except UniFiAPIError as e:
        notes.append(f"speedtest history unavailable, speedtest results were skipped: {e}")
        return []
    def sort_time(test: Dict[str, Any]) -> float:
        value = test.get("time")
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0

    return sorted((t for t in tests if isinstance(t, dict)), key=sort_time)


def _neighbors_or_empty(client: UniFiClient, site_ref: str, notes: List[str]) -> tuple[List[Dict[str, Any]], bool]:
    try:
        return [n for n in client.legacy_stat(site_ref, "rogueap") if isinstance(n, dict)], True
    except UniFiAPIError as e:
        notes.append(f"neighboring networks unavailable; neighbor-based channel comparisons were skipped: {e}")
        return [], False


def _device_extras(
    client: UniFiClient, site_id: str, device: Dict[str, Any], notes: List[str], include_stats: bool = True
) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], bool]:
    """One device's (detail, statistics, read_failed). An HTTP 404 can mean no detail or statistics for an offline
    device; other read errors leave the corresponding check unavailable."""
    try:
        detail = client.device(site_id, device["id"])
    except UniFiAPIError as e:
        return None, None, _device_read_failed(e, device)
    if not include_stats:
        return detail, None, False
    try:
        return detail, client.device_statistics(site_id, device["id"]), False
    except UniFiAPIError as e:
        return detail, None, _device_read_failed(e, device)


def _device_read_failed(error: UniFiAPIError, device: Dict[str, Any]) -> bool:
    return not (device.get("state") == "OFFLINE" and str(error).startswith("HTTP 404 "))


def _legacy_rest_or_empty(
    client: UniFiClient, site_ref: str, resource: str, notes: List[str]
) -> tuple[List[Dict[str, Any]], bool]:
    try:
        return client.legacy_rest(site_ref, resource), True
    except UniFiAPIError as e:
        notes.append(f"legacy rest/{resource} unavailable, network names may be missing: {e}")
        return [], False


def _legacy_v2_or_empty(
    client: UniFiClient, site_ref: str, resource: str, notes: List[str]
) -> Optional[List[Dict[str, Any]]]:
    try:
        return client.legacy_v2(site_ref, resource)
    except UniFiAPIError as e:
        notes.append(
            f"legacy v2 {resource} unavailable; membership cannot be validated against "
            f"deleted groups, so raw group IDs will be trusted: {e}"
        )
        return None


def _optional_part(read: Callable[[], Any], what: str, impact: str, notes: List[str]) -> Optional[List[Dict[str, Any]]]:
    """An optional list of records: the records, or None (with a warning) when the controller will not give it.
    Unlike ``[]``, None says "could not be read", which a report must not present as "there is none"."""
    try:
        records = read()
    except UniFiAPIError as e:
        notes.append(f"{what} unavailable; {impact}: {e}")
        return None
    return [r for r in records if isinstance(r, dict)] if isinstance(records, list) else []


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
    group definitions. ``networks`` reads the network configuration (``rest/networkconf``) alone.
    ``firewall`` reads the zone-based firewall (policies, zones, zone matrix) and the
    port forwards, ``wlans`` the Wi-Fi network settings (``rest/wlanconf``). ``health`` is ``stat/health``
    (for ``diagnose`` and ``wan``), ``speedtests`` the speedtest history, ``neighbors`` the neighboring
    Wi-Fi networks, ``events`` the event log (None: not read). ``devices`` and ``clients`` can be deferred
    when checks do not use them. Device details and legacy devices are part
    of a normal collection; set their fields to False to defer them, or True in ``extend_snapshot`` to read
    them later. Device statistics can be deferred independently of details, and legacy client records can be
    deferred independently of the required connected-client list.

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
    firewall: bool = False
    wlans: bool = False
    networks: bool = False
    events: Optional[EventQuery] = None
    users_required: bool = False
    devices: Optional[bool] = None
    clients: Optional[bool] = None
    device_extras: Optional[bool] = None
    device_stats: Optional[bool] = None
    legacy_devices: Optional[bool] = None
    legacy_clients: Optional[bool] = None


def _events_or_empty(
    client: UniFiClient,
    site_ref: str,
    wanted: EventQuery,
    notes: List[str],
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
        notes.append(f"event log unavailable; event history was skipped: {e}")
        return [], False, False


def describe_snapshot(snap: "Snapshot") -> str:
    """What a snapshot holds, as 'read 4 devices, 2 connected clients, ...' (empty parts left out).
    Used by --verbose to show what was actually read."""
    fw = snap.firewall or FirewallData()
    parts = [(snap.devices, "devices"), (snap.clients, "connected clients"),
             (snap.legacy_devices, "legacy devices"), (snap.legacy_clients, "legacy clients"),
             (snap.all_users, "known clients"), (snap.networks, "networks"),
             (snap.client_groups or [], "client groups"), (snap.health, "health subsystems"),
             (snap.speedtests, "speedtests"), (snap.neighbors, "neighbor rows"), (snap.events, "events"),
             (fw.policies or [], "firewall policies"), (fw.port_forwards or [], "port forwards"),
             (snap.wlans or [], "Wi-Fi networks")]
    found = [f"{len(items)} {name}" for items, name in parts if items]
    return "read " + (", ".join(found) if found else "nothing")


_CORE_READS = frozenset({"devices", "clients", "legacy_devices", "legacy_clients"})


def _extra_reads(reads: "_Reads") -> bool:
    """Was an optional read (network configuration, health, events, ...) asked for in this collection? The device
    detail reads (``extras0``, ...) are a stage of their own."""
    return any(name not in _CORE_READS and not name.startswith("extras") for name in reads.notes)


class _Reads:
    """Runs the independent reads of one collection, one by one or on the client's thread pool, and keeps each
    read's warnings apart so they are shown in a fixed order whatever finished first.

    Every task is a function of its notes list (where it records what degraded). With no pool a task runs when
    it is submitted, so a required read that fails stops the collection at once, as it always did; with a pool
    the failure surfaces from ``result`` in the order the results are asked for, which is deterministic.
    """

    def __init__(self, pool: Optional[ThreadPoolExecutor]) -> None:
        self._pool = pool
        self._futures: Dict[str, Any] = {}
        self.notes: Dict[str, List[str]] = {}

    def submit(self, name: str, task: Callable[[List[str]], Any]) -> None:
        notes = self.notes.setdefault(name, [])
        if self._pool is None:
            self._futures[name] = (True, task(notes))
        else:
            # the worker runs in a copy of this context, so its log records carry the run's request id
            self._futures[name] = (False, self._pool.submit(contextvars.copy_context().run, task, notes))

    def result(self, name: str) -> Any:
        done, value = self._futures[name]
        return value if done else value.result()

    def show_warnings(self, order: List[str]) -> None:
        for name in order:
            for message in self.notes.get(name, []):
                warn(message)


# The order in which a collection's warnings are shown (it does not depend on which read finished first).
_WARNING_ORDER = ["events", "neighbors", "devices", "extras", "alluser", "clients", "legacy_devices",
                  "legacy_clients", "networks", "health", "speedtests", "groups", "fw_policies", "fw_zones",
                  "fw_matrix", "fw_forwards", "wlans"]


def _submit_extras(reads: "_Reads", needs: Needs, client: UniFiClient, site_ref: str, now_ms: Optional[int],
                   users: bool) -> None:
    """Queue the optional reads that ``needs`` asks for (``users``: also the client history)."""
    if needs.events is not None:
        wanted = needs.events
        reads.submit("events", lambda notes: _events_or_empty(client, site_ref, wanted, notes, now_ms))
    if needs.neighbors:
        reads.submit("neighbors", lambda notes: _neighbors_or_empty(client, site_ref, notes))
    if users and (needs.groups or needs.offline or needs.reservations):
        if needs.users_required:
            reads.submit("alluser", lambda notes: (client.legacy_stat(site_ref, "alluser"), True))
        else:
            reads.submit("alluser", lambda notes: _legacy_users_or_empty(client, site_ref, notes))
    if needs.reservations or needs.networks:
        reads.submit("networks", lambda notes: _legacy_rest_or_empty(client, site_ref, "networkconf", notes))
    if needs.health:
        reads.submit("health", lambda notes: _legacy_health_or_empty(client, site_ref, notes))
    if needs.speedtests:
        reads.submit("speedtests", lambda notes: _speedtests_or_empty(client, site_ref, notes))
    if needs.groups:
        reads.submit("groups", lambda notes: _legacy_v2_or_empty(client, site_ref, "network-members-groups", notes))
    if needs.wlans:
        reads.submit("wlans", partial(_optional_part, lambda: client.legacy_rest(site_ref, "wlanconf"),
                                      "Wi-Fi network settings", "whatever needs them was skipped"))
    if needs.firewall:
        classic = "this controller may use the classic firewall, which is not shown"
        for name, what, impact, read in (
                ("fw_policies", "firewall policies", classic, lambda: client.legacy_v2(site_ref, "firewall-policies")),
                ("fw_zones", "firewall zones", "zone names are missing",
                 lambda: client.legacy_v2(site_ref, "firewall/zone")),
                ("fw_matrix", "firewall zone matrix", "the matrix is not shown",
                 lambda: client.legacy_v2(site_ref, "firewall/zone-matrix")),
                ("fw_forwards", "port forwards", "port forwards are not shown",
                 lambda: client.legacy_rest(site_ref, "portforward"))):
            reads.submit(name, partial(_optional_part, read, what, impact))


def _submit_device_extras(reads: "_Reads", snap: Snapshot, client: UniFiClient, include_stats: bool) -> None:
    for i, device in enumerate(snap.devices):
        reads.submit(f"extras{i}", partial(_device_extras, client, snap.site["id"], device,
                                          include_stats=include_stats))


def _apply_device_extras(snap: Snapshot, reads: "_Reads", include_stats: bool) -> None:
    failed = 0
    for i, device in enumerate(snap.devices):
        detail, stats, read_failed = reads.result(f"extras{i}")
        if detail is not None:
            snap.device_details[device["id"]] = detail
        if stats is not None:
            snap.device_stats[device["id"]] = stats
        snap.degraded = snap.degraded or read_failed
        if detail is None or (include_stats and stats is None):
            failed += 1
    if failed:
        reads.notes.setdefault("extras", []).append(f"detail/statistics unavailable for {failed} device(s)")


def _apply_extras(snap: Snapshot, reads: "_Reads", needs: Needs, users: bool) -> None:
    """Put the results of ``_submit_extras`` into the snapshot."""
    if needs.events is not None:
        snap.events, snap.events_truncated, snap.events_available = reads.result("events")
        snap.event_window_seconds = needs.events.since_seconds
    if needs.neighbors:
        snap.neighbors, snap.neighbors_available = reads.result("neighbors")
    if users and (needs.groups or needs.offline or needs.reservations):
        snap.all_users, snap.all_users_available = reads.result("alluser")
    if needs.reservations or needs.networks:
        snap.networks, snap.networks_available = reads.result("networks")
    if needs.health:
        snap.health = reads.result("health")
    if needs.speedtests:
        snap.speedtests = reads.result("speedtests")
    if needs.groups:
        snap.client_groups = reads.result("groups")
    if needs.wlans:
        snap.wlans = reads.result("wlans")
    if needs.firewall:
        snap.firewall = FirewallData(policies=reads.result("fw_policies"), zones=reads.result("fw_zones"),
                                     matrix=reads.result("fw_matrix"), port_forwards=reads.result("fw_forwards"))


def collect_snapshot(
    client: UniFiClient,
    site: str,
    needs: Needs = Needs(),  # noqa: B008  (frozen, so one shared default is safe)
    now_ms: Optional[int] = None,
) -> Snapshot:
    """One read of the controller: the devices and connected clients always, and whatever ``needs``
    adds (see ``Needs``). ``now_ms`` fixes the clock of the event window, for tests.

    The reads do not depend on each other (except a device's detail and statistics on the device list), so
    when the client has more than one worker they run side by side; the result and the order of the warnings
    are the same either way."""
    client.stage("Connecting to the controller")
    site_info = client.resolve_site(site)
    site_ref = site_info.get("internalReference") or site
    site_id = site_info["id"]
    snap = Snapshot(site=site_info, devices=[], clients=[])
    with client.parallel() as pool:
        reads = _Reads(pool)
        try:
            if needs.devices is not False:
                reads.submit("devices", lambda notes: client.devices(site_id))
            if needs.clients is not False:
                reads.submit("clients", lambda notes: client.clients(site_id))
            if needs.legacy_devices is not False:
                reads.submit("legacy_devices", lambda notes: _legacy_devices_or_empty(client, site_ref, notes))
            if needs.legacy_clients is not False:
                reads.submit("legacy_clients", lambda notes: _legacy_clients_or_empty(client, site_ref, notes))
            _submit_extras(reads, needs, client, site_ref, now_ms, users=True)
            if needs.devices is not False:
                client.stage("Reading devices")
                snap.devices = reads.result("devices")
            if needs.device_extras is not False:
                _submit_device_extras(reads, snap, client, needs.device_stats is not False)
            if needs.clients is not False:
                client.stage("Reading clients")
                snap.clients = reads.result("clients")
            if needs.legacy_devices is not False:
                snap.legacy_devices, snap.legacy_devices_available = reads.result("legacy_devices")
            if needs.legacy_clients is not False:
                snap.legacy_clients, snap.legacy_clients_available = reads.result("legacy_clients")
            if _extra_reads(reads):
                client.stage("Reading network details")          # only when an optional read was asked for
            _apply_extras(snap, reads, needs, users=True)
            if needs.device_extras is not False:
                client.stage("Reading device details")
                _apply_device_extras(snap, reads, needs.device_stats is not False)
        finally:
            reads.show_warnings(_WARNING_ORDER)
    snap.degraded = snap.degraded or any(notes for name, notes in reads.notes.items() if name != "extras")
    if client.tracing():
        client.debug(describe_snapshot(snap), "snapshot.read")
    return snap


def extend_snapshot(client: UniFiClient, snap: Snapshot, needs: Needs, now_ms: Optional[int] = None) -> None:
    """Read more into a snapshot that ``collect_snapshot`` already made: the optional reads in ``needs`` (the
    network configuration, client groups, health, speedtests, neighbors and the event log), not the devices
    and clients or the client history, which are already there. A command that first looks something up
    in the cheap data (``client``) uses this to read the rest only when the lookup found something."""
    site_ref = snap.site.get("internalReference") or snap.site.get("name") or ""
    client.stage("Reading more details")
    with client.parallel() as pool:
        reads = _Reads(pool)
        try:
            if needs.legacy_devices is True:
                reads.submit("legacy_devices", lambda notes: _legacy_devices_or_empty(client, site_ref, notes))
            if needs.device_extras is True:
                _submit_device_extras(reads, snap, client, needs.device_stats is not False)
            _submit_extras(reads, needs, client, site_ref, now_ms, users=False)
            if needs.legacy_devices is True:
                snap.legacy_devices, snap.legacy_devices_available = reads.result("legacy_devices")
            if needs.device_extras is True:
                _apply_device_extras(snap, reads, needs.device_stats is not False)
            _apply_extras(snap, reads, needs, users=False)
        finally:
            reads.show_warnings(_WARNING_ORDER)
    snap.degraded = snap.degraded or any(notes for name, notes in reads.notes.items() if name != "extras")
    if client.tracing():
        client.debug(describe_snapshot(snap), "snapshot.read")


def collect_event_snapshot(
    client: UniFiClient,
    site: str,
    wanted: EventQuery,
    now_ms: Optional[int] = None,
) -> Snapshot:
    """Only the event log (no devices or clients), for the `events` command."""
    client.stage("Connecting to the controller")
    site_info = client.resolve_site(site)
    site_ref = site_info.get("internalReference") or site
    notes: List[str] = []
    client.stage("Reading the event log")
    events, truncated, available = _events_or_empty(client, site_ref, wanted, notes, now_ms)
    for message in notes:
        warn(message)
    snap = Snapshot(
        site=site_info,
        devices=[],
        clients=[],
        events=events,
        events_truncated=truncated,
        event_window_seconds=wanted.since_seconds,
        events_available=available,
        degraded=not available,
    )
    if client.tracing():
        client.debug(describe_snapshot(snap), "snapshot.read")
    return snap
