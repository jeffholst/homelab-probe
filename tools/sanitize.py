"""Deterministic sanitiser for recordings of a real controller (a development tool, not part of the package).

``Sanitizer`` turns what identifies a network (MACs, IP addresses, ids, device, client and network names, SSIDs,
the ISP) into synthetic values, the same way every time and the same in every place, so the relationships between
records survive:

* one real value always becomes one synthetic value (a MAC in ``stat/sta`` and in an event line match);
* the address class is kept (private, carrier-grade NAT, link-local, public, IPv4 or IPv6), as are the /24 a
  private address is in and its last octet, so reservations still sit in or out of the DHCP pool;
* a MAC keeps the two low bits of its first octet (randomized stays randomized) and a device stays a device;
* the output depends only on the input, never on the clock or on the order the controller listed things in.

``check_no_leaks`` is the safety net: it fails when a real value is still in the output, and when anything shaped like
an address is in the output that the sanitiser did not issue. Neither check prints a value, only where and what kind.
"""

import ipaddress
import re
from collections.abc import Iterable
from typing import Any, Dict, List, Optional, Set, Tuple

# Keys whose string values are names (a device, client, SSID, network or ISP); any other key is left alone.
NAME_KEYS = frozenset({"name", "hostname", "target", "essid", "ssid", "last_uplink_name",
                       "last_connection_network_name", "network", "gw_name", "isp_name", "isp_organization",
                       "internalReference"})
KIND_OF_KEY = {"essid": "ssid", "ssid": "ssid", "isp_name": "isp", "isp_organization": "isp",
               "hostname": "host", "target": "host"}
LABEL = {"ssid": "SSID {}", "isp": "ISP {}", "name": "Name {}", "host": "host-{}"}
KIND_PRIORITY = ("ssid", "isp", "name", "host")        # when one text is several kinds, the first listed wins
# Values never touched: they carry no identity and the code compares them (versions, monitor targets, enums).
KEEP_KEYS = frozenset({"firmwareVersion", "applicationVersion", "version", "type", "state", "status",
                       "subsystem", "key", "category", "subcategory", "severity", "event", "model", "band", "radio",
                       "security", "oui"})
SAFE_NAMES = frozenset({"wan", "wan1", "wan2", "lan", "default", "internet", "unknown"})
SKIP_PARENTS = frozenset({"DURATION"})                  # event parameters whose "name" is a quantity, not a name
MIN_NAME = 3                                            # shorter texts cannot be told from ordinary words

_HEX = "0-9A-Fa-f"
ADDRESS = re.compile(
    rf"(?P<uuid>(?<![{_HEX}-])[{_HEX}]{{8}}-[{_HEX}]{{4}}-[{_HEX}]{{4}}-[{_HEX}]{{4}}-[{_HEX}]{{12}}(?![{_HEX}-]))"
    rf"|(?P<mac>(?<![{_HEX}:-])(?:[{_HEX}]{{2}}[:-]){{5}}[{_HEX}]{{2}}(?![{_HEX}:-]))"
    rf"|(?P<oid>(?<![{_HEX}])[0-9a-f]{{24}}(?![{_HEX}]))"
    rf"|(?P<v6>(?<![{_HEX}:])(?:(?:[{_HEX}]{{1,4}}:){{7}}[{_HEX}]{{1,4}}|[{_HEX}:]*::[{_HEX}:]*)(?![{_HEX}:]))"
    r"|(?P<v4>(?<!\d)(?<!\d\.)(?:\d{1,3}\.){3}\d{1,3}(?!\d)(?!\.\d))")

# What an address-shaped token may be without having been issued: addresses that identify nothing.
HARMLESS = frozenset({"0.0.0.0", "255.255.255.255", "127.0.0.1", "::", "::1", "ff:ff:ff:ff:ff:ff",
                      "00:00:00:00:00:00"})


class LeakError(Exception):
    """The sanitised data still contains something that identifies the real network."""


def normalize_mac(text: str) -> str:
    return text.lower().replace("-", ":")


def _is_mac(text: str) -> bool:
    return bool(re.fullmatch(rf"(?:[{_HEX}]{{2}}[:-]){{5}}[{_HEX}]{{2}}", text))


def _ip(text: str) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def _address_class(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """'' for an address that identifies nothing (kept as is), else the class the synthetic one must keep."""
    if ip.is_unspecified or ip.is_loopback or ip.is_multicast or str(ip) in HARMLESS:
        return ""
    if isinstance(ip, ipaddress.IPv4Address):
        if ip == ipaddress.ip_address("255.255.255.255") or (ip.packed[0] >= 240):
            return ""                                       # broadcast, netmasks and other reserved space
        if ip.is_link_local:
            return "link-local"
        if ip in ipaddress.ip_network("100.64.0.0/10"):
            return "cgnat"
        if any(ip in ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
            return "private"
        return "public"
    if ip.is_link_local:
        return "link-local6"
    if ip in ipaddress.ip_network("fc00::/7"):
        return "private6"
    return "public6"


def _walk(data: Any, visit, keep: bool = False, key: str = "", parent: str = "") -> Any:
    """Rebuild ``data`` with ``visit(text, key, parent)`` applied to each string that is not under a keep key."""
    if isinstance(data, dict):
        return {k: _walk(v, visit, keep or k in KEEP_KEYS, k, key) for k, v in data.items()}
    if isinstance(data, list):
        return [_walk(v, visit, keep, key, parent) for v in data]
    if isinstance(data, str) and not keep:
        return visit(data, key, parent)
    return data


class Sanitizer:
    """Learn every identifying value in the data, give each a synthetic one, and rewrite the data.

    ``device_macs`` are the MACs of the controller's own devices (they become ``a0:...``; every other MAC, client or
    neighbor, becomes ``b0:...``). ``presets`` pins chosen values (the site's id to ``site-1``, its name to
    ``Default``); the key is compared without case.
    """

    def __init__(self, device_macs: Iterable[str] = (), presets: Optional[Dict[str, str]] = None) -> None:
        self.device_macs = {normalize_mac(m) for m in device_macs}
        self.presets = {k.lower(): v for k, v in (presets or {}).items()}
        self.issued: Set[str] = set(self.presets.values())
        self.real: Dict[str, Set[str]] = {"mac": set(), "ip": set(), "id": set(), "name": set()}
        self._name_kind: Dict[str, str] = {}
        self._map: Dict[str, str] = {}
        self._names_re: Optional[re.Pattern] = None

    # -- learning -----------------------------------------------------------------------------------------

    def learn(self, data: Any) -> "Sanitizer":
        _walk(data, self._note)
        self._assign()
        return self

    def _note(self, text: str, key: str, parent: str) -> str:
        for m in ADDRESS.finditer(text):
            kind, found = m.lastgroup, m.group()
            if kind == "mac":
                if normalize_mac(found) not in HARMLESS:
                    self.real["mac"].add(normalize_mac(found))
            elif kind in ("uuid", "oid"):
                self.real["id"].add(found.lower())
            else:
                ip = _ip(found)
                if ip is not None and _address_class(ip):
                    self.real["ip"].add(str(ip))
        if key in NAME_KEYS and parent not in SKIP_PARENTS:
            name = text.strip()
            if ADDRESS.fullmatch(name):
                return text                                     # an address used as a name: mapped as an address
            if name.lower() not in SAFE_NAMES and (len(name) >= MIN_NAME or name.lower() in self.presets):
                kind = KIND_OF_KEY.get(key, "name")
                old = self._name_kind.get(name.lower())
                if old is None or KIND_PRIORITY.index(kind) < KIND_PRIORITY.index(old):
                    self._name_kind[name.lower()] = kind
                self.real["name"].add(name.lower())
        return text

    def _assign(self) -> None:
        for n, value in enumerate(sorted(self.real["id"]), 1):
            self._map[value] = self.presets.get(value) or (
                f"{n:08x}-0000-4000-8000-{n:012x}" if "-" in value else f"{n:024x}")
        self._assign_macs()
        self._assign_ips()
        counters = {kind: 0 for kind in LABEL}
        for name in sorted(self.real["name"], key=lambda n: (KIND_PRIORITY.index(self._name_kind[n]), n)):
            if name in self.presets:
                self._map[name] = self.presets[name]
                continue
            kind = self._name_kind[name]
            counters[kind] += 1
            self._map[name] = LABEL[kind].format(counters[kind])
        self.issued |= set(self._map.values())
        names = sorted((n for n in self.real["name"]), key=lambda n: (-len(n), n))
        self._names_re = re.compile(
            r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(n) for n in names) + r")(?![A-Za-z0-9])",
            re.IGNORECASE) if names else None

    def _assign_macs(self) -> None:
        counters = {"a": 0, "b": 0}
        for mac in sorted(self.real["mac"]):
            local = int(mac[:2], 16) & 0x03               # keep "locally administered" and "multicast" as they were
            role = "a" if mac in self.device_macs else "b"
            counters[role] += 1
            n = counters[role]
            lead = (0xA0 if role == "a" else 0xB0) | local
            self._map[mac] = f"{lead:02x}:00:00:00:{n >> 8:02x}:{n & 0xFF:02x}"

    def _assign_ips(self) -> None:
        by_class: Dict[str, List[ipaddress.IPv4Address | ipaddress.IPv6Address]] = {}
        for text in self.real["ip"]:
            ip = ipaddress.ip_address(text)
            by_class.setdefault(_address_class(ip), []).append(ip)
        for cls, addresses in by_class.items():
            addresses.sort()
            if cls in ("private", "cgnat", "link-local"):
                nets: Dict[Any, int] = {}
                for ip in addresses:
                    net = ipaddress.ip_network(f"{ip}/24", strict=False)
                    nets.setdefault(net, len(nets))
                for ip in addresses:
                    k = nets[ipaddress.ip_network(f"{ip}/24", strict=False)]
                    last = ip.packed[3]
                    if cls == "private":
                        self._map[str(ip)] = f"10.{200 + k // 256}.{k % 256}.{last}"
                    elif cls == "cgnat":
                        self._map[str(ip)] = f"100.{64 + k // 256}.{k % 256}.{last}"
                    else:
                        self._map[str(ip)] = f"169.254.{k % 256}.{last}"
            elif cls == "public":
                documentation = [f"{net}.{i}" for net in ("192.0.2", "198.51.100", "203.0.113") for i in range(1, 255)]
                for ip, synthetic in zip(addresses, documentation, strict=False):
                    self._map[str(ip)] = synthetic
            else:
                prefix = {"private6": "fd00:db8", "link-local6": "fe80", "public6": "2001:db8"}[cls]
                for n, ip in enumerate(addresses, 1):
                    self._map[str(ip)] = f"{prefix}::{n:x}"

    # -- rewriting ----------------------------------------------------------------------------------------

    def _swap(self, m: re.Match) -> str:
        kind, found = m.lastgroup, m.group()
        if kind == "mac":
            return self._map.get(normalize_mac(found), found)
        if kind in ("uuid", "oid"):
            return self._map.get(found.lower(), found)
        ip = _ip(found)
        return self._map.get(str(ip), found) if ip is not None else found

    def text(self, value: str) -> str:
        value = ADDRESS.sub(self._swap, value)
        if self._names_re is not None:
            value = self._names_re.sub(lambda m: self._map[m.group().lower()], value)
        return value

    def apply(self, data: Any) -> Any:
        return _walk(data, lambda text, key, parent: text if parent in SKIP_PARENTS and key == "name"
                     else self.text(text))

    def __call__(self, data: Any) -> Any:
        return self.learn(data).apply(data)

    def check(self, output: Any) -> None:
        check_no_leaks(output, self)


# -- the safety net ---------------------------------------------------------------------------------------------

def _strings(data: Any, keep: bool = False, path: str = "") -> Iterable[Tuple[str, str]]:
    if isinstance(data, dict):
        for k, v in data.items():
            yield from _strings(v, keep or k in KEEP_KEYS, f"{path}.{k}" if path else str(k))
    elif isinstance(data, list):
        for v in data:
            yield from _strings(v, keep, path + "[]")
    elif isinstance(data, str) and not keep:
        yield path, data


def check_no_leaks(output: Any, sanitizer: Sanitizer) -> None:
    """Raise ``LeakError`` listing where (never what) a leak is. Two rules:

    * a real MAC, address, id or name the sanitiser learned must not appear in the output;
    * every MAC, address or id-shaped token in the output must be one the sanitiser issued (or harmless), so an
      address it never saw cannot slip through.
    """
    problems: List[str] = []
    issued = {v.lower() for v in sanitizer.issued}
    real_names = sorted(sanitizer.real["name"], key=len, reverse=True)
    name_re = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(n) for n in real_names) + r")(?![A-Za-z0-9])",
                         re.IGNORECASE) if real_names else None
    for path, text in _strings(output):
        for m in ADDRESS.finditer(text):
            kind, found = m.lastgroup, m.group()
            ip = _ip(found) if kind in ("v4", "v6") else None
            if kind in ("v4", "v6") and (ip is None or not _address_class(ip)):
                continue                                    # not an address, or one that identifies nothing
            normal = normalize_mac(found) if kind == "mac" else found.lower() if kind in ("uuid", "oid") else str(ip)
            if normal not in issued and normal not in HARMLESS:
                problems.append(f"{path}: a {kind} address the sanitiser did not issue")
        if name_re is not None:
            stripped = text
            for label in sorted(sanitizer.issued, key=len, reverse=True):
                stripped = stripped.replace(label, "")     # a synthetic label may contain a short real word
            if name_re.search(stripped):
                problems.append(f"{path}: a real name is still present")
    if problems:
        unique = sorted(set(problems))
        raise LeakError(f"{len(unique)} leak(s) in the sanitised data:\n  " + "\n  ".join(unique[:50]))
