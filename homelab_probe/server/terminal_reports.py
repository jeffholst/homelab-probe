"""Fixed, read-only document adapters and renderers. No CLI handlers are invoked."""

import argparse
from typing import Any, Callable, cast

from fastapi import Request

from .. import documents
from ..client_view import render_detail
from ..diagnose import render_findings
from ..events import render_events_text
from ..firewall import render_text as render_firewall
from ..new_clients import render_table as render_new_clients
from ..query import render_table
from ..topology import render_text as render_topology
from ..wan import render_text as render_wan
from ..wifi import render_text as render_wifi
from .errors import ApiError
from .report_validation import areas, checked_site, event_query
from .routes import effective_settings
from .terminal_policy import ParsedCommand

MAX_EVENTS = 2000
MAX_SINCE = 14 * 86400
MAX_DAYS = 3650


def validate(command: ParsedCommand, request: Request) -> None:
    """Apply web-specific ranges before any document/controller call."""
    args = command.args
    if command.operation in ("help", "version"):
        return
    args.site = checked_site(args.site or request.app.state.config.site)
    for field in ("search", "ap", "network", "ssid", "switch", "query", "event", "client", "device"):
        if len(getattr(args, field, None) or "") > 120:
            raise ApiError(422, "invalid_parameter", "A report filter exceeds 120 characters.")
    if hasattr(args, "since") and not 1 <= args.since <= MAX_SINCE:
        raise ApiError(422, "invalid_parameter", "The event window must be between one second and 14 days.")
    if command.operation == "events":
        args.wanted = event_query(f"{args.since // 60}m", args.category, args.severity, args.search)
        args.limit = MAX_EVENTS if args.limit == 0 else min(args.limit, MAX_EVENTS)
    if command.operation == "wan" and args.days > MAX_DAYS:
        raise ApiError(422, "invalid_parameter", "The day count must be between 1 and 3650.")
    if command.operation == "diagnose":
        args.areas = areas(args.only, args.skip, args.no_events)


def adapter(operation: str, args: argparse.Namespace, request: Request) -> Callable[[Any], documents.Document]:
    """Only these reviewed callables may build a report; the input never names a Python function."""
    settings = effective_settings(request) if operation in {
        "diagnose", "audit", "wan", "topology", "client"} or (
            operation == "query" and args.offline) else None
    site = args.site
    builders: dict[str, Callable[[Any], documents.Document]] = {
        "info": lambda c: documents.info_document(c),
        "diagnose": lambda c: documents.diagnose_document(
            c, site, settings, args.areas, args.since, args.show_ignored, echo=False),
        "audit": lambda c: documents.audit_document(c, site, settings, args.show_ignored, echo=False),
        "wan": lambda c: documents.wan_document(c, site, args.days, settings, echo=False),
        "wifi": lambda c: documents.wifi_document(c, site, args.min_signal, args.band or "", args.ap, echo=False),
        "topology": lambda c: documents.topology_document(c, site, settings, args.clients, echo=False),
        "firewall": lambda c: documents.firewall_document(c, site, args.all, args.search, echo=False),
        "events": lambda c: documents.events_document(
            c, site, args.wanted, args.client, args.device, args.event, args.limit, args.summary, echo=False),
        "query": lambda c: documents.query_document(
            c, site, args.kind, args.search, args.include_offline, args.switch or "", args.down, args.errors,
            args.offline, settings, args.network, args.ssid, args.ap, echo=False),
        "new-clients": lambda c: documents.new_clients_document(c, site, args.search, echo=False),
        "client": lambda c: documents.client_document(c, site, args.query, settings, args.since,
                                                     not args.no_events, echo=False),
    }
    return builders[operation]


def render(operation: str, args: argparse.Namespace, document: documents.Document) -> str:
    data = document.data
    renderers: dict[str, Callable[[], str]] = {
        "info": lambda: "\n".join([f"Application: {data['application']}", *[
            f"Site: {s['name']} ref={s['ref']} id={s['id']}" for s in data["sites"]]]),
        "diagnose": lambda: render_findings(data, False, args.show_ignored),
        "audit": lambda: render_findings(data, False, args.show_ignored),
        "wan": lambda: render_wan(data),
        "wifi": lambda: render_wifi(data, args.all, args.ap),
        "topology": lambda: render_topology(data, False, args.clients),
        "firewall": lambda: render_firewall(data, args.zones, False,
                                            zone_names=cast(documents.FirewallDocument, document).zone_names),
        "events": lambda: render_events_text(data, args.summary, document.meta["more"], document.meta["cap_truncated"]),
        "query": lambda: render_table(data, args.kind, args.offline),
        "new-clients": lambda: render_new_clients(data),
        "client": lambda: render_detail(data, False),
    }
    return renderers[operation]()
