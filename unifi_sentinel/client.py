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

from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import quote

import requests
import urllib3

from .config import Config

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


def _segment(value: str) -> str:
    """A value placed in a URL path, percent-encoded so it cannot add path parts or a query."""
    encoded = quote(str(value), safe="")
    return encoded.replace(".", "%2E") if encoded in {".", ".."} else encoded


class UniFiAPIError(Exception):
    """Raised for connection failures and non-2xx responses."""


class UniFiClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        verify_ssl: bool = True,
        timeout: int = 15,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        if not verify_ssl:
            # User opted out (VERIFY_SSL=false); suppress the per-request warning.
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.session = requests.Session()
        self.session.headers.update(
            {"X-API-KEY": api_key, "Accept": "application/json"}
        )

    @classmethod
    def from_config(cls, config: Config) -> "UniFiClient":
        return cls(config.controller_url, config.api_key, config.verify_ssl)

    # -- transport ---------------------------------------------------------

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.get(
                url, params=params, verify=self.verify_ssl, timeout=self.timeout)
        except requests.exceptions.SSLError as e:
            raise self._tls_error() from e
        except requests.exceptions.RequestException as e:
            raise UniFiAPIError(f"Connection error for {url}: {self._redact(str(e))}") from e
        return self._decode_response(resp, url)

    def _post_system_log(self, site_ref: str, query: Dict[str, Any]) -> Any:
        """POST a read-only query to the fixed system-log path. Never takes a path."""
        unexpected = set(query) - SYSTEM_LOG_QUERY_KEYS
        if unexpected:
            raise ValueError(f"unsupported system-log query key(s): {', '.join(sorted(unexpected))}")
        url = self.base_url + SYSTEM_LOG_PATH.format(site=_segment(site_ref))
        try:
            resp = self.session.post(
                url, json=query, verify=self.verify_ssl, timeout=self.timeout)
        except requests.exceptions.SSLError as e:
            raise self._tls_error() from e
        except requests.exceptions.RequestException as e:
            raise UniFiAPIError(f"Connection error for {url}: {self._redact(str(e))}") from e
        return self._decode_response(resp, url)

    def _redact(self, text: str) -> str:
        """``text`` with the API key hidden, for anything taken from a response or an exception
        (a misbehaving proxy or controller could echo the request headers)."""
        key = self.session.headers.get("X-API-KEY")
        return text.replace(key, "***") if isinstance(key, str) and key else text

    def _tls_error(self) -> UniFiAPIError:
        return UniFiAPIError(
            f"TLS certificate verification failed for {self.base_url}. Install a "
            f"trusted certificate on the controller, or set VERIFY_SSL=false in .env "
            f"if it uses a self-signed one."
        )

    def _decode_response(self, resp: requests.Response, url: str) -> Any:
        if resp.status_code == 401:
            raise UniFiAPIError(f"401 Unauthorized for {url}: invalid API key.")
        if not resp.ok:
            raise UniFiAPIError(f"HTTP {resp.status_code} for {url}: {self._redact(resp.text)[:500]}")
        try:
            return resp.json()
        except ValueError as e:
            raise UniFiAPIError(f"Non-JSON response from {url}") from e

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
        raise UniFiAPIError(f"Site '{site}' not found. Available: {known}")

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
            raise UniFiAPIError("Unexpected response from the event log (no 'data' list)")
        return body
