"""HTTP client for the UniFi Network controller.

Primary API: the official Integration API (``/proxy/network/integration/v1``),
which is documented, versioned and paginated. Site IDs are UUIDs.

Legacy API: the undocumented ``/proxy/network/api`` endpoints. The Integration
API does not expose per-port counters or which switch port a client is on, so
the legacy endpoints remain available for that data only.

Every request is a GET, with one approved exception: the event log
(``v2/system-log/all``) can only be queried with a POST that carries the filter.
That query changes nothing on the controller, and it is sent only by
``UniFiClient.system_log`` to that one fixed path. There is deliberately no
general-purpose POST/PUT/PATCH/DELETE method on this class.
"""

import logging
import threading
import time
import warnings
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

import requests
import urllib3
from requests.adapters import HTTPAdapter

from .config import DEFAULT_TIMEOUT, Config
from .logs import log_event

_log = logging.getLogger(__name__)

INTEGRATION_PREFIX = "/proxy/network/integration/v1"
LEGACY_PREFIX = "/proxy/network/api"
LEGACY_V2_PREFIX = "/proxy/network/v2/api"
# The single approved POST: a read-only query of the event log.
SYSTEM_LOG_PATH = LEGACY_V2_PREFIX + "/site/{site}/system-log/all"
# The only keys a system-log query may carry (anything else raises before sending).
SYSTEM_LOG_QUERY_KEYS = frozenset({
    "timestampFrom", "timestampTo", "pageNumber", "pageSize",
    "categories", "severities", "keys", "searchText",
})
PAGE_SIZE = 200
GET_RETRIES = 2                        # extra attempts for a GET after a transient failure
RETRY_BACKOFF_S = 0.5                  # wait before the first retry; doubles each time
RETRY_STATUSES = frozenset({502, 503, 504})   # a gateway or proxy that is briefly unavailable


def _segment(value: str) -> str:
    """A value placed in a URL path, percent-encoded so it cannot add path parts or a query."""
    encoded = quote(str(value), safe="")
    return encoded.replace(".", "%2E") if encoded in {".", ".."} else encoded


class UniFiAPIError(Exception):
    """Raised for connection failures and non-2xx responses. ``kind`` says which (``tls``, ``unauthorized``,
    ``forbidden``, ``timeout``, ``connection``, ``request``, ``http``, ``bad_body`` or ``site``) and ``status`` is the
    HTTP status when there was one, so a caller can explain a failure without matching the wording of the message."""

    def __init__(self, message: str = "", *, kind: str = "", status: Optional[int] = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


class UniFiClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        verify_ssl: bool | str = True,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = GET_RETRIES,
        workers: int = 1,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.verify_ssl = verify_ssl           # True, False, or the path of a CA bundle
        self.timeout = timeout
        self.retries = max(0, retries)
        self._sleep = time.sleep               # replaced in tests so backoff does not wait
        # --verbose: called with one line per request attempt (never with the API key, a response
        # body or a query's values); the counters feed the summary line at the end of a run.
        self.trace: Optional[Callable[[str], None]] = None
        self.attempts_made = 0
        self.attempts_retried = 0
        self.seconds_waiting = 0.0
        self.workers = max(1, workers)         # requests in flight at once during a parallel read (1: one by one)
        self._lock = threading.Lock()          # guards the counters and the trace output across threads
        self._in_parallel = False
        # a ``requests.Session``, or anything with its ``get``/``post``/``headers`` (the demo and the tests replace it)
        self.session: Any = requests.Session()
        if self.workers > 1:
            adapter = HTTPAdapter(pool_connections=self.workers, pool_maxsize=self.workers)
            self.session.mount("https://", adapter)
            self.session.mount("http://", adapter)
        self.session.headers.update(
            {"X-API-KEY": api_key, "Accept": "application/json"}
        )

    @classmethod
    def from_config(cls, config: Config) -> "UniFiClient":
        return cls(config.controller_url, config.api_key, config.verify_ssl, config.timeout,
                   workers=config.parallel)

    @contextmanager
    def parallel(self) -> Iterator[Optional[ThreadPoolExecutor]]:
        """A section in which independent reads may run at the same time, on a pool of ``workers`` threads
        (``None`` when ``workers`` is 1, meaning: do them one by one). Only GETs and the event-log query are
        ever submitted, one request per call, exactly as in the sequential case.

        Sharing one ``requests.Session`` between threads is safe here because nothing mutates it while the
        section runs (no cookies, no changed headers); the connection pool is sized for the workers. The
        "unverified HTTPS request" warning is hidden once for the whole section (``warnings.catch_warnings``
        changes global state, so it must not be entered by several threads at once) and then restored.
        """
        if self.workers <= 1:
            yield None
            return
        with warnings.catch_warnings():
            if self.verify_ssl is False:
                warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
            self._in_parallel = True
            try:
                with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="unifi") as pool:
                    yield pool
            finally:
                self._in_parallel = False

    # -- transport ---------------------------------------------------------

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base_url}{path}"
        label = f"GET {path}" + (f"?{urlencode(params)}" if params else "")
        return self._exchange(
            lambda: self.session.get(url, params=params, verify=self.verify_ssl, timeout=self.timeout),
            url, retries=self.retries, label=label)

    def _post_system_log(self, site_ref: str, query: Dict[str, Any]) -> Any:
        """POST a read-only query to the fixed system-log path. Never takes a path."""
        unexpected = set(query) - SYSTEM_LOG_QUERY_KEYS
        if unexpected:
            raise ValueError(f"unsupported system-log query key(s): {', '.join(sorted(unexpected))}")
        url = self.base_url + SYSTEM_LOG_PATH.format(site=_segment(site_ref))
        # Not retried: it is the one request that is not a GET, so it stays as plain as possible.
        label = "POST " + SYSTEM_LOG_PATH.format(site=_segment(site_ref)) + f" (query keys: {', '.join(sorted(query))})"
        return self._exchange(
            lambda: self.session.post(url, json=query, verify=self.verify_ssl, timeout=self.timeout),
            url, retries=0, label=label)

    def _exchange(self, send: Callable[[], requests.Response], url: str, retries: int, label: str = "") -> Any:
        """Run one request (``send``) with the error handling shared by GET and POST.

        A connection failure, a timeout or a 502/503/504 is retried up to ``retries`` more times
        with a doubling pause; a TLS failure, any other status and a bad body are not, because
        trying again cannot change them. Everything that can be shown is stripped of the API key.
        """
        attempts = retries + 1
        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            try:
                with self._quiet_insecure_warnings():
                    resp = send()
            except requests.exceptions.SSLError as e:
                self._note(label, started, "TLS certificate verification failed")
                raise self._tls_error(e) from e
            except requests.exceptions.Timeout as e:
                self._note(label, started, "timed out")
                failure = UniFiAPIError(
                    f"timed out after {self.timeout:g} s{self._tries(attempt)}: {url}; "
                    "a slow gateway may need a longer --timeout", kind="timeout")
                cause: BaseException = e
            except requests.exceptions.ConnectionError as e:
                self._note(label, started, "connection error")
                failure = UniFiAPIError(
                    f"Connection error for {url}{self._tries(attempt)}: {self._redact(str(e))}", kind="connection")
                cause = e
            except requests.exceptions.RequestException as e:
                self._note(label, started, "request error")
                raise UniFiAPIError(
                    f"Request error for {url}: {self._redact(str(e))}", kind="request") from e
            except OSError as e:               # e.g. requests cannot read the CA bundle file
                self._note(label, started, "could not send")
                raise UniFiAPIError(f"cannot make the request to {url}: {self._redact(str(e))}", kind="request") from e
            else:
                self._note(label, started, str(resp.status_code))
                if resp.status_code in RETRY_STATUSES and attempt < attempts:
                    self._back_off(label, attempt, attempts)
                    continue
                return self._decode_response(resp, url, attempt)
            if attempt == attempts:
                raise failure from cause
            self._back_off(label, attempt, attempts)
        raise AssertionError("unreachable")     # pragma: no cover

    def _note(self, label: str, started: float, outcome: str) -> None:
        """Count one attempt and, with --verbose, say how it went and how long it took."""
        elapsed = time.perf_counter() - started
        with self._lock:
            self.attempts_made += 1
            self.seconds_waiting += elapsed
            if self.tracing():
                method, _, path = label.partition(" ")
                self.debug(f"{label} -> {outcome} ({elapsed * 1000:.0f} ms)", "http.request",
                           method=method, path=path, outcome=outcome, duration_ms=round(elapsed * 1000))

    def _back_off(self, label: str, attempt: int, attempts: int) -> None:
        pause = RETRY_BACKOFF_S * 2 ** (attempt - 1)
        with self._lock:
            self.attempts_retried += 1
            if self.tracing():
                method, _, path = label.partition(" ")
                self.debug(f"{label} -> retrying in {pause:g} s (attempt {attempt + 1} of {attempts})", "http.retry",
                           method=method, path=path, pause_s=pause, attempt=attempt + 1, attempts=attempts)
        self._sleep(pause)

    def tracing(self) -> bool:
        """True when a trace line would go anywhere: a ``trace`` function is set or DEBUG logging is on."""
        return self.trace is not None or _log.isEnabledFor(logging.DEBUG)

    def debug(self, message: str, event: str, /, **fields: Any) -> None:
        """One trace line (``--verbose``): a DEBUG record with the API key hidden, and ``trace`` when it is set.
        The message and fields come only from the path, status and fixed words, never from a response."""
        line = self._redact(message)
        log_event(_log, logging.DEBUG, event, line, **fields)
        if self.trace is not None:
            self.trace(line)

    def summary(self) -> str:
        """One line for the end of a --verbose run."""
        retried = f", {self.attempts_retried} retried" if self.attempts_retried else ""
        return (f"{self.attempts_made} request(s){retried}, {self.seconds_waiting:.1f} s in requests "
                "(added up over all of them, so more than the wall time when they overlap)")

    @staticmethod
    def _tries(attempt: int) -> str:
        return f" (after {attempt} attempts)" if attempt > 1 else ""

    @contextmanager
    def _quiet_insecure_warnings(self) -> Iterator[None]:
        """Hide urllib3's "unverified HTTPS request" warning for this client's own requests only.

        The user chose ``UNIFI_VERIFY_SSL=false``, so repeating the warning is noise; but it is not
        turned off for the whole process (the old ``urllib3.disable_warnings`` did that).
        ``warnings.catch_warnings`` is not thread-safe, so a future parallel fetch must set the
        filter once up front instead of per request.
        """
        if self.verify_ssl is False and not self._in_parallel:       # a parallel section did it once already
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
                yield
        else:
            yield

    def _redact(self, text: str) -> str:
        """``text`` with the API key hidden, for anything taken from a response or an exception
        (a misbehaving proxy or controller could echo the request headers)."""
        key = self.session.headers.get("X-API-KEY")
        return text.replace(key, "***") if isinstance(key, str) and key else text

    def _tls_error(self, error: requests.exceptions.SSLError) -> UniFiAPIError:
        detail = self._redact(str(error))
        if isinstance(self.verify_ssl, str):
            if "invalid CA certificate" in detail:
                return UniFiAPIError(
                    f"TLS certificate verification failed for {self.base_url}: the certificate in "
                    f"{self.verify_ssl} (UNIFI_VERIFY_SSL) is not usable as a CA bundle. Use the CA that signed "
                    f"the controller's certificate, install a certificate with proper CA/signing metadata, or set "
                    f"UNIFI_VERIFY_SSL=false if you accept an unverified lab connection.", kind="tls"
                )
            if "IP address mismatch" in detail or "Hostname mismatch" in detail:
                return UniFiAPIError(
                    f"TLS certificate verification failed for {self.base_url}: the controller certificate is trusted "
                    f"by {self.verify_ssl} (UNIFI_VERIFY_SSL), but it is not valid for this UNIFI_URL host name or "
                    f"address. Use a URL named in the certificate, or install a certificate whose subjectAltName "
                    f"includes this host.", kind="tls"
                )
            return UniFiAPIError(
                f"TLS certificate verification failed for {self.base_url}: the certificate is not signed "
                f"by anything in the CA bundle {self.verify_ssl} (UNIFI_VERIFY_SSL). Use the CA that signed the "
                f"controller's certificate, or its own certificate file.", kind="tls"
            )
        return UniFiAPIError(
            f"TLS certificate verification failed for {self.base_url}. Install a "
            f"trusted certificate on the controller, point UNIFI_VERIFY_SSL at a CA bundle that "
            f"trusts it, or set UNIFI_VERIFY_SSL=false in .env if you accept an unverified connection.", kind="tls"
        )

    def _decode_response(self, resp: requests.Response, url: str, attempt: int = 1) -> Any:
        if resp.status_code == 401:
            raise UniFiAPIError(f"401 Unauthorized for {url}: invalid API key.", kind="unauthorized", status=401)
        if resp.status_code == 403:
            raise UniFiAPIError(
                f"403 Forbidden for {url}: the API key is valid but is not allowed to make this request. "
                "Check the key's access in Settings > Control Plane > Integrations (some legacy endpoints "
                "may also reject API keys on some controller versions).", kind="forbidden", status=403)
        if not resp.ok:
            raise UniFiAPIError(
                f"HTTP {resp.status_code} for {url}{self._tries(attempt)}: {self._redact(resp.text)[:500]}",
                kind="http", status=resp.status_code)
        try:
            return resp.json()
        except ValueError as e:
            raise UniFiAPIError(f"Non-JSON response from {url}", kind="bad_body") from e

    def _paginate(self, path: str) -> Iterator[Dict[str, Any]]:
        """Yield every item from an offset/limit paginated Integration API list."""
        offset = 0
        while True:
            page = self._get(path, {"offset": offset, "limit": PAGE_SIZE})
            items = page.get("data", [])
            yield from items
            offset += len(items)
            if not items or offset >= page.get("totalCount", offset):
                return

    # -- Integration API ---------------------------------------------------

    def info(self) -> Dict[str, Any]:
        """Application info, including the Network application version."""
        return self._get(f"{INTEGRATION_PREFIX}/info")

    def sites(self) -> List[Dict[str, Any]]:
        return list(self._paginate(f"{INTEGRATION_PREFIX}/sites"))

    def resolve_site(self, site: str) -> Dict[str, Any]:
        """Find a site by UUID, internal reference (e.g. 'default') or name."""
        sites = self.sites()
        for s in sites:
            if site in (s.get("id"), s.get("internalReference"), s.get("name")):
                return s
        known = [s.get("internalReference") or s.get("name") for s in sites]
        raise UniFiAPIError(f"Site '{site}' not found. Available: {known}", kind="site")

    def devices(self, site_id: str) -> List[Dict[str, Any]]:
        return list(self._paginate(f"{INTEGRATION_PREFIX}/sites/{site_id}/devices"))

    def device(self, site_id: str, device_id: str) -> Dict[str, Any]:
        return self._get(f"{INTEGRATION_PREFIX}/sites/{site_id}/devices/{device_id}")

    def device_statistics(self, site_id: str, device_id: str) -> Dict[str, Any]:
        return self._get(
            f"{INTEGRATION_PREFIX}/sites/{site_id}/devices/{device_id}/statistics/latest"
        )

    def clients(self, site_id: str) -> List[Dict[str, Any]]:
        """Currently connected clients."""
        return list(self._paginate(f"{INTEGRATION_PREFIX}/sites/{site_id}/clients"))

    # -- Legacy API --------------------------------------------------------

    def legacy_stat(self, site_ref: str, resource: str) -> List[Dict[str, Any]]:
        """GET /api/s/{site}/stat/{resource}. ``site_ref`` is the internal
        reference (e.g. 'default'), not the UUID."""
        body = self._get(f"{LEGACY_PREFIX}/s/{_segment(site_ref)}/stat/{resource}")
        return body.get("data", [])

    def legacy_rest(self, site_ref: str, resource: str) -> List[Dict[str, Any]]:
        """GET /api/s/{site}/rest/{resource} (e.g. 'networkconf')."""
        body = self._get(f"{LEGACY_PREFIX}/s/{_segment(site_ref)}/rest/{resource}")
        return body.get("data", [])

    def legacy_v2(self, site_ref: str, resource: str) -> List[Dict[str, Any]]:
        """GET /v2/api/site/{site}/{resource} (e.g. 'network-members-groups')."""
        body = self._get(f"{LEGACY_V2_PREFIX}/site/{_segment(site_ref)}/{resource}")
        return body.get("data", []) if isinstance(body, dict) else body

    # -- event log (the one approved POST) ----------------------------------

    def system_log(self, site_ref: str, query: Dict[str, Any]) -> Dict[str, Any]:
        """One page of the controller's event log (newest first).

        ``query`` may use ``timestampFrom``/``timestampTo`` (milliseconds), ``pageNumber``,
        ``pageSize``, and the server-side filters ``categories``, ``severities``, ``keys``
        and ``searchText``. Returns ``{"data": [...], "page_number", "total_element_count",
        "total_page_count"}``.
        """
        body = self._post_system_log(site_ref, query)
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise UniFiAPIError("Unexpected response from the event log (no 'data' list)", kind="bad_body")
        return body
