#!/usr/bin/env python3
"""Record a real controller into a sanitised fixture (a development tool, not part of the package).

    uv run tools/record_fixture.py [--output FILE] [--env-file FILE] [--event-days N]

It reads the controller named by ``CONTROLLER_URL``/``API_KEY`` (environment or ``./.env``) once, the way ``diagnose``
does, with GET requests and the one approved read-only event log query (the recording session refuses anything
else). From what came back it keeps only the fields ``tests/contract.py`` lists (nothing else the controller says
about itself, its users or its keys is ever written), replaces every MAC, address, id and name with a synthetic
one (``tools/sanitize.py``), proves nothing real is left (the leak check), and writes the result in the shape of
``tests/fixtures/controller.json``, owner-only, to ``tools/recorded/controller.json`` (git-ignored).

The recording is for checking the contract and reproducing a problem on realistic data; it does not replace the
hand-written fixture, which the tests depend on name by name. Output to the terminal is counts and field names,
never a value. Nothing is written when the leak check fails.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from contract import CONTRACT, RecordingSession, endpoint_of, problems, records_of, segments  # noqa: E402
from sanitize import LeakError, Sanitizer, check_no_leaks  # noqa: E402

from unifi_sentinel.client import UniFiAPIError, UniFiClient  # noqa: E402
from unifi_sentinel.config import ConfigError, load_config  # noqa: E402
from unifi_sentinel.snapshot import EventQuery, Needs, collect_snapshot  # noqa: E402

DEFAULT_OUTPUT = Path(__file__).resolve().parent / "recorded" / "controller.json"
INFO_FIELDS = [["applicationVersion"]]


def project(value: Any, specs: List[List[str]]) -> Any:
    """Keep only what ``specs`` (field paths as segment lists) name; a declared leaf keeps its whole subtree."""
    if any(not spec for spec in specs) and not any(spec for spec in specs):
        return value
    specs = [spec for spec in specs if spec]
    if isinstance(value, list):
        rest = [spec[1:] for spec in specs if spec[0] == "[]"]
        return [project(item, rest) for item in value] if rest else []
    if isinstance(value, dict):
        kept = {}
        for key, item in value.items():
            rest = [spec[1:] for spec in specs if spec[0] in (key, "*")]
            if rest:
                kept[key] = project(item, rest)
        return kept
    return value


def _specs(endpoint: str) -> List[List[str]]:
    return [segments(path) for path in sorted(CONTRACT[endpoint].declared())]


def build_fixture(exchanges: List[Tuple[str, str, Any]], now_ms: float) -> Dict[str, Any]:
    """The recorded responses in the shape of ``tests/fixtures/controller.json``, reduced to the contract.

    Times become ages (``age_s``, ``last_seen_age_s``), as the fixture stores them, so the recording stays
    current whenever it is replayed."""
    fx: Dict[str, Any] = {"info": {}, "sites": [], "devices": [], "device_detail": {}, "device_stats": {},
                          "clients": [], "legacy": {}, "legacy_rest": {}, "legacy_v2": {}, "system_log": []}
    for _method, path, body in exchanges:
        endpoint = endpoint_of(path)
        if path.endswith("/integration/v1/info"):
            fx["info"] = project(body, INFO_FIELDS)
        elif endpoint not in CONTRACT:
            continue
        elif endpoint == "integration/device":
            fx["device_detail"][path.rsplit("/", 1)[1]] = project(body, _specs(endpoint))
        elif endpoint == "integration/device-statistics":
            fx["device_stats"][path.split("/devices/", 1)[1].split("/", 1)[0]] = project(body, _specs(endpoint))
        else:
            kept = [project(record, _specs(endpoint)) for record in records_of(body)]
            if endpoint.startswith("integration/"):
                fx[endpoint.split("/", 1)[1]].extend(kept)
            elif endpoint == "legacy/system-log":
                fx["system_log"].extend(kept)
            elif endpoint.startswith("legacy/v2/"):
                name = endpoint.rsplit("/", 1)[1]
                fx["legacy_v2"][name] = {"data": kept} if name == "speedtest" else kept
            else:
                fx["legacy_rest" if "/rest/" in path else "legacy"][endpoint.rsplit("/", 1)[1]] = kept
    for test in fx["legacy_v2"].get("speedtest", {}).get("data", []):
        if isinstance(test.get("time"), (int, float)):
            test["age_s"] = max(0, int((now_ms - test.pop("time")) / 1000))
    for user in fx["legacy"].get("alluser", []):
        if isinstance(user.get("last_seen"), (int, float)):
            user["last_seen_age_s"] = max(0, int(now_ms / 1000 - user.pop("last_seen")))
    for event in fx["system_log"]:
        if isinstance(event.get("timestamp"), (int, float)):
            event["age_s"] = max(0, int((now_ms - event.pop("timestamp")) / 1000))
    return fx


def site_presets(exchanges: List[Tuple[str, str, Any]], fx: Dict[str, Any]) -> Dict[str, str]:
    """The fixed names the fake controller routes by: the site used becomes ``site-1``, ``Default`` and ``default``."""
    presets: Dict[str, str] = {}
    for _method, path, _body in exchanges:
        if "/integration/v1/sites/" in path:
            site_id = path.split("/sites/", 1)[1].split("/", 1)[0]
            presets[site_id] = "site-1"
            for site in fx["sites"]:
                if site.get("id") == site_id:
                    presets[str(site.get("name"))] = "Default"
                    presets[str(site.get("internalReference"))] = "default"
            break
    return presets


def sanitise(fx: Dict[str, Any], exchanges: List[Tuple[str, str, Any]]) -> Dict[str, Any]:
    """The fixture with every identifying value replaced; raises ``LeakError`` rather than return one that leaks."""
    device_macs = [d.get("macAddress", "") for d in fx["devices"]] + [
        d.get("mac", "") for d in fx["legacy"].get("device", [])]
    sanitizer = Sanitizer(device_macs, site_presets(exchanges, fx))
    clean = sanitizer(fx)
    check_no_leaks(clean, sanitizer)
    return clean


def record(client: UniFiClient, site: str, event_days: int) -> Tuple[Dict[str, Any], Dict[str, List[str]], float]:
    """Read everything once. Returns the sanitised fixture, the contract problems found, and the recording time."""
    recording = RecordingSession(client.session)
    client.session = recording
    client.workers = 1
    now_ms = time.time() * 1000
    client.info()
    collect_snapshot(client, site, Needs(offline=True, reservations=True, groups=True, health=True, speedtests=True,
                                         neighbors=True, events=EventQuery(since_seconds=event_days * 86400)),
                     now_ms=int(now_ms))
    records = recording.records()
    found = {name: problems(name, records.get(name, [])) for name in CONTRACT}
    fx = build_fixture(recording.exchanges, now_ms)
    return sanitise(fx, recording.exchanges), {n: p for n, p in found.items() if p}, now_ms


def write_private(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        os.fchmod(f.fileno(), 0o600)
        json.dump(data, f, indent=2)
        f.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record a real controller into a sanitised fixture.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, metavar="FILE",
                        help=f"Where to write the fixture (default: {DEFAULT_OUTPUT.relative_to(ROOT)})")
    parser.add_argument("--env-file", type=Path, metavar="FILE", help="Read settings from this .env file")
    parser.add_argument("--event-days", type=int, default=7, metavar="N",
                        help="Days of event log to record (default 7)")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.env_file)
        fixture, missing, _now = record(UniFiClient.from_config(config), config.site, args.event_days)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3
    except LeakError as e:
        print(f"ERROR: nothing written; {e}", file=sys.stderr)
        return 4
    except UniFiAPIError:
        print("ERROR: nothing written; controller could not be read", file=sys.stderr)
        return 3
    write_private(args.output, fixture)
    counts = {key: len(value) for key, value in fixture.items() if isinstance(value, (list, dict))}
    print(f"wrote {args.output} ({os.path.getsize(args.output)} bytes, owner-only); leak check passed")
    print("records: " + ", ".join(f"{key} {n}" for key, n in counts.items()))
    for name, found in missing.items():
        print(f"contract: {name}: " + "; ".join(found))
    if not missing:
        print("contract: every field the code reads was present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
