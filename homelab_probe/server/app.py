"""The application factory: ``create_app`` builds the FastAPI app of ``hlp serve``.

Read-only and local: it makes no request toward the controller on its own account (the document routes of the next
stage do, through ``ControllerService``), it has no route that forwards a path, and it only answers GET.
"""

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .. import __version__, logs
from ..config import Config
from .security import SecurityHeaders
from .service import ControllerService

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
               service: Optional[ControllerService] = None, demo: bool = False,
               hosts: Optional[List[str]] = None) -> FastAPI:
    """The app for ``config``. ``settings_path`` is the ``hlp.toml`` to use and ``state_dir`` the data directory (both
    used by the stages that follow), ``service`` the way to the controller (one is made from ``config`` when none is
    given: the synthetic network for a ``demo``), ``hosts`` the ``Host`` values to answer to (see
    ``security.allowed_hosts``)."""
    app = FastAPI(
        title="Homelab Probe", version=__version__, docs_url=None, redoc_url=None,    # their pages load a CDN script
        openapi_url=f"{API}/openapi.json",
        middleware=[Middleware(SecurityHeaders), Middleware(RequestLog),
                    Middleware(TrustedHostMiddleware, allowed_hosts=hosts or ["localhost", "127.0.0.1", "[::1]"])],
    )
    app.state.config, app.state.settings_path, app.state.state_dir = config, settings_path, state_dir
    app.state.service, app.state.demo = service or ControllerService(config, demo=demo), demo

    @app.get("/", include_in_schema=False)
    def root() -> Dict[str, str]:
        return {"name": "Homelab Probe", "version": __version__, "api": API,
                "note": "API only: no web app is built in yet"}

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> Dict[str, str]:
        """Is the process up? No data and no controller read."""
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz(request: Request) -> JSONResponse:
        """Can the controller be read? One read of its application info through the cache (so a probe every few
        seconds costs the controller one read per ``ttl``); the reason is a fixed word, never the error's text."""
        ready, reason = request.app.state.service.ready()
        body: Dict[str, Any] = {"ready": ready, "demo": bool(request.app.state.demo)}
        if not ready:
            body["reason"] = reason
        return JSONResponse(body, status_code=200 if ready else 503)

    @app.get(f"{API}/meta")
    def meta(request: Request) -> Dict[str, Any]:
        """What a client may know before it logs in: the version, and whether setup and login are needed."""
        return {"version": __version__, "needs_setup": False, "login_required": False,
                "demo": bool(request.app.state.demo)}

    @app.get(f"{API}/platforms")
    def platforms() -> List[Dict[str, Any]]:
        """The platforms this server can show; UniFi is the only one."""
        return [{"id": "unifi", "name": "UniFi", "configured": True}]

    return app
