"""Service test fixtures and synthetic factories for shared adaptive planning.

Standard-library only (Python 3.10+ compatible). Does not require external SDK dependencies.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
import uuid

from adaptive_planning.types import (
    ArtifactRef,
    AttemptId,
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
    ResultVersion,
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
        interval="",
        since_revision=0,
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
        self._temporary_owner: Any = None
        self._temporary_identity: tuple[int, int] | None = None
        self._temporary_closed = False

    def cleanup(self) -> None:
        """Close only the temporary directory created and owned by this fixture."""
        if self._temporary_owner is not None and not self._temporary_closed:
            self._temporary_closed = True
            self._temporary_owner.cleanup()

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
            if plan_id is None and attempt.committed_metadata.plan_id:
                plan_id = attempt.committed_metadata.plan_id

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


def seed_orphan_use(fixture: ServiceFixture, resource_id: ResourceId) -> ReservationView:
    """Test-only Store.write seed of an unresolved, store-scoped host writer."""
    from adaptive_planning import plans, resources
    from adaptive_planning.types import DomainError

    def insert(db):
        row = db.execute("SELECT kind, repository_id FROM resources WHERE resource_id=?", (resource_id,)).fetchone()
        if row is None:
            raise DomainError("not_found", "The test resource is not registered")
        kind, parent = row
        if kind == "repository":
            execution = ExecutionContext("repository", resource_id, None, True, (), True, None)
        elif kind == "external":
            execution = ExecutionContext("external-only", None, None, False, (resource_id,), True, None)
        else:
            execution = ExecutionContext("repository" if parent else "non-repository", parent,
                                         resource_id, False, (), True, None)
        canonical = resources.resolve_context(db, execution)[0]
        host = db.execute("SELECT host_id FROM installation WHERE id=1").fetchone()[0]
        use_id, ts = str(uuid.uuid4()), fixture.clock().isoformat()
        db.execute("INSERT INTO absent_host_work (absent_work_id, host_id, resource_id, scope, declared_identity, "
                   "status, created_at) VALUES ('seeded-orphan', ?, ?, ?, 'synthetic-orphan', 'unresolved', ?)",
                   (host, canonical, kind, ts))
        db.execute("INSERT INTO resource_uses (use_id, resource_id, plan_id, attempt_id, absent_work_id, scope, "
                   "status, created_at) VALUES (?, ?, NULL, NULL, 'seeded-orphan', ?, 'unresolved', ?)",
                   (use_id, canonical, kind, ts))
        plans._insert_event(db, None, None, "seeded_orphan_use", "synthetic-orphan", ts,
                            {"use_id": use_id, "resource_id": canonical})
        return ReservationView(use_id, None, None, "seeded-orphan", (canonical,), "synthetic-orphan", True, True,
                               kind == "repository")
    with plans.service_context("http://127.0.0.1:8790", fixture.clock):
        return fixture.store.write(insert)


@dataclass(frozen=True)
class SeededResult:
    """TEST-ONLY reference to synthetic accepted history; grants no command authority."""

    plan_id: PlanId
    step_id: StepId
    attempt_id: AttemptId
    result_version: ResultVersion
    artifact: ArtifactRef


def seed_accepted_result(
    fixture: ServiceFixture, plan_id: PlanId, step_id: StepId, artifact: ArtifactRef,
) -> SeededResult:
    """TEST-ONLY historical seed in a live, owned temporary complete-task store.

    No service acceptance/checkpoint/review operation is invoked, no capability is
    issued, and no command execution or human approval is asserted.
    """
    import json
    from tempfile import TemporaryDirectory

    from adaptive_planning import core, plans
    from adaptive_planning.store import Store
    from adaptive_planning.types import DomainError, _to_artifact_ref, _to_checkpoint, _to_review

    def require_owned_store():
        if (not isinstance(fixture, ServiceFixture)
                or not isinstance(fixture._temporary_owner, TemporaryDirectory)
                or fixture._temporary_closed or fixture._temporary_identity is None):
            raise DomainError("invalid", "Synthetic history requires a live fixture-owned temporary directory")
        try:
            owned = Path(fixture._temporary_owner.name).resolve(strict=True)
            current_identity = owned.stat()
            directory = fixture.directory.resolve(strict=True)
            if not isinstance(fixture.store, Store):
                raise DomainError("invalid", "Synthetic history requires the fixture's actual Store")
            database = fixture.store.path.resolve(strict=True)
        except (OSError, TypeError, AttributeError) as error:
            raise DomainError("invalid", "The owned temporary fixture is no longer valid") from error
        if ((current_identity.st_dev, current_identity.st_ino) != fixture._temporary_identity
                or not owned.is_dir() or directory != owned or database != owned / "planning.sqlite"
                or getattr(fixture.service, "_store", None) is not fixture.store
                or fixture.store.host_id != "synthetic-host"):
            raise DomainError("invalid", "Synthetic history must use the same owned temporary fixture and database")
        return database

    database = require_owned_store()
    artifact = _to_artifact_ref(plans.to_jsonable(artifact))

    def insert(db):
        # Recheck ownership before any SQL mutation and bind the actual connection.
        if require_owned_store() != database or not any(
                name == "main" and Path(path).resolve() == database
                for _, name, path in db.execute("PRAGMA database_list")):
            raise DomainError("invalid", "The seed connection does not belong to the temporary fixture")
        plan = plans._load_plan(db, plan_id)
        if plan.authorization.mode != "complete-task":
            raise DomainError("forbidden", "Synthetic accepted history requires complete-task scope")
        previous = plans.project_plan(db, plan_id)
        try:
            core.validate_published_snapshot(previous.legacy_snapshot)
        except core.PlanValidationError as error:
            raise DomainError("invalid", "Synthetic seed requires strictly valid existing history") from error
        target = next((node for node in plan.steps if node.step_id == step_id), None)
        if target is None:
            raise DomainError("not_found", "The synthetic seed step does not belong to this plan")
        if target.state != "pending" or db.execute(
                f"SELECT 1 FROM attempts WHERE plan_id=? AND step_id=? AND state IN ({plans._LIVE_SQL})",
                (plan_id, step_id)).fetchone():
            raise DomainError("invalid", "Synthetic history requires a pending step without a live attempt")
        steps = {node.step_id: node for node in plan.steps}
        if any(steps[dependency].state != "complete" or not steps[dependency].evidence
               for dependency in target.definition["dependencies"]):
            raise DomainError("invalid", "Synthetic accepted history requires completed prerequisites with evidence")

        worker, reviewer = "synthetic-history-worker", "synthetic-history-reviewer"
        check = target.definition["completion_check"]
        limitation = ("Synthetic test history; no command execution or human approval is asserted.",)
        evidence = Evidence("Synthetic passing historical checkpoint", artifact, None, check, "passed", limitation, ())
        review_evidence = Evidence("Synthetic passing distinct-reviewer history", artifact, None, check,
                                   "passed", limitation, ())
        checkpoint_value = _to_checkpoint(plans.to_jsonable(Checkpoint(
            "passed", check, (evidence,), Reassessment("Synthetic unchanged reassessment", (), (), "unchanged",
                                                      "Synthetic historical result", (), False, None),
            worker, None, artifact)))
        review_value = _to_review(plans.to_jsonable(Review(reviewer, artifact, "passed", (), (review_evidence,), None)))
        attempt_id, result_version, review_id = (str(uuid.uuid4()) for _ in range(3))
        ts, revision = plans._timestamp(plan.updated_at), plan.revision + 1
        artifact_json = plans.to_jsonable(artifact)
        links = {"synthetic_history": True, "step_id": step_id, "attempt_id": attempt_id,
                 "result_version": result_version, "artifact": artifact_json}
        attempt_payload = {
            "epoch": plans._installation_epoch(db), "generation": plan.generation,
            "definition": copy.deepcopy(target.definition), "context": plans.to_jsonable(context(None, writes=False)),
            "last_activity": ts, "recovery_required": False, "prior_state": "submitted",
            "result_version": result_version, "artifact": artifact_json, "synthetic_history": True,
        }

        def encoded(value):
            return json.dumps(plans.to_jsonable(value), sort_keys=True, ensure_ascii=False)

        db.execute("INSERT INTO attempts(plan_id,attempt_id,step_id,assignee,state,version,definition_version,"
                   "started_at,stopped_at,payload) VALUES(?,?,?,?,'accepted',4,?,?,?,?)",
                   (plan_id, attempt_id, step_id, worker, target.definition["definition_version"], ts, ts,
                    encoded(attempt_payload)))
        plans._insert_event(db, plan_id, None, "synthetic_history_checkpoint", worker, ts,
                            {**links, "summary": "Synthetic passing historical checkpoint"})
        checkpoint_sequence = plans._store_sequence(db)
        db.execute("UPDATE events SET origin_worker=? WHERE sequence=?", (worker, checkpoint_sequence))
        db.execute("INSERT INTO checkpoints(plan_id,attempt_id,sequence,outcome,check_name,evidence,origin_worker,"
                   "relay,artifact,reassessment,created_at) VALUES(?,?,?,'passed',?,?,?,NULL,?,?,?)",
                   (plan_id, attempt_id, checkpoint_sequence, check, encoded(checkpoint_value.evidence), worker,
                    encoded(artifact), encoded(checkpoint_value.reassessment), ts))
        db.execute("INSERT INTO reviews(review_id,plan_id,attempt_id,step_id,reviewer,verdict,findings,evidence,"
                   "artifact,verification_use_id,created_at) VALUES(?,?,?,?,?,'passed','[]',?,?,NULL,?)",
                   (review_id, plan_id, attempt_id, step_id, reviewer, encoded(review_value.evidence), encoded(artifact), ts))
        plans._insert_event(db, plan_id, None, "synthetic_history_review", reviewer, ts,
                            {**links, "review_id": review_id, "summary": "Synthetic matching distinct-reviewer history"})
        review_sequence = plans._store_sequence(db)
        db.execute("INSERT INTO result_versions(plan_id,result_version,step_id,artifact_kind,identity,summary,digest,"
                   "created_at) VALUES(?,?,?,?,?,?,?,?)",
                   (plan_id, result_version, step_id, artifact.kind, artifact.identity, artifact.summary, artifact.digest, ts))
        raw = db.execute("SELECT payload FROM steps WHERE plan_id=? AND step_id=?", (plan_id, step_id)).fetchone()[0]
        step_payload = json.loads(raw)
        step_payload["evidence"] = list(target.evidence) + [plans.format_evidence(evidence), plans.format_evidence(review_evidence)]
        db.execute("UPDATE steps SET state='complete',payload=? WHERE plan_id=? AND step_id=?",
                   (encoded(step_payload), plan_id, step_id))
        # The draft/Authorization bytes remain unchanged; only materialized revision/time/status advance.
        db.execute("UPDATE plans SET revision=?,updated_at=? WHERE plan_id=?", (revision, ts, plan_id))
        plans._insert_event(db, plan_id, revision, "synthetic_history_accepted_result", "synthetic-history-fixture", ts,
                            {**links, "checkpoint_sequence": checkpoint_sequence, "review_sequence": review_sequence,
                             "review_id": review_id, "summary": "Synthetic accepted-result history"})
        accepted_sequence = plans._store_sequence(db)
        attempt_payload.update(checkpoint_sequence=checkpoint_sequence, review_sequence=review_sequence,
                               accepted_sequence=accepted_sequence)
        db.execute("UPDATE attempts SET payload=? WHERE plan_id=? AND attempt_id=?",
                   (encoded(attempt_payload), plan_id, attempt_id))

        # Validate the assembled relational links, not just renderer projection.
        rows = db.execute(
            "SELECT a.step_id,a.state,a.definition_version,a.payload,c.sequence,c.outcome,c.check_name,c.evidence,"
            "c.origin_worker,c.artifact,r.reviewer,r.verdict,r.artifact,r.evidence,v.step_id "
            "FROM attempts a JOIN checkpoints c ON c.plan_id=a.plan_id AND c.attempt_id=a.attempt_id "
            "JOIN reviews r ON r.plan_id=a.plan_id AND r.attempt_id=a.attempt_id AND r.step_id=a.step_id "
            "JOIN result_versions v ON v.plan_id=a.plan_id AND v.step_id=a.step_id AND v.result_version=? "
            "WHERE a.plan_id=? AND a.attempt_id=? AND r.review_id=?",
            (result_version, plan_id, attempt_id, review_id)).fetchall()
        expected = (step_id, "accepted", target.definition["definition_version"], encoded(attempt_payload),
                    checkpoint_sequence, "passed", check, encoded(checkpoint_value.evidence), worker, encoded(artifact),
                    reviewer, "passed", encoded(artifact), encoded(review_value.evidence), step_id)
        events = db.execute("SELECT sequence,plan_revision,payload FROM events WHERE plan_id=? "
                            "AND sequence IN (?,?,?) ORDER BY sequence",
                            (plan_id, checkpoint_sequence, review_sequence, accepted_sequence)).fetchall()
        if (rows != [expected] or reviewer == worker or len(events) != 3
                or [row[1] for row in events] != [None, None, revision]
                or any(any(json.loads(row[2]).get(key) != value for key, value in links.items()) for row in events)
                or db.execute("PRAGMA foreign_key_check").fetchall()):
            raise DomainError("invalid", "The synthetic accepted history is not coherent")
        view = plans._commit_projection(db, plan_id, previous)
        try:
            core.validate_published_snapshot(view.legacy_snapshot)
        except core.PlanValidationError as error:
            raise DomainError("invalid", "Synthetic accepted history is not a strict published snapshot") from error
        if view.draft.authorization != plan.authorization:
            raise DomainError("invalid", "Synthetic history changed execution authorization")
        return SeededResult(plan_id, step_id, attempt_id, result_version, artifact)

    with plans.service_context("http://127.0.0.1:8790", fixture.clock):
        return fixture.store.write(insert)


def make_service(directory: Path | None = None, clock: FakeClock | None = None) -> ServiceFixture:
    """Lazy-import the service; omitted directory creates an owned synthetic temporary store."""
    if clock is None:
        clock = FakeClock()

    # Lazy import service components
    from adaptive_planning.app import PlanningService
    from adaptive_planning.store import Store

    owner = None
    if directory is None:
        from tempfile import TemporaryDirectory
        owner = TemporaryDirectory(prefix="adaptive-planning-synthetic-")
        directory = Path(owner.name).resolve()
    try:
        store = Store(directory / "planning.sqlite", clock, host_id="synthetic-host")
        service = PlanningService(store, base_url="http://127.0.0.1:8790", clock=clock)
        epoch = store.installation_epoch()
        fixture = ServiceFixture(service=service, store=store, directory=directory, clock=clock, epoch=epoch)
        if owner is not None:
            identity = directory.stat()
            fixture._temporary_owner = owner
            fixture._temporary_identity = (identity.st_dev, identity.st_ino)
        return fixture
    except BaseException:
        if owner is not None:
            owner.cleanup()
        raise
