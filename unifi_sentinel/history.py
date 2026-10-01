"""Saved inventories and the differences between them: `snapshot` and `diff`.

`snapshot` writes the current inventory to a local JSON file and `diff` compares two of
them (or one against the live network) to answer "what changed since it last worked?".
Both only read from the controller; the files are written locally, never to the controller.
Saved files hold real MACs and IPs, so they are git-ignored and created owner-only.
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .client_view import DeviceIndex, addressing, known_clients
from .config import ConfigError
from .query import query_rows
from .reservations import build_reservations
from .snapshot import Snapshot

SCHEMA_VERSION = 1
DEFAULT_DIR = "snapshots"
FILE_PREFIX = "snapshot-"
_FILE_RE = re.compile(r"^snapshot-(\d{8}-\d{6})(?:-(\d+))?\.json$")
MAX_LISTED = 15          # connection changes shown before "... and N more" (--all lifts it)
Record = Dict[str, Any]


# -- capturing ---------------------------------------------------------------

def _where(snap: Snapshot, idx: DeviceIndex, rec: Record) -> Tuple[str, str]:
    """(the device a client is on, its port) for a connected client, or the last uplink the
    controller recorded for an offline one. Blank when unknown (e.g. offline Wi-Fi)."""
    live, sta, user = rec["live"], rec["sta"] or {}, rec["user"] or {}
    if rec["online"]:
        mac = ((sta.get("sw_mac") if rec["wired"] else sta.get("ap_mac")) or "").upper()
        if not mac and live:
            mac = next((m for m, d in idx.integration.items() if d.get("id") == live.get("uplinkDeviceId")), "")
        port = sta.get("sw_port") if rec["wired"] else None
        return (idx.name(mac) if mac else ""), ("" if port is None else str(port))
    if rec["wired"]:
        mac = (user.get("last_uplink_mac") or "").upper()
        device = idx.name(mac) if mac else ""
        if not device or device == mac:
            device = user.get("last_uplink_name") or device
        port = user.get("last_uplink_remote_port")
        return device, ("" if port is None else str(port))
    return "", ""


def capture(snap: Snapshot, application_version: str = "", now: Optional[datetime] = None) -> Record:
    """The current inventory as a plain, versioned record.

    Built from the same rows the other commands print (not from raw API payloads), so the
    format stays stable across controller versions. Values that change constantly (uptime,
    last-seen times, traffic) are left out so a diff shows real changes, not churn.
    """
    now = now or datetime.now().astimezone()
    idx = DeviceIndex(snap)

    devices = [{
        "mac": r["MAC Address"], "name": r["Name"], "ip": r["IP Address"], "model": r["Model"],
        "type": r["Type"].replace("Device - ", ""), "firmware": r.get("Firmware", ""),
        "state": r["Status"], "uplink": r["Switch"], "uplink_port": r["Port"],
    } for r in query_rows(snap, "devices")]

    clients = []
    for rec in known_clients(snap):
        info = addressing(snap, rec)
        device, port = _where(snap, idx, rec)
        clients.append({
            "mac": rec["mac"], "name": rec["name"], "ip": rec["ip"],
            "connection": "Wired" if rec["wired"] else "Wireless",
            "status": "Online" if rec["online"] else "Offline",
            "network": info["network"], "vlan": info["vlan"] if info["vlan"] is not None else "",
            "uplink": device, "uplink_port": port, "groups": sorted(info["groups"]),
        })

    reservations = [{"mac": r["MAC Address"], "name": r["Name"], "reserved_ip": r["Reserved IP"],
                     "network": r["Network"]} for r in build_reservations(snap)]

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

    def age_key(p: Path) -> Tuple[str, int]:
        # By timestamp, then by the collision suffix as a number: sorting the names as text
        # would put "...-1.json" (the newer) before "....json" (the older) of the same second.
        stamp, n = _FILE_RE.match(p.name).groups()
        return stamp, int(n or 0)

    try:
        return sorted((p for p in directory.iterdir() if p.is_file() and _FILE_RE.match(p.name)), key=age_key)
    except OSError as e:
        raise ConfigError(f"cannot read snapshot directory {directory}: {e.strerror or e}") from e


def save_snapshot(record: Record, path: Optional[Path] = None, directory: Path = Path(DEFAULT_DIR),
                  force: bool = False) -> Path:
    """Write ``record`` as JSON, owner-only. With no ``path`` a timestamped name is chosen in
    ``directory`` (never overwriting an existing file). An explicit ``path`` that exists is
    only replaced with ``force``."""
    if path is None:
        stamp = datetime.fromisoformat(record["captured_at"]).strftime("%Y%m%d-%H%M%S")
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


def load_snapshot(path: Path) -> Record:
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
    if record["schema_version"] != SCHEMA_VERSION:
        raise ConfigError(f"{path} uses snapshot format {record['schema_version']!r}; "
                          f"this version of unifi-sentinel reads format {SCHEMA_VERSION}")
    for key in ("site", "controller"):
        if not isinstance(record.get(key), dict):
            raise ConfigError(f"{path} is damaged: '{key}' is missing or not an object")
    for key in ("devices", "clients", "reservations"):
        if not isinstance(record.get(key), list):
            raise ConfigError(f"{path} is damaged: '{key}' is missing or not a list")
        if any(not isinstance(item, dict) or not isinstance(item.get("mac"), str) or not item["mac"]
               for item in record[key]):
            raise ConfigError(f"{path} is damaged: '{key}' contains an item without a MAC address")
    return record


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


def _location(r: Record) -> str:
    where = r.get("uplink") or ""
    return f"{where} port {r['uplink_port']}" if where and r.get("uplink_port") else where


def _compare(old: Record, new: Record, fields: List[Tuple[str, str]]) -> List[Dict[str, Any]]:
    changes = []
    for field, _label in fields:
        if field == "location":
            a, b = _location(old), _location(new)
            if not (a and b):          # unknown on one side (e.g. offline Wi-Fi): not a move
                continue
        else:
            a, b = old.get(field), new.get(field)
        if a != b:
            changes.append({"field": field, "old": a, "new": b})
    return changes


def _diff_list(old: List[Record], new: List[Record], fields: List[Tuple[str, str]]) -> Dict[str, Any]:
    before, after = {r["mac"]: r for r in old}, {r["mac"]: r for r in new}
    changed = []
    for mac in sorted(set(before) & set(after)):
        changes = _compare(before[mac], after[mac], fields)
        if changes:
            changed.append({"mac": mac, "name": after[mac].get("name") or before[mac].get("name") or mac,
                            "changes": changes})
    return {"added": [after[m] for m in sorted(set(after) - set(before))],
            "removed": [before[m] for m in sorted(set(before) - set(after))],
            "changed": changed}


def diff_snapshots(old: Record, new: Record) -> Dict[str, Any]:
    """Everything that differs between two captured inventories, matched by MAC address."""
    controller = []
    old_v, new_v = old["controller"].get("application_version"), new["controller"].get("application_version")
    if old_v and new_v and old_v != new_v:
        controller.append({"field": "application_version", "old": old_v, "new": new_v})
    result = {
        "same_site": (old.get("site") or {}).get("id") == (new.get("site") or {}).get("id"),
        "controller": controller,
        "devices": _diff_list(old["devices"], new["devices"], DEVICE_FIELDS),
        "clients": _diff_list(old["clients"], new["clients"], CLIENT_FIELDS),
        "reservations": _diff_list(old["reservations"], new["reservations"], RESERVATION_FIELDS),
    }
    result["total"] = len(controller) + sum(
        len(part["added"]) + len(part["removed"]) + sum(len(c["changes"]) for c in part["changed"])
        for part in (result["devices"], result["clients"], result["reservations"]))
    return result


# -- rendering ---------------------------------------------------------------

def _value(field: str, v: Any) -> str:
    if field == "groups":
        return ", ".join(v) if v else "none"
    return "none" if v in (None, "") else str(v)


def _describe(kind: str, r: Record) -> str:
    bits = {"devices": [r.get("model"), r.get("ip")], "clients": [r.get("ip"), r.get("connection")],
            "reservations": [r.get("reserved_ip"), r.get("network")]}[kind]
    detail = ", ".join(str(b) for b in bits if b)
    return f"{r.get('name') or r['mac']} ({detail})" if detail else str(r.get("name") or r["mac"])


def _section(kind: str, title: str, part: Dict[str, Any], fields: List[Tuple[str, str]],
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


def render_diff(diff: Dict[str, Any], old_label: str, new_label: str, show_all: bool = False) -> str:
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


def label_for(record: Record, name: str) -> str:
    """'name (captured 2026-09-30 20:15)' for a saved snapshot."""
    try:
        when = datetime.fromisoformat(record["captured_at"]).strftime("%Y-%m-%d %H:%M")
    except (KeyError, ValueError):
        return name
    return f"{name} (captured {when})"
