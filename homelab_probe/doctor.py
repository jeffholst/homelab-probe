"""``doctor``: is the tool itself installed and set up right?

``diagnose`` looks at the network; this looks at the tool: the versions, the settings files it found, whether the
settings are valid and safe, and whether the controller answers, accepts the API key, has the site and offers the
optional endpoints the commands use. Every finding is a ``Check`` with a **stable id** (``config.env_file``,
``endpoint.stat_alluser``...), a status and a message, so a script, the CLI and a setup wizard can all use the same
results.

Nothing here is a secret or identifies the network: the API key is never shown, the controller's host name or address
is replaced by ``<controller>``, notification destinations are named by kind only, and every message is built from
fixed wording (an error is explained from its ``kind``, never by repeating its text), so the output can be pasted
into an issue. The checks only GET (and send the one approved read-only event-log query); nothing is sent anywhere.
"""

import os
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import requests

from . import __version__
from .client import UniFiAPIError, UniFiClient
from .config import (
    Config,
    ConfigError,
    EnvFileReport,
    env_file_warning,
    find_env_file,
    inspect_env_file,
    load_config,
)
from .notify import Event, destinations_from_config, render_text
from .settings import DEFAULT_FILENAME, expired_rules, load_settings
from .util import plural, printable

JSON_VERSION = 1
OK, WARN, FAIL, SKIP, INFO = "ok", "warn", "fail", "skip", "info"
STATUSES = (OK, WARN, FAIL, SKIP, INFO)
LABELS = {OK: "OK", WARN: "WARN", FAIL: "FAIL", SKIP: "SKIP", INFO: "INFO"}
TESTED_VERSION = "10.6.106"
SECTIONS = {"install": "Installation", "config": "Configuration", "notify": "Notifications",
            "controller": "Controller", "endpoint": "What the controller offers"}


@dataclass(frozen=True)
class Reader:
    """One read the commands depend on: how to make it, whether every command needs it, and what is lost without it."""

    id: str
    title: str
    read: Callable[["_Target"], Any]
    required: bool
    impact: str


@dataclass(frozen=True)
class _Target:
    client: UniFiClient
    ref: str
    site_id: str
    device_id: str
    now_ms: int


READERS: List[Reader] = [
    Reader("integration_devices", "Devices (Integration API)", lambda t: t.client.devices(t.site_id), True,
           "every command needs it"),
    Reader("integration_clients", "Connected clients (Integration API)", lambda t: t.client.clients(t.site_id), True,
           "every command needs it"),
    Reader("integration_device_detail", "Device details (Integration API)",
           lambda t: t.client.device(t.site_id, t.device_id), False,
           "firmware and uplink details are missing from `diagnose`, `topology` and `query devices`"),
    Reader("integration_device_stats", "Device statistics (Integration API)",
           lambda t: t.client.device_statistics(t.site_id, t.device_id), False,
           "uptime and the CPU and memory checks are missing"),
    Reader("stat_device", "Legacy device records (stat/device)", lambda t: t.client.legacy_stat(t.ref, "device"), False,
           "the port, Wi-Fi radio, PoE, overheating, storage and restart checks, `query ports` and the switch exports"),
    Reader("stat_sta", "Legacy connected clients (stat/sta)", lambda t: t.client.legacy_stat(t.ref, "sta"), False,
           "where a client is plugged in, Wi-Fi quality, `query networks`/`wlans` counts and the client filters"),
    Reader("stat_alluser", "Client history (stat/alluser)", lambda t: t.client.legacy_stat(t.ref, "alluser"), False,
           "offline clients, reservations, `new-clients`, `snapshot` and `diff`"),
    Reader("stat_health", "Controller health (stat/health)", lambda t: t.client.legacy_stat(t.ref, "health"), False,
           "the health and internet checks and `wan`"),
    Reader("rest_networkconf", "Network settings (rest/networkconf)",
           lambda t: t.client.legacy_rest(t.ref, "networkconf"), False,
           "network names and VLANs, the reservation checks and `query networks`"),
    Reader("rest_wlanconf", "Wi-Fi network settings (rest/wlanconf)", lambda t: t.client.legacy_rest(t.ref, "wlanconf"),
           False, "the `audit` Wi-Fi checks and `query wlans`"),
    Reader("stat_rogueap", "Neighboring networks (stat/rogueap)", lambda t: t.client.legacy_stat(t.ref, "rogueap"),
           False, "the channel plan of `wifi`"),
    Reader("v2_speedtest", "Speedtest history (v2)", lambda t: t.client.legacy_v2(t.ref, "speedtest"), False,
           "the speedtest part of `wan` and its check"),
    Reader("v2_groups", "Client groups (v2)", lambda t: t.client.legacy_v2(t.ref, "network-members-groups"), False,
           "group names in `new-clients`"),
    Reader("v2_firewall_policies", "Firewall policies (v2)", lambda t: t.client.legacy_v2(t.ref, "firewall-policies"),
           False, "`firewall` (a controller on the classic firewall has none)"),
    Reader("v2_firewall_zone", "Firewall zones (v2)", lambda t: t.client.legacy_v2(t.ref, "firewall/zone"), False,
           "zone names in `firewall`"),
    Reader("v2_firewall_matrix", "Firewall zone matrix (v2)",
           lambda t: t.client.legacy_v2(t.ref, "firewall/zone-matrix"), False, "the zone matrix of `firewall`"),
    Reader("rest_portforward", "Port forwards (rest/portforward)", lambda t: t.client.legacy_rest(t.ref, "portforward"),
           False, "the port forwards of `firewall`"),
]
EVENTS_TITLE = "Event log (the one read-only POST)"
EVENTS_IMPACT = "`events` and the event checks of `diagnose` and `client`"

# Every check id with its title; ids are an interface (never renamed or reused), documented in docs/configuration.md.
CHECKS: Dict[str, str] = {
    "install.version": "Versions",
    "config.env_file": ".env file",
    "config.env_permissions": ".env permissions",
    "config.env_contents": "Settings in .env",
    "config.settings_file": "Settings file",
    "config.environment": "Required settings",
    "config.tls": "TLS verification",
    "config.limits": "Timeout, parallel reads and site",
    "notify.configured": "Notification destinations",
    "notify.dry_run": "Notification dry run",
    "controller.address": "Controller address",
    "controller.reachable": "Controller and API key",
    "controller.version": "Controller version",
    "controller.site": "Site",
    **{f"endpoint.{r.id}": r.title for r in READERS},
    "endpoint.events": EVENTS_TITLE,
}

_SECRET_VARIABLES = ("UNIFI_API_KEY", "NOTIFY_NTFY_URL", "NOTIFY_NTFY_TOKEN", "NOTIFY_WEBHOOK_URL",
                     "NOTIFY_WEBHOOK_TOKEN", "NOTIFY_SMTP_HOST", "NOTIFY_SMTP_USER", "NOTIFY_SMTP_PASSWORD",
                     "NOTIFY_EMAIL_FROM", "NOTIFY_EMAIL_TO")


def scrub(text: str) -> str:
    """``text`` without any secret or address from the environment: the key and notification settings become ``***``,
    the controller's URL, ``host:port`` and host become ``<controller>``. A second safety net: the messages are
    already built from fixed words."""
    for name in _SECRET_VARIABLES:
        value = os.environ.get(name, "").strip()
        if len(value) >= 4:
            text = text.replace(value, "***")
    url = os.environ.get("UNIFI_URL", "").strip()
    if len(url) >= 4:
        parts = urlsplit(url)
        for hidden in (url, url.rstrip("/"), parts.netloc, parts.hostname or ""):
            if len(hidden) >= 3:
                text = text.replace(hidden, "<controller>")
    return text


@dataclass(frozen=True)
class Check:
    id: str
    status: str
    message: str
    fix: str = ""

    @property
    def section(self) -> str:
        return self.id.partition(".")[0]

    @property
    def title(self) -> str:
        return CHECKS[self.id]

    def to_dict(self) -> Dict[str, str]:
        return {"id": self.id, "section": self.section, "status": self.status, "title": self.title,
                "message": self.message, "fix": self.fix}


def make(check_id: str, status: str, message: str, fix: str = "") -> Check:
    """A check whose text is on one line and free of secrets and addresses."""
    assert check_id in CHECKS and status in STATUSES, (check_id, status)
    return Check(check_id, status, printable(scrub(message)), printable(scrub(fix)))


@dataclass(frozen=True)
class Options:
    env_file: Optional[Path] = None
    site: Optional[str] = None
    timeout: Optional[float] = None
    parallel: Optional[int] = None
    config: Optional[Path] = None
    offline: bool = False
    events: bool = True


# -- the tool and its files ------------------------------------------------------------------------------------------

def _install_checks() -> List[Check]:
    version = ".".join(str(n) for n in sys.version_info[:3])
    return [make("install.version", INFO,
                 f"hlp {__version__}, Python {version} on {sys.platform}, requests {requests.__version__}")]


def _lines(numbers: List[int]) -> str:
    """'line 4' or 'lines 4, 9'."""
    return ("line " if len(numbers) == 1 else "lines ") + ", ".join(str(n) for n in numbers)


def env_contents_check(report: EnvFileReport) -> Check:
    """What is in the ``.env`` file: names and line numbers only, never a value."""
    if report.clean:
        return make("config.env_contents", OK,
                    f"{plural(report.settings, 'setting')}, each listed once, all recognized")
    problems: List[str] = []
    fixes: List[str] = []
    for name, found in report.duplicates.items():
        problems.append(f"{name} is listed {plural(len(found), 'time')} ({_lines(found)}) and the last one "
                        f"(line {found[-1]}) is used")
    if report.duplicates:
        fixes.append("keep one line for each setting")
    for line, name, suggestion in report.unknown:
        what = f"{name} (line {line}) is not a setting" if name else f"line {line} sets a name that is not a setting"
        problems.append(what + (f", did you mean {suggestion}?" if suggestion else ""))
    if report.unknown:
        fixes.append("correct or remove the names that are not settings (they are ignored)")
    for line in report.misplaced:
        problems.append(f"HLP_ENV (line {line}) does nothing in a .env file, because it names the file; set it in "
                        "the environment")
    for line in report.bad_lines:
        problems.append(f"line {line} cannot be read (check quotes and the = sign)")
    if report.misplaced or report.bad_lines:
        fixes.append("fix or remove those lines")
    for name in report.empty:
        problems.append(f"{name} is empty, which counts as not set")
    if report.empty:
        fixes.append("give an empty setting a value or delete its line")
    for name in report.overridden:
        problems.append(f"{name} is also set in the environment with another value, and the environment wins over "
                        "the .env file")
    if report.overridden:
        fixes.append("unset the variable in the shell, or change it there instead of in the file")
    return make("config.env_contents", WARN, "; ".join(problems), "; ".join(fixes))


def _file_checks(options: Options) -> Tuple[List[Check], Optional[Path]]:
    """The .env file, its permissions and the settings file. Also returns the .env path (None: none, or unusable)."""
    checks: List[Check] = []
    path: Optional[Path] = None
    try:
        path = find_env_file(options.env_file)
    except ConfigError as e:
        checks.append(make("config.env_file", FAIL, str(e),
                           "give an existing file to --env-file or HLP_ENV, or unset it"))
        checks.append(make("config.env_permissions", SKIP, "skipped: there is no .env file to look at"))
        checks.append(make("config.env_contents", SKIP, "skipped: there is no .env file to look at"))
    else:
        if path is not None:
            checks.append(make("config.env_file", OK, f"found {path}"))
            warning = env_file_warning(path)
            if warning is not None:
                checks.append(make("config.env_permissions", WARN, warning))
            elif sys.platform.startswith("win"):
                checks.append(make("config.env_permissions", INFO, "not checked on Windows"))
            else:
                checks.append(make("config.env_permissions", OK, "only you can read it (it holds the API key)"))
            checks.append(env_contents_check(inspect_env_file(path)))
        else:
            from_environment = bool(os.environ.get("UNIFI_URL") and os.environ.get("UNIFI_API_KEY"))
            checks.append(make(
                "config.env_file", INFO if from_environment else WARN,
                "no .env file in the current directory; the settings come from the environment" if from_environment
                else "no .env file in the current directory and no --env-file or HLP_ENV",
                "" if from_environment else "copy example.env to .env here, or run from the directory that has it"))
            checks.append(make("config.env_permissions", SKIP, "skipped: there is no .env file to look at"))
            checks.append(make("config.env_contents", SKIP, "skipped: there is no .env file to look at"))
    checks.append(_settings_check(options))
    return checks, path


def _settings_check(options: Options) -> Check:
    name = options.config if options.config is not None else Path(DEFAULT_FILENAME)
    try:
        settings = load_settings(options.config)
    except ConfigError as e:
        return make("config.settings_file", FAIL, str(e), f"fix {name} (diagnose and the other reports stop at it)")
    if options.config is None and not name.is_file():
        return make("config.settings_file", INFO, f"no {DEFAULT_FILENAME} here; the defaults apply (optional)")
    ended = len(expired_rules(settings.ignore))
    rules = plural(len(settings.ignore), "ignore rule")
    if ended:
        return make("config.settings_file", WARN, f"{name} is valid, {rules}, {ended} of them expired",
                    "an expired rule no longer applies: delete it or give it a later until date")
    return make("config.settings_file", OK, f"{name} is valid, {rules}")


def _load(options: Options) -> Tuple[Optional[Config], Check]:
    try:
        config = load_config(options.env_file, site_override=options.site)
    except ConfigError as e:
        return None, make("config.environment", FAIL, str(e))
    if options.timeout is not None:
        config = replace(config, timeout=options.timeout)
    if options.parallel is not None:
        config = replace(config, parallel=options.parallel)
    return config, make("config.environment", OK, "UNIFI_URL and UNIFI_API_KEY are set and valid")


def _config_checks(config: Optional[Config], first: Check) -> List[Check]:
    checks = [first]
    if config is None:
        for check_id in ("config.tls", "config.limits"):
            checks.append(make(check_id, SKIP, "skipped: the required settings are not valid"))
        return checks
    parts = urlsplit(config.controller_url)
    if parts.scheme == "http":
        checks.append(make("config.tls", WARN, "plain http: the API key travels in clear text",
                           "use https://; ALLOW_INSECURE_HTTP is for a lab network you trust"))
    elif config.verify_ssl is False:
        checks.append(make("config.tls", WARN,
                           "certificate checking is off: the key is sent without checking who answers",
                           "trust the controller's signing CA with UNIFI_VERIFY_SSL=/path/to/ca.pem"))
    elif isinstance(config.verify_ssl, str):
        checks.append(make("config.tls", OK, f"certificates are verified against {config.verify_ssl}"))
    else:
        checks.append(make("config.tls", OK, "certificates are verified against the system's trusted authorities",
                           "for a private CA, set UNIFI_VERIFY_SSL to its CA file; use a self-signed leaf certificate "
                           "as a trust anchor only if OpenSSL accepts it"))
    checks.append(make("config.limits", OK, f"timeout {config.timeout:g} s, "
                       f"{plural(config.parallel, 'request')} at once, site {config.site}"))
    return checks


def _notify_checks(config: Optional[Config]) -> List[Check]:
    if config is None:
        return [make("notify.configured", SKIP, "skipped: the required settings are not valid"),
                make("notify.dry_run", SKIP, "skipped: the required settings are not valid")]
    destinations = destinations_from_config(config)
    kinds = [d.kind for d in destinations]
    if not kinds:
        return [make("notify.configured", INFO, "no destination configured (optional: `diagnose --notify`)"),
                make("notify.dry_run", SKIP, "skipped: nothing to send to")]
    plaintext = [d.kind for d in destinations
                 if (d.url and urlsplit(d.url).scheme.lower() == "http")
                 or (d.smtp is not None and d.smtp.security == "none")]
    status = WARN if plaintext else OK
    message = f"configured: {', '.join(kinds)}"
    fix = ""
    if plaintext:
        message += f"; unencrypted: {', '.join(plaintext)}"
        fix = "use HTTPS for ntfy and webhooks, and STARTTLS or implicit TLS for email"
    title, _ = render_text([Event("new", "warning", "device.offline", "Sample device", "device is offline")])
    return [make("notify.configured", status, message, fix),
            make("notify.dry_run", OK, f"would send one message to {', '.join(kinds)} titled '{title}'; "
                 "nothing was sent")]


# -- the controller --------------------------------------------------------------------------------------------------

def explain(error: UniFiAPIError, config: Config) -> Tuple[str, str]:
    """(message, fix) for a failed request, from its kind, never from its text (which has the address in it)."""
    kind = error.kind
    if kind == "tls":
        if error.tls_reason == "hostname_mismatch":
            return ("the controller's TLS certificate does not match the UNIFI_URL host name or address",
                    "use a URL named in the certificate, or install a certificate with a matching subjectAltName")
        if error.tls_reason == "invalid_ca":
            return ("a certificate in the TLS verification chain is not usable as a CA certificate",
                    "check the controller's certificate chain and trust configuration; for a private CA, "
                    "set UNIFI_VERIFY_SSL to its CA file")
        if error.tls_reason == "untrusted":
            return ("TLS certificate verification failed with the configured CA bundle",
                    "check the configured CA bundle and the controller's certificate chain")
        if isinstance(config.verify_ssl, str):
            return ("the controller's TLS certificate was not accepted using the configured CA bundle",
                    "check the configured CA bundle and the controller's certificate chain")
        return ("the controller's TLS certificate was not accepted",
                "for a private CA, point UNIFI_VERIFY_SSL at its CA file (PEM); use a self-signed leaf certificate "
                "as a trust anchor only if OpenSSL accepts it; "
                "UNIFI_VERIFY_SSL=false is a last resort")
    if kind == "unauthorized":
        return ("the controller rejected the API key (401)",
                "create a key under Settings > Control Plane > Integrations and put it in UNIFI_API_KEY")
    if kind == "forbidden":
        return ("403: the key is valid but not allowed to make this request",
                "check the key's access in Settings > Control Plane > Integrations")
    if kind == "timeout":
        return (f"no answer within {config.timeout:g} s", "raise UNIFI_TIMEOUT or pass --timeout 60 for a slow gateway")
    if kind == "connection":
        return ("could not connect", "check the host and port in UNIFI_URL and that this machine can reach it")
    if kind == "bad_body":
        return ("answered with something that is not JSON",
                "UNIFI_URL may point at a login page or a proxy instead of the controller")
    if kind == "http":
        return (f"HTTP {error.status}", "")
    return ("the request could not be made", "")


def _skip_rest(reason: str, site: bool = False) -> List[Check]:
    """The checks that depend on the controller answering (and, with ``site``, on the site), all skipped."""
    checks = [make("controller.site", SKIP, reason)] if site else []
    checks += [make(f"endpoint.{r.id}", SKIP, reason) for r in READERS]
    return checks + [make("endpoint.events", SKIP, reason)]


def _reader_check(reader: Reader, target: _Target, config: Config) -> Tuple[Check, Any]:
    check_id = f"endpoint.{reader.id}"
    try:
        records = reader.read(target)
    except UniFiAPIError as e:
        what, _ = explain(e, config)
        if reader.required:
            return make(check_id, FAIL, f"unavailable ({what}): {reader.impact}"), None
        return make(check_id, WARN, f"unavailable ({what}): without it, {reader.impact}"), None
    count = plural(len(records), "record") if isinstance(records, list) else "answered"
    return make(check_id, OK, count), records


def _events_check(target: _Target, config: Config) -> Check:
    try:
        body = target.client.system_log(target.ref, {"timestampFrom": target.now_ms - 3_600_000,
                                                     "timestampTo": target.now_ms, "pageNumber": 0, "pageSize": 1})
    except UniFiAPIError as e:
        what, _ = explain(e, config)
        return make("endpoint.events", WARN, f"unavailable ({what}): without it, {EVENTS_IMPACT} are skipped")
    return make("endpoint.events", OK, f"answered ({body.get('total_element_count', 0)} events in the last hour)")


def _controller_checks(config: Config, options: Options, factory: Callable[[Config], UniFiClient],
                       now: float) -> List[Check]:
    checks: List[Check] = []
    parts = urlsplit(config.controller_url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    checks.append(make("controller.address", OK, f"{parts.scheme}, port {port} (the host is not shown)"))
    client = factory(config)
    client.retries = 0          # the first attempt tells the truth; retries would hide a flaky link
    try:
        info = client.info()
    except UniFiAPIError as e:
        message, fix = explain(e, config)
        checks.append(make("controller.reachable", FAIL, message, fix))
        checks.append(make("controller.version", SKIP, "skipped: the controller did not answer"))
        return checks + _skip_rest("skipped: the controller did not answer", site=True)
    checks.append(make("controller.reachable", OK, "answered, and accepted the API key"))
    version = printable(str(info.get("applicationVersion") or "unknown")) if isinstance(info, dict) else "unknown"
    if version == TESTED_VERSION:
        checks.append(make("controller.version", OK, f"UniFi Network {version}, the version this tool was tested on"))
    else:
        checks.append(make("controller.version", INFO,
                           f"UniFi Network {version}; this tool was tested on {TESTED_VERSION} only, so a field may "
                           "differ (run a command with --verbose and open an issue if something looks wrong)"))
    try:
        site = client.resolve_site(config.site)
    except UniFiAPIError as e:
        if e.kind != "site":
            message, fix = explain(e, config)
            checks.append(make("controller.site", FAIL, message, fix))
        else:
            checks.append(make("controller.site", FAIL, f"site '{config.site}' was not found; the controller has "
                               f"{plural(len(client.sites()), 'site')}",
                               "run `hlp info` to list them and set UNIFI_SITE_ID or --site"))
        return checks + _skip_rest("skipped: the site was not found")
    checks.append(make("controller.site", OK, f"found ({plural(len(client.sites()), 'site')} on the controller)"))
    now_ms = int(now * 1000)
    device_id = ""
    for reader in READERS:
        if reader.id.startswith("integration_device_") and not device_id:
            checks.append(make(f"endpoint.{reader.id}", SKIP, "skipped: the controller has no devices to look at"))
            continue
        target = _Target(client, site.get("internalReference") or config.site, site["id"], device_id, now_ms)
        check, records = _reader_check(reader, target, config)
        checks.append(check)
        if reader.id == "integration_devices" and isinstance(records, list) and records:
            device_id = str(records[0].get("id") or "")
    target = _Target(client, site.get("internalReference") or config.site, site["id"], device_id, now_ms)
    if options.events:
        checks.append(_events_check(target, config))
    else:
        checks.append(make("endpoint.events", SKIP, "skipped (--no-events)"))
    return checks


def _controller_skipped(reason: str) -> List[Check]:
    """Every controller check, skipped for one reason, in the order they would have run."""
    head = [make(check_id, SKIP, reason)
            for check_id in ("controller.address", "controller.reachable", "controller.version")]
    return head + _skip_rest(reason, site=True)


def check_controller(config: Config, factory: Optional[Callable[[Config], UniFiClient]] = None, events: bool = False,
                     now: Optional[float] = None) -> List[Check]:
    """Only the checks of the controller and of the endpoints it offers, for a ``Config`` that was not read from the
    environment (the guided setup tests what was just typed). The first read is not retried, as in ``doctor``."""
    return _controller_checks(config, Options(events=events), factory or UniFiClient.from_config,
                              time.time() if now is None else now)


def run_checks(options: Options, factory: Optional[Callable[[Config], UniFiClient]] = None,
               now: Optional[float] = None) -> List[Check]:
    """Every check, in order. Never raises for a problem it is there to report. ``factory`` makes the client (the
    default is looked up when it is used, so a test can replace ``UniFiClient.from_config``)."""
    checks = _install_checks()
    files, _ = _file_checks(options)
    if any(c.id == "config.env_file" and c.status == FAIL for c in files):
        config, first = None, make("config.environment", SKIP, "skipped: the env file could not be found")
    else:
        config, first = _load(options)
    checks += files + _config_checks(config, first) + _notify_checks(config)
    if config is None:
        return checks + _controller_skipped("skipped: the required settings are not valid")
    if options.offline:
        return checks + _controller_skipped("skipped (--offline)")
    return checks + _controller_checks(config, options, factory or UniFiClient.from_config,
                                       time.time() if now is None else now)


# -- output ----------------------------------------------------------------------------------------------------------

def summary(checks: List[Check]) -> Dict[str, int]:
    return {status: sum(c.status == status for c in checks) for status in STATUSES}


def exit_failed(checks: List[Check]) -> bool:
    return any(c.status == FAIL for c in checks)


def render(checks: List[Check]) -> str:
    """The checks by section, one line each, then what to do about the ones that need it."""
    lines = ["Homelab Probe doctor"]
    section = ""
    for check in checks:
        if check.section != section:
            section = check.section
            lines += ["", SECTIONS[section]]
        lines.append(f"  [{LABELS[check.status]:<4}] {check.title}: {check.message}")
        if check.fix and check.status in (WARN, FAIL, INFO, OK):
            lines.append(f"         -> {check.fix}")
    counts = summary(checks)
    lines += ["", f"{counts[OK]} ok, {plural(counts[WARN], 'warning')}, {counts[FAIL]} failed, {counts[SKIP]} skipped, "
                  f"{counts[INFO]} for your information"]
    lines.append("Everything needed works." if not counts[FAIL] and not counts[WARN]
                 else "Nothing is broken, but see the warnings." if not counts[FAIL]
                 else f"{plural(counts[FAIL], 'check')} failed: fix those first.")
    return "\n".join(lines)


def to_dict(checks: List[Check]) -> Dict[str, Any]:
    return {"version": JSON_VERSION,
            "tool": {"version": __version__, "python": ".".join(str(n) for n in sys.version_info[:3]),
                     "platform": sys.platform},
            "checks": [c.to_dict() for c in checks], "summary": summary(checks)}
