"""Read-only HTML and public JSON over the service's committed projections."""
from __future__ import annotations

import re
from html import escape
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, urlencode

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


def _plan_card(plan_id: str, objective: str, status: str, revision: int, published_at: str,
               workflow: str = "", attributes: str = "") -> str:
    return (
        '<article class="plan-card"' + attributes + '><h2><a href="' + escape(_plan_link(plan_id), quote=True) + '">'
        + escape(objective) + '</a></h2><p class="plan-card-meta">'
        + escape(status) + " · Rev #" + str(revision) + '</p><p>Published: <time>'
        + escape(published_at) + '</time></p><code class="task-id-code">' + escape(plan_id)
        + "</code>" + workflow + "</article>")


def render_plan_index(plans: tuple[PlanView, ...]) -> str:
    cards = []
    for view in plans:
        workflow = ""
        if view.draft.workflow is not None:
            workflow = '<p class="workflow-info">Workflow: ' + escape(
                "; ".join(f"{key}: {value}" for key, value in view.draft.workflow.items())) + "</p>"
        cards.append(_plan_card(
            view.plan_id, view.draft.objective, view.status, view.metadata.revision, view.published_at, workflow))
    content = "".join(cards) or '<p class="empty-message">No committed plans.</p>'
    return _index_document(content, "/api/plans")


def _index_document(content: str, plans_url: str, navigation: str | None = None) -> str:
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        '<title>Shared plans</title><link rel="stylesheet" href="/style.css"></head>'
        '<body data-mode="service" data-plans-url="' + escape(plans_url, quote=True)
        + '"><div class="app-container">'
        '<header class="app-header"><div class="header-top"><h1>Shared plans</h1>'
        '<div id="conn-pill" class="status-pill status-pill--connecting" role="status">'
        '<span id="conn-label">Connecting…</span></div></div>'
        '<p>Read-only dashboard of committed publications. Host edits are not controlled by this service.</p>'
        '<div id="reconnect-banner" class="banner banner--warning" hidden role="alert">'
        '<span id="reconnect-text"></span></div></header>'
        '<main id="plan-list" class="plan-list" aria-label="Committed plans">' + content + '</main>'
        + (navigation or "") + '</div><script src="/app.js"></script></body></html>')


def _index_query(limit: int | None, cursor: tuple[str, str] | None = None) -> str:
    pairs = ([cursor] if cursor is not None else []) + ([("limit", str(limit))] if limit is not None else [])
    return urlencode(pairs)


def _index_target(path: str, limit: int | None, cursor: tuple[str, str] | None = None) -> str:
    query = _index_query(limit, cursor)
    return path + "?" + query if query else path


def _index_navigation(links: list[tuple[str, str, str]]) -> str:
    anchors = "".join('<a id="plan-index-' + key + '" href="' + escape(href, quote=True) + '">' + label + "</a>"
                      for key, label, href in links)
    return '<nav id="plan-index-nav" class="plan-index-nav" aria-label="Plan pages">' + anchors + "</nav>"


def render_compact_plan_index(page: dict, limit: int | None = None, cursor: tuple[str, str] | None = None) -> str:
    """Root page over one compact index page; navigation sits outside the replaced plan-list region."""
    cards = "".join(
        _plan_card(item["plan_id"], item["objective"], item["status"], item["revision"], item["published_at"],
                   attributes=' data-plan-id="' + escape(item["plan_id"], quote=True) + '"')
        for item in page["plans"])
    if not cards:
        cards = ('<p class="empty-message">No committed plans.</p>' if page["collection_fence"]["count"] == 0
                 else '<p class="empty-message">No plans on this page.</p>')
    nav_limit = projections.PLAN_INDEX_DEFAULT if limit is None else limit
    links = []
    if page["previous_cursor"] is not None:
        links.append(("first", "First", _index_target("/", nav_limit)))
        links.append(("newer", "Newer", _index_target("/", nav_limit, ("before", page["previous_cursor"]))))
    if page["next_cursor"] is not None:
        links.append(("older", "Older", _index_target("/", nav_limit, ("after", page["next_cursor"]))))
    return _index_document(cards, _index_target("/api/plan-index", limit, cursor), _index_navigation(links))


def render_plan_index_restart(limit: int | None = None, cursor: tuple[str, str] | None = None) -> str:
    """Readable root page for a continuation that no longer matches the plan collection."""
    notice = ('<p class="index-notice" role="alert">The plan list changed since this page was opened. '
              "Restart from the first page to continue.</p>")
    plans_url = _index_target("/api/plan-index", limit, cursor)
    limit = projections.PLAN_INDEX_DEFAULT if limit is None else limit
    return _index_document(notice, plans_url,
                           _index_navigation([("first", "First", _index_target("/", limit))]))


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


def _plan_index_arguments(values: dict[str, str]) -> tuple[str | None, str | None, int]:
    if "after" in values and "before" in values:
        raise DomainError("invalid", "after and before are mutually exclusive")
    return values.get("after"), values.get("before"), _integer(values, "limit", projections.PLAN_INDEX_DEFAULT)


def dashboard_routes(service: PlanningService) -> list[BaseRoute]:
    def measurements_page(request):
        def read():
            from adaptive_planning.measurement_dashboard import COHORTS, render_measurements
            is_api = request.url.path == "/api/measurements"
            allowed = {"plan_id", "after", "limit"} | (set() if is_api else {"cohort"})
            values = _query(request, allowed)
            cohort = values.get("cohort", "all" if values.get("plan_id") else "operational")
            if cohort not in COHORTS:
                raise DomainError("invalid", "Unsupported measurement cohort")
            data = service.read_measurements(values.get("plan_id"), _integer(values, "after", 0), _integer(values, "limit", 100))
            response = _bounded_json(data)  # Bound the full report before cohort filtering.
            if is_api:
                return response
            return HTMLResponse(render_measurements(data, cohort, values), headers=HEADERS)
        return _public_read(read)

    def index(request):
        def read():
            values = _query(request, {"limit", "after", "before"})
            after, before, limit = _plan_index_arguments(values)
            chosen = limit if "limit" in values else None
            cursor = ("after", after) if after is not None else ("before", before) if before is not None else None
            try:
                page = service.read_plan_index(after=after, before=before, limit=limit)
            except DomainError as error:
                if error.code != "conflict":
                    raise
                return HTMLResponse(render_plan_index_restart(chosen, cursor), status_code=409, headers=HEADERS)
            _bounded_json(page)  # Preserve the public response limit before rendering any subset.
            return HTMLResponse(render_compact_plan_index(page, chosen, cursor), headers=HEADERS)
        return _public_read(read)

    def plan_page(request):
        def read():
            view = service.read_plan(request.path_params["plan_id"])
            base = "/api/plans/" + quote(view.plan_id, safe="")
            attrs = {"mode": "service", "plan-id": view.plan_id, "plan-url": base,
                     "coordination-url": base + "/coordination", "events-url": base + "/events",
                     "recovery-warnings-url": base + "/recovery-warnings"}
            body = "<body " + " ".join(f'data-{key}="{escape(value, quote=True)}"'
                                          for key, value in attrs.items()) + ">"
            template = _asset("index.html").decode("utf-8")
            if '<body data-mode="legacy">' not in template:
                raise RuntimeError("Packaged dashboard body marker is missing")
            marker = '<p id="recovery-state"></p>'
            if template.count(marker) != 1:
                raise RuntimeError("Packaged dashboard recovery marker is missing or ambiguous")
            template = template.replace(marker, marker + '\n        <p id="recovery-warnings" role="status">'
                                        'Recorded inactivity warnings unavailable.</p>', 1)
            return HTMLResponse(template.replace('<body data-mode="legacy">', body, 1), headers=HEADERS)
        return _public_read(read)

    def api_plans(request):
        def read():
            _query(request, set())
            return _bounded_json({"plans": service.list_plans(include_terminal=True)})
        return _public_read(read)

    def api_plan_index(request):
        def read():
            after, before, limit = _plan_index_arguments(_query(request, {"limit", "after", "before"}))
            return _bounded_json(service.read_plan_index(after=after, before=before, limit=limit))
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

    def api_recovery_warnings(request):
        def read():
            _query(request, set())
            response = _bounded_json(service.read_recovery_warnings(request.path_params["plan_id"]))
            if request.method == "HEAD":
                return Response(headers=dict(response.headers))
            return response
        return _public_read(read)

    def script(request):
        return Response(_asset("app.js"), media_type="text/javascript", headers=HEADERS)

    def stylesheet(request):
        return Response(_asset("style.css"), media_type="text/css", headers=HEADERS)

    def measurements_stylesheet(request):
        return Response(_asset("measurements.css"), media_type="text/css", headers=HEADERS)

    return [Route("/", index, methods=["GET", "HEAD"]),
            Route("/measurements", measurements_page, methods=["GET", "HEAD"]),
            Route("/api/measurements", measurements_page, methods=["GET", "HEAD"]),
            Route("/plans/{plan_id}", plan_page, methods=["GET", "HEAD"]),
            Route("/api/plans", api_plans, methods=["GET", "HEAD"]),
            Route("/api/plan-index", api_plan_index, methods=["GET", "HEAD"]),
            Route("/api/plans/{plan_id}", api_plan, methods=["GET", "HEAD"]),
            Route("/api/plans/{plan_id}/coordination", api_coordination, methods=["GET", "HEAD"]),
            Route("/api/plans/{plan_id}/events", api_events, methods=["GET", "HEAD"]),
            Route("/api/plans/{plan_id}/recovery-warnings", api_recovery_warnings, methods=["GET", "HEAD"]),
            Route("/app.js", script, methods=["GET", "HEAD"]),
            Route("/measurements.css", measurements_stylesheet, methods=["GET", "HEAD"]),
            Route("/style.css", stylesheet, methods=["GET", "HEAD"])]
