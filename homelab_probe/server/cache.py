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
* **Bounded bookkeeping:** entries and failures are capped and expired; per-key locks are removed after their last user.

Answers are copied on the way in and out: nobody can change what another request will see.
"""

import contextlib
import contextvars
import copy
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Dict, Hashable, Iterator, List, Optional, Tuple

from .. import logs
from ..client import UniFiAPIError

DEFAULT_TTL = 30.0           # seconds an answer is fresh
DEFAULT_STALE = 600.0        # seconds an answer may still be served when the controller cannot be read
DEFAULT_ERROR_TTL = 5.0      # seconds a failure is remembered
DEFAULT_MAX_ENTRIES = 1024
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


@dataclass
class _KeyLock:
    lock: threading.Lock
    users: int = 0


class ResponseCache:
    def __init__(self, ttl: float = DEFAULT_TTL, stale_ttl: float = DEFAULT_STALE, error_ttl: float = DEFAULT_ERROR_TTL,
                 max_in_flight: int = 6, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self.ttl, self.stale_ttl, self.error_ttl = ttl, stale_ttl, error_ttl
        self._clock, self._wall = clock, wall
        self.max_entries = max(1, max_entries)
        self._entries: OrderedDict[Hashable, _Entry] = OrderedDict()
        self._errors: OrderedDict[Hashable, Tuple[float, UniFiAPIError]] = OrderedDict()
        self._key_locks: Dict[Hashable, _KeyLock] = {}
        self._generation = 0
        self._guard = threading.Lock()                  # protects the cache maps and generation
        self._wire = threading.BoundedSemaphore(max(1, max_in_flight))

    def clear(self) -> None:
        """Forget every answer and prevent earlier in-flight reads from repopulating the cache."""
        with self._guard:
            self._generation += 1
            self._entries.clear()
            self._errors.clear()

    def fetch(self, key: Hashable, read: Callable[[], Any], label: str = "") -> Any:
        """The answer for ``key``: from the cache while it is fresh, else from ``read()`` (once, however many ask)."""
        with self._guard:
            self._prune_locked(self._clock())
            generation = self._generation
        hit = self._fresh(key)
        if hit is not None:
            return self._give(hit, label, "hit")
        with self._lock_for(key):
            hit = self._fresh(key)                       # someone may have read it while this caller waited
            if hit is not None:
                return self._give(hit, label, "hit")
            failure = self._recent_error(key)
            if failure is not None:
                stale = self._stale(key)
                if stale is not None:
                    return self._serve_stale(stale, failure, label)
                self._note(label, "coalesced")
                raise failure
            try:
                with self._wire:
                    value = read()
            except UniFiAPIError as error:
                return self._after_failure(key, error, label, generation)
            entry = _Entry(copy.deepcopy(value), self._clock(), self._wall())
            with self._guard:
                if generation == self._generation:
                    self._entries[key] = entry
                    self._entries.move_to_end(key)
                    self._errors.pop(key, None)
                    self._prune_locked(self._clock())
            return self._give(entry, label, "miss")

    # -- the pieces --------------------------------------------------------------------------------------------

    @contextlib.contextmanager
    def _lock_for(self, key: Hashable) -> Iterator[None]:
        """Hold a per-key lock and remove its bookkeeping after the last waiter leaves."""
        with self._guard:
            state = self._key_locks.get(key)
            if state is None:
                state = _KeyLock(threading.Lock())
                self._key_locks[key] = state
            state.users += 1
        try:
            with state.lock:
                yield
        finally:
            with self._guard:
                state.users -= 1
                if state.users == 0:
                    self._key_locks.pop(key, None)

    def _fresh(self, key: Hashable) -> Optional[_Entry]:
        with self._guard:
            entry = self._entries.get(key)
            if entry is not None and self._clock() - entry.taken < self.ttl:
                self._entries.move_to_end(key)
                return entry
        return None

    def _stale(self, key: Hashable) -> Optional[_Entry]:
        with self._guard:
            entry = self._entries.get(key)
            if entry is not None and self._clock() - entry.taken <= self.stale_ttl:
                self._entries.move_to_end(key)
                return entry
        return None

    def _recent_error(self, key: Hashable) -> Optional[UniFiAPIError]:
        with self._guard:
            remembered = self._errors.get(key)
            if remembered is not None and self._clock() - remembered[0] < self.error_ttl:
                self._errors.move_to_end(key)
                return remembered[1]
        return None

    def _after_failure(self, key: Hashable, error: UniFiAPIError, label: str, generation: int) -> Any:
        with self._guard:
            entry = self._entries.get(key) if generation == self._generation else None
            if entry is None or self._clock() - entry.taken > self.stale_ttl:
                if generation == self._generation:
                    self._errors[key] = (self._clock(), error)
                    self._errors.move_to_end(key)
                entry = None
            else:
                self._entries.move_to_end(key)
                self._errors[key] = (self._clock(), error)
                self._errors.move_to_end(key)
            self._prune_locked(self._clock())
        if entry is None:
            raise error
        return self._serve_stale(entry, error, label)

    def _serve_stale(self, entry: _Entry, error: UniFiAPIError, label: str) -> Any:
        moment = time.strftime("%H:%M:%S", time.localtime(entry.wall))
        logs.warn(f"the controller could not be read ({error.kind or 'error'}); served from the cache as of {moment}")
        return self._give(entry, label, "stale")

    def _prune_locked(self, now: float) -> None:
        for key, entry in list(self._entries.items()):
            if now - entry.taken > self.stale_ttl:
                del self._entries[key]
        for key, (taken, _) in list(self._errors.items()):
            if now - taken >= self.error_ttl:
                del self._errors[key]
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        while len(self._errors) > self.max_entries:
            self._errors.popitem(last=False)

    def _give(self, entry: _Entry, label: str, outcome: str) -> Any:
        ages = _ages.get()
        if ages is not None:
            ages.append(entry.wall)
        self._note(label, outcome)
        return copy.deepcopy(entry.value)

    @staticmethod
    def _note(label: str, outcome: str) -> None:
        logs.log_event(_log, logging.DEBUG, "server.cache", f"{label or 'read'}: {outcome}", outcome=outcome,
                       label=label)
