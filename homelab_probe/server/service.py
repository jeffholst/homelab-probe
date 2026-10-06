"""The server's only way to the controller: ``ControllerService`` hands out clients that read through one cache.

``UniFiClient`` keeps per-run counters and a "parallel section" flag, so one client cannot serve overlapping requests.
The service therefore makes **a new client for every document it builds** and shares only what is safe to share: one
``requests`` session (a connection pool that no cookie can change) and one ``ResponseCache``. Every read of a client
goes through ``CachingClient``, which overrides the two methods that all reads end in, ``_get`` and
``_post_system_log``. The documents and the snapshot code are the ones the command line uses, unchanged.

The only requests that can leave are the ones ``UniFiClient`` can make: GETs, and the one read-only event-log POST.
"""

import http.cookiejar
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Hashable, List, Optional, Tuple

from .. import logs
from ..client import GET_RETRIES, SYSTEM_LOG_QUERY_KEYS, UniFiAPIError, UniFiClient
from ..config import Config
from ..demo.session import DemoSession
from ..documents import Document
from .cache import DEFAULT_ERROR_TTL, DEFAULT_STALE, DEFAULT_TTL, ResponseCache, track_ages, track_stale

_log = logging.getLogger(__name__)
TIME_KEYS = ("timestampFrom", "timestampTo")      # the event-log window: its edges move every millisecond


def _canonical(params: Optional[Dict[str, Any]]) -> Tuple[Tuple[str, str], ...]:
    return tuple(sorted((str(k), str(v)) for k, v in (params or {}).items()))


def request_key(method: str, path: str, params: Optional[Dict[str, Any]], ttl: float) -> Hashable:
    """What identifies a read. The edges of an event-log window are rounded down to ``ttl`` seconds, otherwise no two
    queries would ever be the same (each says "the last 24 hours" from its own millisecond)."""
    if params and any(key in params for key in TIME_KEYS):
        step = max(1, int(ttl * 1000))
        params = {k: (int(v) // step * step if k in TIME_KEYS and isinstance(v, (int, float)) else v)
                  for k, v in params.items()}
    return (method, path, _canonical(params))


class CachingClient(UniFiClient):
    """A ``UniFiClient`` whose GETs and event-log query are answered by a shared ``ResponseCache`` when they can be."""

    def __init__(self, cache: ResponseCache, session: Any, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.session = session                          # the shared one; the one made by the base class is dropped
        self.cache = cache

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self.cache.fetch(request_key("GET", path, params, self.cache.ttl),
                                lambda: super(CachingClient, self)._get(path, params), f"GET {path}")

    def _post_system_log(self, site_ref: str, query: Dict[str, Any]) -> Any:
        unexpected = set(query) - SYSTEM_LOG_QUERY_KEYS         # the same rule, before the cache can answer
        if unexpected:
            raise ValueError(f"unsupported system-log query key(s): {', '.join(sorted(unexpected))}")
        return self.cache.fetch(request_key("POST", site_ref, query, self.cache.ttl),
                                lambda: super(CachingClient, self)._post_system_log(site_ref, query),
                                "POST system-log")


class ControllerService:
    """Builds documents for the server's routes: ``build`` runs a document function with a client of its own."""

    def __init__(self, config: Config, *, session: Any = None, demo: bool = False, ttl: float = DEFAULT_TTL,
                 stale_ttl: float = DEFAULT_STALE, error_ttl: float = DEFAULT_ERROR_TTL,
                 max_in_flight: Optional[int] = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.config, self.demo = config, demo
        self.cache = ResponseCache(ttl, stale_ttl, error_ttl, max_in_flight or config.parallel, clock)
        if session is None:
            session = DemoSession() if demo else self._real_session(config)
        self.session = session
        self._clock = clock
        self._last_refresh: Optional[float] = None
        self._refresh_lock = threading.Lock()
        self.last_warnings = 0              # warnings of the most recent build (an optional read failed, or stale data)
        self.last_build: Optional[float] = None

    @staticmethod
    def _real_session(config: Config) -> Any:
        """The session every client shares: the pool and the API key of ``UniFiClient.from_config``, and no cookies
        (the controller could set one; a long-lived session must not carry it into the next request)."""
        session = UniFiClient.from_config(config).session
        session.cookies.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
        return session

    def client(self) -> CachingClient:
        """A new client for one document build, reading through the shared cache and session."""
        config = self.config
        return CachingClient(self.cache, self.session, config.controller_url, config.api_key, config.verify_ssl,
                             config.timeout, GET_RETRIES, config.parallel)

    def refresh(self, min_interval: float = 0.0) -> bool:
        """Forget the cache: the next requests read the controller again. False (and nothing forgotten) when the cache
        was cleared less than ``min_interval`` seconds ago, so a loop of refreshes cannot become a loop of reads."""
        with self._refresh_lock:
            now = self._clock()
            if self._last_refresh is not None and now - self._last_refresh < min_interval:
                return False
            self._last_refresh = now
        self.cache.clear()
        return True

    def build(self, make: Callable[[UniFiClient], Document]) -> "Built":
        """``make(client)`` with a client of its own. ``generated_at`` is the age of the oldest answer it used (UTC),
        so a page built from the cache says how old its data is; ``warnings`` are the document's, including a note
        for every answer served stale."""
        with track_ages() as ages, track_stale(), logs.collect_warnings(quiet=True) as served:
            document = make(self.client())
        moment = min(ages) if ages else time.time()
        stamp = datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        warnings = list(dict.fromkeys([*document.warnings, *served]))
        self.last_warnings, self.last_build = len(warnings), time.time()
        return Built(document, stamp, warnings)

    def ready(self) -> Tuple[bool, str]:
        """Can the controller be read? One cached read of its application info; the reason is a fixed word."""
        try:
            self.build(lambda client: Document("info", client.info()))
        except UniFiAPIError as error:
            return False, error.kind or "error"
        return True, ""


@dataclass(frozen=True)
class Built:
    """A document with when its data was read (UTC, ISO 8601) and the warnings of the build."""

    document: Document
    generated_at: str
    warnings: List[str]
