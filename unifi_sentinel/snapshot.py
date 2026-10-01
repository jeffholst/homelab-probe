"""Data layer: one consistent read of the controller, independent of output format."""

import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .client import UniFiAPIError, UniFiClient


EVENT_PAGE_SIZE = 500   # events requested per system-log page
MAX_EVENTS = 20_000     # never read more than this many events in one run


def warn(msg: str) -> None:
    print(f"Warning: {msg}", file=sys.stderr)


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


def _legacy_or_empty(client: UniFiClient, site_ref: str, resource: str) -> List[Dict[str, Any]]:
    try:
        return client.legacy_stat(site_ref, resource)
    except UniFiAPIError as e:
        warn(f"legacy stat/{resource} unavailable, port mapping will be incomplete: {e}")
        return []


def _legacy_health_or_empty(client: UniFiClient, site_ref: str) -> List[Dict[str, Any]]:
    try:
        return client.legacy_stat(site_ref, "health")
    except UniFiAPIError as e:
        warn(f"legacy stat/health unavailable, controller health and WAN checks were skipped: {e}")
        return []


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


def _events_or_empty(
    client: UniFiClient,
    site_ref: str,
    since_seconds: int,
    categories: List[str],
    severities: List[str],
    search: str,
    now_ms: Optional[int] = None,
) -> tuple[List[Dict[str, Any]], bool]:
    now = int(time.time() * 1000) if now_ms is None else now_ms
    query: Dict[str, Any] = {
        "timestampFrom": now - since_seconds * 1000,
        "timestampTo": now,
        "pageSize": EVENT_PAGE_SIZE,
    }
    if categories:
        query["categories"] = [c.upper() for c in categories]
    if severities:
        query["severities"] = [s.upper() for s in severities]
    if search:
        query["searchText"] = search

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
                return events, False
        return events, page < total_pages
    except UniFiAPIError as e:
        warn(f"event log unavailable; event history was skipped: {e}")
        return [], False


def collect_snapshot(
    client: UniFiClient,
    site: str,
    include_offline: bool = False,
    include_reservations: bool = False,
    include_groups: bool = False,
    include_health: bool = False,
    include_events: bool = False,
    event_since_seconds: int = 86400,
    event_categories: Optional[List[str]] = None,
    event_severities: Optional[List[str]] = None,
    event_search: str = "",
    now_ms: Optional[int] = None,
) -> Snapshot:
    """``include_offline``, ``include_reservations`` and ``include_groups`` all need the
    legacy ``stat/alluser`` list (``include_health`` reads ``stat/health`` for ``diagnose``); reservations also need the network configuration
    (names, VLANs) and groups need the client group definitions."""
    site_info = client.resolve_site(site)
    site_ref = site_info.get("internalReference") or site
    events, events_truncated = _events_or_empty(
        client, site_ref, event_since_seconds, event_categories or [],
        event_severities or [], event_search, now_ms
    ) if include_events else ([], False)
    devices = client.devices(site_info["id"])
    details, stats = _device_extras(client, site_info["id"], devices)
    if include_groups:
        all_users = client.legacy_stat(site_ref, "alluser")
    elif include_offline or include_reservations:
        all_users = _legacy_or_empty(client, site_ref, "alluser")
    else:
        all_users = []
    return Snapshot(
        site=site_info,
        devices=devices,
        device_details=details,
        device_stats=stats,
        clients=client.clients(site_info["id"]),
        legacy_devices=_legacy_or_empty(client, site_ref, "device"),
        legacy_clients=_legacy_or_empty(client, site_ref, "sta"),
        all_users=all_users,
        networks=(
            _legacy_rest_or_empty(client, site_ref, "networkconf") if include_reservations else []
        ),
        health=_legacy_health_or_empty(client, site_ref) if include_health else [],
        client_groups=(
            _legacy_v2_or_empty(client, site_ref, "network-members-groups") if include_groups else []
        ),
        events=events,
        events_truncated=events_truncated,
        event_window_seconds=event_since_seconds if include_events else 0,
    )


def collect_event_snapshot(
    client: UniFiClient,
    site: str,
    since_seconds: int,
    categories: Optional[List[str]] = None,
    severities: Optional[List[str]] = None,
    search: str = "",
    now_ms: Optional[int] = None,
) -> Snapshot:
    site_info = client.resolve_site(site)
    site_ref = site_info.get("internalReference") or site
    events, truncated = _events_or_empty(
        client, site_ref, since_seconds, categories or [], severities or [], search, now_ms
    )
    return Snapshot(site=site_info, devices=[], clients=[], events=events,
                    events_truncated=truncated, event_window_seconds=since_seconds)
