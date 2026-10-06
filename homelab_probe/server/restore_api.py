"""Restoring a backup over the API (issue #226): ``POST /api/v1/backup/restore``.

The same checks as the preview (``backup_api``: the passphrase, the whole archive, an enabled administrator), then the
replacement described in ``homelab_probe.restore``: settings, configuration, accounts, certificates, notes and triage
replaced **as one set**, never merged.

* **Who.** An administrator (a session and the CSRF token), or, on a server that is not set up or has no enabled
  administrator, the setup token: that is how a **fresh installation** is restored. A ``--read-only`` server refuses
  (``403 read_only``), and so does a demo.
* **Confirmation.** ``confirm`` must be ``true``: the caller has seen the preview, which says the accounts and their
  passwords are replaced.
* **A recovery backup first.** When there is anything to replace, an encrypted backup of the present state is written
  to ``recovery/`` (owner-only; the newest three are kept) with the ``recovery_passphrase``, typed twice, which is not
  the passphrase of the backup being restored: the owner must be able to open it without that one. A recovery backup
  that cannot be written stops the restore before anything changes.
* **One at a time, and quiet.** The restore takes the place of ``Maintenance`` (a second one is ``503``), the routes
  that write a file answer ``503 restore_in_progress`` and the scheduler runs no job meanwhile. It holds the locks of
  the accounts and of the per-site files that the other writers (the command line too) take.
* **After it.** Every session ends (everybody logs in with the credentials of the backup), the configuration is read
  again and the controller service is made anew (a variable set in the environment keeps winning over the restored
  ``.env``), and a server that was waiting for its setup is set up. The audit trail of this installation is kept and
  gets ``backup.restored`` (the date of the backup, the number of files, the name of the recovery backup): never the
  passphrases or any content. A failure is ``backup.restore_failed`` with a fixed reason and the previous state back.
"""

import contextlib
import logging
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Literal, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import ConfigDict, Field

from .. import backup, logs, restore
from ..backup import BackupError
from ..config import ConfigError
from . import backup_api
from .auth import AuthState, audit_event, local_write, setup_ok
from .backup_crypto import MAX_PASSPHRASE, MIN_PASSPHRASE, open_sealed, seal
from .errors import ApiError, error_responses
from .scheduler import Scheduler
from .service import ControllerService
from .settings_api import settings_path_of
from .wizard import Draft, reload_default, setup_access

_log = logging.getLogger(__name__)
JOB_WAIT_SECONDS = 60.0
WORDING = {403: "A read-only server or a demo does not restore", 422: "The passphrase or the backup is not acceptable",
           500: "The restore could not be made; the previous state was put back where it could",
           503: "A restore is in progress, or too many backups at once"}


class RestoreBody(backup_api.PreviewBody):
    recovery_passphrase: Optional[str] = Field(default=None, max_length=MAX_PASSPHRASE * 2, repr=False)
    recovery_confirm: Optional[str] = Field(default=None, max_length=MAX_PASSPHRASE * 2, repr=False)
    confirm: Literal[True] = Field(description="Must be true: the preview was read")
    model_config = ConfigDict(extra="forbid")


def recovery_passphrase(body: RestoreBody) -> str:
    """The checked recovery passphrase of ``body`` (it is needed only when there is a present state to keep)."""
    given = body.recovery_passphrase or ""
    if not given:
        raise ApiError(422, "recovery_passphrase_required", "A passphrase for the recovery backup of the present state "
                       "is needed.")
    if len(given) < MIN_PASSPHRASE or len(given) > MAX_PASSPHRASE:
        raise ApiError(422, "invalid_recovery_passphrase",
                       f"The recovery passphrase needs {MIN_PASSPHRASE} to {MAX_PASSPHRASE} characters.")
    if given != body.recovery_confirm:
        raise ApiError(422, "recovery_passphrase_mismatch", "The two recovery passphrases are not the same.")
    return given


def reloaded(request: Request) -> None:
    """Read the settings again and make the controller service from them; ``ApiError`` when they cannot be loaded."""
    state = request.app.state
    setup = state.setup
    directory = Path(state.state_dir or ".")
    reload = (setup.reload if setup is not None and setup.reload is not None else state.reload) \
        or (lambda: reload_default(directory))
    try:
        config = reload()
    except ConfigError:
        raise ApiError(500, "reload_failed", "The backup was restored but its settings could not be loaded: restart "
                       "the server.") from None
    factory = setup.service_factory if setup is not None and setup.service_factory is not None else ControllerService
    state.service = factory(config)
    state.config = config
    logs.register_secrets(*config.secret_values())
    previous = state.auth                  # the session limits and the audit rotation come from the settings
    state.auth = AuthState.for_directory(directory, config)
    previous.audit.close()
    if setup is not None and setup.mode:
        setup.mode, setup.draft = None, Draft(site=setup.draft.site)
        setup.generation += 1


@contextlib.contextmanager
def quiet_scheduler(scheduler: Optional[Scheduler]) -> Iterator[None]:
    """Wait (a minute at most) for a scheduled job that is running and keep the next from starting while the restore
    runs; ``BackupError("busy")`` when the job does not end."""
    if scheduler is None:
        yield
        return
    with scheduler.idle(JOB_WAIT_SECONDS) as acquired:
        if not acquired:
            raise BackupError("busy")
        yield


def router() -> APIRouter:
    api = APIRouter(prefix=backup_api.API, tags=["backup"])

    @api.post("/restore", dependencies=[Depends(setup_access)], summary="Restore a backup over this installation",
              responses={200: {"description": "What was restored", "content": {"application/json": {"schema": {
                  "type": "object"}}}}, **error_responses(401, 403, 422, 500, 503, text=WORDING)})
    @setup_ok
    @local_write
    def backup_restore(request: Request, body: RestoreBody) -> Dict[str, Any]:
        state = request.app.state
        if state.demo:
            raise ApiError(403, "demo", "A demo does not restore backups.")
        directory, settings_file, actor = Path(state.state_dir or "."), settings_path_of(request.app), \
            backup_api.actor_of(request)
        blob = backup_api.decode_archive(body.archive)
        with backup_api.Slot():
            try:
                package = backup.unpack(open_sealed(blob, body.passphrase))
                backup.inspect(package)
                existing = bool(backup.live_files(directory, settings_file, ()))
            except BackupError as error:
                raise backup_api.failure(error) from error
            recovery = recovery_passphrase(body) if existing else ""
            if not state.maintenance.begin():
                raise ApiError(503, "restore_in_progress", str(BackupError("restore_in_progress")))
            try:
                with quiet_scheduler(state.scheduler):
                    name = _replace(request, package, directory, settings_file, recovery, existing)
            except BackupError as error:
                audit_event(request, "backup.restore_failed", actor, reason=error.code)
                raise backup_api.failure(error) from error
            finally:
                state.maintenance.end()
        state.auth.sessions.end_all()
        audit_event(request, "backup.restored", actor, created=package.manifest["created_at"],
                    version=package.manifest["app_version"], files=len(package.files), recovery=name or "none")
        reloaded(request)
        return {"restored": True, "created_at": package.manifest["created_at"], "recovery_backup": name,
                "sessions_ended": True,
                "message": "The backup was restored. Every session has ended: log in with the accounts of the backup."}

    return api


def _replace(request: Request, package: backup.Package, directory: Path, settings_file: Path, recovery: str,
             existing: bool) -> Optional[str]:
    """The restore itself, with the locks held; the name of the recovery backup, or ``None`` when there was nothing to
    keep."""
    touched: List[Path] = [restore.target_path(directory, settings_file, e.target)
                           for e in restore.plan(package, directory, settings_file)]
    touched += list(backup.live_files(directory, settings_file, ()).values())
    name: Optional[str] = None
    with backup.locked(touched, settings_file):
        entries = restore.plan(package, directory, settings_file)
        if existing:
            present = backup.collect(directory, settings_file, (), lock=False)
            sealed = seal(backup.pack(present, (), backup_api.CLOCK()), recovery)
            name = restore.write_recovery(directory, sealed, time.strftime("%Y%m%d-%H%M%SZ",
                                                                            time.gmtime(backup_api.CLOCK())))
        restore.apply(directory, settings_file, entries)
    restore.prune_recovery(directory)
    return name
