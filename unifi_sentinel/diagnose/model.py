"""The vocabulary of the checks: severities, exit codes, the catalogue of finding codes and ``Finding``."""

from dataclasses import dataclass
from typing import Any, Dict, Optional

CRITICAL, WARNING, INFO = "critical", "warning", "info"
SEVERITY_ORDER = {CRITICAL: 0, WARNING: 1, INFO: 2}
EMOJI = {CRITICAL: "\U0001F6D1", WARNING: "\u26A0\uFE0F", INFO: "\u2139\uFE0F"}

# Process exit codes for `diagnose` (see exit_code). Tool errors use cli.EXIT_ERROR.
EXIT_OK, EXIT_WARNING, EXIT_CRITICAL = 0, 1, 2

LINK_LOCAL_PREFIX = "169.254."
GATEWAY_TYPES = {"Gateway", "Dream Machine"}
# Subsystems whose controller status just reflects disconnected devices we already report.
DEVICE_SUBSYSTEMS = {"lan", "wlan"}
# Every check's stable ``code``, with what it reports. Codes are an interface (``diagnose --json``,
# scripts, and later notifications key on them): never reuse or rename one; add new ones here.
# tests/test_diagnose_json.py checks that every Finding in this package uses a code from this table
# and that every code here is used.
CODES = {
    "device.offline": "a UniFi device is not online (critical for a gateway or a device that others uplink through)",
    "device.cpu_high": "device CPU utilization at or above the warning threshold",
    "device.memory_high": "device memory utilization at or above the warning threshold",
    "device.overheating": "an online UniFi device reports that it is overheating",
    "controller.pending_adoption": "devices waiting to be adopted",
    "controller.legacy_unavailable": "legacy device data could not be read, so the port and overheating checks "
                                     "were skipped",
    "health.subsystem": "a controller health subsystem is in a warning or error state",
    "health.device_subsystem": "lan/wlan subsystem status that only reflects disconnected devices",
    "internet.latency": "internet latency at or above the threshold",
    "internet.drops": "internet drops at or above the threshold",
    "internet.speedtest_failed": "the last speedtest failed",
    "wan.availability": "24-hour internet availability below the threshold",
    "wan.monitor_availability": "one monitored internet target below the availability threshold",
    "wan.speedtest_slow": "the last speedtest download is well below the 30-day median",
    "wan.double_nat": "the WAN address is private: the gateway is behind another router doing NAT",
    "wan.cgnat": "the WAN address is in the carrier-grade NAT range (100.64.0.0/10)",
    "wan.link_local_address": "the WAN address is link-local: the gateway got no address from the ISP",
    "client.no_ip": "a connected client has no IP address",
    "client.link_local_ip": "a connected client has a link-local (169.254.x.x) address",
    "ip.duplicate": "the same IP is in use by several clients or devices",
    "reservation.ip_mismatch": "an online client's IP differs from its reservation",
    "reservation.outside_subnet": "a reserved IP is outside its network's subnet",
    "reservation.duplicate": "the same IP is reserved for several clients",
    "reservation.ip_in_use": "a reserved IP is in use by a different client or device",
    "reservation.offline": "a client with a reservation has been offline longer than the threshold",
    "reservation.in_dhcp_pool": "a reserved IP lies inside its network's dynamic DHCP range",
    "reservation.pool_unknown": "a network's DHCP range is missing or invalid, so its reservations cannot be checked",
    "reservation.private_mac": "a reservation is tied to a randomized (private) MAC address",
    "client.private_mac_summary": "how many connected clients use randomized (private) MAC addresses",
    "reservation.never_seen": "a reservation whose client has no last-seen time",
    "port.link_flaps": "a switch port's link has gone down repeatedly since boot",
    "port.drops": "a switch port is dropping packets above the threshold",
    "port.stp": "an up port is not in the STP forwarding state",
    "port.poe_budget": "a switch's PoE budget use is at or above the threshold",
    "port.errors": "a port has rx/tx errors",
    "port.half_duplex": "a port link is half duplex",
    "port.slow_link": "a port negotiated at or below the slow-link speed",
    "link.below_capability": "an uplink negotiated below what both ends support",
    "wifi.weak_signal": "a Wi-Fi client's signal is at or below the threshold",
    "wifi.client_retries": "a Wi-Fi client retries too many transmissions",
    "wifi.client_satisfaction": "a Wi-Fi client's satisfaction is below the threshold",
    "wifi.radio_utilization": "an AP radio's channel utilization is at or above the threshold",
    "wifi.radio_retries": "an AP radio retries too many transmissions",
    "wifi.radio_satisfaction": "an AP radio's satisfaction is below the threshold",
    "event.ip_conflict": "the controller reported an IP conflict in the event window",
    "event.client_disconnects": "a client disconnected repeatedly in the event window",
    "event.client_roams": "a client roamed repeatedly in the event window",
    "event.device_unreachable": "a device was reported unreachable in the event window",
    "event.internet_latency": "the controller reported high internet latency in the event window",
    "event.log_truncated": "the event log read hit its cap, so event counts may be low",
}


@dataclass(frozen=True)
class Finding:
    severity: str  # CRITICAL, WARNING or INFO
    subject: str
    message: str
    target_mac: Optional[str] = None
    code: str = ""   # a key of CODES; empty only for findings built outside the checks (tests)

    def to_dict(self) -> Dict[str, Any]:
        """The JSON form used by ``diagnose --json`` and by the ``client`` and ``topology`` views."""
        return {"severity": self.severity, "code": self.code, "subject": self.subject,
                "message": self.message, "mac": self.target_mac or ""}
