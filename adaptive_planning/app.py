"""Shared planning service: idempotent command execution and immutable, bounded reads."""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from typing import Callable

from adaptive_planning import plans
from adaptive_planning.ownership import authenticate_orchestrator
from adaptive_planning.store import Store
from adaptive_planning.types import (
    BOOTSTRAP_KINDS,
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

# Only planning mutations are enabled; every other kind is rejected before any state is touched.
HANDLERS: dict[str, Callable[[sqlite3.Connection, Command], MutationReceipt]] = {
    "create_plan": plans.create_plan,
    "revise_plan": plans.revise_plan,
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
        handler = HANDLERS.get(command.kind)
        if handler is None:
            raise DomainError("unsupported", f"Command kind {command.kind!r} is not enabled")
        if command.kind not in BOOTSTRAP_KINDS:
            authenticate_orchestrator(db, command, self._clock())
        digest = plans.command_digest(command)

        stored = db.execute(
            "SELECT command_digest, response_payload, plan_id FROM requests WHERE epoch = ? AND key = ?",
            (epoch, command.key)).fetchone()
        if stored is not None:
            if stored[0] != digest:
                raise DomainError("conflict", f"Request key {command.key!r} was already used for a different command")
            committed, committed_metadata, private = plans.decode_response(stored[1])
            return MutationReceipt(committed, committed_metadata, plans._metadata(db, stored[2]), True, private)

        try:
            receipt = handler(db, command)
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
            (str(uuid.uuid4()), epoch, command.key, digest, receipt.committed_metadata.plan_id,
             plans.encode_response(receipt), self._clock().isoformat()))
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
