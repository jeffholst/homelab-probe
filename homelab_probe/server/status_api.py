"""What the server knows about its own health: ``GET /api/v1/status``, and the explicit test notification
``POST /api/v1/notifications/test``.

**The status says only what this server has seen**, and says when it saw it. It makes no request of its own to the
controller (a page that polls it would otherwise read the controller): it reports the outcome of the reads the server
made for its other routes and its scheduler. A server that has read nothing yet says ``unknown``, not ``ok``.

* **controller**: ``state`` is ``ok``, ``unreachable``, ``certificate`` (TLS), ``key_rejected`` or ``unknown``, from
  the last reads (an error status from one endpoint is still an answer: the controller was reachable), and when the
  last read succeeded. ``data`` says whether the last answer was an older one served because a read failed (``stale``)
  and how many warnings the last response carried (an optional read failed).
* **scheduler**: whether it is on and, for each job, the last run, its result and the last success.
* **Role visibility.** A viewer gets the three above without reasons or failure kinds. An administrator also gets the
  failure kind and the job reasons (fixed words), the notification destinations (kinds only) and the outcome of the
  last delivery to each, the state of the storage (the data directory, the audit log, ``snapshots/``, the free disk
  space) and ``problems``, a list of what is wrong in fixed words. No address, path, URL, token or message is ever in
  it.
* **Deliveries** are those this server made (the scheduler's and the test's); a cron job is another process and is
  not seen here.

The test notification sends one fixed message to every configured destination, only when an administrator asks (with
the CSRF token), at most once every ``TEST_INTERVAL`` seconds, through the delivery code of ``diagnose --notify``, and
answers with each destination's kind and a fixed reason, never a URL or a token.
"""

import math
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Request

from ..notify import destinations_from_config, send
from .auth import admin, audit_event
from .errors import ApiError, error_responses

API = "/api/v1"
TEST_INTERVAL = 10.0                       # seconds between two test notifications
LOW_DISK_BYTES = 100 * 1024 * 1024         # less free space than this on the data directory is a problem
_TEST_LOCK = threading.Lock()


def iso(moment: Optional[float]) -> Optional[str]:
    """``moment`` (seconds since the epoch) as an ISO 8601 UTC time, or None."""
    return None if moment is None else datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Deliveries:
    """The outcome of the last delivery to each destination this server made, for the status."""

    def __init__(self, clock: Any = time.time) -> None:
        self._clock = clock
        self._last: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def record(self, results: List[Tuple[str, bool, str]]) -> None:
        now = self._clock()
        with self._lock:
            for kind, delivered, reason in results:
                self._last[kind] = {"at": now, "delivered": delivered, "reason": reason}

    def last(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {kind: dict(value) for kind, value in self._last.items()}


def storage(app: Any) -> Dict[str, str]:
    """The state of what the server writes, in fixed words: ``ok``, ``absent`` (not made yet), ``missing``,
    ``not_writable``, ``unsafe`` (``snapshots/`` is a symbolic link), ``low`` (disk space) or ``unknown``."""
    data = Path(app.state.state_dir or ".")
    writable = os.access(data, os.W_OK | os.X_OK)
    directory = "ok" if data.is_dir() and writable else "missing" if not data.exists() else "not_writable"
    audit = data / "audit.log"
    log = ("ok" if os.access(audit, os.W_OK) else "not_writable") if audit.exists() else (
        "ok" if directory == "ok" else "not_writable")
    snapshots = data / "snapshots"
    if snapshots.is_symlink():
        kept = "unsafe"
    elif not snapshots.exists():
        kept = "absent"
    else:
        kept = "ok" if os.access(snapshots, os.W_OK | os.X_OK) else "not_writable"
    try:
        disk = "low" if shutil.disk_usage(data).free < LOW_DISK_BYTES else "ok"
    except OSError:
        disk = "unknown"
    return {"data_directory": directory, "audit_log": log, "snapshots": kept, "disk": disk}


def build_status(app: Any, is_admin: bool, now: Optional[float] = None) -> Dict[str, Any]:
    """The status document for a viewer or an administrator."""
    now = time.time() if now is None else now
    service, config = app.state.service, app.state.config
    wire = service.cache.wire
    stale = wire.stale_at is not None and (wire.ok_at is None or wire.stale_at > wire.ok_at)
    document: Dict[str, Any] = {
        "generated_at": iso(now), "read_only": bool(app.state.read_only),
        "controller": {"state": wire.state, "last_read_ok_at": iso(wire.ok_at)},
        "data": {"stale": stale, "warnings": service.last_warnings, "last_response_at": iso(service.last_build)},
        "scheduler": {"enabled": app.state.scheduler is not None, "jobs": {}},
    }
    jobs = app.state.scheduler.status() if app.state.scheduler is not None else {}
    for name, job in jobs.items():
        shown = {"last_run_at": iso(job["last_run_at"]), "result": job["result"],
                 "last_success_at": iso(job["last_success_at"]), "next_run_at": iso(job["next_run_at"])}
        if is_admin:
            shown.update(reason=job["reason"], duration_ms=job["duration_ms"])
        document["scheduler"]["jobs"][name] = shown
    if not is_admin:
        return document
    document["controller"].update(last_failure_at=iso(wire.failed_at), last_failure=wire.failed_kind or None)
    last = app.state.deliveries.last()
    document["notifications"] = {
        "destinations": [d.kind for d in destinations_from_config(config)],
        "last_delivery": {kind: {"at": iso(value["at"]), "delivered": value["delivered"], "reason": value["reason"]}
                          for kind, value in last.items()}}
    document["storage"] = storage(app)
    document["problems"] = problems(document)
    return document


def problems(document: Dict[str, Any]) -> List[str]:
    """What is wrong, as ``area:word``, from an administrator's status."""
    found: List[str] = []
    state = document["controller"]["state"]
    if state not in ("ok", "unknown"):
        found.append(f"controller:{state}")
    if document["data"]["stale"]:
        found.append("data:stale")
    for name, job in document["scheduler"]["jobs"].items():
        if job["result"] == "failed":
            found.append(f"scheduler.{name}:failed")
    for kind, delivery in document["notifications"]["last_delivery"].items():
        if not delivery["delivered"]:
            found.append(f"notifications.{kind}:failed")
    for area, word in document["storage"].items():
        if word not in ("ok", "absent"):
            found.append(f"storage.{area}:{word}")
    return found


STATUS_SCHEMA: Dict[str, Any] = {
    "type": "object", "required": ["generated_at", "read_only", "controller", "data", "scheduler"],
    "properties": {
        "generated_at": {"type": "string"}, "read_only": {"type": "boolean"},
        "controller": {"type": "object", "required": ["state", "last_read_ok_at"], "properties": {
            "state": {"enum": ["ok", "unreachable", "certificate", "key_rejected", "unknown"]},
            "last_read_ok_at": {"type": ["string", "null"]}}},
        "data": {"type": "object", "required": ["stale", "warnings", "last_response_at"]},
        "scheduler": {"type": "object", "required": ["enabled", "jobs"]},
        "notifications": {"type": "object"}, "storage": {"type": "object"},
        "problems": {"type": "array", "items": {"type": "string"}},
    },
}
TEST_SCHEMA: Dict[str, Any] = {
    "type": "object", "required": ["delivered", "results"],
    "properties": {"delivered": {"type": "boolean"}, "results": {"type": "array", "items": {
        "type": "object", "required": ["destination", "delivered", "reason"],
        "properties": {"destination": {"type": "string"}, "delivered": {"type": "boolean"},
                       "reason": {"type": "string"}}}}},
}


def router() -> APIRouter:
    api = APIRouter(prefix=API)

    @api.get("/status", summary="What this server has seen of the controller, its jobs and its storage",
             responses={200: {"description": "The status (more for an administrator)", "content": {
                 "application/json": {"schema": STATUS_SCHEMA}}}, **error_responses(401)})
    def status(request: Request) -> Dict[str, Any]:
        is_admin = request.state.session.role == "admin"
        return build_status(request.app, is_admin)

    @api.post("/notifications/test", dependencies=[Depends(admin)], summary="Send a test notification now",
              responses={200: {"description": "How each destination answered", "content": {
                  "application/json": {"schema": TEST_SCHEMA}}}, **error_responses(
                      401, 403, 409, 429, 500, text={409: "No notification destination is configured",
                                                    429: "A test was sent a moment ago: try again after Retry-After"})})
    def notifications_test(request: Request) -> Dict[str, Any]:
        config = request.app.state.config
        destinations = destinations_from_config(config)
        if not destinations:
            raise ApiError(409, "no_destination", "No notification destination is configured.")
        with _TEST_LOCK:
            waited = time.monotonic() - getattr(request.app.state, "test_notification_at", -math.inf)
            if waited < TEST_INTERVAL:
                seconds = math.ceil(TEST_INTERVAL - waited)
                raise ApiError(429, "too_soon", f"A test was sent a moment ago. Try again in {seconds} seconds.",
                               headers={"Retry-After": str(seconds)}, retry_after=seconds)
            request.app.state.test_notification_at = time.monotonic()
        results = send(destinations, [], False, config.timeout, test=True)
        request.app.state.deliveries.record(results)
        delivered = [kind for kind, ok, _ in results if ok]
        audit_event(request, "notification.tested", request.state.session.username,
                    destinations=",".join(kind for kind, _, _ in results), delivered=len(delivered))
        return {"delivered": bool(delivered),
                "results": [{"destination": kind, "delivered": ok, "reason": reason} for kind, ok, reason in results]}

    return api
