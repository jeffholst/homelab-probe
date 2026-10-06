"""``run`` starts the server with uvicorn. Kept apart from ``app`` so a test can build the app without a socket."""

import logging
import os
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

import uvicorn

from .. import logs
from ..accounts import AccountError, AccountStore
from ..config import (
    DEFAULT_ENV_FILE,
    ENV_FILE_VAR,
    SETUP_TOKEN_VAR,
    Config,
    ConfigError,
    build_config,
    env_file_warning,
    find_env_file,
    layered_values,
)
from ..util import check_bind, is_loopback
from .app import create_app
from .auth import AuthState
from .security import allowed_hosts
from .wizard import MIN_TOKEN_LENGTH, MODE_ADMIN, MODE_SETUP, SetupState, administrator_exists

_log = logging.getLogger(__name__)
DEMO_USER = "demo"


def has_administrator(store: AccountStore) -> bool:
    try:
        return administrator_exists(store)
    except AccountError as error:                  # a damaged accounts file is a start-up error, not a traceback
        raise ConfigError(str(error)) from error


UNCONFIGURED_URL, UNCONFIGURED_KEY = "https://unconfigured.invalid", "unconfigured"


def setup_token() -> Optional[str]:
    """The setup token the operator chose (``HLP_SETUP_TOKEN``), or None to have one made."""
    token = os.environ.get(SETUP_TOKEN_VAR, "").strip() or None
    if token is not None and len(token) < MIN_TOKEN_LENGTH:
        raise ConfigError(f"{SETUP_TOKEN_VAR} must be at least {MIN_TOKEN_LENGTH} characters (leave it unset and the "
                          f"server makes one)")
    return token


def resolve_config(env_file: Optional[Path], directory: Path, site_override: Optional[str] = None,
                   allow_public: bool = False) -> Tuple[Config, Optional[SetupState], str]:
    """(config, setup, problem) for a server whose settings are in ``env_file`` (else ``HLP_ENV``, else the ``.env`` in
    the data directory) and the environment, read **without** changing the process environment. When they make a
    usable configuration, ``setup`` is None. When **nothing is configured** (neither ``UNIFI_URL`` nor
    ``UNIFI_API_KEY``) the server needs the guided setup: ``config`` is a stand-in with the server's own settings (the
    session limits, the audit log) and no controller, ``setup`` is a ``SetupState`` in the setup mode and ``problem``
    says what is missing (for the console of the operator). Settings that exist but are broken, and a file that was
    named and is missing, stay errors, as everywhere."""
    named = env_file if env_file is not None else (
        Path(os.environ[ENV_FILE_VAR]) if os.environ.get(ENV_FILE_VAR, "").strip() else None)
    if named is not None:
        path: Optional[Path] = find_env_file(named)
    else:
        default = directory / DEFAULT_ENV_FILE
        path = default if default.is_file() else None
    values = layered_values(path)
    warning = env_file_warning(path) if path is not None else None
    warnings = (warning,) if warning else ()
    try:
        return build_config(values, path, warnings, site_override), None, ""
    except ConfigError as error:
        if values.get("UNIFI_URL") or values.get("UNIFI_API_KEY"):
            raise                                   # configured, but not well: that is not a first run
        problem = str(error)
    standin = {**values, "UNIFI_URL": UNCONFIGURED_URL, "UNIFI_API_KEY": UNCONFIGURED_KEY}
    config = build_config(standin, path, warnings, site_override)          # another broken setting stays an error
    return config, SetupState(MODE_SETUP, "no_config", setup_token(), site=config.site, allow_public=allow_public,
                              env_named=named is not None), problem


def run(config: Config, host: str, port: int, settings_path: Optional[Path], state_dir: Path,
        demo: bool = False, announce: Callable[[str], None] = lambda message: None,
        allowed: Sequence[str] = (), forwarded_allow_ips: Optional[str] = None,
        setup: Optional[SetupState] = None, reload: Optional[Callable[[], Config]] = None,
        read_only: bool = False, scheduler: bool = False, env_named: bool = False) -> None:
    """Serve until interrupted. Raises ``ValueError`` for a ``host`` that cannot be bound (every address, with no
    ``allowed`` host) and ``ConfigError`` when no administrator exists. ``allowed`` are the extra ``Host`` names the
    server answers to; ``forwarded_allow_ips`` the proxies whose ``X-Forwarded-*`` headers are believed (none by
    default). A demo keeps its accounts in a temporary directory, with an administrator ``demo`` and a random password
    that ``announce`` shows once; the directory is removed when the server stops. With ``setup`` (from
    ``resolve_config``) the server is not configured: it starts without an administrator and without a controller,
    shows the setup token once through ``announce`` (unless the operator chose it) and answers only the guided
    setup (``wizard``)."""
    host = check_bind(host, allowed)
    scratch = Path(tempfile.mkdtemp(prefix="hlp-demo-")) if demo else None
    try:
        directory = scratch or state_dir
        auth = AuthState.for_directory(directory, config)
        if scratch is not None:
            password = secrets.token_urlsafe(12)
            auth.accounts.store.add(DEMO_USER, "admin", password)
            announce(f"Demo login: user {DEMO_USER}, password {password} (synthetic data; valid until you stop it)")
        administrator = has_administrator(auth.accounts.store)
        if setup is None and not administrator:       # settings but nobody to log in: the first administrator only
            setup = SetupState(MODE_ADMIN, "no_admin", setup_token(), site=config.site)
        if setup is not None:
            setup.reload = reload
        if setup is not None and setup.mode == MODE_ADMIN:
            how = f"with this token: {setup.token}" if setup.token_shown else f"with the token in {SETUP_TOKEN_VAR}"
            announce(f"No administrator yet: open the server in a browser and create one {how} "
                     f"(or run `hlp web-user add NAME --role admin --data-dir {directory}` and restart).")
        elif setup is not None and setup.mode:
            if administrator:
                announce("Not set up: log in as an administrator in a browser to finish the setup.")
            else:
                announce(f"Not set up: open the server in a browser to finish the setup with this token: {setup.token}"
                         if setup.token_shown else
                         f"Not set up: open the server in a browser to finish the setup; the token is the value of "
                         f"{SETUP_TOKEN_VAR}.")
        app = create_app(config, settings_path, directory, auth=auth, demo=demo,
                         hosts=allowed_hosts(host, port, allowed), setup=setup, read_only=read_only,
                         scheduler=scheduler, env_named=env_named)
        if not is_loopback(host):
            announce("This server can be reached from other machines: a login travels in clear text over plain HTTP"
                     + (", and so do the setup token and the API key you type into the setup" if setup and setup.mode
                        else "") + ". Put a reverse proxy that terminates TLS in front of it, or use an SSH tunnel "
                     "(docs/web.md).")
        logs.log_event(_log, logging.INFO, "server.start", f"Serving on http://{host}:{port}", host=host, port=port,
                       demo=demo, setup=bool(setup and setup.mode))
        # Proxy headers are believed only from the proxies named with --forwarded-allow-ips (and then the client address
        # and the scheme come from them). By default none: the address of a client is the one that connected (the
        # throttle and the audit log depend on it), not whatever a local process puts in X-Forwarded-For.
        proxies = {"proxy_headers": True, "forwarded_allow_ips": forwarded_allow_ips} if forwarded_allow_ips \
            else {"proxy_headers": False}
        if forwarded_allow_ips:
            announce(f"Believing X-Forwarded-For and X-Forwarded-Proto from: {forwarded_allow_ips}")
        uvicorn.run(app, host=host, port=port, log_config=None, access_log=False, server_header=False,
                    date_header=False, **proxies)
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
