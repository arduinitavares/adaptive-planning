"""Shared planning service: idempotent command execution and immutable, bounded reads."""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Callable

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount

from adaptive_planning.admin import load_service_token,resolve_store_hold
from adaptive_planning.config import Config, DEFAULT_STALE_SECONDS, RequestBoundary, http_allowlists

from adaptive_planning import attempts, checkpoints, imports, plans, recovery, resources, reviews
from adaptive_planning.ownership import command_relay, require_kind_capabilities, require_orchestrator
from adaptive_planning.store import Store,store_is_held
from adaptive_planning.types import (
    Command,
    CoordinationPage,
    DomainError,
    Event,
    InstallationView,
    MutationReceipt,
    PlanId,
    PlanView,
    ReadyStep,
    WORKER_KINDS,
)

# Mutations share one transaction and the same domain authentication/replay authority.
HANDLERS: dict[str, Callable[[sqlite3.Connection, Command], MutationReceipt]] = {
    "resolve_store_hold": resolve_store_hold,
    "create_plan": plans.create_plan,
    "import_plan": imports.import_plan,
    "revise_plan": plans.revise_plan,
    "takeover": plans.takeover,
    "register_resource": resources.register_resource,
    "reconcile_resources": resources.reconcile_resources,
    "assign_step": attempts.assign_step,
    "bind_availability": attempts.bind_availability,
    "start_step": attempts.start_step,
    "report_activity": attempts.report_activity,
    "record_checkpoint": checkpoints.record_checkpoint,
    "resolve_reassessment": checkpoints.resolve_reassessment,
    "invalidate_result": checkpoints.invalidate_result,
    "resolve_invalidation": checkpoints.resolve_invalidation,
    "record_review": reviews.record_review,
    "accept_step": attempts.accept_step,
    "reserve_verification": reviews.reserve_verification,
    "release_resource_use": reviews.release_resource_use,
    "reopen_attempt": attempts.reopen_attempt,
    "sweep_stale": recovery.sweep_stale,
    "resolve_attempt": recovery.resolve_attempt,
    "append_late_evidence": recovery.append_late_evidence,
}


class PlanningService:
    def __init__(self, store: Store, *, base_url: str, clock: Callable[[], datetime],
                 stale_seconds: int = DEFAULT_STALE_SECONDS) -> None:
        if isinstance(stale_seconds, bool) or not isinstance(stale_seconds, int) or stale_seconds < 1:
            raise DomainError("invalid", "stale_seconds must be a positive integer")
        self._store = store
        self._base_url = base_url.rstrip("/")
        self._clock = clock
        self._stale_seconds = stale_seconds

    def _run_write(self, operation: Callable[[sqlite3.Connection], object]) -> object:
        with plans.service_context(self._base_url, self._clock), attempts.activity_policy(self._stale_seconds):
            return self._store.write(operation)

    def _run_read(self, operation: Callable[[sqlite3.Connection], object]) -> object:
        with plans.service_context(self._base_url, self._clock):
            return self._store.read(operation)

    # ------------------------------------------------------------------ commands
    def execute(self, command: Command) -> MutationReceipt:
        return self._run_write(lambda db: self._execute(db, command))  # type: ignore[return-value]

    def _execute(self, db: sqlite3.Connection, command: Command) -> MutationReceipt:
        epoch = db.execute("SELECT epoch FROM installation WHERE id = 1").fetchone()[0]
        if command.epoch != epoch:
            raise DomainError("stale_epoch", "The installation epoch has changed; re-read service info")
        if command.kind not in HANDLERS:
            raise DomainError("unsupported", f"Command kind {command.kind!r} is not enabled")
        require_kind_capabilities(
            command.kind, command_relay(command), command.orchestrator_token, command.attempt_token)
        # The matrix leaves the orchestrator token only on orchestrator and relayed commands. Authentication
        # of the current owner, epoch and generation precedes exact replay, which precedes new-command checks.
        try:
            if command.kind in WORKER_KINDS:
                attempts.require_attempt(db, command)
            elif command.orchestrator_token is not None:
                require_orchestrator(db, command)
            replayed = plans.replay_request(db, command)
            if replayed is not None:
                replayed = imports.import_replay(db, command, replayed)
                return recovery.replacement_replay(db, command, replayed)
            receipt = HANDLERS[command.kind](db, command)
        except DomainError as error:
            if error.current_metadata is None and error.code in ("stale", "conflict") and command.plan_id:
                try:
                    error.current_metadata = plans._metadata(db, command.plan_id)
                except DomainError:
                    pass
            raise
        db.execute(
            "INSERT INTO requests (request_id, epoch, key, command_digest, plan_id, response_payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), epoch, command.key, plans.command_digest(command),
             receipt.committed_metadata.plan_id,
             plans.encode_response(imports.stored_receipt(db, command, receipt)), self._clock().isoformat()))
        return receipt

    # ------------------------------------------------------------------ reads
    def read_service_info(self) -> InstallationView:
        def run(db: sqlite3.Connection) -> InstallationView:
            epoch, version, host_id, hold, cutoff = db.execute(
                "SELECT epoch, schema_version, host_id, store_hold, restore_cutoff FROM installation WHERE id = 1"
            ).fetchone()
            return InstallationView(epoch=epoch, schema_version=version, host_id=host_id,
                                    dashboard_url=f"{self._base_url}/", store_hold=store_is_held(db),
                                    restore_cutoff=cutoff)
        return self._run_read(run)  # type: ignore[return-value]

    def list_plans(self, include_terminal: bool = False) -> tuple[PlanView, ...]:
        return self._run_read(lambda db: plans.list_plans(db, include_terminal))  # type: ignore[return-value]

    def read_plan(self, plan_id: PlanId) -> PlanView:
        return self._run_read(lambda db: plans.project_plan(db, plan_id))  # type: ignore[return-value]

    def read_plan_index(
        self, after: str | None = None, before: str | None = None, limit: int = plans.PLAN_INDEX_DEFAULT,
    ) -> dict:
        return self._run_read(  # type: ignore[return-value]
            lambda db: plans.read_plan_index(db, after, before, limit))

    def list_ready(self, plan_id: PlanId) -> tuple[ReadyStep, ...]:
        return self._run_read(lambda db: attempts.ready_steps(db, plan_id))  # type: ignore[return-value]

    def read_events(
        self, plan_id: PlanId, after: int = 0, limit: int = plans.EVENTS_LIMIT_DEFAULT,
    ) -> tuple[Event, ...]:
        return self._run_read(lambda db: plans.read_events(db, plan_id, after, limit))  # type: ignore[return-value]

    def read_coordination(
        self, plan_id: PlanId, after: str | None = None, limit: int = plans.COORDINATION_PAGE_DEFAULT,
    ) -> CoordinationPage:
        return self._run_read(  # type: ignore[return-value]
            lambda db: plans.read_coordination_page(db, plan_id, after, limit))

    def read_recovery_warnings(self, plan_id: PlanId) -> dict:
        return self._run_read(  # type: ignore[return-value]
            lambda db: recovery.recovery_warnings(db, plan_id, self._clock(), self._stale_seconds))


def build_app(config: Config) -> Starlette:
    """Compose one service authority, its read-only dashboard and the official MCP app."""
    # Credential loading precedes even Store construction: ordinary startup never initializes it.
    token = load_service_token(config)
    clock = lambda: datetime.now(timezone.utc)
    store = Store(config.data_dir / "planning.sqlite", clock)
    try:
        return _compose_app(config, token, clock, store)
    except BaseException:
        store.close()
        raise


def _compose_app(config, token, clock, store):
    service = PlanningService(store, base_url=config.lan_base_url or config.base_url, clock=clock,
                              stale_seconds=config.stale_seconds)
    from adaptive_planning.transport import MCPHttpErrors, MCPServerDiagnostics, build_mcp, mcp_server_diagnostics
    from adaptive_planning.dashboard import dashboard_routes
    server = build_mcp(service)
    allowed_pairs = http_allowlists(config)
    mcp_app = server.streamable_http_app(
        streamable_http_path="/mcp", json_response=True, stateless_http=True,
        max_request_body_size=12 * 1024 * 1024,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                    allowed_hosts=[host for host, _ in allowed_pairs],
                                                    allowed_origins=[origin for _, origin in allowed_pairs]),
        host="0.0.0.0",
    )

    @asynccontextmanager
    async def lifespan(app):
        try:
            with mcp_server_diagnostics():
                async with server.session_manager.run():
                    yield
        finally:
            store.close()

    async def public_server_error(request, error):
        # Starlette can answer before re-raising to the outer RequestBoundary. Ignore
        # exception text and use the boundary's same constant public failure shape.
        return JSONResponse({"error": {"code": "internal_error", "message": "The request could not be completed."}},
                            status_code=500, headers={"Cache-Control": "no-store"})

    app = Starlette(routes=[*dashboard_routes(service),
                            Mount("/", app=MCPHttpErrors(MCPServerDiagnostics(mcp_app)))], lifespan=lifespan,
                    exception_handlers={500: public_server_error})
    app.state.service = service
    app.state.mcp = server
    app.state.close = store.close
    # Starlette normally places ServerErrorMiddleware outside user middleware. Wrap the
    # complete eagerly built stack so every route and error path meets the same boundary.
    app.middleware_stack = RequestBoundary(app.build_middleware_stack(), config, token)
    return app
