"""Application backup over the API (issue #226): ``POST /api/v1/backup`` (export) and ``POST /api/v1/backup/preview``.

An encrypted file of this application's own state (see ``homelab_probe.backup``): **not** a UniFi controller backup,
and it cannot restore anything on the controller. Administrators only, with the CSRF token; both are POSTs because the
passphrase travels in the body, never in a URL.

* **Export** reads the data directory and changes nothing there (it takes the same locks the writers take, for a
  moment, so a file is never read half-way), so it also works with ``--read-only``. The passphrase is typed twice and
  must be at least ``MIN_PASSPHRASE`` characters; it encrypts the archive (scrypt and AES-256-GCM) and is then
  forgotten: it is not stored, logged or put in an error. **Without it the backup cannot be opened**, and it has to be
  kept apart from the installation: a backup that can only be opened with a key that is on the lost machine would be
  worth nothing.
* **Preview** opens an uploaded backup (``archive`` is its base64) and says what a restore would do, with no secret in
  the answer (see ``backup.preview``). It changes nothing. A wrong passphrase, a modified file and anything that is not
  a backup are refused with a fixed message each. Restoring is the next stage.
* **Audit.** ``backup.exported`` (the optional categories, the number of files and the size) and ``backup.previewed``
  (the date of the backup and its version): never the passphrase, a name or the content.
* At most ``SLOTS`` of these run at once (the key derivation takes memory), and a further one is ``503 backup_busy``.
"""

import base64
import binascii
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .. import backup, restore
from ..backup import BackupError
from .auth import admin, audit_event, setup_ok
from .backup_crypto import MAX_PASSPHRASE, MIN_PASSPHRASE, open_sealed, seal
from .errors import ApiError, error_responses
from .settings_api import settings_path_of
from .wizard import SETUP_ACTOR, setup_access

API = "/api/v1/backup"
SLOTS = 2
MAX_ARCHIVE = 64 * 1024 * 1024                       # a sealed backup, in bytes
_SLOTS = threading.BoundedSemaphore(SLOTS)
CLOCK = time.time
STATUSES = {"busy": 503, "unsafe": 500, "restore_failed": 500, "restore_pending": 500, "recovery_failed": 500,
            "journal_damaged": 500, "restore_in_progress": 503}
WORDING = {422: "The passphrase or the backup is not acceptable", 503: "Too many backups at once, or a file is busy"}


def actor_of(request: Request) -> str:
    """Who is acting: the administrator of the session, or the setup token while there is no administrator."""
    session = getattr(request.state, "session", None)
    return str(session.username) if session is not None else SETUP_ACTOR


def failure(error: BackupError) -> ApiError:
    """The API error for a refused backup; the message is the fixed text of ``backup.MESSAGES``."""
    return ApiError(STATUSES.get(error.code, 422), f"backup_{error.code}", str(error))


class ExportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passphrase: str = Field(max_length=MAX_PASSPHRASE * 2, repr=False)
    confirm: str = Field(max_length=MAX_PASSPHRASE * 2, repr=False, description="The passphrase again")
    include: List[Literal["snapshots", "audit"]] = Field(default_factory=list, max_length=2,
                                                         description="The optional categories to add")


class PreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passphrase: str = Field(max_length=MAX_PASSPHRASE * 2, repr=False)
    archive: str = Field(max_length=MAX_ARCHIVE * 4 // 3 + 16, repr=False, description="The backup file, base64")


class Slot:
    """One of the ``SLOTS`` places to derive a key in; ``503 backup_busy`` when none is free."""

    def __enter__(self) -> None:
        if not _SLOTS.acquire(blocking=False):
            raise ApiError(503, "backup_busy", "Another backup is being made or read; try again in a moment.")

    def __exit__(self, *exc: object) -> None:
        _SLOTS.release()


def decode_archive(text: str) -> bytes:
    """The bytes of a backup sent as base64; ``not_a_backup`` for anything else, ``too_large`` over ``MAX_ARCHIVE``."""
    try:
        blob = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise failure(BackupError("not_a_backup")) from None
    if len(blob) > MAX_ARCHIVE:
        raise failure(BackupError("too_large"))
    return blob


def router() -> APIRouter:
    api = APIRouter(prefix=API, tags=["backup"])

    @api.post("", dependencies=[Depends(admin)], summary="Export an encrypted backup of the application's own state",
              responses={200: {"description": "The encrypted backup (a file download)",
                               "content": {"application/octet-stream": {"schema": {"type": "string",
                                                                                  "format": "binary"}}}},
                         **error_responses(401, 403, 422, 500, 503, text=WORDING)})
    def backup_export(request: Request, body: ExportBody) -> Response:
        if len(body.passphrase) < MIN_PASSPHRASE or len(body.passphrase) > MAX_PASSPHRASE:
            raise ApiError(422, "invalid_passphrase", f"The passphrase needs {MIN_PASSPHRASE} to {MAX_PASSPHRASE} "
                           "characters.")
        if body.passphrase != body.confirm:
            raise ApiError(422, "passphrase_mismatch", "The two passphrases are not the same.")
        directory = Path(request.app.state.state_dir or ".")
        now = CLOCK()
        with Slot():
            try:
                files = backup.collect(directory, settings_path_of(request.app), body.include)
                sealed = seal(backup.pack(files, body.include, now), body.passphrase)
            except BackupError as error:
                raise failure(error) from error
        audit_event(request, "backup.exported", request.state.session.username,
                    optional=",".join(sorted(set(body.include))) or "none", files=len(files), size=len(sealed))
        stamp = time.strftime("%Y%m%d-%H%M%SZ", time.gmtime(now))
        return Response(sealed, media_type="application/octet-stream", headers={
            "Content-Disposition": f'attachment; filename="homelab-probe-backup-{stamp}.hlpbackup"',
            "Cache-Control": "no-store"})

    @api.post("/preview", dependencies=[Depends(setup_access)],
              summary="What restoring a backup would do (changes nothing)",
              responses={200: {"description": "The preview, without secrets", "content": {
                  "application/json": {"schema": {"type": "object"}}}},
                  **error_responses(401, 403, 422, 500, 503, text=WORDING)})
    @setup_ok
    def backup_preview(request: Request, body: PreviewBody) -> Dict[str, Any]:
        blob = decode_archive(body.archive)
        directory = Path(request.app.state.state_dir or ".")
        with Slot():
            try:
                package = backup.unpack(open_sealed(blob, body.passphrase))
                shown = backup.preview(package, directory, settings_path_of(request.app), os.environ,
                                       bool(request.app.state.env_named))
                shown["recovery"] = {"required": bool(backup.live_files(directory, settings_path_of(request.app), ())),
                                     "keep": restore.RECOVERY_KEEP, "folder": restore.RECOVERY_DIR}
            except BackupError as error:
                raise failure(error) from error
        audit_event(request, "backup.previewed", actor_of(request), created=shown["created_at"],
                    version=shown["app_version"])
        return shown

    return api
