"""diagnose settings: thresholds and an ignore list, from an optional TOML file."""

import datetime
import difflib
import fnmatch
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if sys.version_info >= (3, 11):
    import tomllib  # pragma: no cover  (only one of the two imports runs on a given Python; neither can count)
else:  # Python 3.10
    import tomli as tomllib  # pragma: no cover

from .config import ConfigError
from .util import printable

DEFAULT_FILENAME = "hlp.toml"


@dataclass(frozen=True)
class IgnoreRule:
    """Suppress findings. ``code`` is a finding code (``port.slow_link``), matched exactly: it survives a change
    of wording and silences the check everywhere; ``subject`` is a case-insensitive name (``*`` and ``?``
    wildcards); ``message`` is a case-insensitive substring. Every field that is given must match. ``reason`` is
    required so ignores stay explainable. ``until`` is the last day the rule applies (inclusive); after it the
    rule is expired and the finding comes back."""

    subject: str = ""
    message: str = ""
    reason: str = ""
    code: str = ""
    until: Optional[datetime.date] = None

    def expired(self, today: datetime.date) -> bool:
        return self.until is not None and today > self.until

    def describe(self) -> str:
        """The fields that select findings, for a message (names come from the user's own file; still cleaned)."""
        given = (("code", self.code), ("subject", self.subject), ("message", self.message))
        return ", ".join(f'{name} "{printable(value)}"' for name, value in given if value)

    def matches(self, subject: str, message: str, code: str = "") -> bool:
        if self.code and self.code != code:
            return False
        pattern = self.subject.lower().replace("[", "[[]")
        if self.subject and not fnmatch.fnmatchcase(subject.lower(), pattern):
            return False
        if self.message and self.message.lower() not in message.lower():
            return False
        return True


def expired_rules(rules: Tuple[IgnoreRule, ...],
                  today: Optional[datetime.date] = None) -> List[Tuple[IgnoreRule, datetime.date]]:
    """(rule, its until date) for each rule whose date has passed (``today`` is the local date unless a test
    gives one)."""
    today = today or datetime.date.today()
    return [(rule, rule.until) for rule in rules if rule.until is not None and rule.expired(today)]


_DATE_TEXT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _invalid_unquoted_until(text: str, error: Exception) -> Optional[int]:
    """The TOML parser rejects an impossible bare date before ``_parse`` can name its ignore rule."""
    line_number = getattr(error, "lineno", None)
    if not isinstance(line_number, int):
        match = re.search(r"\(at line ([0-9]+), column [0-9]+\)", str(error))
        line_number = int(match.group(1)) if match else None
    if line_number is None:
        return None

    rule_number = 0
    for number, line in enumerate(text.splitlines(), 1):
        if re.fullmatch(r"\s*\[\[\s*ignore\s*\]\]\s*(?:#.*)?", line):
            rule_number += 1
        if number != line_number:
            continue
        match = re.fullmatch(r"\s*until\s*=\s*([0-9]{4}-[0-9]{2}-[0-9]{2})\s*(?:#.*)?", line)
        if match and rule_number:
            try:
                datetime.date.fromisoformat(match.group(1))
            except ValueError:
                return rule_number
    return None


def _parse_until(value: Any, number: int) -> datetime.date:
    """A date written as ``2026-10-10``: a TOML date or a string. Both Python versions get the same strict form
    (3.11 would also take ``20261010`` and week dates), and a date with a time is refused."""
    problem = f"[[ignore]] #{number}: until must be a date like 2026-10-10"
    if isinstance(value, datetime.datetime):
        raise ConfigError(problem + " (a date, without a time)")
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str) and _DATE_TEXT.fullmatch(value):
        try:
            return datetime.date.fromisoformat(value)
        except ValueError:
            pass
    raise ConfigError(problem)


@dataclass(frozen=True)
class DiagnoseSettings:
    resource_warn_pct: float = 90        # CPU/memory at or above: warning
    resource_critical_pct: float = 98    # CPU/memory at or above: critical
    storage_warn_pct: float = 90         # a device's storage used at or above: warning
    storage_critical_pct: float = 98     # a device's storage used at or above: critical
    recent_reboot_minutes: int = 60      # a device up for less than this: info (it restarted recently)
    slow_link_mbps: int = 100            # ports negotiated at or below: info
    wan_latency_warn_ms: float = 100     # internet latency at or above: warning
    wan_drops_warn: int = 10             # internet drops at or above: warning (heuristic)
    wan_availability_warn_pct: float = 99   # 24h internet availability below: warning
    wan_speed_drop_pct: float = 70       # last speedtest download below this % of the 30-day median: warning
    link_flap_count: int = 5             # port link-down count (since boot) at or above: warning
    port_drop_pct: float = 0.1           # dropped/total packets (%) at or above: warning
    min_packets_for_drop_pct: int = 1000 # minimum packet count before evaluating drop percentage
    poe_warn_pct: float = 80             # switch PoE budget used at or above: warning
    poe_critical_pct: float = 95         # switch PoE budget used at or above: critical
    event_flap_count: int = 10           # disconnects/unreachable events in the window at or above: warning
    wifi_weak_signal_dbm: float = -75    # client signal at or below: warning
    wifi_retry_pct: float = 30           # client or radio TX retries at or above: warning
    wifi_min_attempts: int = 1000        # minimum client TX attempts before judging retries
    wifi_satisfaction_warn: float = 50   # client or radio satisfaction below: warning
    radio_util_warn_pct: float = 70      # radio channel utilization at or above: warning
    radio_util_critical_pct: float = 90  # radio channel utilization at or above: critical
    reserved_offline_warn_days: float = 1       # a reserved client offline this many days: warning
    reserved_offline_critical_days: float = 7   # a reserved client offline this many days: critical
    notify_repeat_hours: float = 24             # a critical finding still unresolved is notified again (0: never)
    new_client_window_hours: float = 24         # a client first seen within this long: info (a new device; 0: off)
    ignore: Tuple[IgnoreRule, ...] = ()


def server_settings_path(named: Optional[Path], data_dir: Path) -> Path:
    """The settings file of ``hlp serve``: the one named with ``--config``, else ``hlp.toml`` in the data directory.
    The server, its pre-flight check at start and the settings API all use this one answer."""
    return named if named is not None else data_dir / DEFAULT_FILENAME


def known_codes() -> Dict[str, str]:
    """Every finding code an ignore rule may name: those of ``diagnose`` and of ``audit`` (one settings file serves
    both). Imported on use because ``diagnose`` itself imports this module."""
    from .audit import AUDIT_CODES
    from .diagnose.model import CODES
    return {**CODES, **AUDIT_CODES}


def _number(name: str, value: Any, lo: float, hi: Optional[float] = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"[thresholds] {name} must be a number, got {value!r}")
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigError(f"[thresholds] {name} must be finite, got {value!r}")
    if value < lo or (hi is not None and value > hi):
        limit = f"between {lo:g} and {hi:g}" if hi is not None else f"at least {lo:g}"
        raise ConfigError(f"[thresholds] {name} must be {limit}, got {value!r}")
    return value


def _parse(data: Dict[str, Any]) -> DiagnoseSettings:
    unknown = set(data) - {"thresholds", "ignore"}
    if unknown:
        raise ConfigError(f"unknown top-level key(s): {', '.join(sorted(unknown))} "
                          "(expected [thresholds] and [[ignore]])")

    defaults = DiagnoseSettings()
    thresholds = data.get("thresholds", {})
    if not isinstance(thresholds, dict):
        raise ConfigError("[thresholds] must be a table")
    known = {"resource_warn_pct", "resource_critical_pct", "storage_warn_pct", "storage_critical_pct",
             "recent_reboot_minutes", "slow_link_mbps",
             "wan_latency_warn_ms", "wan_drops_warn", "wan_availability_warn_pct",
             "wan_speed_drop_pct", "link_flap_count", "port_drop_pct",
             "min_packets_for_drop_pct", "poe_warn_pct", "poe_critical_pct",
             "event_flap_count", "wifi_weak_signal_dbm", "wifi_retry_pct", "wifi_min_attempts",
             "wifi_satisfaction_warn", "radio_util_warn_pct", "radio_util_critical_pct",
             "reserved_offline_warn_days", "reserved_offline_critical_days", "notify_repeat_hours",
             "new_client_window_hours"}
    if set(thresholds) - known:
        raise ConfigError(f"unknown [thresholds] key(s): {', '.join(sorted(set(thresholds) - known))} "
                          f"(valid: {', '.join(sorted(known))})")

    warn = _number("resource_warn_pct", thresholds.get("resource_warn_pct", defaults.resource_warn_pct), 0, 100)
    critical = _number("resource_critical_pct",
                       thresholds.get("resource_critical_pct", defaults.resource_critical_pct), 0, 100)
    if warn > critical:
        raise ConfigError("[thresholds] resource_warn_pct must not exceed resource_critical_pct")
    storage_warn = _number("storage_warn_pct", thresholds.get("storage_warn_pct", defaults.storage_warn_pct), 0, 100)
    storage_critical = _number("storage_critical_pct",
                               thresholds.get("storage_critical_pct", defaults.storage_critical_pct), 0, 100)
    if storage_warn > storage_critical:
        raise ConfigError("[thresholds] storage_warn_pct must not exceed storage_critical_pct")
    reboot_minutes = _number("recent_reboot_minutes",
                             thresholds.get("recent_reboot_minutes", defaults.recent_reboot_minutes), 1)
    slow = _number("slow_link_mbps", thresholds.get("slow_link_mbps", defaults.slow_link_mbps), 0)
    latency = _number("wan_latency_warn_ms",
                      thresholds.get("wan_latency_warn_ms", defaults.wan_latency_warn_ms), 0)
    availability = _number("wan_availability_warn_pct",
                           thresholds.get("wan_availability_warn_pct", defaults.wan_availability_warn_pct), 0, 100)
    speed_drop = _number("wan_speed_drop_pct",
                         thresholds.get("wan_speed_drop_pct", defaults.wan_speed_drop_pct), 0, 100)
    drops = _number("wan_drops_warn", thresholds.get("wan_drops_warn", defaults.wan_drops_warn), 0)
    flaps = _number("link_flap_count", thresholds.get("link_flap_count", defaults.link_flap_count), 1)
    drop_pct = _number("port_drop_pct", thresholds.get("port_drop_pct", defaults.port_drop_pct), 0, 100)
    min_packets = _number(
        "min_packets_for_drop_pct",
        thresholds.get("min_packets_for_drop_pct", defaults.min_packets_for_drop_pct), 1)
    poe_warn = _number("poe_warn_pct", thresholds.get("poe_warn_pct", defaults.poe_warn_pct), 0, 100)
    poe_critical = _number("poe_critical_pct",
                           thresholds.get("poe_critical_pct", defaults.poe_critical_pct), 0, 100)
    if poe_warn > poe_critical:
        raise ConfigError("[thresholds] poe_warn_pct must not exceed poe_critical_pct")
    event_flaps = _number("event_flap_count", thresholds.get("event_flap_count", defaults.event_flap_count), 1)
    weak = _number("wifi_weak_signal_dbm",
                   thresholds.get("wifi_weak_signal_dbm", defaults.wifi_weak_signal_dbm), -120, 0)
    retry = _number("wifi_retry_pct", thresholds.get("wifi_retry_pct", defaults.wifi_retry_pct), 0, 100)
    min_attempts = _number("wifi_min_attempts",
                           thresholds.get("wifi_min_attempts", defaults.wifi_min_attempts), 1)
    satisfaction = _number("wifi_satisfaction_warn",
                           thresholds.get("wifi_satisfaction_warn", defaults.wifi_satisfaction_warn), 0, 100)
    util_warn = _number("radio_util_warn_pct",
                        thresholds.get("radio_util_warn_pct", defaults.radio_util_warn_pct), 0, 100)
    util_critical = _number("radio_util_critical_pct",
                            thresholds.get("radio_util_critical_pct", defaults.radio_util_critical_pct), 0, 100)
    if util_warn > util_critical:
        raise ConfigError("[thresholds] radio_util_warn_pct must not exceed radio_util_critical_pct")
    offline_warn = _number("reserved_offline_warn_days",
                           thresholds.get("reserved_offline_warn_days", defaults.reserved_offline_warn_days), 0)
    offline_critical = _number(
        "reserved_offline_critical_days",
        thresholds.get("reserved_offline_critical_days", defaults.reserved_offline_critical_days), 0)
    if offline_warn > offline_critical:
        raise ConfigError("[thresholds] reserved_offline_warn_days must not exceed reserved_offline_critical_days")
    repeat_hours = _number("notify_repeat_hours",
                           thresholds.get("notify_repeat_hours", defaults.notify_repeat_hours), 0, 24 * 365)
    new_client_hours = _number("new_client_window_hours",
                               thresholds.get("new_client_window_hours", defaults.new_client_window_hours), 0, 24 * 365)
    for name, value in (("wan_drops_warn", drops), ("link_flap_count", flaps),
                        ("min_packets_for_drop_pct", min_packets), ("wifi_min_attempts", min_attempts),
                        ("event_flap_count", event_flaps), ("recent_reboot_minutes", reboot_minutes)):
        if value != int(value):
            raise ConfigError(f"[thresholds] {name} must be a whole number")

    raw_rules = data.get("ignore", [])
    if not isinstance(raw_rules, list):
        raise ConfigError("ignore rules must be written as [[ignore]] tables")
    rules: List[IgnoreRule] = []
    for i, raw in enumerate(raw_rules, 1):
        if not isinstance(raw, dict) or set(raw) - {"code", "subject", "message", "reason", "until"}:
            raise ConfigError(f"[[ignore]] #{i}: only code, subject, message, reason and until are allowed")
        if any(not isinstance(value, str) for key, value in raw.items() if key != "until"):
            raise ConfigError(f"[[ignore]] #{i}: code, subject, message and reason must be strings")
        fields = {key: value for key, value in raw.items() if key != "until"}
        if "until" in raw:
            fields["until"] = _parse_until(raw["until"], i)
        rule = IgnoreRule(**fields)
        if not (rule.code or rule.subject or rule.message):
            raise ConfigError(f"[[ignore]] #{i}: give a code, a subject and/or a message to match")
        if rule.code and rule.code not in known_codes():
            codes = sorted(known_codes())
            close = difflib.get_close_matches(rule.code, codes, n=1)
            raise ConfigError(f"[[ignore]] #{i}: unknown code {rule.code!r}"
                              + (f" (did you mean {close[0]!r}?)" if close else "")
                              + "; codes are matched exactly, without wildcards or case folding. Valid codes: "
                              + ", ".join(codes))
        if not rule.reason.strip():
            raise ConfigError(f"[[ignore]] #{i}: a reason is required")
        rules.append(rule)

    return DiagnoseSettings(
        resource_warn_pct=warn, resource_critical_pct=critical, slow_link_mbps=int(slow),
        storage_warn_pct=storage_warn, storage_critical_pct=storage_critical,
        recent_reboot_minutes=int(reboot_minutes),
        wan_latency_warn_ms=latency, wan_drops_warn=int(drops), wan_availability_warn_pct=availability,
        wan_speed_drop_pct=speed_drop, event_flap_count=int(event_flaps),
        link_flap_count=int(flaps), port_drop_pct=drop_pct,
        min_packets_for_drop_pct=int(min_packets), poe_warn_pct=poe_warn,
        poe_critical_pct=poe_critical, wifi_weak_signal_dbm=weak, wifi_retry_pct=retry,
        wifi_min_attempts=int(min_attempts), wifi_satisfaction_warn=satisfaction,
        radio_util_warn_pct=util_warn, radio_util_critical_pct=util_critical,
        reserved_offline_warn_days=offline_warn, reserved_offline_critical_days=offline_critical,
        notify_repeat_hours=repeat_hours, new_client_window_hours=new_client_hours,
        ignore=tuple(rules))


def load_settings(path: Optional[Path] = None) -> DiagnoseSettings:
    """Settings from ``path``, else ``./hlp.toml`` if present, else defaults.

    An explicit ``path`` must exist. Any problem raises ConfigError.
    """
    if path is None:
        path = Path(DEFAULT_FILENAME)
        if not path.is_file():
            return DiagnoseSettings()
    elif not path.is_file():
        raise ConfigError(f"config file not found: {path}")

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"{path}: could not read config file: {e}") from e
    except UnicodeDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from e
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        rule_number = _invalid_unquoted_until(text, e)
        if rule_number is not None:
            raise ConfigError(
                f"{path}: [[ignore]] #{rule_number}: until must be a valid date like 2026-10-10"
            ) from e
        raise ConfigError(f"{path}: invalid TOML: {e}") from e
    try:
        return _parse(data)
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from e
