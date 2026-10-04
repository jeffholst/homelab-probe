"""Internet health: `hlp wan`.

Answers "is it my internet or my LAN?" from data the controller already keeps:
the WAN and internet subsystems of `stat/health` (including the controller's own
24-hour monitoring of a few targets), the gateway's WAN link, and the stored
speedtest history. Everything here is read with GET.
"""

import ipaddress
import json
import re
import statistics
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from .events import describe_duration
from .query import format_table
from .settings import DiagnoseSettings
from .snapshot import Snapshot
from .util import clean_data, describe_age, number

DEFAULT_DAYS = 30
JSON_VERSION = 1               # the format of `wan --json`; it changes only when a field is removed or renamed
SPEEDTEST_BASELINE_DAYS = 30   # `diagnose` compares the last speedtest with this many days
MIN_SPEEDTESTS = 5             # a median over fewer runs says little about what is normal
GATEWAY_TYPES = {"udm", "ugw", "uxg", "ucg"}


# -- NAT in front of the gateway ----------------------------------------------

_PRIVATE_V4 = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")
_LINK_LOCAL_V4 = ipaddress.ip_network("169.254.0.0/16")
_PRIVATE_V6 = ipaddress.ip_network("fc00::/7")
_LINK_LOCAL_V6 = ipaddress.ip_network("fe80::/10")
_DOCUMENTATION_RANGES = [
    ipaddress.ip_network(n) for n in
    ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")
]


def classify_wan_address(text: Any) -> str:
    """What kind of address the gateway's WAN port has, from the address alone.

    ``"private"`` (RFC 1918, or an IPv6 unique-local ``fc00::/7``): another router that does NAT
    sits in front of the gateway. ``"cgnat"``: the shared 100.64.0.0/10 range of carrier-grade
    NAT. ``"link_local"``: no address was obtained. ``"public"``: globally reachable, or in a
    documentation range used by synthetic data. Other special-use addresses are ``"unknown"``.
    ``"none"``: nothing or ``0.0.0.0`` (the WAN is down). ``"unknown"``: not an IP address or an
    unsupported special-use address.

    The NAT ranges are listed explicitly because Python's ``is_private`` also covers documentation
    and benchmarking ranges; the documentation ranges are intentionally accepted for synthetic data.
    """
    value = "" if text is None else str(text).strip()
    if not value:
        return "none"
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return "unknown"
    if ip.is_unspecified:
        return "none"
    if isinstance(ip, ipaddress.IPv4Address):
        if any(ip in net for net in _PRIVATE_V4):
            return "private"
        if ip in _CGNAT_V4:
            return "cgnat"
        if ip in _LINK_LOCAL_V4:
            return "link_local"
    else:
        if ip in _PRIVATE_V6:
            return "private"
        if ip in _LINK_LOCAL_V6:
            return "link_local"
    if ip.is_multicast or ip.is_reserved:
        return "unknown"
    return "public" if ip.is_global or any(ip in net for net in _DOCUMENTATION_RANGES) else "unknown"


_NAT_MESSAGES = {
    "private": ("WAN address {ip} is private: the gateway is behind another router doing NAT (double NAT); "
                "inbound port forwards and some VPNs will not work until that router is put in bridge mode "
                "or forwards the ports too"),
    "cgnat": ("WAN address {ip} is in the carrier-grade NAT range (100.64.0.0/10): the ISP shares one public "
              "address between customers, so inbound port forwards and some VPNs will not work"),
    "link_local": "WAN address {ip} is link-local: the gateway got no address from the ISP",
}


def nat_status(snap: Snapshot) -> Dict[str, Any]:
    """Whether the gateway's WAN address says it is behind NAT: ``{"wan_ip", "kind", "message"}``.

    Read from the ``wan_ip`` of the ``wan`` health entry (the primary WAN). It needs no outside
    lookup, so it cannot see a modem or router in front of the gateway that translates addresses
    while handing the gateway a public one; ``message`` is empty unless there is something to say.
    """
    ip = str(_health(snap, "wan").get("wan_ip") or "").strip()
    kind = classify_wan_address(ip)
    return {"wan_ip": ip, "kind": kind, "message": _NAT_MESSAGES.get(kind, "").format(ip=ip)}


def _health(snap: Snapshot, subsystem: str) -> Dict[str, Any]:
    return next((h for h in snap.health if h.get("subsystem") == subsystem), {})


def _when(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _age(ms: Any, now_ms: int) -> str:
    seconds = (now_ms - ms) / 1000 if isinstance(ms, (int, float)) else None
    return "" if seconds is None or seconds < 0 else describe_age(int(seconds))


# -- speedtests ----------------------------------------------------------------

def speedtests_since(tests: List[Dict[str, Any]], days: int, now_ms: int) -> List[Dict[str, Any]]:
    """Speedtests newer than ``days`` days, ignoring entries that are not result records."""
    cutoff = now_ms - days * 86400 * 1000
    return [t for t in tests if isinstance(t, dict) and isinstance(t.get("time"), (int, float))
            and t["time"] >= cutoff]


def speedtest_group(test: Dict[str, Any]) -> Any:
    return test.get("wan_networkgroup") or test.get("interface_name")


def speedtests_for_baseline(tests: List[Dict[str, Any]], days: int, now_ms: int) -> List[Dict[str, Any]]:
    recent = speedtests_since(tests, days, now_ms)
    if not recent:
        return []
    group = speedtest_group(recent[-1])
    return [test for test in recent if speedtest_group(test) == group]


def median_download(tests: List[Dict[str, Any]]) -> Optional[float]:
    values = [d for d in (number(t.get("download_mbps")) for t in tests) if d is not None]
    return statistics.median(values) if len(values) >= MIN_SPEEDTESTS else None


def _spread(tests: List[Dict[str, Any]], key: str) -> Optional[Dict[str, float]]:
    values = [v for v in (number(t.get(key)) for t in tests) if v is not None]
    if not values:
        return None
    return {"min": min(values), "median": statistics.median(values), "max": max(values)}


def _speedtest_section(snap: Snapshot, days: int, settings: DiagnoseSettings, now_ms: int) -> Dict[str, Any]:
    stored = [t for t in snap.speedtests if isinstance(t, dict)]
    recent = speedtests_for_baseline(stored, days, now_ms)
    last = stored[-1] if stored else None
    median = median_download(recent)
    limit = median * settings.wan_speed_drop_pct / 100 if median else None
    slow = [t for t in recent
            if limit is not None
            and (download := number(t.get("download_mbps"))) is not None
            and download < limit]
    return {
        "days": days, "count": len(recent), "total_stored": len(stored),
        "last": None if last is None else {
            "time": last.get("time"), "when": _when(last.get("time")), "age": _age(last.get("time"), now_ms),
            "download_mbps": number(last.get("download_mbps")), "upload_mbps": number(last.get("upload_mbps")),
            "latency_ms": number(last.get("latency_ms")), "interface": last.get("interface_name") or ""},
        "download": _spread(recent, "download_mbps"), "upload": _spread(recent, "upload_mbps"),
        "latency": _spread(recent, "latency_ms"),
        "slow_threshold_pct": settings.wan_speed_drop_pct,
        "slow_runs": [{"when": _when(t.get("time")), "download_mbps": number(t.get("download_mbps")),
                       "upload_mbps": number(t.get("upload_mbps")), "latency_ms": number(t.get("latency_ms"))}
                      for t in reversed(slow)],            # newest first
    }


# -- the report ----------------------------------------------------------------

def _links(snap: Snapshot) -> List[Dict[str, Any]]:
    gateway = next((d for d in snap.legacy_devices if d.get("type") in GATEWAY_TYPES), None)
    links = []
    gateway = gateway or {}
    for key in sorted(k for k in gateway if re.fullmatch(r"wan\d+", k)):
        w = gateway[key] if isinstance(gateway[key], dict) else {}
        tx, rx = number(w.get("tx_bytes-r")), number(w.get("rx_bytes-r"))
        links.append({
            "port": key, "interface": w.get("name") or "", "up": w.get("up"),
            "speed_mbps": number(w.get("speed")), "max_speed_mbps": number(w.get("max_speed")),
            "full_duplex": w.get("full_duplex"), "latency_ms": number(w.get("latency")),
            "tx_mbps": None if tx is None else tx * 8 / 1e6, "rx_mbps": None if rx is None else rx * 8 / 1e6,
        })
    return links


def monitoring(snap: Snapshot) -> List[Dict[str, Any]]:
    """The controller's own monitoring of the internet connection, per WAN."""
    stats = _health(snap, "wan").get("uptime_stats")
    result = []
    for name, st in sorted((stats or {}).items() if isinstance(stats, dict) else []):
        if not isinstance(st, dict):
            continue
        targets: Dict[Any, Dict[str, Any]] = {}
        for listed, alerting in ((st.get("monitors"), False), (st.get("alerting_monitors"), True)):
            for m in listed if isinstance(listed, list) else []:
                if isinstance(m, dict):
                    entry = targets.setdefault((m.get("target"), m.get("type")), {
                        "target": m.get("target") or "?", "type": m.get("type") or "",
                        "availability": number(m.get("availability")),
                        "latency_ms": number(m.get("latency_average")), "alerts": False})
                    entry["alerts"] = entry["alerts"] or alerting       # configured to raise alerts
        result.append({
            "name": name, "availability": number(st.get("availability")),
            "latency_ms": number(st.get("latency_average")), "period_s": int(number(st.get("time_period")) or 0),
            "targets": sorted(targets.values(), key=lambda t: (str(t["target"]), t["type"]))})
    return result


def build_wan(snap: Snapshot, days: int = DEFAULT_DAYS, settings: Optional[DiagnoseSettings] = None,
              now_ms: Optional[int] = None) -> Dict[str, Any]:
    settings = settings or DiagnoseSettings()
    now = int(time.time() * 1000) if now_ms is None else now_ms
    wan, www = _health(snap, "wan"), _health(snap, "www")
    return {
        "now": {
            "status": wan.get("status") or "unknown", "isp": wan.get("isp_name") or "",
            "wan_ip": wan.get("wan_ip") or "", "gateway": wan.get("gw_name") or "",
            "internet_status": www.get("status") or "unknown",
            "latency_ms": number(www.get("latency")), "drops": number(www.get("drops")),
            "speedtest_status": www.get("speedtest_status") or ""},
        "nat": nat_status(snap),
        "links": _links(snap),
        "monitoring": monitoring(snap),
        "speedtests": _speedtest_section(snap, days, settings, now),
    }


# -- rendering -------------------------------------------------------------

def _mbps(value: Optional[float]) -> str:
    return "?" if value is None else f"{value:.0f} Mbps"


def _rate(value: Optional[float]) -> str:
    return "?" if value is None else (f"{value:.1f} Mbps" if value >= 0.1 else f"{value * 1000:.0f} kbps")


def _pct(value: Optional[float]) -> str:
    return "?" if value is None else f"{value:.1f}%"


def _ms(value: Optional[float]) -> str:
    return "?" if value is None else f"{value:.0f} ms"


def render_text(wan: Dict[str, Any]) -> str:
    wan = clean_data(wan)
    n = wan["now"]
    head = f"Internet: {n['status']}"
    if n["isp"]:
        head += f" ({n['isp']})"
    lines = [head]
    for label, value in (("WAN IP", n["wan_ip"]), ("Gateway", n["gateway"])):
        if value:
            lines.append(f"  {label}: {value}")
    nat = wan["nat"]
    if nat["message"]:
        lines.append(f"  NAT: {nat['message']}")
    elif nat["kind"] == "public":
        lines.append("  NAT: none seen (public WAN address; a modem doing NAT in front of the gateway cannot be seen)")
    detail = [f"latency {_ms(n['latency_ms'])}"]
    if n["drops"] is not None:
        detail.append(f"{n['drops']:.0f} drops")
    detail.append(f"status {n['internet_status']}")
    lines.append("  Now: " + ", ".join(detail))

    for link in wan["links"]:
        duplex = {True: "full duplex", False: "half duplex"}.get(link["full_duplex"], "")
        state = {True: "up", False: "DOWN"}.get(link["up"], "unknown")
        text = f"  Link {link['port']}: {link['interface'] or '?'} {state}"
        if link["speed_mbps"]:
            text += f", {_mbps(link['speed_mbps'])}" + (f" {duplex}" if duplex else "")
            if link["max_speed_mbps"] and link["speed_mbps"] < link["max_speed_mbps"]:
                text += f" (port supports {_mbps(link['max_speed_mbps'])})"
        lines.append(text)
        if link["tx_mbps"] is not None or link["rx_mbps"] is not None:
            lines.append(f"    live: {_rate(link['tx_mbps'])} up, {_rate(link['rx_mbps'])} down")

    for m in wan["monitoring"]:
        lines += ["", f"Last {describe_duration(m['period_s']) if m['period_s'] else '?'} "
                      f"(controller monitoring, {m['name']}): availability {_pct(m['availability'])}, "
                      f"average latency {_ms(m['latency_ms'])}"]
        if m["targets"]:
            lines.append(format_table(
                [{"Target": t["target"], "Type": t["type"], "Availability": _pct(t["availability"]),
                  "Latency": _ms(t["latency_ms"]), "Alerts": "yes" if t["alerts"] else ""}
                 for t in m["targets"]], ["Target", "Type", "Availability", "Latency", "Alerts"]))
    if not wan["monitoring"]:
        lines += ["", "Controller monitoring: not available"]

    s = wan["speedtests"]
    lines += ["", f"Speedtests, last {s['days']} days ({s['count']} run{'s' if s['count'] != 1 else ''})"
                  + ("" if s["count"] == s["total_stored"] else f", {s['total_stored']} stored")]
    last = s["last"]
    if last is None:
        lines.append("  none stored (the controller has not run a speedtest)")
    else:
        lines.append(f"  Last: {last['when']} ({last['age']} ago): download {_mbps(last['download_mbps'])}, "
                     f"upload {_mbps(last['upload_mbps'])}, latency {_ms(last['latency_ms'])}")
        for label, key, fmt in (("Download", "download", _mbps), ("Upload", "upload", _mbps),
                                ("Latency", "latency", _ms)):
            sp = s[key]
            if sp:
                lines.append(f"  {label}: min {fmt(sp['min'])}, median {fmt(sp['median'])}, max {fmt(sp['max'])}")
        if s["slow_runs"]:
            lines += ["", f"  Download below {s['slow_threshold_pct']:g}% of the median ({len(s['slow_runs'])}):"]
            lines += [f"    {r['when']}  download {_mbps(r['download_mbps'])}, "
                      f"upload {_mbps(r['upload_mbps'])}, latency {_ms(r['latency_ms'])}"
                      for r in s["slow_runs"]]
        elif s["download"] and s["count"] >= MIN_SPEEDTESTS:
            lines.append(f"  No run below {s['slow_threshold_pct']:g}% of the median download.")
    return "\n".join(lines)


def to_json(wan: Dict[str, Any]) -> str:
    return json.dumps({"version": JSON_VERSION, **wan}, indent=2)
