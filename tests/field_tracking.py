"""Find out which fields of each controller response the code actually reads.

The fake controller's records are wrapped in dicts and lists that note every key the program looks at
(``get``, ``[]``, ``in``), by path (``uplink.uplink_mac``, ``port_table[].up``). Running every command
against the fixture with this wrapper gives, per endpoint, the fields the code depends on: the contract
that the fixture must satisfy and that a real controller must still satisfy (see ``contract.py``).
"""

from typing import Any, Dict, Set

from conftest import FakeResponse, FakeSession
from contract import endpoint_of


def _wrap(value: Any, log: Set[str], path: str) -> Any:
    if isinstance(value, TrackedDict) or isinstance(value, TrackedList):
        return value
    if isinstance(value, dict):
        return TrackedDict(value, log, path)
    if isinstance(value, list):
        return TrackedList(value, log, path)
    return value


class TrackedDict(dict):
    def __init__(self, data: Dict[str, Any], log: Set[str], prefix: str) -> None:
        super().__init__(data)
        self._log, self._prefix = log, prefix

    def _path(self, key: Any) -> str:
        return f"{self._prefix}.{key}" if self._prefix else str(key)

    def get(self, key, default=None):
        self._log.add(self._path(key))
        return _wrap(super().get(key, default), self._log, self._path(key))

    def __getitem__(self, key):
        self._log.add(self._path(key))
        return _wrap(super().__getitem__(key), self._log, self._path(key))

    def __contains__(self, key):
        self._log.add(self._path(key))
        return super().__contains__(key)

    def items(self):          # keys that are not fixed (wan1, wan2, monitors by name): not part of the contract
        return [(k, _wrap(v, self._log, self._path("*"))) for k, v in super().items()]

    def values(self):
        return [_wrap(v, self._log, self._path("*")) for v in super().values()]


class TrackedList(list):
    def __init__(self, data, log: Set[str], prefix: str) -> None:
        super().__init__(data)
        self._log, self._prefix = log, prefix

    def __iter__(self):
        for item in super().__iter__():
            yield _wrap(item, self._log, self._prefix + "[]")

    def __getitem__(self, index):
        item = super().__getitem__(index)
        if isinstance(index, slice):
            return [_wrap(i, self._log, self._prefix + "[]") for i in item]
        return _wrap(item, self._log, self._prefix + "[]")


class TrackingSession(FakeSession):
    """A fake controller that records which fields of its responses the program reads."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.accessed: Dict[str, Set[str]] = {}

    def _track(self, response: FakeResponse, endpoint: str) -> FakeResponse:
        if not response.ok:
            return response
        log = self.accessed.setdefault(endpoint, set())
        body = response._body
        if endpoint in ("integration/device", "integration/device-statistics"):
            response._body = TrackedDict(body, log, "")
        elif isinstance(body, dict) and isinstance(body.get("data"), list):
            body["data"] = TrackedList(body["data"], log, "")
        elif isinstance(body, list):                 # some v2 endpoints answer with a bare list
            response._body = TrackedList(body, log, "")
        return response

    def get(self, url, params=None, verify=True, timeout=None):
        response = super().get(url, params=params, verify=verify, timeout=timeout)
        return self._track(response, endpoint_of("/" + url.split("://", 1)[1].split("/", 1)[1]))

    def post(self, url, json=None, verify=True, timeout=None):
        response = super().post(url, json=json, verify=verify, timeout=timeout)
        return self._track(response, "legacy/system-log")
