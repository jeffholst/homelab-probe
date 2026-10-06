"""Serving the built web interface: a directory of files (``homelab_probe/web/``, a Vite build) at ``/``.

* ``/`` and every path that is not a file of the bundle, an API route or a probe answer ``index.html`` (the app does
  its own routing, so a deep link such as ``/findings/abc`` must load it), never cached.
* ``/assets/*`` are the hashed files of the build: cached for a year, ``immutable``. A missing asset is a 404, never
  ``index.html`` (a script that answers HTML is a worse error than a missing one).
* ``/api/...``, ``/healthz`` and ``/readyz`` are never answered here (``WebRoute`` does not even match them), so an
  unknown API path stays the JSON 404 it is without a bundle, and so does every method but GET and HEAD.
* Only files of the bundle can be read. The path is checked as text (no ``..`` segment, no backslash, no NUL, not
  absolute, no hidden file), and then the file is opened **one step at a time with no-follow semantics**: the bundle
  directory, then each directory and the file by name relative to the descriptor of the one before
  (``O_NOFOLLOW``), and what is served is read from the descriptor that was opened and checked with ``fstat`` (a
  regular file). There is no moment between "checked" and "opened" in which a file or directory swapped for a link
  could be followed, and a link at any step (to a file inside or outside the bundle) is refused. The bundle
  directory itself is the operator's: it is resolved once when the server starts. POSIX only (the descriptor calls
  do not exist on Windows, where the server is API only). No directory listings.
* These endpoints are public (the login page has to load), and they hold nothing but the bundle. Everything else
  stays behind ``guard``.

With no bundle (an API-only checkout) none of this is mounted and ``/`` shows the notice of ``app.root``.
``security.SecurityHeaders`` gives these answers the policy of a page (``WEB_CSP``) and keeps their cache headers.
"""

import hashlib
import os
import stat
from email.utils import formatdate
from pathlib import Path, PurePosixPath
from typing import Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from fastapi.routing import APIRoute
from starlette.routing import Match
from starlette.types import Scope

from .auth import public
from .security import WEB_FLAG

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "web"     # in the wheel: package data of ``homelab_probe``
INDEX = "index.html"
ASSETS = "assets"

IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"
RESERVED = ("/api", "/healthz", "/readyz")      # the API and the probes: never answered by the bundle

# Fixed here, not taken from the operating system's table (``mimetypes`` differs between machines, and a script served
# as text/plain is not run by a browser). A file of another type is an opaque download.
CONTENT_TYPES: Dict[str, str] = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".json": "application/json", ".map": "application/json", ".webmanifest": "application/manifest+json",
    ".txt": "text/plain; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif",
    ".ico": "image/x-icon", ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf",
}
DEFAULT_TYPE = "application/octet-stream"


def reserved(path: str) -> bool:
    """Is ``path`` the API or a probe (``/api``, ``/api/...``, ``/healthz``, ``/readyz`` and what is under them)?"""
    return any(path == name or path.startswith(name + "/") for name in RESERVED)


def find_bundle(root: Optional[Path] = None) -> Optional[Path]:
    """The real path of the bundle directory, or None when there is none (no directory, or no ``index.html`` that is
    a plain file in it: a half-built or empty directory is not a web interface)."""
    real = Path(os.path.realpath(DEFAULT_ROOT if root is None else root))
    opened = open_file(real, INDEX)
    if opened is None:
        return None
    os.close(opened[0])
    return real


def well_formed(relative: str) -> bool:
    """Is ``relative`` the path of the app rather than an attempt to leave the bundle (a backslash, NUL, a leading
    slash, a ``.`` or ``..`` segment)? Such a path is a 404, never the page: the page is for people who followed a
    link."""
    return not ("\\" in relative or "\0" in relative or relative.startswith("/")
                or any(segment in (".", "..") for segment in relative.split("/")))


_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def open_file(root: Path, relative: str) -> Optional[Tuple[int, os.stat_result]]:
    """Open the file of the bundle ``root`` (a real path) that ``relative`` names: ``(descriptor, fstat)``, or None
    (the caller closes the descriptor). None for anything that is not a regular file inside the bundle reached
    without a link: a path that is not ``well_formed``, an empty or hidden segment, a directory, a missing file, a
    symbolic link at any step. Each step is opened relative to the previous directory's descriptor with
    ``O_NOFOLLOW``, so the answer cannot be changed between checking and opening."""
    if not relative or not well_formed(relative):
        return None
    segments = relative.split("/")
    if any(not segment or segment.startswith(".") for segment in segments):
        return None
    held = []
    try:
        held.append(os.open(root, os.O_RDONLY | _DIRECTORY))
        for segment in segments[:-1]:
            held.append(os.open(segment, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=held[-1]))
        descriptor = os.open(segments[-1], os.O_RDONLY | _NOFOLLOW, dir_fd=held[-1])
    except (OSError, NotImplementedError):    # missing, a link, not a directory, a name too long, no such call here
        return None
    finally:
        for directory in held:
            os.close(directory)
    status = os.fstat(descriptor)
    if not stat.S_ISREG(status.st_mode):
        os.close(descriptor)
        return None
    return descriptor, status


def file_answer(root: Path, relative: str, head: bool = False) -> Optional[Response]:
    """The answer for one file of the bundle, read from the descriptor that was opened and checked (type from its
    suffix, cache header by where it is, length, ETag and Last-Modified from the same ``fstat``), or None. With
    ``head`` the file is not read."""
    opened = open_file(root, relative)
    if opened is None:
        return None
    descriptor, status = opened
    with os.fdopen(descriptor, "rb") as handle:
        body = b"" if head else handle.read()
    digest = hashlib.sha256(f"{status.st_mtime_ns}-{status.st_size}".encode()).hexdigest()[:32]
    headers = {"cache-control": IMMUTABLE if relative.startswith(ASSETS + "/") else REVALIDATE,
               "last-modified": formatdate(status.st_mtime, usegmt=True), "etag": f'"{digest}"'}
    if head:
        headers["content-length"] = str(status.st_size)         # a GET's length is that of what was read
    media_type = CONTENT_TYPES.get(PurePosixPath(relative).suffix.lower(), DEFAULT_TYPE)
    return Response(body, media_type=media_type, headers=headers)


def serve(request: Request, relative: str) -> Response:
    """``index.html`` for ``relative`` empty, the file when it is one, 404 under ``assets`` and for a path that is not
    ``well_formed``, ``index.html`` for the rest (a deep link of the app)."""
    root: Path = request.app.state.web
    head = request.method == "HEAD"
    answer = file_answer(root, relative or INDEX, head)
    if answer is None and well_formed(relative) and not _in_assets(relative):
        answer = file_answer(root, INDEX, head)
    if answer is None:
        raise HTTPException(status_code=404)       # the same body as a path that no route has
    request.scope[WEB_FLAG] = True
    return answer


def _in_assets(relative: str) -> bool:
    return relative == ASSETS or relative.startswith(ASSETS + "/")


class WebRoute(APIRoute):
    """A route that answers only GET and HEAD for a path that is not the API or a probe: for anything else it does
    not match, so the router behaves as if the route did not exist (404 for an unknown path, 405 only where a real
    route is)."""

    def matches(self, scope: Scope) -> Tuple[Match, Scope]:
        if scope["type"] == "http" and (scope["method"] not in ("GET", "HEAD") or reserved(scope["path"])):
            return Match.NONE, {}
        return super().matches(scope)


@public
def web_file(request: Request, relative: str) -> Response:
    """Any GET or HEAD that no other route answered: a file of the bundle, or the page of the app."""
    return serve(request, relative)


def router() -> APIRouter:
    """The route that serves the bundle: add it **after** every other route, since it matches any path."""
    routes = APIRouter(route_class=WebRoute)
    routes.add_api_route("/{relative:path}", web_file, methods=["GET", "HEAD"], include_in_schema=False,
                         response_model=None)
    return routes
