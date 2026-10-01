"""Configuration from environment variables and an optional ``.env`` file.

The ``.env`` file is found like this, first match wins:

1. the file given with ``--env-file``;
2. the file named by the ``UNIFI_SENTINEL_ENV`` environment variable;
3. ``.env`` in the **current working directory**.

Parent directories are not searched, and neither is the installed package's directory,
so an installed copy behaves like a checkout and an unrelated project's ``.env`` is never
picked up. Real environment variables always win over values in the file.

The API key is a credential, so two things are checked: the ``.env`` file that was read
should not be readable by other users (a warning, like ``ssh`` gives, not an error), and the
controller URL must be ``https://`` unless ``ALLOW_INSECURE_HTTP`` opts in, because the key
travels in a header of every request.
"""

import os
import shlex
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlsplit

from dotenv import load_dotenv

ENV_FILE_VAR = "UNIFI_SENTINEL_ENV"
DEFAULT_ENV_FILE = ".env"
DEFAULT_SITE = "default"
MAX_SITE_LENGTH = 128
TRUE_WORDS = ("true", "yes", "1", "on")
FALSE_WORDS = ("false", "no", "0", "off")
_UNSAFE_SITE_CHARACTERS = "/\\?#"
SECRET_FILE_GROUP_OTHER_BITS = 0o077   # any of these set means someone besides the owner can read


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    controller_url: str
    api_key: str = field(repr=False)        # never shown, even by repr() in a traceback or log
    site: str = DEFAULT_SITE
    verify_ssl: bool = True
    warnings: Tuple[str, ...] = field(default=(), compare=False)   # for cli.main to print


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


def validate_site(text: Optional[str]) -> str:
    """The site name, reference or UUID, safe to use in a URL. Names may contain spaces and
    non-ASCII letters; path separators, ``?``, ``#`` and control characters are rejected."""
    site = (text or "").strip() or DEFAULT_SITE
    if len(site) > MAX_SITE_LENGTH:
        raise ConfigError(f"SITE_ID is too long ({len(site)} characters, at most {MAX_SITE_LENGTH})")
    bad = sorted({
        c for c in site
        if c in _UNSAFE_SITE_CHARACTERS or ord(c) < 32 or 127 <= ord(c) <= 159
    })
    if bad:
        shown = ", ".join(repr(c) for c in bad)
        raise ConfigError(f"SITE_ID {site!r} contains {shown}, which cannot be part of a site name")
    return site


def validate_controller_url(text: Optional[str], allow_http: bool = False) -> str:
    """The controller URL without a trailing slash. It needs a scheme and a host; ``http://``
    is refused unless ``allow_http`` (the key would be sent in clear text), and a URL carrying
    a user name or password, a query or a fragment is refused."""
    url = (text or "").strip()
    if any(c.isspace() or c == "\\" or ord(c) < 32 or 127 <= ord(c) <= 159 for c in url):
        raise ConfigError("CONTROLLER_URL must not contain spaces, backslashes or control characters")
    try:
        parts = urlsplit(url)
        _ = parts.port                         # raises ValueError for a bad port
    except ValueError as e:
        raise ConfigError(f"CONTROLLER_URL is not a valid URL ({e})") from e
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ConfigError(
            "CONTROLLER_URL must look like https://host[:port] (a scheme and a host are required)")
    if parts.username is not None or parts.password is not None:
        raise ConfigError("CONTROLLER_URL must not contain a user name or password")
    if parts.query or parts.fragment:
        raise ConfigError("CONTROLLER_URL must not contain a query (?) or fragment (#)")
    if parts.scheme.lower() == "http" and not allow_http:
        raise ConfigError(
            "CONTROLLER_URL uses http://, which would send the API key in clear text. Use "
            "https:// (set VERIFY_SSL=false for a self-signed certificate), or set "
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


def load_config(env_file: Optional[Path] = None) -> Config:
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

    controller_url = os.getenv("CONTROLLER_URL")
    api_key = os.getenv("API_KEY")

    if not controller_url:
        raise ConfigError(
            "CONTROLLER_URL is not set. Copy example.env to .env in the directory you run "
            "the command from (or pass --env-file) and configure it."
        )
    if not api_key or api_key == "your-api-key-here":
        raise ConfigError(
            "API_KEY is not set or is the placeholder value. Create one under "
            "Settings > Control Plane > Integrations and add it to .env."
        )

    allow_http = parse_bool("ALLOW_INSECURE_HTTP", os.getenv("ALLOW_INSECURE_HTTP"), False)
    url = validate_controller_url(controller_url, allow_http)
    if url.lower().startswith("http://"):
        warnings.append("CONTROLLER_URL uses http://: the API key is sent in clear text "
                        "(allowed by ALLOW_INSECURE_HTTP)")
    return Config(
        controller_url=url,
        api_key=api_key,
        site=validate_site(os.getenv("SITE_ID")),
        verify_ssl=parse_bool("VERIFY_SSL", os.getenv("VERIFY_SSL")),
        warnings=tuple(warnings),
    )
