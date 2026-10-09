"""What keeps the server from being reached the wrong way (the login itself is in ``auth``).

* It binds a loopback address unless told otherwise (``--host``), and a bind to every address needs at least one
  ``--allowed-host`` (``util.check_bind``).
* A ``Host`` header that is not one of the names it is reached by is refused (``TrustedHostMiddleware``; the list is
  never a wildcard): a page on another site cannot reach it through DNS rebinding.
* There is no CORS middleware, so a browser never lets another origin read an answer.
* Every response, whatever its status, carries a strict content-security policy and the headers below
  (``SecurityHeaders``); the API has no HTML, so the policy forbids everything. The one exception is the web
  interface itself (``static``): its answers carry ``WEB_CSP``, the policy of a page that loads its own scripts,
  styles, images and fonts and talks only to this server, and the cache header they set (hashed files are cached, the
  page is not). Nothing else can ask for that: a handler marks its answer through the request scope, which only
  ``static.serve`` does.
"""

from typing import List, Sequence, Tuple

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..util import bind_host_name, is_wildcard_bind, parse_allowed_host

CSP = ("default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
       "img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'")
# The policy of the web interface (a Vite build: external scripts and styles, no inline code, no eval, no third-party
# host). `style-src 'self'` has no 'unsafe-inline': a style attribute in the HTML would be blocked, but a script that
# sets `element.style` (as React does) is not. Forms may only post to this server; nothing may frame it, embed an
# object or change the base URL.
WEB_CSP = ("default-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'; "
           "script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; "
           "manifest-src 'self'")
WEB_FLAG = "homelab_probe.web"      # set on the request scope by an answer of the web interface (``static.serve``)
WEB_STYLE_NONCE = "homelab_probe.web_style_nonce"  # generated only for an opt-in HTML document
SECURITY_HEADERS: List[Tuple[bytes, bytes]] = [
    (b"content-security-policy", CSP.encode("ascii")),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"x-frame-options", b"DENY"),
    (b"cache-control", b"no-store"),
]


def _forms(host: str) -> List[str]:
    """The ways a ``Host`` header can spell ``host`` (an IPv6 address with and without its brackets)."""
    bare = host.strip("[]")
    return [f"[{bare}]", bare] if ":" in bare else [host]


def allowed_hosts(host: str, port: int, extra: Sequence[str] = ()) -> List[str]:
    """The ``Host`` header values the server answers to: the loopback names, its own bind address (unless that is
    "every address") and the names given with ``--allowed-host``. Never a wildcard. (Starlette compares the host part
    and ignores the port.) Raises ``ValueError`` for a name that is a wildcard or not a host at all."""
    names = ["localhost", "127.0.0.1", "[::1]", "::1"]
    wanted = ([] if is_wildcard_bind(host) else [bind_host_name(host)]) + [parse_allowed_host(name) for name in extra]
    for name in wanted:                # checked here, not only by the command: a caller that skips argparse is safe too
        for form in _forms(name):
            if form not in names:
                names.append(form)
    return names


class SecurityHeaders:
    """Adds ``SECURITY_HEADERS`` to every HTTP response (also an error one) and removes the server banner."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def wrapped(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                web = bool(scope.get(WEB_FLAG))       # the page of the web interface: its own policy and cache header
                policy = WEB_CSP
                nonce = scope.get(WEB_STYLE_NONCE)
                if web and nonce:
                    policy += f"; style-src-elem 'self' 'nonce-{nonce}'; style-src-attr 'none'"
                defaults = [(name, policy.encode("ascii") if web and name == b"content-security-policy" else value)
                            for name, value in SECURITY_HEADERS if not (web and name == b"cache-control")]
                names = {name for name, _ in defaults} | {b"server"}
                kept = [(k, v) for k, v in message.get("headers", []) if k.lower() not in names]
                message = {**message, "headers": kept + defaults}
            await send(message)

        try:
            await self.app(scope, receive, wrapped)
        except Exception:
            if not response_started:
                body = b"Internal Server Error"
                await wrapped({"type": "http.response.start", "status": 500,
                               "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                           (b"content-length", str(len(body)).encode("ascii"))]})
                await wrapped({"type": "http.response.body", "body": body})
            raise
