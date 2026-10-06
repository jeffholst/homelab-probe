"""The scheduler of ``hlp serve --scheduler``: ``diagnose`` and snapshots on a timer, in the server's own process.

Two jobs run one after the other on one background thread (never overlapping each other):

* **diagnose** (every ``SCHEDULER_DIAGNOSE_MINUTES``): the health checks, then the notification step of ``diagnose
  --notify`` (``notify.process``). It uses the **same state file as cron** (``snapshots/<site id>/notify-state.json``)
  and takes the same lock on it, so a scheduled run and a cron run, or two of either, never both announce a finding.
  When no state exists yet the first run records the current findings as already reported instead of announcing all of
  them. A pass that could not read everything does not touch the state: a failed or partial read never means that
  something was fixed.
* **snapshot** (every ``SCHEDULER_SNAPSHOT_HOURS``, 0 for never): the same snapshot the command saves, in the site's
  directory, keeping the newest ``SCHEDULER_SNAPSHOT_KEEP``. After a restart the first one waits until the newest
  snapshot is as old as the interval, so restarting the server does not fill the directory.

Every run has a run id and ends in one ``scheduler.run`` record: the job, the id, the result, a fixed reason, the
milliseconds and counts, never a finding, a name, an address or a message. Nothing runs until the server is set up. When
the server stops, a waiting job gives up and a running one sends and writes nothing more. The scheduler writes files, so
``--read-only`` and a demo refuse it; it never writes its state or a snapshot through a symbolic link at ``snapshots/``
or in the site's directory.
"""

import dataclasses
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import FastAPI

from .. import logs
from ..client import UniFiAPIError
from ..config import ConfigError
from ..diagnose import WARNING, findings_from_document
from ..documents import Document, diagnose_document, snapshot_document
from ..history import DEFAULT_DIR, site_dir, site_snapshots
from ..notify import destinations_from_config, process
from ..triage import TriageError, TriageStore, finding_id
from .settings_api import load_effective, settings_path_of
from .snapshots_api import SnapshotStoreError, store_snapshot

_log = logging.getLogger(__name__)
JOBS = ("diagnose", "snapshot")
TICK_SECONDS = 30.0
STOP_WAIT_SECONDS = 60.0         # how long a stopping server waits for the job that is running


@dataclasses.dataclass
class JobResult:
    """How one run of a job went. ``result`` is ``ok``, ``failed`` or ``skipped``; ``reason`` a fixed word."""

    job: str
    run_id: str
    result: str
    reason: str
    duration_ms: int
    finished: float
    findings: int = 0
    removed: int = 0                  # old snapshots a snapshot run deleted
    destinations: str = ""


class Scheduler:
    def __init__(self, app: FastAPI, clock: Callable[[], float] = time.time,
                 tick_seconds: float = TICK_SECONDS) -> None:
        self.app, self._clock, self.tick_seconds = app, clock, tick_seconds
        self.last: Dict[str, JobResult] = {}
        self.last_ok: Dict[str, float] = {}               # when each job last ended in ``ok``
        self._due: Dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- the thread ---------------------------------------------------------------------------------------------

    def start(self) -> None:
        config = self.app.state.config
        logs.log_event(_log, logging.INFO, "scheduler.start", "The scheduler started",
                       diagnose_minutes=config.scheduler_diagnose_minutes,
                       snapshot_hours=config.scheduler_snapshot_hours, keep=config.scheduler_snapshot_keep)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="hlp-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the thread to stop and wait for the job that is running. A job in a read of the controller cannot be
        interrupted, but once the stop is asked nothing is sent, saved or written any more: it ends as ``skipped``."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(STOP_WAIT_SECONDS)
            if self._thread.is_alive():
                logs.warn("the scheduler's running job did not finish when the server stopped; it will send and "
                          "write nothing")

    def _loop(self) -> None:
        while True:
            self.tick()
            if self._stop.wait(self.tick_seconds):
                return

    # -- one pass ------------------------------------------------------------------------------------------------

    def ready(self) -> bool:
        """Is there a controller to read? Not before the guided setup has finished, and not while a restore replaces
        the files the jobs write."""
        setup = self.app.state.setup
        return (self.app.state.service is not None and (setup is None or not setup.mode)
                and not self.app.state.maintenance.active)

    def tick(self) -> List[JobResult]:
        """Run the jobs that are due, once. Never raises: a job that fails is a ``failed`` result."""
        if not self.ready():
            return []
        config, now, ran = self.app.state.config, self._clock(), []
        intervals = {"diagnose": config.scheduler_diagnose_minutes * 60.0,
                     "snapshot": config.scheduler_snapshot_hours * 3600.0}
        for job in JOBS:
            if intervals[job] <= 0:
                continue
            if job not in self._due:
                self._due[job] = self._first_due(job, now, intervals[job])
            if now < self._due[job]:
                continue
            result = self._run(job)
            ran.append(result)
            self._due[job] = self._clock() + intervals[job]
            now = self._clock()
        return ran

    def status(self) -> Dict[str, Dict[str, Any]]:
        """What each job did last, for the status page: wall-clock times (seconds since the epoch, None if never), the
        result and a fixed reason, and when it is due next."""
        out: Dict[str, Dict[str, Any]] = {}
        for job in JOBS:
            done = self.last.get(job)
            out[job] = {"last_run_at": done.finished if done else None, "result": done.result if done else None,
                        "reason": done.reason if done else None, "duration_ms": done.duration_ms if done else None,
                        "last_success_at": self.last_ok.get(job), "next_run_at": self._due.get(job)}
        return out

    def _first_due(self, job: str, now: float, every: float) -> float:
        """When a job first runs: a diagnose at once; a snapshot when the newest one is as old as the interval."""
        if job != "snapshot":
            return now
        try:
            site = self._site()
            files = site_snapshots(self._base(), site)
            return max(now, files[-1].stat().st_mtime + every) if files else now
        except (UniFiAPIError, ConfigError, OSError, IndexError):
            return now

    def _site(self) -> Dict[str, Any]:
        config = self.app.state.config
        built = self.app.state.service.build(lambda client: Document("site", client.resolve_site(config.site)))
        site: Dict[str, Any] = built.document.data
        return site

    def _base(self) -> Path:
        return Path(self.app.state.state_dir or ".") / DEFAULT_DIR

    def _run(self, job: str) -> JobResult:
        run_id, started = logs.new_id(), time.monotonic()
        counts: Dict[str, Any] = {}
        with logs.bind(request_id=run_id):
            try:
                result, reason = (self._diagnose if job == "diagnose" else self._snapshot)(counts)
            except UniFiAPIError as error:
                result, reason = "failed", f"controller_{error.kind or 'error'}"
            except SnapshotStoreError as error:
                result, reason = "failed", error.code
            except ConfigError:
                result, reason = "failed", "config"
            except OSError:
                result, reason = "failed", "storage"
            except Exception:                    # noqa: BLE001  (a job must never end the thread; the type is logged)
                result, reason = "failed", "error"
            done = JobResult(job, run_id, result, reason, round((time.monotonic() - started) * 1000), self._clock(),
                             **counts)
            self.last[job] = done
            if result == "ok":
                self.last_ok[job] = done.finished
            logs.log_event(_log, logging.INFO if result != "failed" else logging.WARNING, "scheduler.run",
                           f"Scheduled {job}: {result} ({reason})", job=job, run_id=run_id, result=result,
                           reason=reason, duration_ms=done.duration_ms, findings=done.findings,
                           removed=done.removed, destinations=done.destinations)
        return done

    # -- the jobs ------------------------------------------------------------------------------------------------

    def _diagnose(self, counts: Dict[str, Any]) -> Tuple[str, str]:
        state, config = self.app.state, self.app.state.config
        settings = load_effective(settings_path_of(self.app))
        state.service.refresh(0.0)                                  # the checks are of now, not of the cache
        built = state.service.build(lambda client: diagnose_document(client, config.site, settings, echo=False))
        document = built.document
        findings = findings_from_document(document.data)
        counts["findings"] = len(findings)
        if self._stop.is_set():
            return "skipped", "stopping"
        self._triage(document, findings)
        if not destinations_from_config(config):
            return "ok", "no_destination"
        if not document.meta["complete"]:
            return "skipped", "partial_data"                        # a partial read cannot say that anything cleared
        if any(path.is_symlink() for path in (self._base(), site_dir(self._base(), document.meta["site"]))):
            raise SnapshotStoreError("snapshots_unsafe")            # the state is never written through a link
        def delivered(results: List[Tuple[str, bool, str]]) -> None:
            """Called the moment a message went out, so a state that then cannot be saved does not hide it."""
            self.app.state.deliveries.record(results)
            counts["destinations"] = ",".join(f"{kind}:{'sent' if ok else 'failed'}" for kind, ok, _ in results)

        outcome = process(findings, config=config, settings=settings, site=document.meta["site"], base=self._base(),
                          minimum=WARNING, baseline_if_new=True, warn=logs.warn, cancelled=self._stop.is_set,
                          on_delivery=delivered)
        if outcome.kind == "cancelled":
            return "skipped", "stopping"
        return ("failed", "undelivered") if outcome.undelivered else ("ok", outcome.kind)

    def _triage(self, document: Document, findings: List[Any]) -> None:
        """Record what the checks found for the triage (first and last seen) and end the entries of findings that a
        complete read no longer shows. A failure here is a warning, not a failed run: the notifications matter more."""
        site = document.meta["site"]
        present = {finding_id(f.code, f.subject, f.target_mac): f.code for f in findings}
        try:
            TriageStore(site_dir(self._base(), site), site["id"]).reconcile(present, document.meta["complete"],
                                                                           self._clock())
        except TriageError:
            logs.warn("the triage file could not be updated")

    def _snapshot(self, counts: Dict[str, Any]) -> Tuple[str, str]:
        state, config = self.app.state, self.app.state.config
        state.service.refresh(0.0)
        built = state.service.build(lambda client: snapshot_document(client, config.site, echo=False))
        if self._stop.is_set():
            return "skipped", "stopping"
        _, gone = store_snapshot(self._base(), built.document.data, config.scheduler_snapshot_keep)
        counts["removed"] = len(gone)
        return "ok", "saved"
