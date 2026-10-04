"""Saved inventories and the differences between them: `snapshot` and `diff`.

`snapshot` writes the current inventory to a local JSON file and `diff` compares two of
them (or one against the live network) to answer "what changed since it last worked?".
Both only read from the controller; the files are written locally, never to the controller.
Saved files hold real MACs and IPs, so they are git-ignored and created owner-only.
"""

import json
import os
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TypedDict, TypeVar, cast

from . import __version__
from .client_view import AddressingIndex, DeviceIndex, addressing, known_clients
from .config import ConfigError
from .query import query_rows
from .reservations import build_reservations
from .snapshot import Snapshot
from .util import clean_data, normalize_mac

SCHEMA_VERSION = 1
DEFAULT_DIR = "snapshots"
FILE_PREFIX = "snapshot-"
# New names are UTC and end the time with "Z"; names without it were written in local time by
# earlier versions and are still recognised.
_FILE_RE = re.compile(r"^snapshot-(\d{8}-\d{6})(Z)?(?:-(\d+))?\.json$")
JSON_VERSION = 1          # the format of `diff --json`; it changes only when a field is removed or renamed
MAX_LISTED = 15          # connection changes shown before "... and N more" (--all lifts it)


# -- the saved record --------------------------------------------------------
# What ``capture`` writes and ``load_snapshot`` reads back. They name the keys of the file (schema version 1), not the
# controller's payloads, which stay untyped. ``load_snapshot`` validates the file before anything here is trusted.

class DeviceRecord(TypedDict):
    mac: str
    name: str
    ip: str
    model: str
    type: str
    firmware: str
    state: str
    uplink: str                      # the device it plugs into, "" when unknown
    uplink_port: str


class ClientRecord(TypedDict):
    mac: str
    name: str
    ip: str
    connection: str                  # "Wired" or "Wireless"
    status: str                      # "Online" or "Offline"
    network: str
    vlan: int | str                  # "" when the client has none
    uplink: str
    uplink_port: str
    groups: List[str]


class ReservationRecord(TypedDict):
    mac: str
    name: str
    reserved_ip: str
    network: str


class SiteRecord(TypedDict):
    name: str
    id: str


class ControllerRecord(TypedDict):
    application_version: str


class SnapshotRecord(TypedDict):
    """The whole saved inventory (a snapshot file)."""

    schema_version: int
    tool_version: str
    captured_at: str                 # ISO 8601 with the local UTC offset
    site: SiteRecord
    controller: ControllerRecord
    devices: List[DeviceRecord]
    clients: List[ClientRecord]
    reservations: List[ReservationRecord]


InventoryRecord = DeviceRecord | ClientRecord | ReservationRecord
Record = TypeVar("Record", DeviceRecord, ClientRecord, ReservationRecord)


# -- capturing ---------------------------------------------------------------

def _where(snap: Snapshot, idx: DeviceIndex, rec: Dict[str, Any]) -> Tuple[str, str]:
    """(the device a client is on, its port) for a connected client, or the last uplink the
    controller recorded for an offline one. Blank when unknown (e.g. offline Wi-Fi)."""
    live, sta, user = rec["live"], rec["sta"] or {}, rec["user"] or {}
    if rec["online"]:
        mac = normalize_mac(sta.get("sw_mac") if rec["wired"] else sta.get("ap_mac"))
        if not mac and live:
            mac = next((m for m, d in idx.integration.items() if d.get("id") == live.get("uplinkDeviceId")), "")
        port = sta.get("sw_port") if rec["wired"] else None
        return (idx.name(mac) if mac else ""), ("" if port is None else str(port))
    if rec["wired"]:
        mac = normalize_mac(user.get("last_uplink_mac"))
        device = idx.name(mac) if mac else ""
        if not device or device == mac:
            device = user.get("last_uplink_name") or device
        port = user.get("last_uplink_remote_port")
        return device, ("" if port is None else str(port))
    return "", ""


def capture(snap: Snapshot, application_version: str = "", now: Optional[datetime] = None) -> SnapshotRecord:
    """The current inventory as a plain, versioned record.

    Built from the same rows the other commands print (not from raw API payloads), so the
    format stays stable across controller versions. Values that change constantly (uptime,
    last-seen times, traffic) are left out so a diff shows real changes, not churn.
    """
    now = now or datetime.now().astimezone()
    idx = DeviceIndex(snap)

    devices: List[DeviceRecord] = [{
        "mac": r["MAC Address"], "name": r["Name"], "ip": r["IP Address"], "model": r["Model"],
        "type": r["Type"].replace("Device - ", ""), "firmware": r.get("Firmware", ""),
        "state": r["Status"], "uplink": r["Switch"], "uplink_port": r["Port"],
    } for r in query_rows(snap, "devices")]

    clients: List[ClientRecord] = []
    lookups = AddressingIndex(snap)
    for rec in known_clients(snap):
        info = addressing(snap, rec, lookups)
        device, port = _where(snap, idx, rec)
        clients.append({
            "mac": rec["mac"], "name": rec["name"], "ip": rec["ip"],
            "connection": "Wired" if rec["wired"] else "Wireless",
            "status": "Online" if rec["online"] else "Offline",
            "network": info["network"], "vlan": info["vlan"] if info["vlan"] is not None else "",
            "uplink": device, "uplink_port": port, "groups": sorted(info["groups"]),
        })

    reservations: List[ReservationRecord] = [
        {"mac": r["MAC Address"], "name": r["Name"], "reserved_ip": r["Reserved IP"], "network": r["Network"]}
        for r in build_reservations(snap)]

    return {
        "schema_version": SCHEMA_VERSION, "tool_version": __version__,
        "captured_at": now.isoformat(timespec="seconds"),
        "site": {"name": snap.site.get("name") or "", "id": snap.site.get("id") or ""},
        "controller": {"application_version": application_version},
        "devices": sorted(devices, key=lambda d: d["mac"]),
        "clients": sorted(clients, key=lambda c: c["mac"]),
        "reservations": sorted(reservations, key=lambda r: r["mac"]),
    }


# -- files -------------------------------------------------------------------

def list_snapshots(directory: Path) -> List[Path]:
    """Snapshot files this tool wrote into ``directory``, oldest first. Anything else in
    the directory is ignored (and never pruned)."""
    if not directory.is_dir():
        return []

    try:
        found = [(key, p) for p in directory.iterdir() if p.is_file() and (key := _age_key(p)) is not None]
    except OSError as e:
        raise ConfigError(f"cannot read snapshot directory {directory}: {e.strerror or e}") from e
    return [p for _, p in sorted(found, key=lambda item: item[0])]


def _age_key(path: Path) -> Optional[Tuple[datetime, int]]:
    """When a snapshot file name says it was taken (as a UTC instant), then its collision suffix
    as a number, or None when the name is not one this tool writes (or not a real date).
    Sorting the names as text would put ``...-1.json`` (the newer) before ``....json`` (the
    older) of the same second, and would order a local-time name against a UTC one wrongly.
    For a legacy local-time name, prefer the offset-bearing ``captured_at`` in its schema-v1
    record to disambiguate a repeated daylight-saving hour. If the file cannot be read or does
    not contain a usable timestamp, fall back to interpreting the name in the local time zone."""
    match = _FILE_RE.match(path.name)
    if not match:
        return None
    stamp, utc, n = match.groups()
    try:
        moment = datetime.strptime(stamp, "%Y%m%d-%H%M%S")
        if utc:
            moment = moment.replace(tzinfo=timezone.utc)
        else:
            fallback = moment.astimezone()  # old names: local time
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                captured_at = record.get("captured_at") if (
                    isinstance(record, dict) and record.get("schema_version") == SCHEMA_VERSION
                ) else None
                captured = datetime.fromisoformat(str(captured_at))
                moment = captured.astimezone(timezone.utc) if captured.tzinfo is not None else fallback
            except (OSError, TypeError, ValueError, AttributeError, json.JSONDecodeError):
                moment = fallback
    except (ValueError, OverflowError, OSError):
        return None
    return moment, int(n or 0)


def save_snapshot(record: SnapshotRecord, path: Optional[Path] = None, directory: Path = Path(DEFAULT_DIR),
                  force: bool = False) -> Path:
    """Write ``record`` as JSON, owner-only. With no ``path`` a timestamped name is chosen in
    ``directory`` (never overwriting an existing file). An explicit ``path`` that exists is
    only replaced with ``force``."""
    if path is None:
        # UTC, so the order of the names never depends on the time zone or on daylight saving;
        # the record itself keeps the local time with its offset.
        stamp = datetime.fromisoformat(record["captured_at"]).astimezone(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
        path, n = directory / f"{FILE_PREFIX}{stamp}.json", 0
        while path.exists():
            n += 1
            path = directory / f"{FILE_PREFIX}{stamp}-{n}.json"
    elif path.exists() and not force:
        raise ConfigError(f"{path} already exists; choose another name or use --force")

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        os.fchmod(f.fileno(), 0o600)
        json.dump(record, f, indent=2)
        f.write("\n")
    return path


def prune(directory: Path, keep: int, protect: Optional[Path] = None) -> List[Path]:
    """Delete the oldest snapshot files beyond the newest ``keep``. Only files named like
    ones this tool writes are touched, and ``protect`` (the one just saved) never is."""
    files = list_snapshots(directory)
    protected = Path(os.path.abspath(protect)) if protect is not None else None
    candidates = [p for p in files if protected is None or Path(os.path.abspath(p)) != protected]
    doomed = candidates[:max(0, len(files) - keep)]
    for p in doomed:
        p.unlink()
    return doomed


def load_snapshot(path: Path) -> SnapshotRecord:
    """Read and validate a snapshot file. Any problem raises ConfigError with a clear message."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot read snapshot {path}: {e.strerror or e}") from e
    try:
        record = json.loads(text)
    except ValueError as e:
        raise ConfigError(f"{path} is not a snapshot file (invalid JSON: {e})") from e
    if not isinstance(record, dict) or "schema_version" not in record:
        raise ConfigError(f"{path} is not a snapshot file (no schema_version)")
    if (
        isinstance(record["schema_version"], bool)
        or not isinstance(record["schema_version"], int)
        or record["schema_version"] != SCHEMA_VERSION
    ):
        raise ConfigError(f"{path} uses snapshot format {record['schema_version']!r}; "
                          f"this version of hlp reads format {SCHEMA_VERSION}")
    record.setdefault("tool_version", "")
    record.setdefault("captured_at", "")
    for key in ("site", "controller"):
        if not isinstance(record.get(key), dict):
            raise ConfigError(f"{path} is damaged: '{key}' is missing or not an object")
    site, controller = record["site"], record["controller"]
    site.setdefault("name", "")
    site.setdefault("id", "")
    controller.setdefault("application_version", "")
    if not all(isinstance(site.get(key), str) for key in ("name", "id")):
        raise ConfigError(f"{path} is damaged: 'site' contains invalid values")
    if not isinstance(controller.get("application_version"), str):
        raise ConfigError(f"{path} is damaged: 'controller' contains invalid values")

    defaults: Dict[str, Dict[str, Any]] = {
        "devices": {"mac": "", "name": "", "ip": "", "model": "", "type": "", "firmware": "",
                    "state": "", "uplink": "", "uplink_port": ""},
        "clients": {"mac": "", "name": "", "ip": "", "connection": "", "status": "", "network": "",
                    "vlan": "", "uplink": "", "uplink_port": "", "groups": []},
        "reservations": {"mac": "", "name": "", "reserved_ip": "", "network": ""},
    }
    string_fields = {
        "devices": ("mac", "name", "ip", "model", "type", "firmware", "state", "uplink", "uplink_port"),
        "clients": ("mac", "name", "ip", "connection", "status", "network", "uplink", "uplink_port"),
        "reservations": ("mac", "name", "reserved_ip", "network"),
    }
    for key in ("devices", "clients", "reservations"):
        if not isinstance(record.get(key), list):
            raise ConfigError(f"{path} is damaged: '{key}' is missing or not a list")
        if any(not isinstance(item, dict) or not isinstance(item.get("mac"), str) or not item["mac"]
               for item in record[key]):
            raise ConfigError(f"{path} is damaged: '{key}' contains an item without a MAC address")
        for item in record[key]:
            for field, default in defaults[key].items():
                item.setdefault(field, default.copy() if isinstance(default, list) else default)
            if any(not isinstance(item[field], str) for field in string_fields[key]):
                raise ConfigError(f"{path} is damaged: '{key}' contains invalid values")
            if key == "clients" and (
                (isinstance(item["vlan"], bool) or not isinstance(item["vlan"], (int, str)))
                or not isinstance(item["groups"], list)
                or any(not isinstance(group, str) for group in item["groups"])
            ):
                raise ConfigError(f"{path} is damaged: 'clients' contains invalid values")
    if not isinstance(record["tool_version"], str) or not isinstance(record["captured_at"], str):
        raise ConfigError(f"{path} is damaged: snapshot metadata contains invalid values")
    # Defaults and field types are checked above (isinstance cannot check a TypedDict).
    return cast(SnapshotRecord, record)


def resolve(ref: str, directory: Path) -> Path:
    """A snapshot reference: a path, or a file name inside ``directory``."""
    path = Path(ref)
    if path.is_file():
        return path
    inside = directory / ref
    if inside.is_file():
        return inside
    raise ConfigError(f"snapshot not found: {ref} (also looked in {directory}/)")


# -- comparing ---------------------------------------------------------------

# (field, label) in the order they are reported
DEVICE_FIELDS = [("name", "Renamed"), ("ip", "IP changed"), ("firmware", "Firmware changed"),
                 ("state", "State changed"), ("location", "Moved"), ("model", "Model changed")]
CLIENT_FIELDS = [("name", "Renamed"), ("ip", "IP changed"), ("connection", "Connection type changed"),
                 ("network", "Network changed"), ("vlan", "VLAN changed"), ("location", "Moved"),
                 ("groups", "Groups changed"), ("status", "Connection status changed")]
RESERVATION_FIELDS = [("name", "Renamed"), ("reserved_ip", "Reserved IP changed"),
                      ("network", "Network changed")]


class Change(TypedDict):
    """One field that differs between two inventories."""

    field: str
    old: Any
    new: Any


class ChangedRecord(TypedDict):
    """A device, client or reservation present in both inventories with at least one change."""

    mac: str
    name: str
    changes: List[Change]


class DiffPart(TypedDict):
    """The differences for one kind of record (devices, clients or reservations)."""

    added: Sequence[InventoryRecord]
    removed: Sequence[InventoryRecord]
    changed: List[ChangedRecord]


class Diff(TypedDict):
    """The result of ``diff_snapshots``; ``total`` counts every difference."""

    same_site: bool
    controller: List[Change]
    devices: DiffPart
    clients: DiffPart
    reservations: DiffPart
    total: int


def _location(r: Mapping[str, Any]) -> str:
    where = r.get("uplink") or ""
    return f"{where} port {r['uplink_port']}" if where and r.get("uplink_port") else where


def _compare(old: Mapping[str, Any], new: Mapping[str, Any], fields: List[Tuple[str, str]]) -> List[Change]:
    changes: List[Change] = []
    for field, _label in fields:
        a: Any
        b: Any
        if field == "location":
            a, b = _location(old), _location(new)
            if not (a and b):          # unknown on one side (e.g. offline Wi-Fi): not a move
                continue
        else:
            a, b = old.get(field), new.get(field)
        if a != b:
            changes.append({"field": field, "old": a, "new": b})
    return changes


def _diff_list(old: List[Record], new: List[Record], fields: List[Tuple[str, str]]) -> DiffPart:
    before, after = {r["mac"]: r for r in old}, {r["mac"]: r for r in new}
    changed: List[ChangedRecord] = []
    for mac in sorted(set(before) & set(after)):
        changes = _compare(before[mac], after[mac], fields)
        if changes:
            changed.append({"mac": mac, "name": after[mac].get("name") or before[mac].get("name") or mac,
                            "changes": changes})
    return {"added": [after[m] for m in sorted(set(after) - set(before))],
            "removed": [before[m] for m in sorted(set(before) - set(after))],
            "changed": changed}


def diff_snapshots(old: SnapshotRecord, new: SnapshotRecord) -> Diff:
    """Everything that differs between two captured inventories, matched by MAC address."""
    controller: List[Change] = []
    old_v, new_v = old["controller"].get("application_version"), new["controller"].get("application_version")
    if old_v and new_v and old_v != new_v:
        controller.append({"field": "application_version", "old": old_v, "new": new_v})
    devices = _diff_list(old["devices"], new["devices"], DEVICE_FIELDS)
    clients = _diff_list(old["clients"], new["clients"], CLIENT_FIELDS)
    reservations = _diff_list(old["reservations"], new["reservations"], RESERVATION_FIELDS)
    total = len(controller) + sum(
        len(part["added"]) + len(part["removed"]) + sum(len(c["changes"]) for c in part["changed"])
        for part in (devices, clients, reservations))
    return {
        "same_site": old["site"].get("id") == new["site"].get("id"),
        "controller": controller, "devices": devices, "clients": clients, "reservations": reservations,
        "total": total,
    }


# -- rendering ---------------------------------------------------------------

def _value(field: str, v: Any) -> str:
    if field == "groups":
        return ", ".join(v) if v else "none"
    return "none" if v in (None, "") else str(v)


def _describe(kind: str, r: Mapping[str, Any]) -> str:
    bits = {"devices": [r.get("model"), r.get("ip")], "clients": [r.get("ip"), r.get("connection")],
            "reservations": [r.get("reserved_ip"), r.get("network")]}[kind]
    detail = ", ".join(str(b) for b in bits if b)
    return f"{r.get('name') or r['mac']} ({detail})" if detail else str(r.get("name") or r["mac"])


def _section(kind: str, title: str, part: DiffPart, fields: List[Tuple[str, str]],
             show_all: bool, noun: str) -> List[str]:
    lines: List[str] = []

    def block(label: str, items: List[str], cap: bool = False) -> None:
        if not items:
            return
        lines.append(f"  {label} ({len(items)}):")
        shown = items if show_all or not cap else items[:MAX_LISTED]
        lines.extend(f"    {item}" for item in shown)
        if len(shown) < len(items):
            lines.append(f"    ... and {len(items) - len(shown)} more (use --all)")

    block(f"New {noun}", [_describe(kind, r) for r in part["added"]])
    block(f"Missing {noun}", [_describe(kind, r) for r in part["removed"]])
    for field, label in fields:
        rows = [(c["name"], ch) for c in part["changed"] for ch in c["changes"] if ch["field"] == field]
        if field == "status" and kind == "clients":      # connect/disconnect churn is capped
            block("Went online", [n for n, ch in rows if ch["new"] == "Online"], cap=True)
            block("Went offline", [n for n, ch in rows if ch["new"] == "Offline"], cap=True)
            continue
        items = [f"{n}: {_value(field, ch['old'])} -> {_value(field, ch['new'])}" for n, ch in rows]
        block(label, items, cap=field in ("ip",) and kind == "clients")
    return [title] + lines + [""] if lines else []


def render_diff(diff: Mapping[str, Any], old_label: str, new_label: str, show_all: bool = False) -> str:
    """The text of a diff: ``diff_snapshots`` or its ``diff_data`` document (the ``version`` is not used)."""
    diff, old_label, new_label = clean_data(diff), clean_data(old_label), clean_data(new_label)
    out = [f"Comparing {old_label} -> {new_label}", ""]
    if not diff["same_site"]:
        out += ["Warning: these snapshots are from different sites; most devices will look new or missing.", ""]
    if diff["controller"]:
        c = diff["controller"][0]
        out += ["Controller", f"  Application version: {c['old']} -> {c['new']}", ""]
    sections = (_section("devices", "Devices", diff["devices"], DEVICE_FIELDS, show_all, "devices")
                + _section("clients", "Clients", diff["clients"], CLIENT_FIELDS, show_all, "clients")
                + _section("reservations", "DHCP reservations", diff["reservations"],
                           RESERVATION_FIELDS, show_all, "reservations"))
    if not sections and not diff["controller"]:
        return "\n".join(out + ["No changes."])
    return "\n".join(out + sections).rstrip() + f"\n\n{diff['total']} change(s)"


def diff_data(diff: Diff) -> Dict[str, Any]:
    """What ``diff --json`` prints: the diff with its format version first."""
    return {"version": JSON_VERSION, **diff}


def diff_json(diff: Diff) -> str:
    """``diff_data`` as text."""
    return json.dumps(diff_data(diff), indent=2)


def label_for(record: SnapshotRecord, name: str) -> str:
    """'name (captured 2026-09-30 20:15)' for a saved snapshot."""
    try:
        when = datetime.fromisoformat(record["captured_at"]).strftime("%Y-%m-%d %H:%M")
    except (KeyError, ValueError):
        return name
    return f"{name} (captured {when})"
