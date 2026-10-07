"""Service test fixtures and synthetic factories for shared adaptive planning.

Standard-library only (Python 3.10+ compatible). Does not require external SDK dependencies.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
import uuid

from adaptive_planning.types import (
    ArtifactRef,
    AttemptView,
    Authorization,
    Checkpoint,
    CommandEnvelope,
    CommandKind,
    CommandPayload,
    CoordinationView,
    DocumentRef,
    Evidence,
    ExecutionContext,
    HoldView,
    Metadata,
    MutationReceipt,
    PlanDraft,
    PlanId,
    PrivateGrant,
    Reassessment,
    ReservationView,
    ResourceClaim,
    ResourceId,
    Review,
    ReviewView,
    StepDefinition,
    StepId,
    JSONValue,
    BOOTSTRAP_KINDS,
    WORKER_KINDS,
    ORCHESTRATOR_KINDS,
    parse_command,
)


class FakeClock:
    """Controllable timezone-aware UTC clock for testing."""

    def __init__(self, start: datetime | None = None) -> None:
        if start is None:
            self._current = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)
        else:
            if start.tzinfo is None:
                self._current = start.replace(tzinfo=timezone.utc)
            else:
                self._current = start.astimezone(timezone.utc)

    def __call__(self) -> datetime:
        return self._current

    def advance(self, seconds: int) -> None:
        from datetime import timedelta
        self._current += timedelta(seconds=seconds)


def step(
    step_id: str,
    dependencies: tuple[str, ...] = (),
    *,
    review_required: bool = False,
    final_gate: bool = False,
) -> StepDefinition:
    """Create a synthetic StepDefinition fixture."""
    return StepDefinition(
        step_id=step_id,
        title=f"Step {step_id}",
        dependencies=dependencies,
        completion_check=f"Check for step {step_id}",
        document=None,
        definition_version="v1",
        review_required=review_required,
        final_gate=final_gate,
    )


def draft(
    mode: str = "complete-task",
    steps: tuple[StepDefinition, ...] | None = None,
) -> PlanDraft:
    """Create a synthetic PlanDraft fixture defaulting to independent a, b and dependent c on a."""
    if steps is None:
        steps = (
            step("a"),
            step("b"),
            step("c", dependencies=("a",)),
        )
    auth = Authorization(
        mode=mode,  # type: ignore[arg-type]
        interval="synthetic-interval-0",
        since_revision=1,
        note="Synthetic authorization for testing",
        baseline=(),
        claimed=None,
    )
    return PlanDraft(
        objective="Synthetic plan draft objective",
        acceptance_criteria=("Acceptance criterion 1", "Acceptance criterion 2"),
        constraints=("Constraint 1",),
        facts=("Fact 1",),
        assumptions=("Assumption 1",),
        next_check="Check next step",
        authorization=auth,
        steps=steps,
        documents=(),
        workspace_refs=(),
        workflow=None,
        horizon=10,
        stop_reason=None,
        block_reason=None,
    )


def workspace(
    path: str,
    inode: int,
    *,
    repository_id: str | None = None,
    case_insensitive: bool = False,
) -> ResourceClaim:
    """Create a synthetic ResourceClaim fixture using synthetic-host and device 7."""
    return ResourceClaim(
        host_id="synthetic-host",
        kind="workspace",
        device=7,
        inode=inode,
        canonical_path=path,
        case_insensitive=case_insensitive,
        repository_id=repository_id,
        external_identity=None,
    )


def context(
    workspace_id: str | None,
    *,
    repository_id: str | None = None,
    external_ids: tuple[str, ...] = (),
    writes: bool = True,
) -> ExecutionContext:
    """Create a synthetic ExecutionContext fixture."""
    if not writes:
        kind = "read-only"
    elif repository_id:
        kind = "repository"
    elif workspace_id:
        kind = "non-repository"
    elif external_ids:
        kind = "external-only"
    else:
        kind = "read-only"
    return ExecutionContext(
        kind=kind,
        repository_id=repository_id,
        workspace_id=workspace_id,
        repository_exclusive=False,
        external_ids=external_ids,
        writes=writes,
        checked_base=None,
    )


def checkpoint(
    outcome: str = "passed",
    *,
    affected: tuple[str, ...] = (),
    unbounded: bool = False,
    artifact_version: str = "v1",
    relay: str | None = None,
) -> Checkpoint:
    """Create a synthetic Checkpoint fixture."""
    art = ArtifactRef(
        kind="code",
        identity="synthetic-artifact",
        version=artifact_version,
        digest=None,
        summary="Synthetic artifact output",
    )
    ev = Evidence(
        summary="Synthetic evidence summary",
        artifact=art,
        workspace_id=None,
        check="Synthetic checkpoint check",
        result=outcome,  # type: ignore[arg-type]
        limitations=(),
        blockers=(),
    )
    reassess = Reassessment(
        summary="Synthetic reassessment",
        facts=(),
        assumptions=(),
        disposition="material" if (affected or unbounded) else "unchanged",
        reason="Synthetic reassessment effects" if (affected or unbounded) else "Synthetic unchanged",
        affected_steps=affected,
        unbounded=unbounded,
        proposed_draft=None,
    )
    return Checkpoint(
        outcome=outcome,  # type: ignore[arg-type]
        check="Synthetic checkpoint check",
        evidence=(ev,),
        reassessment=reassess,
        origin_worker="worker-a",
        relay=relay,
        artifact=art,
    )


def legacy_proposal() -> dict[str, JSONValue]:
    """Copy of the valid_v2_proposal fixture from TestPlanValidationV2."""
    return {
        "schemaVersion": 2,
        "taskId": "task-test-01",
        "objective": "Verify the v2 test harness",
        "status": "planning",
        "executionMode": "first-step",
        "nextStepId": "step-1",
        "changeSummary": "Initial v2 plan configuration",
        "steps": [
            {
                "id": "step-1",
                "title": "Set up database schema",
                "status": "pending",
                "dependsOn": [],
                "check": "Database migrations execute with exit code 0",
                "evidence": [],
            },
            {
                "id": "step-2",
                "title": "Seed baseline data",
                "status": "pending",
                "dependsOn": ["step-1"],
                "check": "User count equals 10",
                "evidence": [],
            },
        ],
    }


def coordination(
    attempts: Sequence[AttemptView] = (),
    holds: Sequence[HoldView] = (),
    store_hold: bool = False,
) -> CoordinationView:
    """Create a CoordinationView fixture with empty views/no store hold/store_sequence=0."""
    att_tuple = tuple(attempts)
    hold_tuple = tuple(holds)
    return CoordinationView(
        attempts=att_tuple,
        reservations=(),
        holds=hold_tuple,
        reviews=(),
        result_versions={},
        store_hold=store_hold,
        restore_cutoff=None,
        ownership_actor="synthetic-orchestrator",
        ownership_version=1,
        store_sequence=0,
        next_cursor=None,
        total_counts={"attempts": len(att_tuple), "holds": len(hold_tuple)},
        has_live_attempts=any(a.state in ("assigned", "active") for a in att_tuple),
        has_unresolved_uses=False,
        has_plan_holds=len(hold_tuple) > 0,
    )


class ServiceFixture:
    """Fixture wrapping an active PlanningService with synthetic factory helpers."""

    def __init__(
        self,
        service: Any,
        store: Any,
        directory: Path,
        clock: FakeClock,
        epoch: str,
    ) -> None:
        self.service = service
        self.store = store
        self.directory = directory
        self.clock = clock
        self.epoch = epoch
        self._cached_grants: dict[str, PrivateGrant] = {}
        self._cached_attempt_grants: dict[str, PrivateGrant] = {}

    def command(
        self,
        kind: CommandKind,
        payload: CommandPayload,
        *,
        receipt: MutationReceipt | None = None,
        attempt: MutationReceipt | None = None,
        key: str | None = None,
        expected_revision: int | None = None,
        refresh_metadata: bool = True,
    ) -> CommandEnvelope:
        """Build an immutable, deep-copied CommandEnvelope using normative kind/relay rules."""
        req_key = key if key is not None else uuid.uuid4().hex
        actor = "worker-a" if kind in WORKER_KINDS else "synthetic-orchestrator"

        # Metadata resolution
        plan_id: PlanId | None = None
        generation: str | None = None
        exp_rev: int | None = expected_revision
        ownership_version: int | None = None
        attempt_id: str | None = None
        attempt_version: int | None = None
        definition_version: str | None = None
        cmd_epoch: str = self.epoch

        if receipt is not None:
            plan_id = receipt.committed_metadata.plan_id
            if receipt.private and plan_id:
                self._cached_grants[plan_id] = receipt.private
            cmd_epoch = receipt.committed_metadata.epoch
            generation = receipt.committed_metadata.generation
            if exp_rev is None:
                exp_rev = receipt.committed_metadata.revision
            ownership_version = receipt.committed_metadata.ownership_version

        if attempt is not None:
            attempt_id = attempt.committed.get("attempt_id")
            attempt_version = attempt.committed.get("version", 1)
            definition_version = attempt.committed.get("definition_version", "v1")
            if attempt.private and attempt_id:
                self._cached_attempt_grants[attempt_id] = attempt.private

        # Dynamic metadata refresh from live service if requested
        if refresh_metadata:
            # Invokes normative service.read_service_info directly and propagates errors/missing protocol
            info = self.service.read_service_info()
            cmd_epoch = info.epoch
            self.epoch = info.epoch

            # Bootstrap commands read only installation info
            if kind not in BOOTSTRAP_KINDS and plan_id is not None:
                plan_view = self.service.read_plan(plan_id)
                pm = plan_view.metadata
                generation = pm.generation
                if expected_revision is None:
                    exp_rev = pm.revision
                # Ownership version is strictly preserved from the caller's receipt/grant

                # When own attempt scoped, invoke read_coordination directly and paginate
                if attempt_id is not None:
                    found_attempt = False
                    cursor: str | None = None
                    while True:
                        coord_page = (
                            self.service.read_coordination(plan_id, after=cursor)
                            if cursor is not None
                            else self.service.read_coordination(plan_id)
                        )
                        for item in coord_page.items:
                            if item.kind == "attempt":
                                val = item.value
                                if val.attempt_id == attempt_id:
                                    attempt_version = val.version
                                    found_attempt = True
                                    break
                        if found_attempt:
                            break
                        next_c = coord_page.next_cursor
                        if not next_c:
                            break
                        cursor = next_c
                    if not found_attempt:
                        raise ValueError(f"Attempt '{attempt_id}' not found in coordination for plan '{plan_id}'")

        # Determine tokens based on normative matrix
        orchestrator_token: str | None = None
        attempt_token: str | None = None

        if kind in BOOTSTRAP_KINDS:
            orchestrator_token = None
            attempt_token = None
        elif kind in WORKER_KINDS:
            relay = None
            if isinstance(payload, (dict, Mapping)):
                if kind == "record_checkpoint":
                    cp_val = payload.get("checkpoint")
                    if isinstance(cp_val, Checkpoint):
                        relay = cp_val.relay
                    elif isinstance(cp_val, (dict, Mapping)):
                        relay = cp_val.get("relay")
                else:
                    relay = payload.get("relay")

            if relay is not None:
                if receipt and receipt.private and receipt.private.orchestrator_token:
                    orchestrator_token = receipt.private.orchestrator_token
                elif plan_id and plan_id in self._cached_grants:
                    orchestrator_token = self._cached_grants[plan_id].orchestrator_token
            else:
                orchestrator_token = None

            if attempt and attempt.private and attempt.private.attempt_token:
                attempt_token = attempt.private.attempt_token
            elif attempt_id and attempt_id in self._cached_attempt_grants:
                attempt_token = self._cached_attempt_grants[attempt_id].attempt_token
        elif kind in ORCHESTRATOR_KINDS:
            if receipt and receipt.private and receipt.private.orchestrator_token:
                orchestrator_token = receipt.private.orchestrator_token
            elif plan_id and plan_id in self._cached_grants:
                orchestrator_token = self._cached_grants[plan_id].orchestrator_token
            attempt_token = None

        raw_cmd: dict[str, Any] = {
            "kind": kind,
            "epoch": cmd_epoch,
            "key": req_key,
            "actor": actor,
            "plan_id": plan_id,
            "generation": generation,
            "expected_revision": exp_rev,
            "ownership_version": ownership_version,
            "orchestrator_token": orchestrator_token,
            "attempt_id": attempt_id,
            "attempt_token": attempt_token,
            "attempt_version": attempt_version,
            "definition_version": definition_version,
            "payload": payload,
        }
        return parse_command(raw_cmd)

    def create(
        self,
        mode: str = "complete-task",
        steps: tuple[StepDefinition, ...] | None = None,
        creation_id: str = "fixture-create",
        key: str | None = None,
    ) -> MutationReceipt:
        """Create a plan with synthetic intent and cached grant."""
        p_draft = draft(mode=mode, steps=steps)
        payload = {
            "creation_id": creation_id,
            "draft": p_draft,
            "new_plan_intent": "Synthetic new-plan intent for fixture",
        }
        cmd = self.command("create_plan", payload, key=key)
        receipt = self.service.execute(cmd)
        if receipt.private and receipt.current_metadata.plan_id:
            self._cached_grants[receipt.current_metadata.plan_id] = receipt.private
        return receipt

    def register(self, claim: ResourceClaim, receipt: MutationReceipt) -> ResourceId:
        """Register a resource claim and return committed resource_id."""
        cmd = self.command("register_resource", {"claim": claim}, receipt=receipt)
        res = self.service.execute(cmd)
        return res.committed["resource_id"]

    def assign(self, receipt: MutationReceipt, step_id: str = "a") -> MutationReceipt:
        """Assign a step using read-only context and worker-a."""
        ctx = context(None, writes=False)
        cmd = self.command(
            "assign_step",
            {"step_id": step_id, "assignee": "worker-a", "context": ctx},
            receipt=receipt,
        )
        res = self.service.execute(cmd)
        if res.private and res.committed.get("attempt_id"):
            self._cached_attempt_grants[res.committed["attempt_id"]] = res.private
        return res

    def start(self, receipt: MutationReceipt, attempt: MutationReceipt) -> MutationReceipt:
        """Start a step attempt."""
        cmd = self.command(
            "start_step",
            {"origin_worker": "worker-a", "relay": None, "observed_activity": "started"},
            receipt=receipt,
            attempt=attempt,
        )
        return self.service.execute(cmd)

    def submit(
        self,
        receipt: MutationReceipt,
        attempt: MutationReceipt,
        value: Checkpoint | None = None,
    ) -> MutationReceipt:
        """Submit a checkpoint for an attempt."""
        cp = value if value is not None else checkpoint()
        cmd = self.command(
            "record_checkpoint",
            {"checkpoint": cp},
            receipt=receipt,
            attempt=attempt,
        )
        return self.service.execute(cmd)

    def accept(
        self,
        receipt: MutationReceipt,
        attempt: MutationReceipt,
        value: Checkpoint,
    ) -> MutationReceipt:
        """Accept a completed step without fabricating stop confirmation."""
        step_id = attempt.committed.get("step_id", "a")
        artifact = value.artifact or ArtifactRef(
            kind="code", identity="synthetic-artifact", version="v1", digest=None, summary="accepted"
        )
        cmd = self.command(
            "accept_step",
            {
                "step_id": step_id,
                "artifact": artifact,
                "stopped_resource_ids": (),
                "stopped_reason": None,
            },
            receipt=receipt,
        )
        return self.service.execute(cmd)


def make_service(directory: Path, clock: FakeClock | None = None) -> ServiceFixture:
    """Lazy-import and initialize PlanningService for testing."""
    if clock is None:
        clock = FakeClock()

    # Lazy import service components
    from adaptive_planning.app import PlanningService
    from adaptive_planning.store import Store

    store = Store(directory / "planning.sqlite", clock, host_id="synthetic-host")
    service = PlanningService(store, base_url="http://127.0.0.1:8790", clock=clock)
    epoch = store.installation_epoch()
    return ServiceFixture(service=service, store=store, directory=directory, clock=clock, epoch=epoch)
