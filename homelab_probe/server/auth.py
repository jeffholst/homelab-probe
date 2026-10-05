"""Login for the web server: the cookie, CSRF, roles that default to "no", and the three auth routes.

* ``OriginGuard`` (a middleware, so no route can forget it) refuses every unsafe request (anything but GET, HEAD or
  OPTIONS) whose ``Origin`` header is missing or is not this server's own ``Host``, and any body that is not JSON.
  A browser always sends the origin of a page that posts; a script that does not is meant to use the command line.
* ``require(role)`` is a dependency: a valid session of at least that role, and for an unsafe request the session's
  CSRF token in ``X-CSRF-Token``. Every route that is not public has one (a test enumerates the routes).
* ``/api/v1/auth/login`` checks the password through ``LocalAccounts`` (the same work for a wrong password, an unknown
  user and a disabled one), slows guessing down (``LoginThrottle``), writes the audit log first (a login that cannot be
  recorded does not happen) and sets the cookie.

The cookie is ``HttpOnly``, ``SameSite=Strict``, ``Path=/`` and has no ``Domain`` and no expiry (it ends with the
browser session; the server decides the real end). Over HTTPS it is also ``Secure`` and takes the ``__Host-`` prefix,
which makes a browser refuse to let a subdomain or a plain-HTTP page overwrite it.
"""

import logging
import math
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from .. import logs
from ..accounts import AccountError, AccountStore, AuditLog, LocalAccounts
from ..config import Config
from .errors import ApiError
from .sessions import Session, SessionStore
from .throttle import LoginThrottle

_log = logging.getLogger(__name__)
API = "/api/v1"
SESSION_COOKIE = "hlp_session"
SECURE_SESSION_COOKIE = "__Host-hlp_session"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
ROLE_RANK = {"viewer": 1, "admin": 2}
UNKNOWN_USER = "(unknown user)"


@dataclass
class AuthState:
    """Everything the login needs, kept on ``app.state.auth``."""

    accounts: LocalAccounts
    sessions: SessionStore
    throttle: LoginThrottle
    audit: AuditLog

    @classmethod
    def for_directory(cls, directory: Path, config: Config) -> "AuthState":
        """The accounts file and the audit log of ``directory``, with the session limits of ``config``."""
        audit = AuditLog(directory, config.audit_log_mb, config.audit_log_files)
        return cls(LocalAccounts(AccountStore(directory), audit),
                   SessionStore(config.session_idle_minutes * 60, config.session_max_hours * 3600),
                   LoginThrottle(), audit)


# -- CSRF: the Origin of every unsafe request --------------------------------------------------------------------

def same_origin(origin: str, host: str) -> bool:
    """True when the ``Origin`` header names the same host and port as the ``Host`` header."""
    parts = urlsplit(origin)
    return parts.scheme in ("http", "https") and bool(parts.netloc) and parts.netloc.lower() == host.strip().lower()


class OriginGuard:
    """Refuses an unsafe request whose Origin is not ours, and one whose body is not JSON, before any route sees it."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            refusal: Optional[ApiError] = None
            if not same_origin(headers.get("origin", ""), headers.get("host", "")):
                refusal = ApiError(403, "csrf_origin", "The request does not come from this site.")
            elif (headers.get("content-length", "0") != "0" or "transfer-encoding" in headers) \
                    and headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                refusal = ApiError(415, "unsupported_media_type", "The body must be JSON.")
            if refusal is not None:
                body = {"error": refusal.code, "message": refusal.message}
                await JSONResponse(body, status_code=refusal.status)(scope, receive, send)
                return
        await self.app(scope, receive, send)


# -- who is asking -----------------------------------------------------------------------------------------------

def cookie_name(request: Request) -> str:
    return SECURE_SESSION_COOKIE if request.url.scheme == "https" else SESSION_COOKIE


def address_of(request: Request) -> str:
    return request.client.host if request.client else "unknown"


PUBLIC_ENDPOINTS: Set[str] = set()       # the endpoints that answer without a session (see ``public``)


def public(endpoint: Callable[..., Any]) -> Callable[..., Any]:
    """Mark an endpoint as answering without a session. Apply it **under** the route decorator. Nothing is public
    unless it is marked, and ``tests/test_server_auth.py`` pins the list: a route cannot become public by accident."""
    endpoint.is_public = True        # type: ignore[attr-defined]
    PUBLIC_ENDPOINTS.add(endpoint.__name__)
    return endpoint


class Require:
    """A dependency: a live session of at least ``role``, and for an unsafe request its CSRF token. Without a session
    the answer is 401, with too small a role 403; the session is on ``request.state.session`` afterwards.

    The one instance in ``guard`` is the **default for every route** (it is the dependency of the whole app), and
    lets through the endpoints marked ``public``: a route nobody remembered to declare is a route that needs a
    login. ``admin`` adds the stricter requirement to the routes that need it."""

    def __init__(self, role: str, *, allow_public: bool = False) -> None:
        self.role, self._rank, self._allow_public = role, ROLE_RANK[role], allow_public

    def __call__(self, request: Request) -> Optional[Session]:
        route = request.scope.get("route")
        if self._allow_public and getattr(getattr(route, "endpoint", None), "is_public", False):
            return None
        auth: AuthState = request.app.state.auth
        session = auth.sessions.lookup(request.cookies.get(cookie_name(request)), auth.accounts.store)
        if session is None:
            raise ApiError(401, "not_logged_in", "Log in first.")
        if ROLE_RANK[session.role] < self._rank:
            raise ApiError(403, "forbidden", "Your role may not do this.")
        if request.method not in SAFE_METHODS and not secrets.compare_digest(
                request.headers.get("x-csrf-token", "").encode("utf-8"), session.csrf.encode("utf-8")):
            raise ApiError(403, "csrf_token", "The CSRF token is missing or wrong.")
        request.state.session = session
        return session


guard = Require("viewer", allow_public=True)
admin = Require("admin")


# -- the routes --------------------------------------------------------------------------------------------------

class LoginBody(BaseModel):
    username: str = Field(max_length=4096)
    password: str = Field(max_length=4096)


def _audit(request: Request, event: str, username: str, *, must: bool = False, **fields: Any) -> None:
    """Write an audit entry. A failure to write is fatal for a login (``must``) and only a warning otherwise, so that a
    full disk cannot be turned into a way to stop people from being refused."""
    try:
        request.app.state.auth.audit.write(event, username, address=address_of(request), **fields)
    except AccountError as error:
        if must:
            raise ApiError(500, "audit_unavailable", "The audit log cannot be written, so no login is possible.") \
                from error
        logs.warn(f"the audit log could not be written ({event})")


def _me(auth: AuthState, session: Session) -> Dict[str, Any]:
    idle, absolute = auth.sessions.remaining(session)
    return {"username": session.username, "role": session.role, "csrf_token": session.csrf,
            "idle_seconds_left": idle, "session_seconds_left": absolute}


def public_router() -> APIRouter:
    router = APIRouter(prefix=f"{API}/auth")

    @router.post("/login", summary="Log in")
    @public
    def login(request: Request, body: LoginBody) -> JSONResponse:
        auth: AuthState = request.app.state.auth
        address = address_of(request)
        wait = auth.throttle.wait(address, body.username)
        if wait > 0:
            seconds = math.ceil(wait)
            raise ApiError(429, "too_many_attempts", f"Too many attempts. Try again in {seconds} seconds.",
                           headers={"Retry-After": str(seconds)}, retry_after=seconds)
        user = auth.accounts.authenticate_user(body.username, body.password)
        if user is None:
            known = auth.accounts.store.get(body.username)
            auth.throttle.failed(address, body.username)
            _audit(request, "auth.login_failed", known.username if known else UNKNOWN_USER)
            if auth.throttle.wait(address, body.username) > 0:
                _audit(request, "auth.throttled", known.username if known else UNKNOWN_USER)
            raise ApiError(401, "invalid_credentials", "Invalid username or password.")
        _audit(request, "auth.login", user.username, must=True, role=user.role)
        auth.throttle.succeeded(address, body.username)
        previous = auth.sessions.lookup(request.cookies.get(cookie_name(request)), auth.accounts.store)
        if previous is not None:
            auth.sessions.end(previous)
        value, session = auth.sessions.create(user, address)
        response = JSONResponse(_me(auth, session))
        secure = request.url.scheme == "https"
        response.set_cookie(cookie_name(request), value, httponly=True, samesite="strict", secure=secure, path="/")
        return response

    return router


def session_router() -> APIRouter:
    router = APIRouter(prefix=f"{API}/auth")

    @router.post("/logout", summary="Log out")
    def logout(request: Request) -> JSONResponse:
        auth: AuthState = request.app.state.auth
        session: Session = request.state.session
        auth.sessions.end(session)
        _audit(request, "auth.logout", session.username)
        response = JSONResponse({"status": "ok"})
        response.delete_cookie(cookie_name(request), httponly=True, samesite="strict",
                               secure=request.url.scheme == "https", path="/")
        return response

    @router.get("/me", summary="Who is logged in")
    def me(request: Request) -> Dict[str, Any]:
        return _me(request.app.state.auth, request.state.session)

    return router
