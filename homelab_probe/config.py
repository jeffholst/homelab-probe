"""Configuration from environment variables and an optional ``.env`` file.

The ``.env`` file is found like this, first match wins:

1. the file given with ``--env-file``;
2. the file named by the ``HLP_ENV`` environment variable;
3. ``.env`` in the **current working directory**.

Parent directories are not searched, and neither is the installed package's directory,
so an installed copy behaves like a checkout and an unrelated project's ``.env`` is never
picked up. Real environment variables always win over values in the file.

The API key is a credential, so two things are checked: the ``.env`` file that was read
should not be readable by other users (a warning, like ``ssh`` gives, not an error), and the
controller URL must be ``https://`` unless ``ALLOW_INSECURE_HTTP`` opts in, because the key
travels in a header of every request.
"""

import difflib
import ipaddress
import math
import os
import re
import shlex
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from dotenv import load_dotenv
from dotenv.parser import parse_stream

from .logs import FORMAT_WORDS, LEVEL_WORDS

ENV_FILE_VAR = "HLP_ENV"
DEFAULT_ENV_FILE = ".env"
DEFAULT_SITE = "default"
MAX_SITE_LENGTH = 128
DEFAULT_TIMEOUT = 15.0     # seconds per request
MIN_TIMEOUT, MAX_TIMEOUT = 1.0, 600.0
DEFAULT_PARALLEL, MAX_PARALLEL = 6, 16     # requests in flight at once
DEFAULT_SESSION_IDLE_MINUTES, MAX_SESSION_IDLE_MINUTES = 30, 24 * 60      # a web session ends after this idle time
DEFAULT_SESSION_MAX_HOURS, MAX_SESSION_MAX_HOURS = 12, 24 * 30     # ... and in any case after this long
DEFAULT_AUDIT_MB, MAX_AUDIT_MB = 5, 1024           # size of one audit log file
DEFAULT_AUDIT_FILES, MAX_AUDIT_FILES = 10, 1000    # audit log files kept in all (the current one and the rotated ones)
_CA_BUNDLE_SUFFIXES = (".pem", ".crt", ".cer")
TRUE_WORDS = ("true", "yes", "1", "on")
FALSE_WORDS = ("false", "no", "0", "off")
_UNSAFE_SITE_CHARACTERS = "/\\?#"
SECRET_FILE_GROUP_OTHER_BITS = 0o077   # any of these set means someone besides the owner can read

# Every variable the code reads (``example.env`` documents them; ``tests/test_env_contents.py`` pins the list). Not
# ``HLP_ENV``: it names the file, so it is read before the file and does nothing inside it.
KNOWN_VARIABLES = (
    "UNIFI_URL", "UNIFI_API_KEY", "UNIFI_SITE_ID", "UNIFI_VERIFY_SSL", "UNIFI_TIMEOUT", "UNIFI_PARALLEL_REQUESTS",
    "ALLOW_INSECURE_HTTP", "LOG_LEVEL", "LOG_FORMAT", "AUDIT_LOG_MAX_MB", "AUDIT_LOG_FILES",
    "SESSION_IDLE_MINUTES", "SESSION_MAX_HOURS",
    "NOTIFY_NTFY_URL", "NOTIFY_NTFY_TOKEN", "NOTIFY_WEBHOOK_URL", "NOTIFY_WEBHOOK_TOKEN",
    "NOTIFY_SMTP_HOST", "NOTIFY_SMTP_PORT", "NOTIFY_SMTP_SECURITY", "NOTIFY_SMTP_USER", "NOTIFY_SMTP_PASSWORD",
    "NOTIFY_EMAIL_FROM", "NOTIFY_EMAIL_TO",
)
MAX_NAME_LENGTH = 64      # a longer or odder key is never repeated in a message: it may be a pasted secret
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


SMTP_SECURITY_WORDS = ("starttls", "ssl", "none")
SMTP_DEFAULT_PORTS = {"starttls": 587, "ssl": 465, "none": 25}
MAX_RECIPIENTS = 20
_ADDRESS = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def _smtp_host(host: str) -> Optional[str]:
    if host.startswith("[") or host.endswith("]"):
        if not (host.startswith("[") and host.endswith("]")):
            return None
        try:
            return str(ipaddress.IPv6Address(host[1:-1]))
        except ValueError:
            return None
    labels = host.split(".")
    if len(host) > 253 or any(not _DNS_LABEL.fullmatch(label) for label in labels):
        return None
    return host


@dataclass(frozen=True)
class SmtpSettings:
    """Where `diagnose --notify` sends email. Everything here is a secret or close to one (the mail host, the
    account, the addresses), so nothing is shown by ``repr`` and no message repeats a value."""

    host: str = field(repr=False)
    port: int = field(repr=False)
    security: str = field(repr=False)          # "starttls", "ssl" or "none" (lab opt-in, never with a password)
    sender: str = field(repr=False)
    recipients: Tuple[str, ...] = field(repr=False)
    user: str = field(default="", repr=False)
    password: str = field(default="", repr=False)


@dataclass(frozen=True)
class Config:
    controller_url: str
    api_key: str = field(repr=False)        # never shown, even by repr() in a traceback or log
    site: str = DEFAULT_SITE
    verify_ssl: bool | str = True          # False, True, or the path of a CA bundle (file or directory)
    timeout: float = DEFAULT_TIMEOUT
    parallel: int = DEFAULT_PARALLEL
    # Notification destinations (diagnose --notify). The URLs and tokens are secrets: never shown.
    notify_ntfy_url: str = field(default="", repr=False)
    notify_ntfy_token: str = field(default="", repr=False)
    notify_webhook_url: str = field(default="", repr=False)
    notify_webhook_token: str = field(default="", repr=False)
    notify_smtp: Optional[SmtpSettings] = field(default=None, repr=False)
    log_level: str = ""                     # LOG_LEVEL: DEBUG, INFO, WARNING or ERROR; blank: the command's default
    log_format: str = ""                    # LOG_FORMAT: text or json; blank means the command line's own format
    audit_log_mb: int = DEFAULT_AUDIT_MB    # AUDIT_LOG_MAX_MB: size of one web audit log file
    audit_log_files: int = DEFAULT_AUDIT_FILES    # AUDIT_LOG_FILES: how many are kept in all
    session_idle_minutes: int = DEFAULT_SESSION_IDLE_MINUTES    # SESSION_IDLE_MINUTES: web login idle timeout
    session_max_hours: int = DEFAULT_SESSION_MAX_HOURS          # SESSION_MAX_HOURS: web login absolute lifetime
    env_file: Optional[Path] = field(default=None, compare=False)    # the .env that was read, if any (for --verbose)
    warnings: Tuple[str, ...] = field(default=(), compare=False)   # for cli.main to print

    def secret_values(self) -> Tuple[str, ...]:
        """Every value that must never reach a log line: the API key, the notification URLs, tokens and mail
        account (``logs.register_secrets`` hides them in any spelling)."""
        values = [self.api_key, self.notify_ntfy_url, self.notify_ntfy_token, self.notify_webhook_url,
                  self.notify_webhook_token]
        if self.notify_smtp is not None:
            smtp = self.notify_smtp
            values += [smtp.host, smtp.user, smtp.password, smtp.sender, *smtp.recipients]
        return tuple(value for value in values if value)


def parse_bool(name: str, text: Optional[str], default: bool = True) -> bool:
    """A yes/no setting. Unset or blank gives ``default``; anything that is not one of the
    documented words is an error (a typo must not silently mean "verify" or "don't")."""
    word = (text or "").strip().lower()
    if not word:
        return default
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    raise ConfigError(
        f"{name} must be one of {', '.join(TRUE_WORDS + FALSE_WORDS)} (got {text!r})")


def parse_verify(text: Optional[str]) -> bool | str:
    """``UNIFI_VERIFY_SSL``: the usual yes/no words, or the path of a CA bundle (a PEM file, or a directory
    of certificates) to trust instead of the system store, which is the proper way to accept a
    self-signed controller certificate. Unset or blank verifies with the system store."""
    value = (text or "").strip()
    if not value or value.lower() in TRUE_WORDS + FALSE_WORDS:
        return parse_bool("UNIFI_VERIFY_SSL", value)
    path = Path(value).expanduser()
    if not (path.is_file() or path.is_dir()):
        if not (any(c in value for c in "/\\~") or value.lower().endswith(_CA_BUNDLE_SUFFIXES)):
            raise ConfigError(f"UNIFI_VERIFY_SSL must be one of {', '.join(TRUE_WORDS + FALSE_WORDS)}, "
                              f"or the path of a CA bundle file (got {text!r})")
        raise ConfigError(f"UNIFI_VERIFY_SSL names a CA bundle that does not exist: {value}")
    if not os.access(path, os.R_OK):
        raise ConfigError(f"UNIFI_VERIFY_SSL names a CA bundle that cannot be read: {value}")
    return str(path)


def validate_notify_url(name: str, text: Optional[str], allow_http: bool = False) -> str:
    """A notification destination URL, or '' when unset. The URL is a secret (a topic name or a
    token is part of it), so no message here repeats it. It needs a scheme and a host, may carry
    a path and a query, and must be ``https://`` unless ``allow_http`` (the lab opt-in)."""
    url = (text or "").strip()
    if not url:
        return ""
    if any(c.isspace() or c == "\\" or ord(c) < 32 or 127 <= ord(c) <= 159 for c in url):
        raise ConfigError(f"{name} must not contain spaces, backslashes or control characters")
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        raise ConfigError(f"{name} is not a valid URL") from None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"{name} must look like https://host/path (a scheme and a host are required)")
    if parts.username is not None or parts.password is not None:
        raise ConfigError(f"{name} must not contain a user name or password (use the token setting)")
    if parts.fragment:
        raise ConfigError(f"{name} must not contain a fragment (#)")
    if parts.scheme.lower() == "http" and not allow_http:
        raise ConfigError(f"{name} uses http://, which would send notifications in clear text. Use https://, "
                          "or set ALLOW_INSECURE_HTTP=true for a lab network you trust.")
    return url


def validate_notify_token(name: str, text: Optional[str]) -> str:
    token = (text or "").strip()
    if any(c.isspace() or ord(c) < 32 or 127 <= ord(c) <= 159 for c in token):
        raise ConfigError(f"{name} must not contain spaces or control characters")
    return token


def validate_smtp(
    env: Mapping[str, Optional[str]], allow_insecure: bool = False
) -> Tuple[Optional[SmtpSettings], List[str]]:
    """``(settings, warnings)`` for the ``NOTIFY_SMTP_*`` and ``NOTIFY_EMAIL_*`` variables in ``env``; settings
    are None when email is not configured. Transport security is not optional: the connection is STARTTLS or
    implicit TLS with the certificate and host name verified, and plain SMTP needs the same lab opt-in as an
    ``http://`` URL (``allow_insecure``, with a warning every run) and is refused whenever a password is set.
    A half-configured setup is an error, never a silent no-op, and no message repeats a value."""
    def get(name: str) -> str:
        return (env.get(name) or "").strip()

    names = ("NOTIFY_SMTP_HOST", "NOTIFY_SMTP_PORT", "NOTIFY_SMTP_SECURITY", "NOTIFY_SMTP_USER",
             "NOTIFY_SMTP_PASSWORD", "NOTIFY_EMAIL_FROM", "NOTIFY_EMAIL_TO")
    given = [name for name in names if get(name)]
    if not given:
        return None, []
    if not get("NOTIFY_SMTP_HOST"):
        raise ConfigError(f"{given[0]} is set but NOTIFY_SMTP_HOST is not: set the mail server, or remove the "
                          "email settings")
    host = _smtp_host(get("NOTIFY_SMTP_HOST"))
    if host is None:
        raise ConfigError("NOTIFY_SMTP_HOST must be a host name or an address (no scheme, port, spaces or "
                          "control characters)")

    security = get("NOTIFY_SMTP_SECURITY").lower() or "starttls"
    if security not in SMTP_SECURITY_WORDS:
        raise ConfigError(f"NOTIFY_SMTP_SECURITY must be one of {', '.join(SMTP_SECURITY_WORDS)}")
    warnings: List[str] = []
    user, password = get("NOTIFY_SMTP_USER"), get("NOTIFY_SMTP_PASSWORD")
    if security == "none":
        if password:
            raise ConfigError("NOTIFY_SMTP_SECURITY=none would send NOTIFY_SMTP_PASSWORD without encryption, which "
                              "is refused: use starttls or ssl, or remove the password")
        if not allow_insecure:
            raise ConfigError("NOTIFY_SMTP_SECURITY=none sends notifications in clear text. Use starttls or ssl, "
                              "or set ALLOW_INSECURE_HTTP=true for a lab network you trust.")
        warnings.append("NOTIFY_SMTP_SECURITY=none: notification email is sent unencrypted "
                        "(allowed by ALLOW_INSECURE_HTTP)")

    port_text = get("NOTIFY_SMTP_PORT")
    port = SMTP_DEFAULT_PORTS[security]
    if port_text:
        if not port_text.isascii() or not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
            raise ConfigError("NOTIFY_SMTP_PORT must be a port number from 1 to 65535")
        port = int(port_text)

    for name, value in (("NOTIFY_SMTP_USER", user), ("NOTIFY_SMTP_PASSWORD", password)):
        if any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
            raise ConfigError(f"{name} must not contain control characters")
    if bool(user) != bool(password):
        raise ConfigError("NOTIFY_SMTP_USER and NOTIFY_SMTP_PASSWORD go together: set both, or neither for a "
                          "server that needs no login")

    sender = get("NOTIFY_EMAIL_FROM")
    raw_to = get("NOTIFY_EMAIL_TO")
    if not sender or not raw_to:
        raise ConfigError("NOTIFY_EMAIL_FROM and NOTIFY_EMAIL_TO are required with NOTIFY_SMTP_HOST")
    if len(sender) > 254 or not _ADDRESS.fullmatch(sender):
        raise ConfigError("NOTIFY_EMAIL_FROM must be a plain address like name@example.com (no display name)")
    recipients: List[str] = []
    for number, entry in enumerate(raw_to.split(","), 1):
        entry = entry.strip()
        if len(entry) > 254 or not _ADDRESS.fullmatch(entry):
            raise ConfigError(f"NOTIFY_EMAIL_TO entry {number} must be a plain address like name@example.com "
                              "(separate several with commas; no display names)")
        if entry not in recipients:
            recipients.append(entry)
    if len(recipients) > MAX_RECIPIENTS:
        raise ConfigError(f"NOTIFY_EMAIL_TO has more than {MAX_RECIPIENTS} addresses")
    return SmtpSettings(host=host, port=port, security=security, sender=sender, recipients=tuple(recipients),
                        user=user, password=password), warnings


def parse_timeout(text: Optional[str]) -> float:
    """The per-request timeout in seconds (``UNIFI_TIMEOUT`` or ``--timeout``); blank means the default."""
    value = (text or "").strip()
    if not value:
        return DEFAULT_TIMEOUT
    try:
        seconds = float(value)
    except ValueError:
        raise ConfigError(f"UNIFI_TIMEOUT must be a number of seconds (got {text!r})") from None
    if not math.isfinite(seconds) or not MIN_TIMEOUT <= seconds <= MAX_TIMEOUT:
        raise ConfigError(f"UNIFI_TIMEOUT must be between {MIN_TIMEOUT:g} and {MAX_TIMEOUT:g} seconds (got {text!r})")
    return seconds


def parse_parallel(text: Optional[str]) -> int:
    """How many requests may be in flight at once (``UNIFI_PARALLEL_REQUESTS`` or ``--parallel``); 1 means one by
    one. Blank means the default."""
    value = (text or "").strip()
    if not value:
        return DEFAULT_PARALLEL
    try:
        number = int(value)
    except ValueError:
        raise ConfigError(
            f"UNIFI_PARALLEL_REQUESTS must be a whole number from 1 to {MAX_PARALLEL} (got {text!r})") from None
    if not 1 <= number <= MAX_PARALLEL:
        raise ConfigError(f"UNIFI_PARALLEL_REQUESTS must be between 1 and {MAX_PARALLEL} (got {text!r})")
    return number


def _whole_number(name: str, text: Optional[str], default: int, low: int, high: int) -> int:
    value = (text or "").strip()
    if not value:
        return default
    try:
        number = int(value)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number from {low} to {high} (got {text!r})") from None
    if not low <= number <= high:
        raise ConfigError(f"{name} must be between {low} and {high} (got {text!r})")
    return number


def parse_audit_log_mb(text: Optional[str]) -> int:
    """How large one audit log file may grow, in megabytes (``AUDIT_LOG_MAX_MB``); blank means the default."""
    return _whole_number("AUDIT_LOG_MAX_MB", text, DEFAULT_AUDIT_MB, 1, MAX_AUDIT_MB)


def parse_audit_log_files(text: Optional[str]) -> int:
    """How many audit log files are kept in all (``AUDIT_LOG_FILES``, at least 2: the current one and one rotated);
    blank means the default."""
    return _whole_number("AUDIT_LOG_FILES", text, DEFAULT_AUDIT_FILES, 2, MAX_AUDIT_FILES)


def parse_session_idle_minutes(text: Optional[str]) -> int:
    """How long a web login may sit unused before it ends, in minutes (``SESSION_IDLE_MINUTES``); blank: 30."""
    return _whole_number("SESSION_IDLE_MINUTES", text, DEFAULT_SESSION_IDLE_MINUTES, 1, MAX_SESSION_IDLE_MINUTES)


def parse_session_max_hours(text: Optional[str]) -> int:
    """How long a web login lasts at most, in hours (``SESSION_MAX_HOURS``); blank: 12."""
    return _whole_number("SESSION_MAX_HOURS", text, DEFAULT_SESSION_MAX_HOURS, 1, MAX_SESSION_MAX_HOURS)


def parse_log_level(text: Optional[str]) -> str:
    """``LOG_LEVEL``: one of the documented words in any case, or blank (the command's default)."""
    word = (text or "").strip().lower()
    by_word = {level.lower(): level for level in LEVEL_WORDS}
    if word and word not in by_word:
        raise ConfigError(f"LOG_LEVEL must be one of {', '.join(LEVEL_WORDS)} (got {text!r})")
    return by_word.get(word, "")


def parse_log_format(text: Optional[str]) -> str:
    """``LOG_FORMAT``: ``text`` or ``json`` in any case, or blank (the command line keeps its own format)."""
    word = (text or "").strip().lower()
    if word and word not in FORMAT_WORDS:
        raise ConfigError(f"LOG_FORMAT must be one of {', '.join(FORMAT_WORDS)} (got {text!r})")
    return word


def validate_site(text: Optional[str], name: str = "UNIFI_SITE_ID") -> str:
    """The site name, reference or UUID, safe to use in a URL. Names may contain spaces and
    non-ASCII letters; path separators, ``?``, ``#`` and control characters are rejected. ``name`` is what the
    messages call the setting (``UNIFI_SITE_ID``, or ``--site`` for the command-line option)."""
    site = (text or "").strip() or DEFAULT_SITE
    if len(site) > MAX_SITE_LENGTH:
        raise ConfigError(f"{name} is too long ({len(site)} characters, at most {MAX_SITE_LENGTH})")
    bad = sorted({
        c for c in site
        if c in _UNSAFE_SITE_CHARACTERS or ord(c) < 32 or 127 <= ord(c) <= 159
    })
    if bad:
        shown = ", ".join(repr(c) for c in bad)
        raise ConfigError(f"{name} {site!r} contains {shown}, which cannot be part of a site name")
    return site


def validate_controller_url(text: Optional[str], allow_http: bool = False) -> str:
    """The controller URL without a trailing slash. It needs a scheme and a host; ``http://``
    is refused unless ``allow_http`` (the key would be sent in clear text), and a URL carrying
    a user name or password, a query or a fragment is refused."""
    url = (text or "").strip()
    if any(c.isspace() or c == "\\" or ord(c) < 32 or 127 <= ord(c) <= 159 for c in url):
        raise ConfigError("UNIFI_URL must not contain spaces, backslashes or control characters")
    try:
        parts = urlsplit(url)
        _ = parts.port                         # raises ValueError for a bad port
    except ValueError as e:
        raise ConfigError(f"UNIFI_URL is not a valid URL ({e})") from e
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ConfigError(
            "UNIFI_URL must look like https://host[:port] (a scheme and a host are required)")
    if parts.username is not None or parts.password is not None:
        raise ConfigError("UNIFI_URL must not contain a user name or password")
    if parts.query or parts.fragment:
        raise ConfigError("UNIFI_URL must not contain a query (?) or fragment (#)")
    if parts.scheme.lower() == "http" and not allow_http:
        raise ConfigError(
            "UNIFI_URL uses http://, which would send the API key in clear text. Use "
            "https:// (set UNIFI_VERIFY_SSL=false for a self-signed certificate), or set "
            "ALLOW_INSECURE_HTTP=true for a lab network you trust.")
    return url.rstrip("/")


def env_file_warning(path: Path) -> Optional[str]:
    """A warning when ``path`` (the file with the API key) can be read by group or others,
    else None. Modes mean little on Windows, so nothing is checked there. A symlink is
    judged by the file it points to, which is the one that is read."""
    if sys.platform.startswith("win"):
        return None
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None
    if not mode & SECRET_FILE_GROUP_OTHER_BITS:
        return None
    return (f"{path} is accessible to other users (mode {mode:04o}) and holds your API key; "
            f"run: chmod 600 {shlex.quote(str(path))}")


def find_env_file(explicit: Optional[Path] = None) -> Optional[Path]:
    """The ``.env`` file to load, or None when there is none. A file named explicitly (option
    or environment variable) must exist; the default is only used if it is there."""
    named = explicit if explicit is not None else (
        Path(os.environ[ENV_FILE_VAR]) if os.environ.get(ENV_FILE_VAR, "").strip() else None)
    if named is not None:
        if not named.is_file():
            source = "--env-file" if explicit is not None else ENV_FILE_VAR
            raise ConfigError(f"env file not found: {named} (from {source})")
        return named
    default = Path.cwd() / DEFAULT_ENV_FILE
    return default if default.is_file() else None


@dataclass(frozen=True)
class EnvFileReport:
    """What is in a ``.env`` file, for ``doctor``: names and line numbers only, never a value (values are secrets).
    ``unknown`` rows are ``(line, name, suggestion)``; ``name`` is blank when the key is not a plain name (it could
    be a pasted secret), ``suggestion`` is the closest known variable or blank."""

    settings: int = 0                                                  # distinct known variables in the file
    duplicates: Dict[str, List[int]] = field(default_factory=dict)     # a known variable on several lines
    unknown: List[Tuple[int, str, str]] = field(default_factory=list)
    misplaced: List[int] = field(default_factory=list)                 # lines setting HLP_ENV, which does nothing here
    bad_lines: List[int] = field(default_factory=list)                 # lines the parser cannot read
    empty: List[str] = field(default_factory=list)                     # known variables whose value is blank
    overridden: List[str] = field(default_factory=list)                # the environment sets these differently

    @property
    def clean(self) -> bool:
        return not (self.duplicates or self.unknown or self.misplaced or self.bad_lines or self.empty
                    or self.overridden)


def _suggestion(name: str) -> str:
    lowered = {known.lower(): known for known in KNOWN_VARIABLES}       # a lower-case spelling is a typo too
    close = difflib.get_close_matches(name.lower(), list(lowered), n=1, cutoff=0.8)
    return lowered[close[0]] if close else ""


def inspect_env_file(path: Path) -> EnvFileReport:
    """Read ``path`` the way ``load_config`` will and report what would confuse a person: a variable on several lines
    (the last one is used), a name that is not a setting, a line that cannot be read, a blank value, and a variable
    the environment already sets differently (the environment wins over the file; a value that refers to another
    variable with ``$`` is not compared). Call it **before**
    ``load_config``, which copies the file into the environment. An unreadable file gives an empty report:
    ``load_config`` reports that."""
    try:
        with path.open(encoding="utf-8") as stream:
            bindings = list(parse_stream(stream))
    except (OSError, UnicodeError):
        return EnvFileReport()
    lines: Dict[str, List[int]] = {}
    last: Dict[str, Optional[str]] = {}
    unknown: List[Tuple[int, str, str]] = []
    misplaced: List[int] = []
    bad_lines: List[int] = []
    unknown_seen = set()
    for binding in bindings:
        line = binding.original.line
        if binding.error:
            bad_lines.append(line)
        elif binding.key is None:
            continue
        elif binding.key in KNOWN_VARIABLES:
            lines.setdefault(binding.key, []).append(line)
            last[binding.key] = binding.value
        elif binding.key == ENV_FILE_VAR:
            misplaced.append(line)
        elif binding.key not in unknown_seen:
            unknown_seen.add(binding.key)
            shown = binding.key if len(binding.key) <= MAX_NAME_LENGTH and _NAME.fullmatch(binding.key) else ""
            unknown.append((line, shown, _suggestion(shown) if shown else ""))
    return EnvFileReport(
        settings=len(lines),
        duplicates={name: found for name, found in lines.items() if len(found) > 1},
        unknown=unknown, misplaced=misplaced, bad_lines=bad_lines,
        empty=[name for name, value in last.items() if not (value or "").strip()],
        overridden=[name for name, value in last.items() if value is not None and "$" not in value
                    and name in os.environ and os.environ[name] != value],
    )


def load_config(env_file: Optional[Path] = None, site_override: Optional[str] = None) -> Config:
    """Load and validate configuration from the environment (and the ``.env`` file)."""
    path = find_env_file(env_file)
    warnings = []
    if path is not None:
        warning = env_file_warning(path)
        if warning:
            warnings.append(warning)
        try:
            load_dotenv(dotenv_path=path)       # does not override variables already set
        except OSError as e:
            raise ConfigError(f"cannot read env file {path}: {e.strerror or e}") from e

    controller_url = os.getenv("UNIFI_URL")
    api_key = os.getenv("UNIFI_API_KEY")

    if not controller_url:
        raise ConfigError(
            "UNIFI_URL is not set. Copy example.env to .env in the directory you run "
            "the command from (or pass --env-file) and configure it."
        )
    if not api_key or api_key == "your-api-key-here":
        raise ConfigError(
            "UNIFI_API_KEY is not set or is the placeholder value. Create one under "
            "Settings > Control Plane > Integrations and add it to .env."
        )

    allow_http = parse_bool("ALLOW_INSECURE_HTTP", os.getenv("ALLOW_INSECURE_HTTP"), False)
    url = validate_controller_url(controller_url, allow_http)
    if url.lower().startswith("http://"):
        warnings.append("UNIFI_URL uses http://: the API key is sent in clear text "
                        "(allowed by ALLOW_INSECURE_HTTP)")
    smtp, smtp_warnings = validate_smtp(os.environ, allow_http)
    warnings += smtp_warnings
    return Config(
        notify_smtp=smtp,
        notify_ntfy_url=validate_notify_url("NOTIFY_NTFY_URL", os.getenv("NOTIFY_NTFY_URL"), allow_http),
        notify_ntfy_token=validate_notify_token("NOTIFY_NTFY_TOKEN", os.getenv("NOTIFY_NTFY_TOKEN")),
        notify_webhook_url=validate_notify_url("NOTIFY_WEBHOOK_URL", os.getenv("NOTIFY_WEBHOOK_URL"), allow_http),
        notify_webhook_token=validate_notify_token("NOTIFY_WEBHOOK_TOKEN", os.getenv("NOTIFY_WEBHOOK_TOKEN")),
        controller_url=url,
        api_key=api_key,
        site=site_override if site_override is not None else validate_site(os.getenv("UNIFI_SITE_ID")),
        verify_ssl=parse_verify(os.getenv("UNIFI_VERIFY_SSL")),
        timeout=parse_timeout(os.getenv("UNIFI_TIMEOUT")),
        parallel=parse_parallel(os.getenv("UNIFI_PARALLEL_REQUESTS")),
        log_level=parse_log_level(os.getenv("LOG_LEVEL")),
        log_format=parse_log_format(os.getenv("LOG_FORMAT")),
        audit_log_mb=parse_audit_log_mb(os.getenv("AUDIT_LOG_MAX_MB")),
        audit_log_files=parse_audit_log_files(os.getenv("AUDIT_LOG_FILES")),
        session_idle_minutes=parse_session_idle_minutes(os.getenv("SESSION_IDLE_MINUTES")),
        session_max_hours=parse_session_max_hours(os.getenv("SESSION_MAX_HOURS")),
        env_file=path,
        warnings=tuple(warnings),
    )
