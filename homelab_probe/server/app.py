"""The application factory: ``create_app`` builds the FastAPI app of ``hlp serve``.

Read-only and local: it makes no request toward the controller on its own account (the document routes of the next
stage do, through ``ControllerService``), it has no route that forwards a path, and it only answers GET.
"""

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .. import __version__, logs
from ..config import Config
from ..util import is_loopback
from . import routes, settings_api, snapshots_api, users_api, wizard
from .auth import AuthState, OriginGuard, guard, public, public_router, session_router
from .errors import ApiError, api_error_handler, request_validation_error_handler
from .security import SecurityHeaders
from .service import ControllerService
from .wizard import SetupState

API = "/api/v1"
_log = logging.getLogger(__name__)


class RequestLog:
    """One INFO record per request, with a request id (also sent back as ``X-Request-ID``; an id a client sends is
    ignored, so nobody can write into the log). The record has the method, the route *template* (``/clients/{id}``,
    never the path that was asked for, which can hold a MAC address), the status and the milliseconds."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id, started, status = logs.new_id(), time.monotonic(), 500

        async def wrapped(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message = {**message, "headers": [*message.get("headers", []), (b"x-request-id", request_id.encode())]}
            await send(message)

        with logs.bind(request_id=request_id):
            try:
                await self.app(scope, receive, wrapped)
            finally:
                route = getattr(scope.get("route"), "path", "") or "(no route)"
                logs.log_event(_log, logging.INFO, "server.request", f"{scope['method']} {route} -> {status}",
                               method=scope["method"], route=route, status=status,
                               duration_ms=round((time.monotonic() - started) * 1000))


def create_app(config: Config, settings_path: Optional[Path] = None, state_dir: Optional[Path] = None, *,
               service: Optional[ControllerService] = None, auth: Optional[AuthState] = None, demo: bool = False,
               hosts: Optional[List[str]] = None, setup: Optional[SetupState] = None,
               read_only: bool = False) -> FastAPI:
    """The app for ``config``. ``settings_path`` is the ``hlp.toml`` to use and ``state_dir`` the data directory (the
    accounts file and the audit log are there), ``service`` the way to the controller (one is made from ``config``
    when none is given: the synthetic network for a ``demo``), ``auth`` the accounts, sessions and throttle (made from
    ``state_dir`` when none is given), ``hosts`` the ``Host`` values to answer to (see ``security.allowed_hosts``).

    Everything answers only to a logged-in user except ``/``, ``/healthz``, ``/readyz``, ``/api/v1/meta`` and the
    login itself; ``tests/test_server_auth.py`` lists the routes and fails on one that is neither. With ``setup`` (a
    ``SetupState`` with a mode) the server is not set up: no service is made, and only those public routes and the
    setup routes (``wizard``) answer."""
    app = FastAPI(
        title="Homelab Probe", version=__version__, docs_url=None, redoc_url=None,    # their pages load a CDN script
        openapi_url=None,                      # served below, behind the login
        dependencies=[Depends(guard)],         # every route needs a login unless it is marked public
        middleware=[Middleware(SecurityHeaders), Middleware(RequestLog),
                    Middleware(TrustedHostMiddleware, allowed_hosts=hosts or ["localhost", "127.0.0.1", "[::1]"]),
                    Middleware(OriginGuard)],
    )
    app.state.config, app.state.settings_path, app.state.state_dir = config, settings_path, state_dir
    app.state.read_only = read_only
    app.state.setup = setup
    unconfigured = setup is not None and bool(setup.mode)         # a server in a setup mode reads no controller yet
    app.state.service = None if unconfigured else service or ControllerService(config, demo=demo)
    app.state.demo = demo
    app.state.auth = auth or AuthState.for_directory(state_dir or Path("."), config)
    app.add_exception_handler(ApiError, api_error_handler)   # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    routes.install(app)
    app.include_router(wizard.router())
    app.include_router(settings_api.router())
    app.include_router(snapshots_api.router())
    app.include_router(users_api.router())
    app.include_router(public_router())
    app.include_router(session_router())

    @app.get("/", include_in_schema=False)
    @public
    def root() -> Dict[str, str]:
        return {"name": "Homelab Probe", "version": __version__, "api": API,
                "note": "API only: no web app is built in yet"}

    @app.get("/healthz", include_in_schema=False)
    @public
    def healthz() -> Dict[str, str]:
        """Is the process up? No data and no controller read."""
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    @public
    def readyz(request: Request) -> JSONResponse:
        """Can the controller be read? One read of its application info through the cache (so a probe every few
        seconds costs the controller one read per ``ttl``). Public, so it says only yes or no; the reason is in the
        server log."""
        if request.app.state.setup is not None and request.app.state.setup.mode:
            return JSONResponse({"ready": False}, status_code=503)     # not set up: there is no controller to read
        ready, reason = request.app.state.service.ready()
        if not ready:
            logs.warn(f"not ready: the controller could not be read ({reason})")
        return JSONResponse({"ready": ready}, status_code=200 if ready else 503)

    @app.get(f"{API}/meta")
    @public
    def meta(request: Request) -> Dict[str, Any]:
        """What a client may know before it logs in: the version, and whether setup and login are needed."""
        mode = getattr(request.app.state.setup, "mode", None)
        return {"version": __version__, "needs_setup": bool(mode), "setup_mode": mode, "login_required": True,
                "demo": bool(request.app.state.demo), "read_only": bool(request.app.state.read_only),
                "https": request.url.scheme == "https",
                "loopback": is_loopback(request.url.hostname or "")}

    @app.get(f"{API}/platforms")
    def platforms() -> List[Dict[str, Any]]:
        """The platforms this server can show; UniFi is the only one."""
        return [{"id": "unifi", "name": "UniFi", "configured": True}]

    @app.get(f"{API}/openapi.json", include_in_schema=False)
    def openapi(request: Request) -> JSONResponse:
        """The API description (FastAPI's own route is off: it would be public)."""
        return JSONResponse(request.app.openapi())

    return app
