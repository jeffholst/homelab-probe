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


def _legacy_or_empty(client: UniFiClient, site_ref: str, resource: str) -> List[Dict[str, Any]]:
    try:
        return client.legacy_stat(site_ref, resource)
    except UniFiAPIError as e:
        warn(f"legacy stat/{resource} unavailable, port mapping will be incomplete: {e}")
        return []


def collect_snapshot(
    client: UniFiClient, site: str, include_offline: bool = False
) -> Snapshot:
    site_info = client.resolve_site(site)
    site_ref = site_info.get("internalReference") or site
    return Snapshot(
        site=site_info,
        devices=client.devices(site_info["id"]),
        clients=client.clients(site_info["id"]),
        legacy_devices=_legacy_or_empty(client, site_ref, "device"),
        legacy_clients=_legacy_or_empty(client, site_ref, "sta"),
        all_users=_legacy_or_empty(client, site_ref, "alluser") if include_offline else [],
    )
