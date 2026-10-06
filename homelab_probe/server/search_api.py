"""Global search: ``GET /api/v1/unifi/sites/{site}/search?q=``.

One box over the clients, the devices, the Wi-Fi and wired networks, the findings and the subjects of notes of a site
(see ``homelab_probe.search`` for what matches and in which order). Both roles read. It reads the controller only
through the cache and only what ``search.SEARCH_NEEDS`` names (the read of the findings list plus the client history,
the Wi-Fi networks and the networks), so a request that follows a findings or clients request costs no new read of the
controller; it never writes, and notes are read from the site's own file, never another site's.

Text is returned as the controller wrote it: a name can hold markup, control or direction-changing characters, so the
caller renders it as text. A source that could not be read is named in ``unavailable`` and ``complete`` is false.
"""

from pathlib import Path
from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, Query, Request

from .. import logs
from ..client import UniFiAPIError, UniFiClient
from ..config import ConfigError
from ..diagnose import apply_ignores, diagnose
from ..documents import Document, site_identity
from ..history import DEFAULT_DIR, site_dir
from ..notes import NotesStore
from ..search import DEFAULT_LIMIT, KINDS, MAX_LIMIT, MAX_QUERY, MIN_QUERY, SEARCH_NEEDS, build_search, parse_query
from ..settings import DiagnoseSettings
from ..sitefile import StoreError
from ..snapshot import collect_snapshot
from .errors import ApiError, error_responses, from_controller
from .routes import SiteP, checked_site
from .settings_api import load_effective, settings_path_of

UNIFI = "/api/v1/unifi"
QueryQ = Annotated[str, Query(min_length=MIN_QUERY, max_length=MAX_QUERY,
                              description=f"What to look for: {MIN_QUERY} to {MAX_QUERY} characters, a name, a MAC "
                                          "address or an IP address (any spelling), a finding code or message")]
LimitQ = Annotated[int, Query(ge=1, le=MAX_LIMIT, description="The most hits of each kind")]
WORDING = {404: "No such site", 422: "The search text or the limit is not valid",
           500: "The settings file cannot be used"}
HIT_SCHEMA: Dict[str, Any] = {
    "type": "object", "required": ["kind", "key", "label", "matched"],
    "properties": {"kind": {"type": "string", "enum": list(KINDS)}, "key": {"type": "string"},
                   "label": {"type": "string"}, "matched": {"type": "array", "items": {"type": "string"}}}}
SEARCH_SCHEMA: Dict[str, Any] = {
    "type": "object", "required": ["site", "query", "limit", "items", "kinds", "truncated", "complete", "unavailable",
                                   "limitations", "generated_at", "warnings"],
    "properties": {
        "site": {"type": "object", "required": ["id", "name"],
                 "properties": {"id": {"type": "string"}, "name": {"type": "string"}}},
        "query": {"type": "string"}, "limit": {"type": "integer"},
        "items": {"type": "array", "items": HIT_SCHEMA},
        "kinds": {"type": "object", "properties": {kind: {"type": "object", "required": ["total", "shown"],
                                                          "properties": {"total": {"type": "integer"},
                                                                         "shown": {"type": "integer"}}}
                                                   for kind in KINDS}},
        "truncated": {"type": "boolean"}, "complete": {"type": "boolean"},
        "unavailable": {"type": "array", "items": {"type": "string", "enum": list(KINDS)}},
        "limitations": {"type": "array", "items": {"type": "string"}},
        "generated_at": {"type": "string"}, "warnings": {"type": "array", "items": {"type": "string"}}}}


def read(client: UniFiClient, site: str, settings: DiagnoseSettings) -> Document:
    """The snapshot of ``site`` and its findings after the ignore list (the ones the findings list shows), in the
    ``meta`` of a document, so the service stamps it and collects its warnings like any other."""
    with logs.collect_warnings(quiet=True) as warnings:
        snap = collect_snapshot(client, site, SEARCH_NEEDS)
        findings, _ = apply_ignores(diagnose(snap, settings), settings.ignore)
    return Document("search", None, [logs.scrub(w) for w in warnings], {"snapshot": snap, "findings": findings})


def subjects_of(request: Request, site: Dict[str, str]) -> Optional[List[Dict[str, Any]]]:
    """The subjects that have notes in ``site``'s own file, or ``None`` when the file cannot be used (damaged, a
    symbolic link, another site's): the search then says the notes were not searched instead of failing."""
    base = Path(request.app.state.state_dir or ".") / DEFAULT_DIR
    try:
        return NotesStore(site_dir(base, site), site["id"]).subjects()
    except StoreError:
        return None


def router() -> APIRouter:
    api = APIRouter(prefix=f"{UNIFI}/sites/{{site}}")

    @api.get("/search", summary="Search the clients, devices, networks, findings and notes of a site",
             responses={200: {"description": "The hits, at most `limit` of each kind", "content": {
                 "application/json": {"schema": SEARCH_SCHEMA}}},
                        **error_responses(401, 404, 422, 500, 502, 504, text=WORDING)})
    def search(request: Request, site: SiteP, q: QueryQ, limit: LimitQ = DEFAULT_LIMIT) -> Dict[str, Any]:
        name = checked_site(site)
        try:
            query = parse_query(q)
        except ValueError as error:
            raise ApiError(422, "invalid_parameter", str(error)) from None
        try:
            settings = load_effective(settings_path_of(request.app))
        except ConfigError:
            logs.warn("the settings file could not be used")
            raise ApiError(500, "settings_invalid",
                           "The settings file could not be read; see the server log.") from None
        try:
            built = request.app.state.service.build(lambda client: read(client, name, settings))
        except UniFiAPIError as error:
            raise from_controller(error) from error
        meta = built.document.meta
        identity = site_identity(meta["snapshot"].site)
        found = build_search(meta["snapshot"], meta["findings"], query, limit, subjects_of(request, identity))
        return {"site": {"id": identity["id"], "name": identity["name"]}, **found,
                "generated_at": built.generated_at, "warnings": built.warnings}

    return api
