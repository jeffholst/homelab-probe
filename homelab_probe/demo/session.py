"""The synthetic controller: a fixture (``controller.json``, no real network data) and the session that serves it.

The tests use it as their fake controller and ``--demo`` uses it as the controller, so there is one source of truth for
both. Nothing here makes a request: ``DemoSession`` answers from the fixture in memory, with ages (speedtests, events,
last seen) made relative to now so the data always looks recent.
"""

import copy
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

FIXTURE_PATH = Path(__file__).with_name("controller.json")
FIXTURE: Dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
INTEGRATION = "/proxy/network/integration/v1"
LEGACY = "/proxy/network/api/s/default/stat/"
LEGACY_REST = "/proxy/network/api/s/default/rest/"
LEGACY_V2 = "/proxy/network/v2/api/site/default/"
SYSTEM_LOG = LEGACY_V2 + "system-log/all"


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def _page(items, params):
    offset, limit = params["offset"], params["limit"]
    return {"offset": offset, "limit": limit, "count": len(items[offset:offset + limit]),
            "totalCount": len(items), "data": items[offset:offset + limit]}


class DemoSession:
    """Stands in for ``requests.Session``: routes the GETs (and the one event-log POST) to the synthetic fixture, so
    nothing leaves the process. Set ``status`` to force an error response (the tests do)."""

    def __init__(self, fixture: Dict[str, Any] = FIXTURE) -> None:
        self.fx = copy.deepcopy(fixture)  # tests may mutate the data; never share it
        self.headers: Dict[str, str] = {}
        self.status: Optional[int] = None
        self.calls: List[str] = []
        self.posts: List[Tuple[str, Any]] = []   # (path, body) of every POST, so tests can prove what was sent
        now = time.time() * 1000
        for t in self.fx.get("legacy_v2", {}).get("speedtest", {}).get("data", []):
            t["time"] = int(now - t.pop("age_s") * 1000)          # the fixture stores ages, not dates
        for user in self.fx.get("legacy", {}).get("alluser", []):
            if "last_seen_age_s" in user:                          # relative, like the speedtests
                user["last_seen"] = int(now / 1000 - user.pop("last_seen_age_s"))
        self.events = [
            {**{k: v for k, v in e.items() if k != "age_s"}, "timestamp": int(now - e["age_s"] * 1000)}
            for e in self.fx.get("system_log", [])]

    def post(
        self,
        url: str,
        json: Any = None,
        verify: bool | str = True,
        timeout: Optional[float] = None,
    ) -> FakeResponse:
        path = "/" + url.split("://", 1)[1].split("/", 1)[1]
        self.posts.append((path, json))
        if self.status:
            return FakeResponse(self.status, {"error": "forced"})
        if path != SYSTEM_LOG:
            return FakeResponse(404, {})   # the only POST route is the event log query
        return FakeResponse(200, _system_log(self.events, json or {}))

    def get(self, url, params=None, verify=True, timeout=None):
        path = "/" + url.split("://", 1)[1].split("/", 1)[1]
        self.calls.append(path)
        if self.status:
            return FakeResponse(self.status, {"error": "forced"})
        fx = self.fx
        if path == f"{INTEGRATION}/info":
            return FakeResponse(200, fx["info"])
        if path == f"{INTEGRATION}/sites":
            return FakeResponse(200, _page(fx["sites"], params))
        base = f"{INTEGRATION}/sites/site-1"
        if path == f"{base}/devices":
            return FakeResponse(200, _page(fx["devices"], params))
        if path == f"{base}/clients":
            return FakeResponse(200, _page(fx["clients"], params))
        if path.startswith(f"{base}/devices/"):
            rest = path[len(f"{base}/devices/"):]
            if rest.endswith("/statistics/latest"):
                body = fx["device_stats"].get(rest.split("/")[0])
            else:
                body = fx["device_detail"].get(rest)
            return FakeResponse(200, body) if body else FakeResponse(404, {})
        if path == SYSTEM_LOG:
            return FakeResponse(405, {})   # like the real controller: the event log is POST-only
        if path.startswith(LEGACY_V2):
            return FakeResponse(200, fx["legacy_v2"][path[len(LEGACY_V2):]])
        if path.startswith(LEGACY_REST):
            return FakeResponse(200, {"data": fx["legacy_rest"][path[len(LEGACY_REST):]]})
        if path.startswith(LEGACY):
            return FakeResponse(200, {"data": fx["legacy"][path[len(LEGACY):]]})
        return FakeResponse(404, {})


def _system_log(events, q):
    """The event log query, honoring the same filters as the controller."""
    chosen = [e for e in events
              if q.get("timestampFrom", 0) <= e["timestamp"] <= q.get("timestampTo", float("inf"))]
    if q.get("categories"):
        chosen = [e for e in chosen if e["category"] in q["categories"]]
    if q.get("severities"):
        chosen = [e for e in chosen if e["severity"] in q["severities"]]
    if q.get("keys"):
        chosen = [e for e in chosen if e["key"] in q["keys"]]
    if q.get("searchText"):
        needle = q["searchText"].lower()
        chosen = [e for e in chosen if needle in json.dumps(e).lower()]
    chosen.sort(key=lambda e: e["timestamp"], reverse=True)
    size, page = q.get("pageSize", 50), q.get("pageNumber", 0)
    return {"data": chosen[page * size:(page + 1) * size], "page_number": page,
            "total_element_count": len(chosen), "total_page_count": -(-len(chosen) // size)}
