"""Notes on findings, devices and clients: ``/api/v1/unifi/sites/{site}/notes``.

Both roles read; only an administrator adds, edits or deletes (CSRF, refused by ``--read-only``, audited by note id and
the kind of subject, never the text or the MAC). A note belongs to a **subject** with a stable identity (``device:`` or
``client:`` and a MAC in any spelling, or ``finding:`` and the id from the findings list), kept in
``snapshots/<site id>/notes.json`` so it follows renames and cannot cross sites. See ``homelab_probe.notes``. Notes are
plain text, are never sent to the controller, and no subject has to exist now: a note outlives an offline device.
"""

import time
from pathlib import Path
from typing import Annotated, Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..history import DEFAULT_DIR, site_dir
from ..notes import MAX_TEXT, NotesStore
from ..sitefile import StoreError
from ..triage import iso
from .auth import admin, audit_event, local_write
from .errors import ApiError, error_responses
from .routes import SiteP, checked_site
from .snapshots_api import refuse_links, site_of

UNIFI = "/api/v1/unifi"
CLOCK = time.time
NoteP = Annotated[str, PathParam(pattern=r"^[0-9a-f]{16}$", description="The id of a note")]
SubjectQ = Annotated[Optional[str], Query(max_length=64, description="device:MAC, client:MAC or finding:ID")]
WORDING = {404: "No such site or note", 409: "Too many notes", 500: "The local files cannot be used"}
NOTE_SCHEMA: Dict[str, Any] = {
    "type": "object", "required": ["id", "subject", "text", "author", "created_at", "modified_at", "modified_by"],
    "properties": {name: {"type": "string"} for name in ("id", "subject", "text", "author", "created_at",
                                                          "modified_at", "modified_by")}}
LIST_SCHEMA: Dict[str, Any] = {"type": "object", "required": ["items", "total"],
                               "properties": {"items": {"type": "array", "items": NOTE_SCHEMA},
                                              "total": {"type": "integer"}}}


class NewNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(max_length=64)
    text: str = Field(max_length=MAX_TEXT * 2)          # the real limit is applied to the cleaned text


class EditedNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=MAX_TEXT * 2)


def store_for(request: Request, site: str) -> NotesStore:
    record = site_of(request, checked_site(site))
    base = Path(request.app.state.state_dir or ".") / DEFAULT_DIR
    refuse_links(base, site_dir(base, record))
    return NotesStore(site_dir(base, record), str(record.get("id") or ""))


def failure(error: StoreError) -> ApiError:
    """The API error for a note that cannot be kept (the messages are fixed text of ``notes`` and ``sitefile``)."""
    status = {"invalid": 422, "full": 409, "not_found": 404, "locked": 503}.get(error.code, 500)
    return ApiError(status, f"notes_{error.code}", str(error))


def shown(note: Dict[str, Any]) -> Dict[str, Any]:
    return {**note, "created_at": iso(note["created_at"]), "modified_at": iso(note["modified_at"])}


def router() -> APIRouter:
    api = APIRouter(prefix=f"{UNIFI}/sites/{{site}}/notes")

    @api.get("", summary="The notes of a site, or of one subject, oldest first",
             responses={200: {"description": "The notes", "content": {"application/json": {"schema": LIST_SCHEMA}}},
                        **error_responses(401, 404, 422, 500, 502, 504, text=WORDING)})
    def notes_list(request: Request, site: SiteP, subject: SubjectQ = None) -> Dict[str, Any]:
        try:
            found = store_for(request, site).listing(subject)
        except StoreError as error:
            raise failure(error) from error
        return {"items": [shown(note) for note in found], "total": len(found)}

    @api.post("", dependencies=[Depends(admin)], status_code=201, summary="Add a note",
              responses={201: {"description": "The note", "content": {"application/json": {"schema": NOTE_SCHEMA}}},
                         **error_responses(401, 403, 404, 409, 422, 500, 502, 503, 504, text=WORDING)})
    @local_write
    def notes_add(request: Request, site: SiteP, body: NewNote) -> JSONResponse:
        user = request.state.session.username
        try:
            note = store_for(request, site).add(body.subject, body.text, user, CLOCK())
        except StoreError as error:
            raise failure(error) from error
        audit_event(request, "note.added", user, note=note["id"], subject_kind=note["subject"].partition(":")[0])
        return JSONResponse(shown(note), status_code=201)

    @api.patch("/{note}", dependencies=[Depends(admin)], summary="Change the text of a note",
               responses={200: {"description": "The note", "content": {"application/json": {"schema": NOTE_SCHEMA}}},
                          **error_responses(401, 403, 404, 422, 500, 502, 503, 504, text=WORDING)})
    @local_write
    def notes_edit(request: Request, site: SiteP, note: NoteP, body: EditedNote) -> Dict[str, Any]:
        user = request.state.session.username
        try:
            changed = store_for(request, site).edit(note, body.text, user, CLOCK())
        except StoreError as error:
            raise failure(error) from error
        audit_event(request, "note.edited", user, note=note, subject_kind=changed["subject"].partition(":")[0])
        return shown(changed)

    @api.delete("/{note}", dependencies=[Depends(admin)], summary="Delete a note",
                responses={200: {"description": "The note that was deleted", "content": {
                    "application/json": {"schema": NOTE_SCHEMA}}},
                    **error_responses(401, 403, 404, 500, 502, 503, 504, text=WORDING)})
    @local_write
    def notes_delete(request: Request, site: SiteP, note: NoteP) -> Dict[str, Any]:
        user = request.state.session.username
        try:
            gone = store_for(request, site).remove(note)
        except StoreError as error:
            raise failure(error) from error
        audit_event(request, "note.deleted", user, note=note, subject_kind=gone["subject"].partition(":")[0])
        return shown(gone)

    return api
