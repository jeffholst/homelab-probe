"""The report routes: ``/api/v1/unifi/sites/{site}/...``, one per document, each a GET.

A route does what its command does and no more: it checks the query parameters the way the command line does, calls
the document function the command calls (through ``ControllerService``, so the read is cached) and returns the
document with ``generated_at`` and ``warnings``. No route takes a path to forward, and none writes anything.
"""

import argparse
import logging
from typing import Annotated, Any, Callable, Dict, List, Optional

from fastapi import APIRouter, FastAPI, Path, Query, Request
from fastapi.responses import JSONResponse

from .. import logs
from ..client import UniFiAPIError
from ..client_view import candidate_rows
from ..commands import diagnose_areas
from ..config import ConfigError, validate_site
from ..documents import (
    Document,
    audit_document,
    client_document,
    diagnose_document,
    events_document,
    firewall_document,
    info_document,
    new_clients_document,
    query_document,
    topology_document,
    wan_document,
    wifi_document,
)
from ..events import DEFAULT_LIMIT, SEVERITIES, parse_duration
from ..settings import DiagnoseSettings
from ..snapshot import EventQuery
from ..util import normalize_mac
from ..wan import DEFAULT_DAYS
from ..wifi import DEFAULT_MIN_SIGNAL, parse_band
from . import apischema
from .errors import ApiError, from_controller
from .service import Built
from .settings_api import load_effective, settings_file

_log = logging.getLogger(__name__)
UNIFI = "/api/v1/unifi"
REFRESH_MIN_INTERVAL = 5.0       # seconds between two manual refreshes; a faster one is ignored (and says so)
MAX_TEXT = 120
ERROR_SCHEMA = {"type": "object", "required": ["error", "message"],
                "properties": {"error": {"type": "string"}, "message": {"type": "string"}}}
CANDIDATE_SCHEMA = {
    "type": "object", "required": ["Name", "MAC Address", "IP Address", "Status"],
    "properties": {"Name": {"type": "string"}, "MAC Address": {"type": "string"},
                   "IP Address": {"type": "string"}, "Status": {"type": "string"}},
}
AMBIGUOUS_CLIENT_SCHEMA = {
    "type": "object", "required": ["error", "message", "candidates"],
    "properties": {"error": {"type": "string"}, "message": {"type": "string"},
                   "candidates": {"type": "array", "items": CANDIDATE_SCHEMA}},
}
INFO_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "urn:homelab-probe:api:info:v1",
    "title": "info (API response)", "description": "The controller's application info and its sites.",
    "type": "object", "required": ["application", "sites", "generated_at", "warnings"],
    "properties": {
        "application": {"type": "object"},
        "sites": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": ["string", "null"]}, "ref": {"type": ["string", "null"]},
            "id": {"type": ["string", "null"]}}, "required": ["name", "ref", "id"]}},
        "generated_at": apischema.GENERATED_AT, "warnings": apischema.WARNINGS},
}

SiteP = Annotated[str, Path(max_length=MAX_TEXT, description="A site name, internal reference or UUID")]
TextQ = Annotated[str, Query(max_length=MAX_TEXT)]
OptTextQ = Annotated[Optional[str], Query(max_length=MAX_TEXT)]
SinceQ = Annotated[str, Query(max_length=12, description="How far back: 90m, 24h, 7d, 2w")]
ListQ = Annotated[Optional[List[str]], Query()]
RefreshQ = Annotated[bool, Query(description="Read the controller again instead of using the cache (at most every "
                                             "5 s)")]
BandQ = Annotated[Optional[str], Query(max_length=8, description="2.4, 5 or 6")]
SignalQ = Annotated[float, Query(ge=-120, le=0, description="dBm")]
DaysQ = Annotated[int, Query(ge=1, le=3650)]
LimitQ = Annotated[int, Query(ge=0, le=20000, description="0 for all")]
MacP = Annotated[str, Path(pattern=r"^[0-9A-Fa-f:.\-]{12,17}$", description="The client's MAC address")]


def _ok(schema: Dict[str, Any], *, settings: bool = False,
        ambiguous_client: bool = False) -> Dict[int | str, Dict[str, Any]]:
    responses: Dict[int | str, Dict[str, Any]] = {
        200: {"description": "The document with when it was read",
              "content": {"application/json": {"schema": schema}}},
        **{code: {"description": text, "content": {"application/json": {"schema": ERROR_SCHEMA}}}
           for code, text in ((404, "No such site or client"), (422, "A parameter is not valid"),
                              (502, "The controller could not be read"), (504, "The controller timed out"))},
    }
    if ambiguous_client:
        responses[409] = {"description": "More than one client matches",
                          "content": {"application/json": {"schema": AMBIGUOUS_CLIENT_SCHEMA}}}
    if settings:
        responses[500] = {"description": "The settings file could not be used",
                          "content": {"application/json": {"schema": ERROR_SCHEMA}}}
    return responses


def checked_site(site: str) -> str:
    try:
        return validate_site(site)
    except ConfigError:
        raise ApiError(422, "invalid_parameter", "The site name is not valid.") from None


def _name(value: Optional[str], what: str, required: bool = False) -> str:
    text = (value or "").strip()
    if required and not text:
        raise ApiError(422, "invalid_parameter", f"{what} needs a name (an empty one would match everything).")
    return text


def _settings(request: Request) -> DiagnoseSettings:
    try:
        return load_effective(settings_file(request))
    except ConfigError as error:
        logs.warn(f"the settings file could not be used: {error}")
        raise ApiError(500, "settings_invalid", "The settings file could not be read; see the server log.") from None


def respond(request: Request, make: Callable[[Any], Document], *, refresh: bool = False,
             notes: Optional[Callable[[Document], Dict[str, Any]]] = None,
             check: Optional[Callable[[Document], None]] = None) -> JSONResponse:
    """Build the document through the service and wrap it with ``generated_at`` and ``warnings``. ``check`` may raise
    an ``ApiError`` for a document that is a "no" (a client that is not there)."""
    service = request.app.state.service
    skipped = [] if not refresh or service.refresh(REFRESH_MIN_INTERVAL) else [
        "refresh skipped: the data was refreshed less than 5 seconds ago"]
    try:
        built: Built = service.build(make)
    except UniFiAPIError as error:
        raise from_controller(error) from error
    if check is not None:
        check(built.document)
    data = built.document.data
    body: Dict[str, Any] = {"items": data} if isinstance(data, list) else dict(data)
    if notes is not None:
        body.update(notes(built.document))
    body["generated_at"], body["warnings"] = built.generated_at, [*built.warnings, *skipped]
    return JSONResponse(body)


def _duration(text: str) -> int:
    try:
        return parse_duration(text)
    except ValueError as error:
        raise ApiError(422, "invalid_parameter", str(error)) from None


def _event_query(since: str, category: Optional[List[str]], severity: Optional[List[str]], search: str) -> EventQuery:
    categories, severities = category or [], [s.lower() for s in severity or []]
    if any(len(c) > 64 for c in categories) or any(s not in SEVERITIES for s in severities):
        raise ApiError(422, "invalid_parameter", f"severity must be one of {', '.join(SEVERITIES)}.")
    return EventQuery(_duration(since), tuple(categories), tuple(severities), search)


def _client_found(document: Document) -> None:
    matches = document.meta["matches"]
    if not document.data:
        if not matches:
            raise ApiError(404, "client_not_found", "No client has this MAC address.")
        raise ApiError(409, "client_ambiguous", "More than one client matches.", candidates=candidate_rows(matches))


def build_router() -> APIRouter:
    router = APIRouter(prefix=UNIFI)
    at = "/sites/{site}"

    def schema(name: str, extra: Optional[Dict[str, Any]] = None, *, settings: bool = False,
               ambiguous_client: bool = False) -> Dict[int | str, Dict[str, Any]]:
        return _ok(apischema.response_schema(name, extra), settings=settings, ambiguous_client=ambiguous_client)

    @router.get("/sites", responses=_ok(INFO_SCHEMA), summary="The controller and its sites")
    def sites(request: Request, refresh: RefreshQ = False) -> JSONResponse:
        return respond(request, lambda client: info_document(client), refresh=refresh)

    @router.get(f"{at}/diagnose", responses=schema("diagnose", settings=True), summary="Health checks (diagnose)")
    def diagnose(request: Request, site: SiteP, only: ListQ = None, skip: ListQ = None, since: SinceQ = "24h",
                 no_events: bool = False, show_ignored: bool = False, refresh: RefreshQ = False) -> JSONResponse:
        seconds = _duration(since)
        try:
            areas = diagnose_areas(argparse.Namespace(only=only or [], skip=skip or [], no_events=no_events))
        except ValueError as error:
            raise ApiError(422, "invalid_parameter", str(error)) from None
        settings, name = _settings(request), checked_site(site)
        return respond(request, lambda client: diagnose_document(client, name, settings, areas, seconds,
                                                                   show_ignored, echo=False), refresh=refresh)

    @router.get(f"{at}/audit", responses=schema("audit", settings=True), summary="Configuration audit")
    def audit(request: Request, site: SiteP, show_ignored: bool = False, refresh: RefreshQ = False) -> JSONResponse:
        settings, name = _settings(request), checked_site(site)
        return respond(request, lambda client: audit_document(client, name, settings, show_ignored, echo=False),
                        refresh=refresh)

    @router.get(f"{at}/firewall", responses=schema("firewall"), summary="Firewall policies, zones and port forwards")
    def firewall(request: Request, site: SiteP, all: bool = False, search: TextQ = "",
                 refresh: RefreshQ = False) -> JSONResponse:
        name = checked_site(site)
        return respond(request, lambda client: firewall_document(client, name, all, search, echo=False),
                        refresh=refresh)

    @router.get(f"{at}/topology", responses=schema("topology", settings=True), summary="The uplink tree")
    def topology(request: Request, site: SiteP, clients: bool = False, refresh: RefreshQ = False) -> JSONResponse:
        settings, name = _settings(request), checked_site(site)
        return respond(request, lambda client: topology_document(client, name, settings, clients, echo=False),
                        refresh=refresh)

    @router.get(f"{at}/wifi", responses=schema("wifi"), summary="Radios and the channel plan")
    def wifi(request: Request, site: SiteP, band: BandQ = None, ap: TextQ = "",
             min_signal: SignalQ = DEFAULT_MIN_SIGNAL, refresh: RefreshQ = False) -> JSONResponse:
        try:
            wanted = parse_band(band) if band else ""
        except ValueError as error:
            raise ApiError(422, "invalid_parameter", str(error)) from None
        name = checked_site(site)
        return respond(request, lambda client: wifi_document(client, name, min_signal, wanted, ap, echo=False),
                        refresh=refresh)

    @router.get(f"{at}/wan", responses=schema("wan"), summary="Internet health")
    def wan(request: Request, site: SiteP, days: DaysQ = DEFAULT_DAYS, refresh: RefreshQ = False) -> JSONResponse:
        settings, name = _settings(request), checked_site(site)
        return respond(request, lambda client: wan_document(client, name, days, settings, echo=False),
                        refresh=refresh)

    @router.get(f"{at}/events", responses=schema("events", apischema.EVENT_NOTES), summary="Event history")
    def events(request: Request, site: SiteP, since: SinceQ = "24h", category: ListQ = None, severity: ListQ = None,
               event: TextQ = "", client: TextQ = "", device: TextQ = "", search: TextQ = "",
               limit: LimitQ = DEFAULT_LIMIT, refresh: RefreshQ = False) -> JSONResponse:
        wanted, name = _event_query(since, category, severity, search), checked_site(site)
        return respond(
            request, lambda api: events_document(api, name, wanted, client, device, event, limit, False, echo=False),
            refresh=refresh, notes=lambda doc: {"truncated": bool(doc.meta["more"]),
                                                "read_cap_reached": bool(doc.meta["cap_truncated"])})

    @router.get(f"{at}/events/summary", responses=schema("events-summary"),
                summary="Event counts over the whole window")
    def events_summary(request: Request, site: SiteP, since: SinceQ = "24h", category: ListQ = None,
                       severity: ListQ = None, event: TextQ = "", client: TextQ = "", device: TextQ = "",
                       search: TextQ = "", refresh: RefreshQ = False) -> JSONResponse:
        wanted, name = _event_query(since, category, severity, search), checked_site(site)
        return respond(request, lambda api: events_document(api, name, wanted, client, device, event,
                                                              DEFAULT_LIMIT, True, echo=False), refresh=refresh)

    @router.get(f"{at}/clients", responses=schema("query-clients"),
                summary="Connected clients (with include_offline, also previously seen ones)")
    def clients(request: Request, site: SiteP, search: TextQ = "", include_offline: bool = False,
                network: OptTextQ = None, ssid: OptTextQ = None, ap: OptTextQ = None,
                refresh: RefreshQ = False) -> JSONResponse:
        for what, value in (("network", network), ("ssid", ssid), ("ap", ap)):
            if value is not None:
                _name(value, what, required=True)
        name = checked_site(site)
        return respond(request, lambda api: query_document(
            api, name, "clients", search, include_offline, network=network, ssid=ssid, ap=ap, echo=False),
            refresh=refresh)

    @router.get(f"{at}/devices", responses=schema("query-devices"), summary="UniFi devices")
    def devices(request: Request, site: SiteP, search: TextQ = "", include_offline: bool = False,
                refresh: RefreshQ = False) -> JSONResponse:
        name = checked_site(site)
        return respond(request, lambda api: query_document(api, name, "devices", search, include_offline,
                                                             echo=False), refresh=refresh)

    @router.get(f"{at}/networks", responses=schema("query-networks"), summary="Networks and VLANs")
    def networks(request: Request, site: SiteP, search: TextQ = "", refresh: RefreshQ = False) -> JSONResponse:
        name = checked_site(site)
        return respond(request, lambda api: query_document(api, name, "networks", search, echo=False),
                        refresh=refresh)

    @router.get(f"{at}/wlans", responses=schema("query-wlans"), summary="Wi-Fi networks")
    def wlans(request: Request, site: SiteP, search: TextQ = "", refresh: RefreshQ = False) -> JSONResponse:
        name = checked_site(site)
        return respond(request, lambda api: query_document(api, name, "wlans", search, echo=False),
                        refresh=refresh)

    @router.get(f"{at}/ports", responses=schema("query-ports"), summary="Switch ports")
    def ports(request: Request, site: SiteP, search: TextQ = "", switch: TextQ = "", down: bool = False,
              errors: bool = False, refresh: RefreshQ = False) -> JSONResponse:
        name = checked_site(site)
        return respond(request, lambda api: query_document(api, name, "ports", search, switch=switch, down=down,
                                                             errors=errors, echo=False), refresh=refresh)

    @router.get(f"{at}/reservations", responses=schema("query-reservations", settings=True),
                summary="DHCP reservations")
    def reservations(request: Request, site: SiteP, search: TextQ = "", offline: bool = False,
                     refresh: RefreshQ = False) -> JSONResponse:
        settings, name = _settings(request), checked_site(site)
        return respond(request, lambda api: query_document(api, name, "reservations", search, offline=offline,
                                                             settings=settings, echo=False), refresh=refresh)

    @router.get(f"{at}/new-clients", responses=schema("new-clients"), summary="Clients in no client group")
    def new_clients(request: Request, site: SiteP, search: TextQ = "", refresh: RefreshQ = False) -> JSONResponse:
        name = checked_site(site)
        return respond(request, lambda api: new_clients_document(api, name, search, echo=False), refresh=refresh)

    @router.get(f"{at}/clients/{{mac}}", responses=schema("client", settings=True, ambiguous_client=True),
                summary="One client by MAC address")
    def client_detail(request: Request, site: SiteP, mac: MacP, events: bool = True, since: SinceQ = "24h",
                      refresh: RefreshQ = False) -> JSONResponse:
        address = normalize_mac(mac)
        if len(address.replace(":", "")) != 12:
            raise ApiError(422, "invalid_parameter", "The MAC address is not valid.")
        seconds = _duration(since)
        settings, name = _settings(request), checked_site(site)
        return respond(request, lambda api: client_document(api, name, address, settings, seconds, events,
                                                              echo=False), refresh=refresh, check=_client_found)

    return router


def build_schema_router() -> APIRouter:
    """``/api/v1/schemas``: the JSON Schemas of the documents, by name (only names that exist are looked up)."""
    router = APIRouter(prefix="/api/v1/schemas")

    @router.get("", summary="The names of the JSON Schemas")
    def schema_names() -> List[str]:
        return apischema.names()

    @router.get("/{name}", summary="One JSON Schema", responses={404: {"description": "No such schema"}})
    def schema_by_name(name: Annotated[str, Path(max_length=MAX_TEXT)]) -> JSONResponse:
        try:
            return JSONResponse(apischema.load(name))
        except KeyError:
            raise ApiError(404, "schema_not_found", "There is no schema with this name.") from None

    return router


def install(app: FastAPI) -> None:
    app.include_router(build_router())
    app.include_router(build_schema_router())
