"""Shared planning service: idempotent command execution and immutable, bounded reads."""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from typing import Callable

from adaptive_planning import plans
from adaptive_planning.ownership import command_relay, require_kind_capabilities, require_orchestrator
from adaptive_planning.store import Store
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
)

# Only planning and ownership mutations are enabled; every other kind is rejected before any state is touched.
HANDLERS: dict[str, Callable[[sqlite3.Connection, Command], MutationReceipt]] = {
    "create_plan": plans.create_plan,
    "revise_plan": plans.revise_plan,
    "takeover": plans.takeover,
}


class PlanningService:
    def __init__(self, store: Store, *, base_url: str, clock: Callable[[], datetime]) -> None:
        self._store = store
        self._base_url = base_url.rstrip("/")
        self._clock = clock

    def _run_write(self, operation: Callable[[sqlite3.Connection], object]) -> object:
        with plans.service_context(self._base_url, self._clock):
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
            if command.orchestrator_token is not None:
                require_orchestrator(db, command)
            replayed = plans.replay_request(db, command)
            if replayed is not None:
                return replayed
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
             receipt.committed_metadata.plan_id, plans.encode_response(receipt), self._clock().isoformat()))
        return receipt

    # ------------------------------------------------------------------ reads
    def read_service_info(self) -> InstallationView:
        def run(db: sqlite3.Connection) -> InstallationView:
            epoch, version, host_id, hold, cutoff = db.execute(
                "SELECT epoch, schema_version, host_id, store_hold, restore_cutoff FROM installation WHERE id = 1"
            ).fetchone()
            reasons = db.execute(
                "SELECT EXISTS(SELECT 1 FROM store_hold_reasons WHERE epoch = ? AND resolved = 0)",
                (epoch,)).fetchone()[0]
            return InstallationView(epoch=epoch, schema_version=version, host_id=host_id,
                                    dashboard_url=f"{self._base_url}/", store_hold=bool(hold or reasons),
                                    restore_cutoff=cutoff)
        return self._run_read(run)  # type: ignore[return-value]

    def list_plans(self, include_terminal: bool = False) -> tuple[PlanView, ...]:
        return self._run_read(lambda db: plans.list_plans(db, include_terminal))  # type: ignore[return-value]

    def read_plan(self, plan_id: PlanId) -> PlanView:
        return self._run_read(lambda db: plans.project_plan(db, plan_id))  # type: ignore[return-value]

    def list_ready(self, plan_id: PlanId) -> tuple[ReadyStep, ...]:
        return self._run_read(lambda db: plans.list_ready(db, plan_id))  # type: ignore[return-value]

    def read_events(
        self, plan_id: PlanId, after: int = 0, limit: int = plans.EVENTS_LIMIT_DEFAULT,
    ) -> tuple[Event, ...]:
        return self._run_read(lambda db: plans.read_events(db, plan_id, after, limit))  # type: ignore[return-value]

    def read_coordination(
        self, plan_id: PlanId, after: str | None = None, limit: int = plans.COORDINATION_PAGE_DEFAULT,
    ) -> CoordinationPage:
        return self._run_read(  # type: ignore[return-value]
            lambda db: plans.read_coordination_page(db, plan_id, after, limit))
