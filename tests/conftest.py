"""Fake controller backed by a synthetic, sanitized fixture (no real network data)."""

import copy
import json
from pathlib import Path

import pytest

from unifi_sentinel.client import UniFiClient

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "controller.json").read_text())
INTEGRATION = "/proxy/network/integration/v1"
LEGACY = "/proxy/network/api/s/default/stat/"
LEGACY_REST = "/proxy/network/api/s/default/rest/"
LEGACY_V2 = "/proxy/network/v2/api/site/default/"


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


class FakeSession:
    """Routes GETs to the fixture. Set ``status`` to force an error response."""

    def __init__(self, fixture=FIXTURE):
        self.fx = copy.deepcopy(fixture)  # tests may mutate the data; never share it
        self.headers = {}
        self.status = None
        self.calls = []

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
        if path.startswith(LEGACY_V2):
            return FakeResponse(200, fx["legacy_v2"][path[len(LEGACY_V2):]])
        if path.startswith(LEGACY_REST):
            return FakeResponse(200, {"data": fx["legacy_rest"][path[len(LEGACY_REST):]]})
        if path.startswith(LEGACY):
            return FakeResponse(200, {"data": fx["legacy"][path[len(LEGACY):]]})
        return FakeResponse(404, {})


@pytest.fixture
def fake_client():
    client = UniFiClient("https://controller", "key")
    client.session = FakeSession()
    return client
