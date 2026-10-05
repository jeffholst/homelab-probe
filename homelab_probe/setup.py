"""Guided setup: what ``hlp init`` and the web wizard share.

Both are front ends of this module, so a person who sets up from the terminal and one who sets up from the browser end
up with the same files, written the same way:

* ``validate_values`` checks what was typed with the validators every command uses, one setting at a time, and
  returns the problems (never a secret);
* ``render_env`` merges the settings into an existing ``.env`` and keeps everything else in it, comments and settings
  it does not manage included;
* ``write_private_file`` writes a file readable by its owner only, atomically, and keeps a ``.bak`` of what it replaced;
* ``apply`` does all of it for a directory: ``.env``, a commented ``hlp.toml`` (only if there is none), the private
  ``snapshots/`` directory and, if asked, the first administrator, and returns what it did as ``StepResult``s.

Nothing here contacts the controller, prints a secret or puts one in a message.
"""

import io
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from dotenv import dotenv_values
from dotenv.parser import parse_stream

from .accounts import AccountError, AccountStore
from .config import (
    KNOWN_VARIABLES,
    ConfigError,
    build_config,
    parse_audit_log_files,
    parse_audit_log_mb,
    parse_bool,
    parse_log_format,
    parse_log_level,
    parse_parallel,
    parse_session_idle_minutes,
    parse_session_max_hours,
    parse_timeout,
    parse_verify,
    validate_controller_url,
    validate_notify_token,
    validate_notify_url,
    validate_site,
    validate_smtp,
)
from .util import printable

ENV_FILE = ".env"
SETTINGS_FILE = "hlp.toml"
SNAPSHOT_DIR = "snapshots"
PLACEHOLDER_KEY = "your-api-key-here"
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_./:@%+=,-]+$")        # written bare; anything else is single-quoted
SETTINGS_STUB = """# Homelab Probe settings. Every value is optional; the defaults apply. Read from the current
# directory, or pass --config FILE. See docs/diagnose.md for the thresholds and the ignore list. For example:
#
# [thresholds]
# wifi_weak_signal_dbm = -75
#
# [[ignore]]
# code = "device.offline"
# subject = "Spare *"
# reason = "kept in a drawer"
"""


class SetupError(Exception):
    """A setup step cannot be done; the message says why and never holds a secret."""


@dataclass(frozen=True)
class StepResult:
    """What one step of ``apply`` did: ``created``, ``updated``, ``kept`` (already as wanted) or ``skipped``."""

    id: str
    status: str
    message: str
    path: Optional[Path] = None


# -- the .env file ---------------------------------------------------------------------------------------------

def format_value(name: str, value: str) -> str:
    """``value`` as it is written after ``NAME=``: bare when that is safe, else single-quoted (which python-dotenv reads
    back unchanged). A line break or ``${...}`` (which python-dotenv would expand) cannot be written."""
    if not value:
        raise SetupError(f"{name} is empty; leave it out instead")
    if any(c in value for c in "\n\r\0"):
        raise SetupError(f"{name} contains a line break or a NUL character, which a .env file cannot hold")
    if "${" in value:
        raise SetupError(f"{name} contains ${{...}}, which would be expanded when the file is read")
    if _SAFE_VALUE.fullmatch(value):
        return value
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def render_env(values: Mapping[str, Optional[str]], existing: str = "") -> str:
    """``existing`` (the text of a ``.env``) with ``values`` merged in. A setting that is already there is replaced
    where it stands (its other occurrences, which would be ignored anyway, are dropped); one that is not is added at
    the end; a value of ``None`` removes the setting. Comments, blank lines and every setting that is not in
    ``values`` stay."""
    unknown = sorted(set(values) - set(KNOWN_VARIABLES))
    if unknown:
        raise SetupError(f"{', '.join(unknown)} {'is not a setting' if len(unknown) == 1 else 'are not settings'} "
                         "of this tool")
    lines = {name: f"{name}={format_value(name, value)}\n" for name, value in values.items() if value is not None}
    written: set = set()
    out: List[str] = []
    for binding in parse_stream(io.StringIO(existing)):
        original = binding.original.string
        if binding.key is None or binding.error or binding.key not in values:
            out.append(original)                                  # untouched, even a last line with no newline
        elif binding.key in lines and binding.key not in written:
            out.append(lines[binding.key])
            written.add(binding.key)
        # else: a repeat of a setting that was replaced, or one that is being removed
    missing = [name for name in lines if name not in written]
    if missing:
        if out:
            blank = not out[-1].strip()
            if not out[-1].endswith("\n"):
                out.append("\n")                                  # only now is a newline added to an unterminated line
            if not blank:
                out.append("\n")
        out.append("# Added by hlp init\n")
        out.extend(lines[name] for name in missing)
    return "".join(out)


# -- writing files ---------------------------------------------------------------------------------------------

def _refuse_symlink(path: Path) -> None:
    if path.is_symlink():
        raise SetupError(f"{path} is a symbolic link; it is left alone (write the real file, or remove the link)")


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")      # created 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def write_private_file(step: str, path: Path, text: str, backup: bool = True) -> StepResult:
    """Write ``text`` to ``path`` readable by its owner only, in one step (a crash never leaves half a file). An
    existing file with other content is first copied to ``<name>.bak``, also owner-only; one that already holds
    ``text`` is left alone."""
    _refuse_symlink(path)
    existing: Optional[str] = None
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as e:
            raise SetupError(f"cannot read {path} to keep a copy of it ({type(e).__name__})") from e
        if existing == text:
            os.chmod(path, 0o600)
            return StepResult(step, "kept", f"{path} is already as wanted", path)
        if backup:
            copy = path.with_name(path.name + ".bak")
            _refuse_symlink(copy)
            _write_atomically(copy, existing)
    _write_atomically(path, text)
    if existing is None:
        return StepResult(step, "created", f"created {path} (readable by you only)", path)
    kept = f"; the old one is {path.name}.bak" if backup else ""
    return StepResult(step, "updated", f"updated {path}{kept}", path)


def ensure_private_dir(step: str, path: Path) -> StepResult:
    """Make the directory ``path`` (owner-only) if there is none; one that exists is made owner-only too."""
    _refuse_symlink(path)
    if path.is_dir():
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            try:
                os.chmod(path, 0o700)
            except OSError as e:
                raise SetupError(f"{path} is readable by others and could not be made private "
                                 f"({e.strerror or type(e).__name__})") from e
            return StepResult(step, "updated", f"{path} was readable by others: now readable by you only", path)
        return StepResult(step, "kept", f"{path} exists", path)
    path.mkdir(parents=True, mode=0o700)
    return StepResult(step, "created", f"created {path} (readable by you only)", path)


def write_settings_stub(path: Path) -> StepResult:
    """A commented ``hlp.toml``, but only if there is none: it may hold the user's ignore list."""
    _refuse_symlink(path)
    if path.exists():
        return StepResult("setup.settings", "kept", f"{path} exists and is left as it is", path)
    _write_atomically(path, SETTINGS_STUB)
    return StepResult("setup.settings", "created", f"created {path} (every value is optional)", path)


# -- checking what was typed -----------------------------------------------------------------------------------

def _need(name: str, check: Callable[[Optional[str]], object]) -> Callable[[Mapping[str, Optional[str]]], None]:
    def run(values: Mapping[str, Optional[str]]) -> None:
        check(values.get(name))

    return run


def _url(values: Mapping[str, Optional[str]]) -> None:
    if not values.get("UNIFI_URL"):
        raise ConfigError("UNIFI_URL is not set")
    allow_http = parse_bool("ALLOW_INSECURE_HTTP", values.get("ALLOW_INSECURE_HTTP"), False)
    validate_controller_url(values.get("UNIFI_URL"), allow_http)


def _key(values: Mapping[str, Optional[str]]) -> None:
    key = values.get("UNIFI_API_KEY")
    if not key or key == PLACEHOLDER_KEY:
        raise ConfigError("UNIFI_API_KEY is not set or is the placeholder value")


def _notify(values: Mapping[str, Optional[str]]) -> None:
    allow_http = parse_bool("ALLOW_INSECURE_HTTP", values.get("ALLOW_INSECURE_HTTP"), False)
    for kind in ("NTFY", "WEBHOOK"):
        validate_notify_url(f"NOTIFY_{kind}_URL", values.get(f"NOTIFY_{kind}_URL"), allow_http)
        validate_notify_token(f"NOTIFY_{kind}_TOKEN", values.get(f"NOTIFY_{kind}_TOKEN"))
    validate_smtp(values, allow_http)


CHECKS: List[Tuple[str, Callable[[Mapping[str, Optional[str]]], None]]] = [
    ("UNIFI_URL", _url), ("UNIFI_API_KEY", _key),
    ("UNIFI_SITE_ID", _need("UNIFI_SITE_ID", validate_site)),
    ("UNIFI_VERIFY_SSL", _need("UNIFI_VERIFY_SSL", parse_verify)),
    ("UNIFI_TIMEOUT", _need("UNIFI_TIMEOUT", parse_timeout)),
    ("UNIFI_PARALLEL_REQUESTS", _need("UNIFI_PARALLEL_REQUESTS", parse_parallel)),
    ("ALLOW_INSECURE_HTTP", _need("ALLOW_INSECURE_HTTP", lambda text: parse_bool("ALLOW_INSECURE_HTTP", text, False))),
    ("LOG_LEVEL", _need("LOG_LEVEL", parse_log_level)), ("LOG_FORMAT", _need("LOG_FORMAT", parse_log_format)),
    ("AUDIT_LOG_MAX_MB", _need("AUDIT_LOG_MAX_MB", parse_audit_log_mb)),
    ("AUDIT_LOG_FILES", _need("AUDIT_LOG_FILES", parse_audit_log_files)),
    ("SESSION_IDLE_MINUTES", _need("SESSION_IDLE_MINUTES", parse_session_idle_minutes)),
    ("SESSION_MAX_HOURS", _need("SESSION_MAX_HOURS", parse_session_max_hours)),
    ("NOTIFY_*", _notify),
]


def validate_field(values: Mapping[str, Optional[str]], name: str) -> Optional[str]:
    """The problem with the setting ``name`` in ``values`` (scrubbed of the API key), or None when it is good."""
    key = values.get("UNIFI_API_KEY") or ""
    for field, check in CHECKS:
        if field == name:
            try:
                check(values)
            except ConfigError as error:
                message = printable(str(error))
                if key and key in message:
                    # A long key is replaced and the sentence stays readable; a short one would garble it.
                    return (message.replace(key, "***") if len(key) >= 4
                            else f"{name} is not acceptable (the message would have repeated the API key)")
                return message
    return None


def validate_values(values: Mapping[str, Optional[str]]) -> List[Tuple[str, str]]:
    """The problems in ``values`` as ``(setting, message)``, empty when they are good. Every setting is checked on its
    own, so a form can mark each field; the messages are the ones the commands give, and are scrubbed of the API key."""
    problems = [(name, message) for name, _ in CHECKS if (message := validate_field(values, name))]
    if not problems:
        try:
            build_config(values)                                  # what is left: the settings together
        except ConfigError as error:
            problems.append(("", printable(str(error))))
    return problems


# -- doing it --------------------------------------------------------------------------------------------------

def read_existing_env(path: Path) -> str:
    """The text of the ``.env`` at ``path`` ("" when there is none). A symbolic link is refused **before** anything is
    read, so a link cannot make the setup import settings from another file."""
    _refuse_symlink(path)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as e:
        raise SetupError(f"cannot read {path} ({type(e).__name__})") from e


def existing_values(text: str) -> Dict[str, str]:
    """The settings of this tool in the text of a ``.env``."""
    return {k: v for k, v in dotenv_values(stream=io.StringIO(text)).items() if v is not None and k in KNOWN_VARIABLES}


def apply(values: Mapping[str, Optional[str]], directory: Path,
          admin: Optional[Tuple[str, str]] = None) -> List[StepResult]:
    """Check ``values`` and then set ``directory`` up: ``.env`` (merged into the one that is there, which is kept as
    ``.env.bak``), ``hlp.toml`` if there is none, the private ``snapshots/`` directory and, with ``admin`` (a username
    and a password), the first administrator. Raises ``SetupError`` before writing anything if the values are not
    good."""
    directory = Path(directory)
    env_path = directory / ENV_FILE
    existing = read_existing_env(env_path)
    # What the file will hold: the settings already in it that are not being replaced, and the new ones. That is what
    # every command will load, so that is what is checked: a bad value that was already there would fail them too.
    merged = existing_values(existing)
    for name, value in values.items():
        if value is None:
            merged.pop(name, None)
        else:
            merged[name] = value
    problems = validate_values(merged)
    if problems:
        old = " (already in the existing .env)"
        raise SetupError("; ".join(f"{name}: {message}{'' if name in values else old}" if name else message
                                   for name, message in problems))
    text = render_env(values, existing)                              # may raise SetupError: still nothing written
    steps = [write_private_file("setup.env", env_path, text),
             write_settings_stub(directory / SETTINGS_FILE),
             ensure_private_dir("setup.snapshots", directory / SNAPSHOT_DIR)]
    if admin is not None:
        try:
            user = AccountStore(directory).add(admin[0], "admin", admin[1])
        except AccountError as error:
            raise SetupError(str(error)) from error
        steps.append(StepResult("setup.admin", "created", f"created the administrator {user.username}"))
    return steps
