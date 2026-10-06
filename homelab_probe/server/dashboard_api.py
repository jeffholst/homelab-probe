"""``GET /api/v1/unifi/sites/{site}/dashboard``: how the network is, and how much of that is known.

The document is ``documents.dashboard_document`` (see ``homelab_probe.dashboard``): the counts of ``diagnose`` by
severity and by triage state, the headline of the internet connection and Wi-Fi, the device and client counts and a few
notable events, with ``status``, ``complete``, ``stale`` and per-section ``available`` so a partial, old or failed read
never reads as healthy. Both roles read it; it never writes (the triage file is read, and one that cannot be used only
makes the counts by state ``null``) and it makes no request the full ``diagnose`` does not. A controller that cannot be
reached gives **200** with every section unavailable and ``controller.state`` saying why (a dashboard shows that); a
failure that is not about the connection (no such site, an answer that cannot be used) is the usual 404 or 502.
"""

from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..documents import dashboard_document
from . import apischema, findings_api
from .cache import stale_served
from .errors import error_responses
from .findings_api import store_for
from .routes import UNIFI, RefreshQ, SiteP, checked_site, effective_settings, respond

SUMMARY = "How the network is: findings, internet, Wi-Fi, devices, clients and recent events"


def router() -> APIRouter:
    api = APIRouter(prefix=f"{UNIFI}/sites/{{site}}/dashboard")
    responses: Dict[int | str, Dict[str, Any]] = {
        200: {"description": "The summary with when it was read",
              "content": {"application/json": {"schema": apischema.response_schema("dashboard")}}},
        **error_responses(401, 404, 422, 500, 502, 504, text={
            404: "No such site", 502: "The controller answered, but not with data that could be used"}),
    }

    @api.get("", summary=SUMMARY, responses=responses)
    def dashboard(request: Request, site: SiteP, refresh: RefreshQ = False) -> JSONResponse:
        settings, name = effective_settings(request), checked_site(site)
        return respond(request, lambda client: dashboard_document(
            client, name, settings, triage=lambda record: store_for(request, record).load(), stale=stale_served,
            echo=False, now=findings_api.CLOCK()), refresh=refresh)

    return api
