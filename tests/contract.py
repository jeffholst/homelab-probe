"""The contract between this program and the controller: which fields of each response the code reads.

Almost everything the tool reads comes from undocumented endpoints, so what it depends on is written down
here, once, for three uses:

* ``tests/test_contract_fixture.py`` checks that the synthetic fixture has every field (so the fixture cannot
  drift behind the code) and that the table covers every field the code reads (so the code cannot start
  reading a field that is not listed);
* ``tests/test_live_contract.py`` (``pytest -m live``, opt-in, GET only) checks a real controller still returns them;
* ``tools/record_fixture.py`` documents what a recording must contain.

Each endpoint lists its fields in three tiers, as paths (``uplink.speed``, ``port_table[].up``; ``*`` stands for
any key):

* ``always``: in every record (identity fields the code cannot work without);
* ``somewhere``: in at least one record of a normal site (it may be missing from a given device or client, but a
  controller that has none anywhere has changed);
* ``optional``: only for some models, versions or events (alternate names, rare event parameters); reported,
  never required.
"""

import re
import threading
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Set, Tuple


@dataclass(frozen=True)
class Endpoint:
    always: Tuple[str, ...] = ()
    somewhere: Tuple[str, ...] = ()
    optional: Tuple[str, ...] = ()

    def declared(self) -> Set[str]:
        return set(self.always) | set(self.somewhere) | set(self.optional)


CONTRACT: Dict[str, Endpoint] = {
    "integration/sites": Endpoint(always=("id", "internalReference", "name")),
    "integration/devices": Endpoint(
        always=("id", "macAddress", "name", "state", "model"),
        somewhere=("ipAddress", "firmwareUpdatable")),
    "integration/device": Endpoint(
        somewhere=("firmwareVersion", "firmwareUpdatable", "uplink.deviceId", "interfaces.ports[].idx",
                   "interfaces.ports[].maxSpeedMbps")),
    "integration/device-statistics": Endpoint(
        somewhere=("cpuUtilizationPct", "memoryUtilizationPct", "uptimeSec", "lastHeartbeatAt")),
    "integration/clients": Endpoint(
        always=("macAddress", "type"),
        somewhere=("name", "ipAddress", "connectedAt", "uplinkDeviceId")),
    "legacy/stat/device": Endpoint(
        always=("mac", "type"),
        somewhere=("name", "model", "uptime", "uplink.uplink_mac", "uplink.uplink_remote_port", "uplink.port_idx",
                   "uplink.speed", "uplink.max_speed", "total_max_power", "total_used_power", "vap_table[].bssid",
                   "port_table[].port_idx", "port_table[].up", "port_table[].speed", "port_table[].full_duplex",
                   "port_table[].rx_errors", "port_table[].tx_errors", "port_table[].rx_dropped",
                   "port_table[].tx_dropped", "port_table[].rx_packets", "port_table[].tx_packets",
                   "port_table[].rx_bytes", "port_table[].tx_bytes", "port_table[].link_down_count",
                   "port_table[].stp_state", "port_table[].name",
                   "radio_table_stats[].radio", "radio_table_stats[].channel", "radio_table_stats[].bw",
                   "radio_table_stats[].cu_total", "radio_table_stats[].tx_retries_pct",
                   "radio_table_stats[].satisfaction", "radio_table_stats[].num_sta", "radio_table_stats[].tx_power",
                   "wan1.up", "wan1.name", "wan1.speed", "wan1.max_speed", "wan1.full_duplex", "wan1.latency",
                   "wan1.tx_bytes-r", "wan1.rx_bytes-r"),
        optional=("overheating", "uplink.up", "port_table[].poe_class", "port_table[].poe_enable", "port_table[].poe_power",
                  "port_table[].is_uplink", "radio_table_stats[].center_channel", "radio_table_stats[].center_freq",
                  "radio_table_stats[].ext_channel", "radio_table_stats[].extension_channel",
                  "radio_table_stats[].secondary_channel")),
    "legacy/stat/sta": Endpoint(
        always=("mac",),
        somewhere=("name", "hostname", "ip", "is_wired", "oui", "sw_mac", "sw_port", "ap_mac", "essid", "radio",
                   "channel", "signal", "noise", "satisfaction", "tx_rate", "rx_rate", "wifi_tx_attempts",
                   "wifi_tx_retries_percentage", "network", "network_id", "vlan"),
        optional=("network_members_group_ids", "wlanconf_id")),
    "legacy/stat/alluser": Endpoint(
        always=("mac",),
        somewhere=("name", "hostname", "oui", "is_wired", "first_seen", "last_seen", "last_ip", "use_fixedip",
                   "fixed_ip", "last_connection_network_id", "last_connection_network_name",
                   "last_uplink_mac", "last_uplink_name", "last_uplink_remote_port", "network_members_group_ids"),
        optional=("virtual_network_override_enabled", "virtual_network_override_id")),
    "legacy/stat/health": Endpoint(
        always=("subsystem", "status"),
        somewhere=("wan_ip", "isp_name", "gw_name", "latency", "drops", "speedtest_status", "num_disconnected",
                   "num_pending", "uptime_stats.*.availability", "uptime_stats.*.latency_average",
                   "uptime_stats.*.time_period", "uptime_stats.*.monitors[].availability",
                   "uptime_stats.*.monitors[].latency_average", "uptime_stats.*.monitors[].target",
                   "uptime_stats.*.monitors[].type"),
        optional=("uptime_stats.*.alerting_monitors[].availability", "uptime_stats.*.alerting_monitors[].latency_average",
                  "uptime_stats.*.alerting_monitors[].target", "uptime_stats.*.alerting_monitors[].type")),
    "legacy/stat/rogueap": Endpoint(
        always=(),
        somewhere=("bssid", "essid", "channel", "signal", "bw", "band", "ap_mac", "oui", "security"),
        optional=("center_channel", "center_freq", "ext_channel", "extension_channel", "secondary_channel")),
    "legacy/rest/networkconf": Endpoint(
        always=("_id", "name"),
        somewhere=("purpose", "ip_subnet", "vlan_enabled", "dhcpd_enabled", "dhcpd_start", "dhcpd_stop",
                   "dhcp_relay_enabled"),
        optional=("vlan",)),
    # Wi-Fi networks (checked on Network 10.6.106: three WPA2/WPA3 networks). `security` is "wpapsk" there; "open" and
    # "wep" are the legacy API's other values and were not seen live. `wpa3_support` only appears on WPA networks.
    # `wlan_bands` is a list of "2g", "5g" and "6g"; `wpa3_transition` and `wpa_mode` come with WPA networks. The
    # passphrase (`x_passphrase`) is in the same record and is deliberately never read.
    "legacy/rest/wlanconf": Endpoint(
        always=("_id", "name", "security"),
        somewhere=("enabled", "is_guest", "l2_isolation", "hide_ssid", "networkconf_id", "wlan_bands"),
        optional=("wpa3_support", "wpa3_transition", "wpa_mode")),
    "legacy/v2/network-members-groups": Endpoint(always=("id", "name")),
    "legacy/v2/speedtest": Endpoint(
        somewhere=("time", "download_mbps", "upload_mbps", "latency_ms", "interface_name", "wan_networkgroup")),
    # Zone-based firewall (checked on Network 10.6.106). `hits` is missing from a policy that was never used.
    "legacy/v2/firewall-policies": Endpoint(
        always=("name", "enabled", "action", "predefined", "index", "protocol", "source.zone_id", "destination.zone_id",
                "source.matching_target", "destination.matching_target", "source.port_matching_type",
                "destination.port_matching_type"),
        somewhere=("source.network_ids", "destination.network_ids", "destination.ips", "destination.port",
                   "source.match_opposite_networks", "destination.match_opposite_ips"),
        optional=("hits", "source.ips", "source.port", "source.match_opposite_ips", "destination.match_opposite_networks",
                  "source.match_opposite_ports", "destination.match_opposite_ports")),
    "legacy/v2/firewall/zone": Endpoint(always=("_id", "name"), somewhere=("zone_key", "network_ids")),
    "legacy/v2/firewall/zone-matrix": Endpoint(
        always=("_id", "data[]._id", "data[].action", "data[].policy_count"), optional=("name",)),
    # Port forwards (legacy rest/portforward): the controller checked had none, so these are the legacy field names
    # and are not verified against live data; none of them is required.
    "legacy/rest/portforward": Endpoint(
        optional=("name", "enabled", "proto", "dst_port", "fwd", "fwd_port", "pfwd_interface", "src")),
    "legacy/system-log": Endpoint(
        always=("key", "timestamp", "category", "severity"),
        somewhere=("event", "message_raw", "parameters", "subcategory"),
        optional=("title_raw", "parameters.*.model_name", "parameters.ADMIN.name", "parameters.CLIENT.name",
                  "parameters.CLIENTS.clients[].mac", "parameters.CLIENTS.clients[].name",
                  "parameters.CLIENTS.clients[].hostname", "parameters.DEVICE.name", "parameters.DEVICE_FROM.name",
                  "parameters.DEVICE_TO.name", "parameters.DURATION.name", "parameters.IP.name",
                  "parameters.NETWORK.name", "parameters.OBJECT.name", "parameters.SECTION",
                  "parameters.SETTING_NAME", "parameters.WLAN.name", "parameters.*.hostname", "parameters.*.id",
                  "parameters.*.ip", "parameters.*.name")),
}


def segments(path: str) -> List[str]:
    """``port_table[].up`` -> ['port_table', '[]', 'up']"""
    out: List[str] = []
    for part in path.split("."):
        while part.endswith("[]"):
            part = part[:-2]
            out.append(part) if part else None
            out.append("[]")
            break
        else:
            out.append(part)
    return [s for s in out if s != ""]


def present(record, path: str) -> bool:
    """Does ``record`` (a dict) have ``path``? ``[]`` means 'some element of the list', ``*`` 'some key'."""
    def walk(node, segments) -> bool:
        if not segments:
            return True
        head, rest = segments[0], segments[1:]
        if head == "[]":
            return isinstance(node, list) and any(walk(item, rest) for item in node)
        if not isinstance(node, dict):
            return False
        if head == "*":
            return any(walk(value, rest) for value in node.values())
        return head in node and walk(node[head], rest)

    return walk(record, segments(path))


def problems(name: str, records: Iterable) -> List[str]:
    """What is missing from ``records`` of endpoint ``name`` against the contract (empty: it holds)."""
    records = [r for r in records if isinstance(r, dict)]
    spec = CONTRACT[name]
    found: List[str] = []
    if not records:
        return [f"{name}: no records to check"]
    for path in spec.always:
        missing = sum(not present(r, path) for r in records)
        if missing:
            found.append(f"{name}: '{path}' is missing from {missing} of {len(records)} record(s)")
    for path in spec.somewhere:
        if not any(present(r, path) for r in records):
            found.append(f"{name}: no record has '{path}'")
    return found


def covered(name: str, path: str) -> bool:
    """Is a field the code was seen reading listed for ``name`` (or the container of a listed field)?"""
    wanted = segments(path)
    for declared in CONTRACT[name].declared():
        d = segments(declared)
        if len(wanted) <= len(d) and all(w == x or x == "*" or w == "*" for w, x in zip(wanted, d, strict=False)):
            return True
    return False


# -- naming the requests ---------------------------------------------------------------------------------------------

_LEGACY = re.compile(r"/api/s/[^/]+/(.+)$")
_V2 = re.compile(r"/v2/api/site/[^/]+/(.+)$")


def endpoint_of(path: str) -> str:
    """A stable name for a request path, whatever the site reference or ids in it."""
    if path.endswith("/system-log/all"):
        return "legacy/system-log"
    if "/integration/v1/sites/" in path:
        tail = path.split("/sites/", 1)[1].split("/", 1)[1]
        if tail.endswith("/statistics/latest"):
            return "integration/device-statistics"
        return {"devices": "integration/devices", "clients": "integration/clients"}.get(tail, "integration/device")
    if path.endswith("/integration/v1/sites"):
        return "integration/sites"
    if (match := _LEGACY.search(path)) is not None:
        return "legacy/" + match.group(1)
    if (match := _V2.search(path)) is not None:
        return "legacy/v2/" + match.group(1)
    return "other/" + path


def records_of(body: Any) -> List[Any]:
    """The records in a response body: a page's ``data``, a bare list, or the one object itself."""
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        return list(body["data"])
    if isinstance(body, list):
        return list(body)
    return [body]


class RecordingSession:
    """Wraps a ``requests`` session: remembers every successful response and refuses anything that could write.

    Only GET is allowed, plus the one approved read-only POST (the event log query). This is the guard of the live
    contract tests and of the fixture recorder, which talk to a real controller.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.headers = inner.headers
        self.exchanges: List[Tuple[str, str, Any]] = []        # (method, path, body) of each successful response
        self._lock = threading.Lock()

    def mount(self, prefix: str, adapter: Any) -> None:
        self.inner.mount(prefix, adapter)

    @staticmethod
    def _path(url: str) -> str:
        return "/" + url.split("://", 1)[1].split("/", 1)[1]

    def _keep(self, method: str, url: str, response: Any) -> Any:
        if response.ok:
            with self._lock:
                self.exchanges.append((method, self._path(url), response.json()))
        return response

    def get(self, url: str, **kwargs: Any) -> Any:
        return self._keep("GET", url, self.inner.get(url, **kwargs))

    def post(self, url: str, **kwargs: Any) -> Any:
        if not self._path(url).endswith("/system-log/all"):
            raise AssertionError(f"refusing a POST to {self._path(url)}: only the event log query is allowed")
        return self._keep("POST", url, self.inner.post(url, **kwargs))

    def put(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("refusing a PUT: the controller is read-only here")

    patch = delete = request = put

    def records(self) -> Dict[str, List[Any]]:
        """{endpoint: every record received}, in the order they arrived."""
        found: Dict[str, List[Any]] = {}
        for _method, path, body in self.exchanges:
            found.setdefault(endpoint_of(path), []).extend(records_of(body))
        return found
