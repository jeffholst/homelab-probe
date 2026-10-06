"""Notes on findings, devices and clients: ``/api/v1/unifi/sites/{site}/notes``.

Both roles read; only an administrator adds, edits or deletes (CSRF, refused by ``--read-only``, audited by note id and
the kind of subject, never the text or the MAC). A note belongs to a **subject** with a stable identity (``device:`` or
``client:`` and a MAC in any spelling, or ``finding:`` and the id from the findings list), kept in
``snapshots/<site id>/notes.json`` so it follows renames and cannot cross sites. See ``homelab_probe.notes``. Notes are
plain text, are never sent to the controller, and no subject has to exist now: a note outlives an offline device.

* **Concurrent edits.** Every note has a ``revision``; an edit sends the one it read, and when the note was changed
  since, the answer is ``409 notes_conflict`` and nothing is overwritten (reload, then edit again).
* **Retained subjects.** ``GET .../notes/subjects`` lists every subject that has notes (``?q=`` searches the reference
  and the last-known name) with the **last-known** name the notes were written under, when it was recorded and how many
  notes there are. It does not depend on the inventory, so a device that is gone and a finding that cleared are listed
  all the same; whether a subject is in the inventory now is the inventory's to say, never concluded here. Reading
  notes does not need the controller when the site's directory is already here (a failed controller read hides nothing).
"""

import time
from pathlib import Path
from typing import Annotated, Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..client import UniFiAPIError
from ..history import DEFAULT_DIR, site_dir
from ..notes import MAX_NAME, MAX_TEXT, NotesStore
from ..sitefile import StoreError
from ..triage import iso
from ..util import site_key
from .auth import admin, audit_event, local_write
from .errors import ApiError, error_responses, from_controller
from .routes import SiteP, checked_site
from .snapshots_api import refuse_links, site_of

UNIFI = "/api/v1/unifi"
CLOCK = time.time
NoteP = Annotated[str, PathParam(pattern=r"^[0-9a-f]{16}$", description="The id of a note")]
SubjectQ = Annotated[Optional[str], Query(max_length=64, description="device:MAC, client:MAC or finding:ID")]
WORDING = {404: "No such site or note", 409: "Too many notes, or the note was changed since it was read",
           500: "The local files cannot be used"}
RevisionB = Annotated[str, Field(pattern=r"^[0-9a-f]{16}$", description="The revision of the note that was read")]
NOTE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["id", "subject", "text", "author", "created_at", "modified_at", "modified_by", "revision"],
    "properties": {**{name: {"type": "string"} for name in ("id", "subject", "text", "author", "created_at",
                                                              "modified_at", "modified_by", "revision")},
                   "context": {"type": ["object", "null"]}}}
SUBJECT_SCHEMA: Dict[str, Any] = {
    "type": "object", "required": ["subject", "kind", "note_count", "last_note_at", "last_known"],
    "properties": {"subject": {"type": "string"}, "kind": {"type": "string"}, "note_count": {"type": "integer"},
                   "last_note_at": {"type": "string"}, "last_known": {"type": ["object", "null"]}}}
LIST_SCHEMA: Dict[str, Any] = {"type": "object", "required": ["items", "total"],
                               "properties": {"items": {"type": "array", "items": NOTE_SCHEMA},
                                              "total": {"type": "integer"}}}


class NewNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(max_length=64)
    text: str = Field(max_length=MAX_TEXT, description=f"Plain text, at most {MAX_TEXT} characters")
    name: Optional[str] = Field(default=None, max_length=MAX_NAME, description="What the subject is called now")


class EditedNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=MAX_TEXT, description=f"Plain text, at most {MAX_TEXT} characters")
    revision: RevisionB
    name: Optional[str] = Field(default=None, max_length=MAX_NAME, description="What the subject is called now")


def store_for(request: Request, site: str) -> NotesStore:
    try:
        record = site_of(request, checked_site(site))
    except UniFiAPIError as error:
        raise from_controller(error) from error
    base = Path(request.app.state.state_dir or ".") / DEFAULT_DIR
    refuse_links(base, site_dir(base, record))
    return NotesStore(site_dir(base, record), str(record.get("id") or ""))


def failure(error: StoreError) -> ApiError:
    """The API error for a note that cannot be kept (the messages are fixed text of ``notes`` and ``sitefile``)."""
    status = {"invalid": 422, "full": 409, "conflict": 409, "not_found": 404, "locked": 503}.get(error.code, 500)
    return ApiError(status, f"notes_{error.code}", str(error))


def local_store(request: Request, site: str) -> Optional[NotesStore]:
    """The notes of ``site`` read from the data directory alone, when ``site`` is the id of a site whose notes are here
    already (so a controller that cannot be read hides nothing); ``None`` otherwise."""
    base = Path(request.app.state.state_dir or ".") / DEFAULT_DIR
    directory = base / site_key(site)
    if directory.is_symlink() or not (directory / "notes.json").is_file():
        return None
    store = NotesStore(directory, site)
    try:
        store.load()
    except StoreError:
        return None                                                # another site's file, or damaged: the full path says
    return store


def shown(note: Dict[str, Any]) -> Dict[str, Any]:
    out = {**note, "created_at": iso(note["created_at"]), "modified_at": iso(note["modified_at"])}
    if out.get("context"):
        out["context"] = {"name": out["context"]["name"], "recorded_at": iso(out["context"]["at"])}
    return out


def router() -> APIRouter:
    api = APIRouter(prefix=f"{UNIFI}/sites/{{site}}/notes")

    @api.get("", summary="The notes of a site, or of one subject, oldest first",
             responses={200: {"description": "The notes", "content": {"application/json": {"schema": LIST_SCHEMA}}},
                        **error_responses(401, 404, 422, 500, 502, 504, text=WORDING)})
    def notes_list(request: Request, site: SiteP, subject: SubjectQ = None) -> Dict[str, Any]:
        try:
            found = (local_store(request, checked_site(site)) or store_for(request, site)).listing(subject)
        except StoreError as error:
            raise failure(error) from error
        return {"items": [shown(note) for note in found], "total": len(found)}

    @api.get("/subjects", summary="Every subject that has notes, present or not, with its last-known name",
             responses={200: {"description": "The subjects, newest note first", "content": {"application/json": {
                 "schema": {"type": "object", "required": ["items", "total"],
                            "properties": {"items": {"type": "array", "items": SUBJECT_SCHEMA},
                                           "total": {"type": "integer"}}}}}},
                        **error_responses(401, 404, 422, 500, 502, 504, text=WORDING)})
    def notes_subjects(request: Request, site: SiteP,
                       q: Annotated[str, Query(max_length=64, description="Part of a reference or a name")] = "",
                       ) -> Dict[str, Any]:
        try:
            found = (local_store(request, checked_site(site)) or store_for(request, site)).subjects(q)
        except StoreError as error:
            raise failure(error) from error
        items = [{**item, "last_note_at": iso(item["last_note_at"]),
                  "last_known": None if item["last_known"] is None else {
                      "name": item["last_known"]["name"], "recorded_at": iso(item["last_known"]["recorded_at"])}}
                 for item in found]
        return {"items": items, "total": len(items)}

    @api.post("", dependencies=[Depends(admin)], status_code=201, summary="Add a note",
              responses={201: {"description": "The note", "content": {"application/json": {"schema": NOTE_SCHEMA}}},
                         **error_responses(401, 403, 404, 409, 422, 500, 502, 503, 504, text=WORDING)})
    @local_write
    def notes_add(request: Request, site: SiteP, body: NewNote) -> JSONResponse:
        user = request.state.session.username
        try:
            note = store_for(request, site).add(body.subject, body.text, user, CLOCK(), body.name or "")
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
            changed = store_for(request, site).edit(note, body.text, user, CLOCK(), body.revision, body.name)
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
