"""Data layer: one consistent read of the controller, independent of output format."""

import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List

from .client import UniFiAPIError, UniFiClient


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
    client_groups: List[Dict[str, Any]] = field(default_factory=list)


def _legacy_or_empty(client: UniFiClient, site_ref: str, resource: str) -> List[Dict[str, Any]]:
    try:
        return client.legacy_stat(site_ref, resource)
    except UniFiAPIError as e:
        warn(f"legacy stat/{resource} unavailable, port mapping will be incomplete: {e}")
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


def _legacy_v2_or_empty(client: UniFiClient, site_ref: str, resource: str) -> List[Dict[str, Any]]:
    try:
        return client.legacy_v2(site_ref, resource)
    except UniFiAPIError as e:
        warn(f"legacy v2 {resource} unavailable, group names may be missing: {e}")
        return []


def collect_snapshot(
    client: UniFiClient,
    site: str,
    include_offline: bool = False,
    include_reservations: bool = False,
    include_groups: bool = False,
) -> Snapshot:
    """``include_offline``, ``include_reservations`` and ``include_groups`` all need the
    legacy ``stat/alluser`` list; reservations also need the network configuration
    (names, VLANs) and groups need the client group definitions."""
    site_info = client.resolve_site(site)
    site_ref = site_info.get("internalReference") or site
    devices = client.devices(site_info["id"])
    details, stats = _device_extras(client, site_info["id"], devices)
    return Snapshot(
        site=site_info,
        devices=devices,
        device_details=details,
        device_stats=stats,
        clients=client.clients(site_info["id"]),
        legacy_devices=_legacy_or_empty(client, site_ref, "device"),
        legacy_clients=_legacy_or_empty(client, site_ref, "sta"),
        all_users=(
            _legacy_or_empty(client, site_ref, "alluser")
            if include_offline or include_reservations or include_groups
            else []
        ),
        networks=(
            _legacy_rest_or_empty(client, site_ref, "networkconf") if include_reservations else []
        ),
        client_groups=(
            _legacy_v2_or_empty(client, site_ref, "network-members-groups") if include_groups else []
        ),
    )
