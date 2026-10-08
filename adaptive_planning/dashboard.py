"""Read-only HTML and public JSON over the service's committed projections."""
from __future__ import annotations

import re
from html import escape
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import BaseRoute, Route

from adaptive_planning import plans as projections
from adaptive_planning.types import DomainError, PlanView

if TYPE_CHECKING:
    from adaptive_planning.app import PlanningService


CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self' data:; font-src 'self'; object-src 'none'; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'none'")
HEADERS = {"Cache-Control": "no-store", "Content-Security-Policy": CSP,
           "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"}


def _asset(name: str) -> bytes:
    """Task 9 includes these same assets in the package; source checkouts reuse the original tree."""
    installed = resources.files("adaptive_planning").joinpath("assets", "dashboard", name)
    if installed.is_file():
        return installed.read_bytes()
    return (Path(__file__).resolve().parent.parent / "assets" / "dashboard" / name).read_bytes()


def _plan_link(plan_id: str) -> str:
    return "/plans/" + quote(plan_id, safe="")


def render_plan_index(plans: tuple[PlanView, ...]) -> str:
    cards = []
    for view in plans:
        workflow = ""
        if view.draft.workflow is not None:
            workflow = '<p class="workflow-info">Workflow: ' + escape(
                "; ".join(f"{key}: {value}" for key, value in view.draft.workflow.items())) + "</p>"
        cards.append(
            '<article class="plan-card"><h2><a href="' + escape(_plan_link(view.plan_id), quote=True) + '">'
            + escape(view.draft.objective) + '</a></h2><p class="plan-card-meta">'
            + escape(view.status) + " · Rev #" + str(view.metadata.revision) + '</p><p>Published: <time>'
            + escape(view.published_at) + '</time></p><code class="task-id-code">' + escape(view.plan_id)
            + "</code>" + workflow + "</article>")
    content = "".join(cards) or '<p class="empty-message">No committed plans.</p>'
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        '<title>Shared plans</title><link rel="stylesheet" href="/style.css"></head>'
        '<body data-mode="service" data-plans-url="/api/plans"><div class="app-container">'
        '<header class="app-header"><div class="header-top"><h1>Shared plans</h1>'
        '<div id="conn-pill" class="status-pill status-pill--connecting" role="status">'
        '<span id="conn-label">Connecting…</span></div></div>'
        '<p>Read-only dashboard of committed publications. Host edits are not controlled by this service.</p>'
        '<div id="reconnect-banner" class="banner banner--warning" hidden role="alert">'
        '<span id="reconnect-text"></span></div></header>'
        '<main id="plan-list" class="plan-list" aria-label="Committed plans">' + content + '</main></div>'
        '<script src="/app.js"></script></body></html>')


def _bounded_json(value) -> JSONResponse:
    response = JSONResponse(projections.to_jsonable(value), headers=HEADERS)
    if len(response.body) > projections.MAX_PAGE_BYTES:
        raise DomainError("limit_exceeded", "The public result exceeds the response limit")
    return response


def _public_read(operation):
    try:
        return operation()
    except DomainError as error:
        # Use the transport's existing typed, constant-message projection. Never expose exception text.
        from adaptive_planning.transport import _error_payload
        payload = _error_payload(error.code, error.current_metadata)
        code = payload["error"]["code"]
        if code == "internal_error":
            payload = {"error": {"code": code, "message": "The request could not be completed."}}
        status = {"not_found": 404, "conflict": 409, "stale": 409, "stale_epoch": 409,
                  "busy": 503, "limit_exceeded": 413, "internal_error": 500}.get(code, 400)
        return JSONResponse(payload, status_code=status, headers=HEADERS)


def _query(request, allowed: set[str]) -> dict[str, str]:
    items = list(request.query_params.multi_items())
    values = dict(items)
    if len(values) != len(items) or set(values) - allowed:
        raise DomainError("invalid", "Query parameters must be unique and supported")
    return values


def _integer(values: dict[str, str], name: str, default: int) -> int:
    if name not in values:
        return default
    value = values[name]
    if not re.fullmatch(r"-?[0-9]+", value):
        raise DomainError("invalid", "Query parameters must be integers")
    try:
        return int(value)
    except ValueError:
        raise DomainError("invalid", "Query integer is too large") from None


def dashboard_routes(service: PlanningService) -> list[BaseRoute]:
    def index(request):
        def read():
            views = service.list_plans(include_terminal=True)
            _bounded_json({"plans": views})  # Preserve the aggregate public limit before rendering any subset.
            return HTMLResponse(render_plan_index(views), headers=HEADERS)
        return _public_read(read)

    def plan_page(request):
        def read():
            view = service.read_plan(request.path_params["plan_id"])
            base = "/api/plans/" + quote(view.plan_id, safe="")
            attrs = {"mode": "service", "plan-id": view.plan_id, "plan-url": base,
                     "coordination-url": base + "/coordination", "events-url": base + "/events"}
            body = "<body " + " ".join(f'data-{key}="{escape(value, quote=True)}"'
                                          for key, value in attrs.items()) + ">"
            template = _asset("index.html").decode("utf-8")
            if '<body data-mode="legacy">' not in template:
                raise RuntimeError("Packaged dashboard body marker is missing")
            return HTMLResponse(template.replace('<body data-mode="legacy">', body, 1), headers=HEADERS)
        return _public_read(read)

    def api_plans(request):
        def read():
            _query(request, set())
            return _bounded_json({"plans": service.list_plans(include_terminal=True)})
        return _public_read(read)

    def api_plan(request):
        def read():
            _query(request, set())
            return _bounded_json(service.read_plan(request.path_params["plan_id"]))
        return _public_read(read)

    def api_coordination(request):
        def read():
            values = _query(request, {"after", "limit"})
            return _bounded_json(service.read_coordination(request.path_params["plan_id"],
                                 after=values.get("after"), limit=_integer(values, "limit", 100)))
        return _public_read(read)

    def api_events(request):
        def read():
            values = _query(request, {"after", "limit"})
            return _bounded_json({"events": service.read_events(request.path_params["plan_id"],
                                  after=_integer(values, "after", 0), limit=_integer(values, "limit", 100))})
        return _public_read(read)

    def script(request):
        return Response(_asset("app.js"), media_type="text/javascript", headers=HEADERS)

    def stylesheet(request):
        return Response(_asset("style.css"), media_type="text/css", headers=HEADERS)

    return [Route("/", index, methods=["GET", "HEAD"]),
            Route("/plans/{plan_id}", plan_page, methods=["GET", "HEAD"]),
            Route("/api/plans", api_plans, methods=["GET", "HEAD"]),
            Route("/api/plans/{plan_id}", api_plan, methods=["GET", "HEAD"]),
            Route("/api/plans/{plan_id}/coordination", api_coordination, methods=["GET", "HEAD"]),
            Route("/api/plans/{plan_id}/events", api_events, methods=["GET", "HEAD"]),
            Route("/app.js", script, methods=["GET", "HEAD"]),
            Route("/style.css", stylesheet, methods=["GET", "HEAD"])]
