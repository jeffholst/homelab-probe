"""``run`` starts the server with uvicorn. Kept apart from ``app`` so a test can build the app without a socket."""

import logging
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Optional

import uvicorn

from .. import logs
from ..accounts import AccountStore
from ..config import Config, ConfigError
from ..util import require_loopback
from .app import create_app
from .auth import AuthState
from .security import allowed_hosts

_log = logging.getLogger(__name__)
DEMO_USER = "demo"


def require_administrator(store: AccountStore, directory: Path) -> None:
    """Refuse to start without an enabled administrator: nobody could log in, and the server would be a locked door."""
    if not any(u.role == "admin" and not u.disabled for u in store.users()):
        raise ConfigError(f"there is no administrator to log in as: create one with "
                          f"`hlp web-user add NAME --role admin --data-dir {directory}`")


def run(config: Config, host: str, port: int, settings_path: Optional[Path], state_dir: Path,
        demo: bool = False, announce: Callable[[str], None] = lambda message: None) -> None:
    """Serve until interrupted. Raises ``ValueError`` for a ``host`` that is not a loopback address and ``ConfigError``
    when no administrator exists. A demo keeps its accounts in a temporary directory, with an administrator ``demo``
    and a random password that ``announce`` shows once; the directory is removed when the server stops."""
    host = require_loopback(host)
    scratch = Path(tempfile.mkdtemp(prefix="hlp-demo-")) if demo else None
    try:
        directory = scratch or state_dir
        auth = AuthState.for_directory(directory, config)
        if scratch is not None:
            password = secrets.token_urlsafe(12)
            auth.accounts.store.add(DEMO_USER, "admin", password)
            announce(f"Demo login: user {DEMO_USER}, password {password} (synthetic data; valid until you stop it)")
        require_administrator(auth.accounts.store, directory)
        app = create_app(config, settings_path, directory, auth=auth, demo=demo, hosts=allowed_hosts(host, port))
        logs.log_event(_log, logging.INFO, "server.start", f"Serving on http://{host}:{port}", host=host, port=port,
                       demo=demo)
        # No proxy headers are trusted: the address of a client is the one that connected (the throttle and the audit
        # log depend on it), not whatever a local process puts in X-Forwarded-For.
        uvicorn.run(app, host=host, port=port, log_config=None, access_log=False, server_header=False,
                    date_header=False, proxy_headers=False)
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
