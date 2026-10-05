"""``run`` starts the server with uvicorn. Kept apart from ``app`` so a test can build the app without a socket."""

import logging
from pathlib import Path
from typing import Callable, Optional

import uvicorn

from .. import logs
from ..client import UniFiClient
from ..config import Config
from ..util import require_loopback
from .app import create_app
from .security import allowed_hosts

_log = logging.getLogger(__name__)


def run(config: Config, host: str, port: int, settings_path: Optional[Path], state_dir: Path,
        client_factory: Callable[[], UniFiClient], demo: bool = False) -> None:
    """Serve until interrupted. Raises ``ValueError`` for a ``host`` that is not a loopback address."""
    host = require_loopback(host)
    app = create_app(config, settings_path, state_dir, client_factory=client_factory, demo=demo,
                     hosts=allowed_hosts(host, port))
    logs.log_event(_log, logging.INFO, "server.start", f"Serving on http://{host}:{port}", host=host, port=port,
                   demo=demo)
    uvicorn.run(app, host=host, port=port, log_config=None, access_log=False, server_header=False, date_header=False)
