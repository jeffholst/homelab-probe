"""``run`` starts the server with uvicorn. Kept apart from ``app`` so a test can build the app without a socket."""

import logging
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Optional, Sequence

import uvicorn

from .. import logs
from ..accounts import AccountError, AccountStore
from ..config import Config, ConfigError
from ..util import check_bind, is_loopback
from .app import create_app
from .auth import AuthState
from .security import allowed_hosts

_log = logging.getLogger(__name__)
DEMO_USER = "demo"


def require_administrator(store: AccountStore, directory: Path) -> None:
    """Refuse to start without an enabled administrator: nobody could log in, and the server would be a locked door."""
    try:
        users = store.users()
    except AccountError as error:                  # a damaged accounts file is a start-up error, not a traceback
        raise ConfigError(str(error)) from error
    if not any(u.role == "admin" and not u.disabled for u in users):
        raise ConfigError(f"there is no administrator to log in as: create one with "
                          f"`hlp web-user add NAME --role admin --data-dir {directory}`")


def run(config: Config, host: str, port: int, settings_path: Optional[Path], state_dir: Path,
        demo: bool = False, announce: Callable[[str], None] = lambda message: None,
        allowed: Sequence[str] = (), forwarded_allow_ips: Optional[str] = None) -> None:
    """Serve until interrupted. Raises ``ValueError`` for a ``host`` that cannot be bound (every address, with no
    ``allowed`` host) and ``ConfigError`` when no administrator exists. ``allowed`` are the extra ``Host`` names the
    server answers to; ``forwarded_allow_ips`` the proxies whose ``X-Forwarded-*`` headers are believed (none by
    default). A demo keeps its accounts in a temporary directory, with an administrator ``demo`` and a random password
    that ``announce`` shows once; the directory is removed when the server stops."""
    host = check_bind(host, allowed)
    scratch = Path(tempfile.mkdtemp(prefix="hlp-demo-")) if demo else None
    try:
        directory = scratch or state_dir
        auth = AuthState.for_directory(directory, config)
        if scratch is not None:
            password = secrets.token_urlsafe(12)
            auth.accounts.store.add(DEMO_USER, "admin", password)
            announce(f"Demo login: user {DEMO_USER}, password {password} (synthetic data; valid until you stop it)")
        require_administrator(auth.accounts.store, directory)
        app = create_app(config, settings_path, directory, auth=auth, demo=demo,
                         hosts=allowed_hosts(host, port, allowed))
        if not is_loopback(host):
            announce("This server can be reached from other machines: a login travels in clear text over plain HTTP. "
                     "Put a reverse proxy that terminates TLS in front of it, or use an SSH tunnel (docs/web.md).")
        logs.log_event(_log, logging.INFO, "server.start", f"Serving on http://{host}:{port}", host=host, port=port,
                       demo=demo)
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
