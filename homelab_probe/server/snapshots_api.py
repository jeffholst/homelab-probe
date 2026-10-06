"""Saved snapshots over the API: ``/api/v1/unifi/sites/{site}/snapshots`` (list, save) and ``.../diff``.

The files are the ones ``snapshot`` and ``diff`` use: ``snapshots/<site id>/snapshot-*.json`` in the data directory
(plus the older ones saved straight into ``snapshots/`` that name the site). Listing and comparing only read; saving is
the one write, an administrator's, refused by ``serve --read-only``, audited, and made by the same code as the command.

A snapshot is named by its bare file name (``snapshot-20261001-011530Z.json``): a name with a directory part, or any
other file, is never opened, so no path a caller sends reaches the disk.
"""

import threading
from pathlib import Path
from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..client import UniFiAPIError
from ..config import ConfigError
from ..documents import Document, diff_document, snapshot_document
from ..history import (
    DEFAULT_DIR,
    find_snapshot,
    load_snapshot,
    prune,
    save_snapshot,
    site_dir,
    site_snapshots,
    snapshot_summary,
)
from ..setup import SetupError, ensure_private_dir
from . import apischema
from .auth import admin, audit_event, local_write
from .errors import ApiError, error_responses, from_controller
from .routes import MAX_TEXT, REFRESH_MIN_INTERVAL, RefreshQ, SiteP, checked_site, respond

UNIFI = "/api/v1/unifi"
WORDING = {404: "No such site or snapshot", 500: "The saved snapshots cannot be used"}
MAX_KEEP = 10_000
_SAVE_LOCK = threading.Lock()         # one save and prune at a time: two requests cannot prune each other's file
NameQ = Annotated[Optional[str], Query(max_length=MAX_TEXT, description="A snapshot file name, as the list gives it")]
LimitQ = Annotated[int, Query(ge=1, le=200, description="How many of the newest to list")]
SUMMARY_SCHEMA = {
    "type": "object", "required": ["name", "readable"],
    "properties": {"name": {"type": "string"}, "readable": {"type": "boolean"}, "captured_at": {"type": "string"},
                   "devices": {"type": "integer"}, "clients": {"type": "integer"}, "reservations": {"type": "integer"}},
}
LIST_SCHEMA = {
    "type": "object", "required": ["site", "total", "items"],
    "properties": {"site": {"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"}}},
                   "total": {"type": "integer"}, "items": {"type": "array", "items": SUMMARY_SCHEMA}},
}
SAVED_SCHEMA = {
    "type": "object", "required": ["snapshot", "removed", "generated_at", "warnings"],
    "properties": {"snapshot": SUMMARY_SCHEMA, "removed": {"type": "array", "items": {"type": "string"}},
                   "generated_at": apischema.GENERATED_AT, "warnings": apischema.WARNINGS},
}


def snapshot_base(request: Request) -> Path:
    return Path(request.app.state.state_dir or ".") / DEFAULT_DIR


def site_of(request: Request, name: str) -> Dict[str, Any]:
    """The controller's record of the site ``name`` (an id, reference or name), through the cache."""
    try:
        built = request.app.state.service.build(lambda client: Document("site", client.resolve_site(name)))
    except UniFiAPIError as error:
        raise from_controller(error) from error
    site: Dict[str, Any] = built.document.data
    return site


def _unreadable(error: Exception) -> ApiError:
    return ApiError(500, "snapshots_unreadable", "The saved snapshots cannot be read; see the server log.")


def _unsafe() -> ApiError:
    return ApiError(500, "snapshots_unsafe", "The snapshots directory is a symbolic link, which is left alone: use a "
                    "real directory in the data directory.")


def refuse_links(*directories: Path) -> None:
    """The snapshots directory and the site's are never followed through a symbolic link: a link could lead a logged-in
    request to read or write outside the data directory (the setup refuses the same)."""
    if any(directory.is_symlink() for directory in directories):
        raise _unsafe()


def plain(files: List[Path]) -> List[Path]:
    """``files`` without the symbolic links among them: a link in the directory is not a snapshot of this server."""
    return [path for path in files if not path.is_symlink()]


class SaveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keep: Optional[int] = Field(default=None, ge=1, le=MAX_KEEP, description="Afterwards keep only the newest N")


def router() -> APIRouter:
    api = APIRouter(prefix=f"{UNIFI}/sites/{{site}}")

    def listed(request: Request, site: str) -> tuple[Dict[str, Any], List[Path]]:
        record = site_of(request, checked_site(site))
        base = snapshot_base(request)
        refuse_links(base, site_dir(base, record))
        try:
            return record, plain(site_snapshots(base, record))
        except ConfigError as error:
            raise _unreadable(error) from error

    @api.get("/snapshots", summary="The saved snapshots of a site, newest first",
             responses={200: {"description": "The newest snapshots", "content": {"application/json": {
                 "schema": LIST_SCHEMA}}}, **error_responses(404, 422, 500, 502, 504, text=WORDING)})
    def snapshots_list(request: Request, site: SiteP, limit: LimitQ = 50) -> Dict[str, Any]:
        record, files = listed(request, site)
        return {"site": {"id": str(record.get("id") or ""), "name": str(record.get("name") or "")},
                "total": len(files), "items": [snapshot_summary(path) for path in reversed(files[-limit:])]}

    @api.post("/snapshots", dependencies=[Depends(admin)], status_code=201,
              summary="Save a snapshot of the network now (a local write)",
              responses={201: {"description": "The snapshot that was saved", "content": {"application/json": {
                  "schema": SAVED_SCHEMA}}}, **error_responses(404, 422, 500, 502, 504, text=WORDING)})
    @local_write
    def snapshots_save(request: Request, site: SiteP, body: SaveBody) -> JSONResponse:
        name = checked_site(site)
        service = request.app.state.service
        service.refresh(REFRESH_MIN_INTERVAL)                 # a snapshot should be of now, not of the cache
        try:
            built = service.build(lambda client: snapshot_document(client, name, echo=False))
        except UniFiAPIError as error:
            raise from_controller(error) from error
        record = built.document.data
        base = snapshot_base(request)
        directory = site_dir(base, record["site"])
        with _SAVE_LOCK:
            refuse_links(base, directory)
            try:
                ensure_private_dir("snapshots", base)                  # owner-only, as the setup makes it
                ensure_private_dir("snapshots.site", directory)
                path = save_snapshot(record, None, directory)
                gone = prune(directory, body.keep, protect=path, files=site_snapshots(base, record["site"])) \
                    if body.keep else []
            except (OSError, ConfigError, SetupError) as error:
                raise ApiError(500, "snapshot_not_written", "The snapshot could not be written; see the server log.") \
                    from error
        summary = snapshot_summary(path)
        audit_event(request, "snapshot.saved", request.state.session.username, name=path.name,
                    devices=summary["devices"], clients=summary["clients"], removed=len(gone))
        return JSONResponse({"snapshot": summary, "removed": [p.name for p in gone],
                             "generated_at": built.generated_at, "warnings": built.warnings}, status_code=201)

    @api.get("/diff", summary="What changed between two saved snapshots, or one and the network now",
             responses={200: {"description": "The comparison with when it was read", "content": {
                 "application/json": {"schema": apischema.response_schema("diff", {
                     "old": {"type": "string"}, "new": {"type": ["string", "null"]}})}}},
                 **error_responses(404, 422, 500, 502, 504, text=WORDING)})
    def snapshots_diff(request: Request, site: SiteP, old: NameQ = None, new: NameQ = None,
                       refresh: RefreshQ = False) -> JSONResponse:
        record, files = listed(request, site)
        base = snapshot_base(request)

        def pick(name: Optional[str]) -> Path:
            found = find_snapshot(base, record, name) if name else (files[-1] if files else None)
            if found is None or found.is_symlink():
                raise ApiError(404, "snapshot_not_found", "No such saved snapshot for this site.")
            return found

        old_path = pick(old)
        new_path = pick(new) if new else None
        try:
            old_record = load_snapshot(old_path)
            new_record = load_snapshot(new_path) if new_path else None
        except ConfigError as error:
            raise _unreadable(error) from error
        return respond(request, lambda client: diff_document(client, checked_site(site), old_record, new_record,
                                                             echo=False),
                       refresh=refresh, notes=lambda document: {"old": old_path.name,
                                                                 "new": new_path.name if new_path else None})

    return api
