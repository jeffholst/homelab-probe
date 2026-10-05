"""What keeps the server from being reached the wrong way, until login exists.

* It binds only a loopback address (``require_loopback``): nobody else on the network can connect.
* A ``Host`` header that is not the server's own address is refused (``TrustedHostMiddleware``): a page on another
  site cannot reach it through DNS rebinding.
* There is no CORS middleware, so a browser never lets another origin read an answer.
* Every response, whatever its status, carries a strict content-security policy and the headers below
  (``SecurityHeaders``); the API has no HTML, so the policy forbids everything.
"""

from typing import List, Tuple

from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSP = ("default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
       "img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'")
SECURITY_HEADERS: List[Tuple[bytes, bytes]] = [
    (b"content-security-policy", CSP.encode("ascii")),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"x-frame-options", b"DENY"),
    (b"cache-control", b"no-store"),
]


def allowed_hosts(host: str, port: int) -> List[str]:
    """The ``Host`` header values the server answers to: its own bind address and the loopback names. (Starlette
    compares the host part and ignores the port.)"""
    names = ["localhost", "127.0.0.1", "[::1]", "::1"]
    if host not in names and host != "0.0.0.0":
        names.append(host if ":" not in host else f"[{host}]")
    return names


class SecurityHeaders:
    """Adds ``SECURITY_HEADERS`` to every HTTP response (also an error one) and removes the server banner."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                names = {name for name, _ in SECURITY_HEADERS} | {b"server"}
                kept = [(k, v) for k, v in message.get("headers", []) if k.lower() not in names]
                message = {**message, "headers": kept + SECURITY_HEADERS}
            await send(message)

        await self.app(scope, receive, wrapped)
