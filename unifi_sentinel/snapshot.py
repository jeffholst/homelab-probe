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


def collect_snapshot(
    client: UniFiClient, site: str, include_offline: bool = False
) -> Snapshot:
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
        all_users=_legacy_or_empty(client, site_ref, "alluser") if include_offline else [],
    )
