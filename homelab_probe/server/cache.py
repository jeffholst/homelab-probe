"""A cache of what the controller answered, shared by every request of the server.

A browser that polls would otherwise make dozens of reads of the controller per page. The cache sits at the one place
every read goes through (``CachingClient``, below) and keeps the answer of each request for ``ttl`` seconds:

* **Single-flight:** the first caller of a key reads the controller, the others wait for that answer (one lock per
  key), so N requests at once cause one read.
* **Serve stale on error:** when a read fails and an older good answer is at most ``stale_ttl`` old, that answer is
  served with a warning that says how old it is; without one, the error is raised.
* **Error coalescing:** a failure is remembered for ``error_ttl`` seconds, so a burst of requests after an outage
  does not become a burst of reads.
* **A cap:** at most ``max_in_flight`` reads are on the wire at once, whichever request they belong to.

Answers are copied on the way in and out: nobody can change what another request will see.
"""

import contextlib
import contextvars
import copy
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Hashable, Iterator, List, Optional, Tuple

from .. import logs
from ..client import UniFiAPIError

DEFAULT_TTL = 30.0           # seconds an answer is fresh
DEFAULT_STALE = 600.0        # seconds an answer may still be served when the controller cannot be read
DEFAULT_ERROR_TTL = 5.0      # seconds a failure is remembered
_log = logging.getLogger(__name__)
_ages: contextvars.ContextVar[Optional[List[float]]] = contextvars.ContextVar("homelab_probe_cache_ages", default=None)


@contextlib.contextmanager
def track_ages() -> Iterator[List[float]]:
    """The wall-clock times (``time.time()``) at which the answers used inside the block were read from the
    controller. Worker threads of a parallel read run in a copy of the caller's context and share this list."""
    ages: List[float] = []
    token = _ages.set(ages)
    try:
        yield ages
    finally:
        _ages.reset(token)


@dataclass
class _Entry:
    value: Any
    taken: float            # the clock (monotonic) when it was read
    wall: float             # time.time() then, for the warning and for ``generated_at``


class ResponseCache:
    def __init__(self, ttl: float = DEFAULT_TTL, stale_ttl: float = DEFAULT_STALE, error_ttl: float = DEFAULT_ERROR_TTL,
                 max_in_flight: int = 6, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time) -> None:
        self.ttl, self.stale_ttl, self.error_ttl = ttl, stale_ttl, error_ttl
        self._clock, self._wall = clock, wall
        self._entries: Dict[Hashable, _Entry] = {}
        self._errors: Dict[Hashable, Tuple[float, UniFiAPIError]] = {}
        self._key_locks: Dict[Hashable, threading.Lock] = {}
        self._guard = threading.Lock()                  # protects the three dicts above
        self._wire = threading.BoundedSemaphore(max(1, max_in_flight))

    def clear(self) -> None:
        """Forget every answer (a manual refresh): the next request of each key reads the controller."""
        with self._guard:
            self._entries.clear()
            self._errors.clear()

    def fetch(self, key: Hashable, read: Callable[[], Any], label: str = "") -> Any:
        """The answer for ``key``: from the cache while it is fresh, else from ``read()`` (once, however many ask)."""
        hit = self._fresh(key)
        if hit is not None:
            return self._give(hit, label, "hit")
        with self._lock_for(key):
            hit = self._fresh(key)                       # someone may have read it while this caller waited
            if hit is not None:
                return self._give(hit, label, "hit")
            failure = self._recent_error(key)
            if failure is not None:
                self._note(label, "coalesced")
                raise failure
            try:
                with self._wire:
                    value = read()
            except UniFiAPIError as error:
                return self._after_failure(key, error, label)
            entry = _Entry(copy.deepcopy(value), self._clock(), self._wall())
            with self._guard:
                self._entries[key] = entry
                self._errors.pop(key, None)
            return self._give(entry, label, "miss", value)

    # -- the pieces --------------------------------------------------------------------------------------------

    def _lock_for(self, key: Hashable) -> threading.Lock:
        with self._guard:
            return self._key_locks.setdefault(key, threading.Lock())

    def _fresh(self, key: Hashable) -> Optional[_Entry]:
        with self._guard:
            entry = self._entries.get(key)
        return entry if entry is not None and self._clock() - entry.taken < self.ttl else None

    def _recent_error(self, key: Hashable) -> Optional[UniFiAPIError]:
        with self._guard:
            remembered = self._errors.get(key)
        if remembered is not None and self._clock() - remembered[0] < self.error_ttl:
            return remembered[1]
        return None

    def _after_failure(self, key: Hashable, error: UniFiAPIError, label: str) -> Any:
        with self._guard:
            entry = self._entries.get(key)
            if entry is None or self._clock() - entry.taken > self.stale_ttl:
                self._errors[key] = (self._clock(), error)
                entry = None
        if entry is None:
            raise error
        moment = time.strftime("%H:%M:%S", time.localtime(entry.wall))
        logs.warn(f"the controller could not be read ({error.kind or 'error'}); served from the cache as of {moment}")
        return self._give(entry, label, "stale")

    def _give(self, entry: _Entry, label: str, outcome: str, value: Any = None) -> Any:
        ages = _ages.get()
        if ages is not None:
            ages.append(entry.wall)
        self._note(label, outcome)
        return value if outcome == "miss" else copy.deepcopy(entry.value)

    @staticmethod
    def _note(label: str, outcome: str) -> None:
        logs.log_event(_log, logging.DEBUG, "server.cache", f"{label or 'read'}: {outcome}", outcome=outcome,
                       label=label)
