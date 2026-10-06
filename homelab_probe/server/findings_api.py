"""Findings with their local triage: ``GET .../findings`` and ``PUT .../findings/{id}/triage``.

``GET`` is the diagnose document of the site with every finding given an **id**, a **place in the order with its
reasons**, the **triage state** and what is known and not known about it. Both roles read it. It never writes: the file
is read, and when it cannot be (damaged, a symbolic link) the findings are still shown, without triage, and the
response says so.

``PUT .../findings/{id}/triage`` (an administrator, with the CSRF token, refused by ``--read-only``, audited by code and
id, never by a name or a MAC) acknowledges, snoozes until a date, or reopens a finding **that is there now**. A state
never hides or resolves a finding: it stays in the list. See ``homelab_probe.triage`` for the identity, the states and
the rule that only a complete read can show a condition cleared (the scheduler applies it).
"""

import argparse
import datetime
import re
import time
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Optional, Tuple

from fastapi import APIRouter, Depends, Query, Request
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field

from .. import logs
from ..client import UniFiAPIError
from ..commands import diagnose_areas
from ..config import ConfigError
from ..documents import diagnose_document
from ..history import DEFAULT_DIR, site_dir
from ..notes import NotesStore
from ..sitefile import StoreError
from ..triage import MAX_NOTE, STATES, Ranked, TriageError, TriageStore, finding_id, guidance, iso, rank_findings
from .auth import admin, audit_event, local_write
from .errors import ApiError, error_responses, from_controller
from .routes import RefreshQ, SiteP, checked_site
from .settings_api import load_effective, settings_path_of
from .snapshots_api import site_of

UNIFI = "/api/v1/unifi"
CLOCK = time.time
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
IdP = Annotated[str, PathParam(pattern=r"^[0-9a-f]{16}$", description="The id of a finding, from the list")]
WORDING = {404: "No such site, or no such finding now", 409: "Too many findings are tracked",
           500: "The local files cannot be used"}
FINDING_SCHEMA: Dict[str, Any] = {
    "type": "object", "required": ["id", "rank", "severity", "code", "subject", "message", "mac", "priority", "triage"],
    "properties": {"id": {"type": "string"}, "rank": {"type": "integer"}, "severity": {"type": "string"},
                   "code": {"type": "string"}, "subject": {"type": "string"}, "message": {"type": "string"},
                   "mac": {"type": "string"}, "first_seen_at": {"type": ["string", "null"]},
                   "priority": {"type": "object", "required": ["score", "scope", "reasons"]},
                   "triage": {"type": "object", "required": ["state"]},
                   "limitations": {"type": "array", "items": {"type": "string"}},
                   "next_checks": {"type": "array", "items": {"type": "string"}}, "docs": {"type": "string"}},
}
LIST_SCHEMA: Dict[str, Any] = {
    "type": "object", "required": ["items", "summary", "complete", "triage_available", "generated_at", "warnings"],
    "properties": {"items": {"type": "array", "items": FINDING_SCHEMA}, "summary": {"type": "object"},
                   "complete": {"type": "boolean"}, "triage_available": {"type": "boolean"},
                   "notes_available": {"type": "boolean"},
                   "limitations": {"type": "array", "items": {"type": "string"}},
                   "generated_at": {"type": "string"}, "warnings": {"type": "array", "items": {"type": "string"}}},
}


class TriageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal["open", "acknowledged", "snoozed"]
    until: Optional[str] = Field(default=None, max_length=10, description="With snoozed: the last day, 2026-10-10")
    note: str = Field(default="", max_length=MAX_NOTE, description=f"Plain text, at most {MAX_NOTE} characters")


def end_of(day: str) -> float:
    """The moment a snooze through ``day`` (``YYYY-MM-DD``) ends: the start of the next day, UTC."""
    if not _DATE.fullmatch(day):
        raise ApiError(422, "invalid_parameter", "until must be a date like 2026-10-10.")
    try:
        date = datetime.date.fromisoformat(day)
    except ValueError:
        raise ApiError(422, "invalid_parameter", "until must be a date like 2026-10-10.") from None
    after = datetime.datetime.combine(date + datetime.timedelta(days=1), datetime.time(), datetime.timezone.utc)
    return after.timestamp()


def store_for(request: Request, site: Dict[str, Any]) -> TriageStore:
    base = Path(request.app.state.state_dir or ".") / DEFAULT_DIR
    return TriageStore(site_dir(base, site), str(site.get("id") or ""))


def note_counts(request: Request, record: Dict[str, Any]) -> Tuple[Dict[str, int], bool]:
    """(how many notes each subject has, whether the notes file could be read). An unreadable file is **not** zero
    notes: the caller says so (``notes_available``)."""
    store = store_for(request, record)
    try:
        return NotesStore(store.directory, store.site_id).counts(), True
    except StoreError:
        return {}, False


def item_of(r: Ranked) -> Dict[str, Any]:
    """A finding as the list and the change show it: the same complete representation in both."""
    checks, docs = guidance(r.finding["code"])
    return {"id": r.id, "rank": r.rank, **r.finding, "priority": {"score": r.score, "scope": r.scope,
                                                                    "reasons": r.reasons},
            "triage": r.triage, "first_seen_at": iso(r.first_seen_at), "limitations": r.limitations,
            "next_checks": checks, "next_checks_are": "general guidance for this kind of check", "docs": docs}


def failure(error: TriageError) -> ApiError:
    """The API error for a triage change that cannot be made (the messages are fixed text of ``triage``)."""
    status = {"invalid": 422, "full": 409, "locked": 503}.get(error.code, 500)
    return ApiError(status, f"triage_{error.code}", str(error))


def read_findings(request: Request, site: str, no_events: bool = False, refresh: bool = False) -> Dict[str, Any]:
    """The diagnose document of ``site`` and what is needed around it."""
    name = checked_site(site)
    record = site_of(request, name)
    try:
        settings = load_effective(settings_path_of(request.app))
    except ConfigError:
        logs.warn("the settings file could not be used")
        raise ApiError(500, "settings_invalid", "The settings file could not be read; see the server log.") from None
    areas = diagnose_areas(argparse.Namespace(only=[], skip=[], no_events=no_events))
    service = request.app.state.service
    if refresh:
        service.refresh(5.0)
    try:
        built = service.build(lambda client: diagnose_document(client, name, settings, areas, echo=False))
    except UniFiAPIError as error:
        raise from_controller(error) from error
    return {"site": record, "built": built, "complete": bool(built.document.meta["complete"]) and areas is None}


def router() -> APIRouter:
    api = APIRouter(prefix=f"{UNIFI}/sites/{{site}}/findings")

    @api.get("", summary="The findings of a site, in the order to look at them, with their triage",
             responses={200: {"description": "The findings", "content": {"application/json": {
                 "schema": LIST_SCHEMA}}}, **error_responses(401, 404, 422, 500, 502, 504, text=WORDING)})
    def findings_list(request: Request, site: SiteP, no_events: Annotated[bool, Query()] = False,
                      refresh: RefreshQ = False) -> Dict[str, Any]:
        read = read_findings(request, site, no_events, refresh)
        built, record, now = read["built"], read["site"], CLOCK()
        document = built.document
        limitations: List[str] = []
        available, entries = True, {}
        try:
            entries = store_for(request, record).load()
        except TriageError as error:
            available = False
            limitations.append(f"Triage is not shown: {str(error).rstrip('.').lower()}.")
        if not read["complete"]:
            limitations.append("This read was partial or left some checks out: findings may be missing, and "
                               "nothing can be said to have cleared.")
        ranked = rank_findings(document.data["findings"], entries, now, read["complete"])
        notes, notes_available = note_counts(request, record)
        if not notes_available:
            limitations.append("Note counts are not shown: the notes file cannot be used (see the notes API).")
        items = [{**item_of(r), "note_count": notes.get(f"finding:{r.id}", 0)} for r in ranked]
        states = {state: sum(1 for r in ranked if r.triage["state"] == state) for state in STATES}
        return {"site": {"id": str(record.get("id") or ""), "name": str(record.get("name") or "")},
                "items": items, "summary": {**document.data["summary"], **states, "total": len(items)},
                "complete": read["complete"], "triage_available": available, "notes_available": notes_available,
                "limitations": limitations,
                "generated_at": built.generated_at, "warnings": built.warnings}

    @api.put("/{finding}/triage", dependencies=[Depends(admin)], summary="Acknowledge, snooze or reopen a finding",
             responses={200: {"description": "The finding with its new state", "content": {"application/json": {
                 "schema": FINDING_SCHEMA}}}, **error_responses(401, 403, 404, 409, 422, 500, 502, 503, 504,
                                                                text=WORDING)})
    @local_write
    def findings_triage(request: Request, site: SiteP, finding: IdP, body: TriageBody) -> Dict[str, Any]:
        until = end_of(body.until) if body.until is not None else None
        if (body.state == "snoozed") != (until is not None):
            raise ApiError(422, "invalid_parameter", "A snooze needs until, and only a snooze takes it.")
        read = read_findings(request, site)
        document = read["built"].document
        ours = {finding_id(f["code"], f["subject"], f.get("mac") or None): f for f in document.data["findings"]}
        if finding not in ours:
            raise ApiError(404, "finding_not_found", "That finding is not there now.")
        code = ours[finding]["code"]
        user = request.state.session.username
        try:
            store_for(request, read["site"]).set_state(finding, code, body.state, user, CLOCK(), until, body.note)
        except TriageError as error:
            raise failure(error) from error
        audit_event(request, "triage.changed", user, finding=finding, code=code, state=body.state,
                    until=body.until or "")
        entries = store_for(request, read["site"]).load()
        ranked = rank_findings(document.data["findings"], entries, CLOCK(), read["complete"])
        notes, _ = note_counts(request, read["site"])
        found = next(r for r in ranked if r.id == finding)
        return {**item_of(found), "note_count": notes.get(f"finding:{finding}", 0)}

    return api
