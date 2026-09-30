"""diagnose settings: thresholds and an ignore list, from an optional TOML file."""

import fnmatch
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if sys.version_info >= (3, 11):
    import tomllib
else:  # Python 3.9 and 3.10
    import tomli as tomllib

from .config import ConfigError

DEFAULT_FILENAME = "unifi-sentinel.toml"


@dataclass(frozen=True)
class IgnoreRule:
    """Suppress findings. ``subject`` is a case-insensitive name (``*`` and ``?``
    wildcards); ``message`` is a case-insensitive substring. Both must match when
    both are given. ``reason`` is required so ignores stay explainable."""

    subject: str = ""
    message: str = ""
    reason: str = ""

    def matches(self, subject: str, message: str) -> bool:
        pattern = self.subject.lower().replace("[", "[[]")
        if self.subject and not fnmatch.fnmatchcase(subject.lower(), pattern):
            return False
        if self.message and self.message.lower() not in message.lower():
            return False
        return True


@dataclass(frozen=True)
class DiagnoseSettings:
    resource_warn_pct: float = 90        # CPU/memory at or above: warning
    resource_critical_pct: float = 98    # CPU/memory at or above: critical
    slow_link_mbps: int = 100            # ports negotiated at or below: info
    wan_latency_warn_ms: float = 100     # internet latency at or above: warning
    wan_drops_warn: int = 10             # internet drops at or above: warning (heuristic)
    ignore: Tuple[IgnoreRule, ...] = ()


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
    known = {"resource_warn_pct", "resource_critical_pct", "slow_link_mbps",
             "wan_latency_warn_ms", "wan_drops_warn"}
    if set(thresholds) - known:
        raise ConfigError(f"unknown [thresholds] key(s): {', '.join(sorted(set(thresholds) - known))} "
                          f"(valid: {', '.join(sorted(known))})")

    warn = _number("resource_warn_pct", thresholds.get("resource_warn_pct", defaults.resource_warn_pct), 0, 100)
    critical = _number("resource_critical_pct",
                       thresholds.get("resource_critical_pct", defaults.resource_critical_pct), 0, 100)
    if warn > critical:
        raise ConfigError("[thresholds] resource_warn_pct must not exceed resource_critical_pct")
    slow = _number("slow_link_mbps", thresholds.get("slow_link_mbps", defaults.slow_link_mbps), 0)
    latency = _number("wan_latency_warn_ms",
                      thresholds.get("wan_latency_warn_ms", defaults.wan_latency_warn_ms), 0)
    drops = _number("wan_drops_warn", thresholds.get("wan_drops_warn", defaults.wan_drops_warn), 0)

    raw_rules = data.get("ignore", [])
    if not isinstance(raw_rules, list):
        raise ConfigError("ignore rules must be written as [[ignore]] tables")
    rules: List[IgnoreRule] = []
    for i, raw in enumerate(raw_rules, 1):
        if not isinstance(raw, dict) or set(raw) - {"subject", "message", "reason"}:
            raise ConfigError(f"[[ignore]] #{i}: only subject, message and reason are allowed")
        if any(not isinstance(value, str) for value in raw.values()):
            raise ConfigError(f"[[ignore]] #{i}: subject, message and reason must be strings")
        rule = IgnoreRule(**raw)
        if not (rule.subject or rule.message):
            raise ConfigError(f"[[ignore]] #{i}: give a subject and/or a message to match")
        if not rule.reason.strip():
            raise ConfigError(f"[[ignore]] #{i}: a reason is required")
        rules.append(rule)

    return DiagnoseSettings(
        resource_warn_pct=warn, resource_critical_pct=critical, slow_link_mbps=int(slow),
        wan_latency_warn_ms=latency, wan_drops_warn=int(drops), ignore=tuple(rules))


def load_settings(path: Optional[Path] = None) -> DiagnoseSettings:
    """Settings from ``path``, else ``./unifi-sentinel.toml`` if present, else defaults.

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
        raise ConfigError(f"{path}: invalid TOML: {e}") from e
    try:
        return _parse(data)
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from e
