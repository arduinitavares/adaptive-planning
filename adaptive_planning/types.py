"""Typed data contracts, DTOs, command definitions, and errors for shared adaptive planning.

Standard-library only (Python 3.10+ compatible). Does not require external SDK dependencies.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Literal, Mapping, Sequence, TypedDict, Union


# Primitive string aliases
PlanId = str
StepId = str
AttemptId = str
ResourceId = str
ResultVersion = str
DefinitionVersion = str
Epoch = str
Generation = str
RequestKey = str

# JSON-safe recursive value type alias
JSONValue = Union[None, bool, int, float, str, list[Any], dict[str, Any]]

# Clock returning timezone-aware UTC datetime
Clock = Callable[[], datetime]


# Exact disjoint frozensets of command kinds
BOOTSTRAP_KINDS: frozenset[str] = frozenset({
    "create_plan",
    "import_plan",
    "takeover",
})

WORKER_KINDS: frozenset[str] = frozenset({
    "start_step",
    "report_activity",
    "record_checkpoint",
})

ORCHESTRATOR_KINDS: frozenset[str] = frozenset({
    "revise_plan",
    "register_resource",
    "reconcile_resources",
    "invalidate_result",
    "resolve_invalidation",
    "assign_step",
    "bind_availability",
    "resolve_reassessment",
    "reopen_attempt",
    "sweep_stale",
    "record_review",
    "accept_step",
    "resolve_attempt",
    "reserve_verification",
    "release_resource_use",
    "append_late_evidence",
    "resolve_store_hold",
})

CommandKind = Literal[
    "create_plan",
    "revise_plan",
    "takeover",
    "register_resource",
    "reconcile_resources",
    "invalidate_result",
    "resolve_invalidation",
    "assign_step",
    "bind_availability",
    "start_step",
    "report_activity",
    "record_checkpoint",
    "resolve_reassessment",
    "reopen_attempt",
    "sweep_stale",
    "record_review",
    "accept_step",
    "resolve_attempt",
    "reserve_verification",
    "release_resource_use",
    "append_late_evidence",
    "import_plan",
    "resolve_store_hold",
]


# Shared DTOs (frozen dataclasses)
@dataclass(frozen=True)
class InstallationView:
    epoch: Epoch
    schema_version: int
    host_id: str
    dashboard_url: str
    store_hold: bool
    restore_cutoff: str | None


@dataclass(frozen=True)
class Metadata:
    epoch: Epoch
    plan_id: PlanId
    generation: Generation
    revision: int
    ownership_version: int
    dashboard_url: str


@dataclass(frozen=True)
class ArtifactRef:
    kind: Literal["code", "document", "data", "observation"]
    identity: str
    version: str
    digest: str | None
    summary: str


@dataclass(frozen=True)
class DocumentRef:
    path: str
    branch: str | None
    commit: str | None
    version: str
    task_id: StepId | None


@dataclass(frozen=True)
class StepDefinition:
    step_id: StepId
    title: str
    dependencies: tuple[StepId, ...]
    completion_check: str
    document: DocumentRef | None
    definition_version: DefinitionVersion
    review_required: bool
    final_gate: bool


@dataclass(frozen=True)
class Authorization:
    mode: Literal["plan-only", "first-step", "complete-task"]
    interval: str
    since_revision: int
    note: str
    baseline: tuple[StepId, ...]
    claimed: StepId | None


@dataclass(frozen=True)
class RetirementRequest:
    step_id: StepId
    reason: str


@dataclass(frozen=True)
class ReservationView:
    use_id: str
    plan_id: PlanId | None
    attempt_id: AttemptId | None
    absent_work_id: str | None
    resource_ids: tuple[ResourceId, ...]
    actor: str
    writer_active: bool
    unresolved: bool
    repository_exclusive: bool


@dataclass(frozen=True)
class AuthorizationChange:
    mode: Literal["plan-only", "first-step", "complete-task"]
    note: str


@dataclass(frozen=True)
class ResourceClaim:
    host_id: str
    kind: Literal["repository", "workspace", "external"]
    device: int | None
    inode: int | None
    canonical_path: str | None
    case_insensitive: bool
    repository_id: ResourceId | None
    external_identity: str | None


@dataclass(frozen=True)
class ExecutionContext:
    kind: Literal["repository", "non-repository", "external-only", "read-only"]
    repository_id: ResourceId | None
    workspace_id: ResourceId | None
    repository_exclusive: bool
    external_ids: tuple[ResourceId, ...]
    writes: bool
    checked_base: str | None


@dataclass(frozen=True)
class ExecutionContextClaims:
    kind: Literal["repository", "non-repository", "external-only", "read-only"]
    repository_claim: ResourceClaim | None
    workspace_claim: ResourceClaim | None
    repository_exclusive: bool
    external_claims: tuple[ResourceClaim, ...]
    writes: bool
    checked_base: str | None


@dataclass(frozen=True)
class Evidence:
    summary: str
    artifact: ArtifactRef | None
    workspace_id: ResourceId | None
    check: str
    result: Literal["passed", "failed", "blocked", "observation"]
    limitations: tuple[str, ...]
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class HostWork:
    work_id: str
    actor: str
    state: Literal["running", "stopped", "unknown"]
    resource_ids: tuple[ResourceId, ...]
    resource_claims: tuple[ResourceClaim, ...]
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class ResourceResolution:
    use_id: str | None
    absent_work_id: str | None
    resource_ids: tuple[ResourceId, ...]
    resolution: Literal["stopped", "isolated", "unresolved"]
    reason: str


@dataclass(frozen=True)
class PlanDraft:
    objective: str
    acceptance_criteria: tuple[str, ...]
    constraints: tuple[str, ...]
    facts: tuple[str, ...]
    assumptions: tuple[str, ...]
    next_check: str
    authorization: Authorization
    steps: tuple[StepDefinition, ...]
    documents: tuple[DocumentRef, ...]
    workspace_refs: tuple[ResourceId, ...]
    workflow: dict[str, str] | None
    horizon: int = 10
    stop_reason: str | None = None
    block_reason: str | None = None


@dataclass(frozen=True)
class Reassessment:
    summary: str
    facts: tuple[str, ...]
    assumptions: tuple[str, ...]
    disposition: Literal["unchanged", "material"]
    reason: str
    affected_steps: tuple[StepId, ...]
    unbounded: bool
    proposed_draft: PlanDraft | None


@dataclass(frozen=True)
class Checkpoint:
    outcome: Literal["passed", "failed", "blocked"]
    check: str
    evidence: tuple[Evidence, ...]
    reassessment: Reassessment
    origin_worker: str
    relay: str | None
    artifact: ArtifactRef | None


@dataclass(frozen=True)
class Availability:
    prerequisite: StepId
    result_version: ResultVersion
    method: Literal["ancestor", "merge", "cherry-pick", "equivalent", "observation"]
    checked_base: str
    artifact_digest: str | None
    evidence: Evidence


@dataclass(frozen=True)
class Review:
    reviewer: str
    artifact: ArtifactRef
    verdict: Literal["passed", "failed"]
    findings: tuple[str, ...]
    evidence: tuple[Evidence, ...]
    verification_use_id: str | None


@dataclass(frozen=True)
class ReviewView:
    plan_id: PlanId
    step_id: StepId
    attempt_id: AttemptId
    definition_version: DefinitionVersion
    recorded_sequence: int
    review: Review


@dataclass(frozen=True)
class AttemptView:
    attempt_id: AttemptId
    step_id: StepId
    assignee: str
    state: Literal["assigned", "active", "submitted", "accepted", "rejected", "released", "stale"]
    prior_state: str | None
    version: int
    definition_version: DefinitionVersion
    last_activity: str
    context: ExecutionContext | None
    recovery_required: bool
    result_version: ResultVersion | None


@dataclass(frozen=True)
class HoldView:
    hold_id: str
    kind: Literal["reassessment", "invalidation", "stale", "identity"]
    step_id: StepId | None
    source_id: str
    reason: str


@dataclass(frozen=True)
class InvalidationView:
    invalidation_id: str
    producer_step_id: StepId
    result_version: ResultVersion
    affected_steps: tuple[StepId, ...]
    resolved_steps: tuple[StepId, ...]
    recorded_sequence: int


@dataclass(frozen=True)
class CoordinationItem:
    kind: Literal["attempt", "reservation", "review", "hold", "invalidation"]
    value: Union[AttemptView, ReservationView, ReviewView, HoldView, InvalidationView]


@dataclass(frozen=True)
class CoordinationPage:
    plan_id: PlanId
    revision: int
    store_sequence: int
    items: tuple[CoordinationItem, ...]
    next_cursor: str | None
    total_counts: dict[str, int]


@dataclass(frozen=True)
class CoordinationView:
    attempts: tuple[AttemptView, ...]
    reservations: tuple[ReservationView, ...]
    holds: tuple[HoldView, ...]
    reviews: tuple[ReviewView, ...]
    result_versions: dict[StepId, ResultVersion]
    store_hold: bool
    restore_cutoff: str | None
    ownership_actor: str
    ownership_version: int
    store_sequence: int
    next_cursor: str | None
    total_counts: dict[str, int]
    has_live_attempts: bool
    has_unresolved_uses: bool
    has_plan_holds: bool


@dataclass(frozen=True)
class PlanView:
    plan_id: PlanId
    metadata: Metadata
    draft: PlanDraft
    legacy_snapshot: dict[str, JSONValue]
    legacy_publication: dict[str, JSONValue] | None
    coordination: CoordinationView
    status: str
    published_at: str


@dataclass(frozen=True)
class Event:
    sequence: int
    plan_revision: int | None
    plan_id: PlanId | None
    kind: str
    actor: str
    origin_worker: str | None
    relay: str | None
    published_at: str
    payload: dict[str, JSONValue]


@dataclass(frozen=True)
class ReadyStep:
    step_id: StepId
    ready: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PrivateGrant:
    orchestrator_token: str | None
    attempt_token: str | None

    def __repr__(self) -> str:
        orch_rep = "***" if self.orchestrator_token is not None else "None"
        att_rep = "***" if self.attempt_token is not None else "None"
        return f"PrivateGrant(orchestrator_token={orch_rep}, attempt_token={att_rep})"


@dataclass(frozen=True)
class MutationReceipt:
    committed: dict[str, JSONValue]
    committed_metadata: Metadata
    current_metadata: Metadata
    replayed: bool
    private: PrivateGrant | None


class DomainError(ValueError):
    """Domain error exception carrying an error code, message, and optional current metadata."""

    def __init__(self, code: str, message: str, current_metadata: Metadata | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.current_metadata = current_metadata

    def __repr__(self) -> str:
        return f"DomainError(code={self.code!r}, message={self.message!r}, current_metadata={self.current_metadata!r})"


# Command Payload TypedDict variants (all 23 kinds)
class CreatePlanPayload(TypedDict):
    creation_id: str
    draft: PlanDraft
    new_plan_intent: str | None


class RevisePlanPayload(TypedDict):
    draft: PlanDraft
    reason: str
    authorization_change: AuthorizationChange | None
    retire_steps: tuple[RetirementRequest, ...]


class TakeoverPayload(TypedDict):
    expected_ownership_version: int
    reason: str


class RegisterResourcePayload(TypedDict):
    claim: ResourceClaim


class ReconcileResourcesPayload(TypedDict):
    resource_ids: tuple[ResourceId, ...]
    canonical_claim: ResourceClaim
    reason: str


class InvalidateResultPayload(TypedDict):
    step_id: StepId
    result_version: ResultVersion
    reason: str
    evidence: tuple[Evidence, ...]


class ResolveInvalidationPayload(TypedDict):
    invalidation_id: str
    affected_step_id: StepId
    disposition: Literal["revalidated", "superseded", "replanned"]
    corrective_step_id: StepId | None
    corrected_result_version: ResultVersion | None
    evidence: tuple[Evidence, ...]
    reason: str


class AssignStepPayload(TypedDict):
    step_id: StepId
    assignee: str
    context: ExecutionContext


class BindAvailabilityPayload(TypedDict):
    bindings: tuple[Availability, ...]
    checked_base: str


class StartStepPayload(TypedDict):
    origin_worker: str
    relay: str | None
    observed_activity: str


class ReportActivityPayload(TypedDict):
    origin_worker: str
    relay: str | None
    observed_activity: str


class RecordCheckpointPayload(TypedDict):
    checkpoint: Checkpoint


class ResolveReassessmentPayload(TypedDict):
    checkpoint_id: str
    decision: Literal["apply", "reject", "hold"]
    reason: str
    draft: PlanDraft | None
    affected_steps: tuple[StepId, ...]
    unbounded: bool


class ReopenAttemptPayload(TypedDict):
    reason: str


class SweepStalePayload(TypedDict):
    pass


class RecordReviewPayload(TypedDict):
    step_id: StepId
    review: Review


class AcceptStepPayload(TypedDict):
    step_id: StepId
    artifact: ArtifactRef
    stopped_resource_ids: tuple[ResourceId, ...]
    stopped_reason: str | None


class ResolveAttemptPayload(TypedDict):
    resolution: Literal["confirm-owner", "replace", "reject", "release", "attach-context"]
    reason: str
    next_step_state: Literal["pending", "blocked", "retired"]
    stopped_resource_ids: tuple[ResourceId, ...]
    replacement_assignee: str | None
    replacement_context: ExecutionContext | None
    attached_context: ExecutionContext | ExecutionContextClaims | None


class ReserveVerificationPayload(TypedDict):
    step_id: StepId
    verifier: str
    context: ExecutionContext
    handoff_from: str | None
    stopped_reason: str | None


class ReleaseResourceUsePayload(TypedDict):
    use_id: str
    resource_ids: tuple[ResourceId, ...]
    reason: str
    stopped: bool


class AppendLateEvidencePayload(TypedDict):
    origin_attempt_id: AttemptId
    evidence: tuple[Evidence, ...]
    stopped_resource_ids: tuple[ResourceId, ...]
    reason: str


class ImportPlanPayload(TypedDict):
    host_id: str
    source_directory_id: ResourceId | None
    source_directory_claim: ResourceClaim | None
    original_task_id: str
    original_snapshot: dict[str, JSONValue]
    provenance: DocumentRef
    writers_stopped: str
    known_contexts: dict[StepId, ExecutionContext | ExecutionContextClaims]


class ResolveStoreHoldPayload(TypedDict):
    backup_cutoff: str
    inventory_complete: bool
    absent_work_inventory: tuple[HostWork, ...]
    resource_resolutions: tuple[ResourceResolution, ...]
    reason: str


CommandPayload = Union[
    CreatePlanPayload,
    RevisePlanPayload,
    TakeoverPayload,
    RegisterResourcePayload,
    ReconcileResourcesPayload,
    InvalidateResultPayload,
    ResolveInvalidationPayload,
    AssignStepPayload,
    BindAvailabilityPayload,
    StartStepPayload,
    ReportActivityPayload,
    RecordCheckpointPayload,
    ResolveReassessmentPayload,
    ReopenAttemptPayload,
    SweepStalePayload,
    RecordReviewPayload,
    AcceptStepPayload,
    ResolveAttemptPayload,
    ReserveVerificationPayload,
    ReleaseResourceUsePayload,
    AppendLateEvidencePayload,
    ImportPlanPayload,
    ResolveStoreHoldPayload,
    dict[str, Any],
]


@dataclass(frozen=True)
class CommandEnvelope:
    kind: CommandKind
    epoch: Epoch
    key: RequestKey
    actor: str
    plan_id: PlanId | None
    generation: Generation | None
    expected_revision: int | None
    ownership_version: int | None
    orchestrator_token: str | None
    attempt_id: AttemptId | None
    attempt_token: str | None
    attempt_version: int | None
    definition_version: DefinitionVersion | None
    payload: CommandPayload

    def __repr__(self) -> str:
        orch_rep = "***" if self.orchestrator_token is not None else "None"
        att_rep = "***" if self.attempt_token is not None else "None"
        return (
            f"CommandEnvelope(kind={self.kind!r}, epoch={self.epoch!r}, key={self.key!r}, "
            f"actor={self.actor!r}, plan_id={self.plan_id!r}, generation={self.generation!r}, "
            f"expected_revision={self.expected_revision!r}, ownership_version={self.ownership_version!r}, "
            f"orchestrator_token={orch_rep}, attempt_id={self.attempt_id!r}, "
            f"attempt_token={att_rep}, attempt_version={self.attempt_version!r}, "
            f"definition_version={self.definition_version!r}, payload={self.payload!r})"
        )


Command = CommandEnvelope


_PAYLOAD_ALLOWED_FIELDS: dict[str, set[str]] = {
    "create_plan": {"creation_id", "draft", "new_plan_intent"},
    "revise_plan": {"draft", "reason", "authorization_change", "retire_steps"},
    "takeover": {"expected_ownership_version", "reason"},
    "register_resource": {"claim"},
    "reconcile_resources": {"resource_ids", "canonical_claim", "reason"},
    "invalidate_result": {"step_id", "result_version", "reason", "evidence"},
    "resolve_invalidation": {
        "invalidation_id",
        "affected_step_id",
        "disposition",
        "corrective_step_id",
        "corrected_result_version",
        "evidence",
        "reason",
    },
    "assign_step": {"step_id", "assignee", "context"},
    "bind_availability": {"bindings", "checked_base"},
    "start_step": {"origin_worker", "relay", "observed_activity"},
    "report_activity": {"origin_worker", "relay", "observed_activity"},
    "record_checkpoint": {"checkpoint"},
    "resolve_reassessment": {
        "checkpoint_id",
        "decision",
        "reason",
        "draft",
        "affected_steps",
        "unbounded",
    },
    "reopen_attempt": {"reason"},
    "sweep_stale": set(),
    "record_review": {"step_id", "review"},
    "accept_step": {"step_id", "artifact", "stopped_resource_ids", "stopped_reason"},
    "resolve_attempt": {
        "resolution",
        "reason",
        "next_step_state",
        "stopped_resource_ids",
        "replacement_assignee",
        "replacement_context",
        "attached_context",
    },
    "reserve_verification": {"step_id", "verifier", "context", "handoff_from", "stopped_reason"},
    "release_resource_use": {"use_id", "resource_ids", "reason", "stopped"},
    "append_late_evidence": {"origin_attempt_id", "evidence", "stopped_resource_ids", "reason"},
    "import_plan": {
        "host_id",
        "source_directory_id",
        "source_directory_claim",
        "original_task_id",
        "original_snapshot",
        "provenance",
        "writers_stopped",
        "known_contexts",
    },
    "resolve_store_hold": {
        "backup_cutoff",
        "inventory_complete",
        "absent_work_inventory",
        "resource_resolutions",
        "reason",
    },
}

_PAYLOAD_REQUIRED_FIELDS: dict[str, set[str]] = {
    "create_plan": {"creation_id", "draft", "new_plan_intent"},
    "revise_plan": {"draft", "reason", "authorization_change"},  # retire_steps defaults to ()
    "takeover": {"expected_ownership_version", "reason"},
    "register_resource": {"claim"},
    "reconcile_resources": {"resource_ids", "canonical_claim", "reason"},
    "invalidate_result": {"step_id", "result_version", "reason", "evidence"},
    "resolve_invalidation": {
        "invalidation_id",
        "affected_step_id",
        "disposition",
        "corrective_step_id",
        "corrected_result_version",
        "evidence",
        "reason",
    },
    "assign_step": {"step_id", "assignee", "context"},
    "bind_availability": {"bindings", "checked_base"},
    "start_step": {"origin_worker", "relay", "observed_activity"},
    "report_activity": {"origin_worker", "relay", "observed_activity"},
    "record_checkpoint": {"checkpoint"},
    "resolve_reassessment": {
        "checkpoint_id",
        "decision",
        "reason",
        "draft",
        "affected_steps",
        "unbounded",
    },
    "reopen_attempt": {"reason"},
    "sweep_stale": set(),
    "record_review": {"step_id", "review"},
    "accept_step": {"step_id", "artifact", "stopped_resource_ids", "stopped_reason"},
    "resolve_attempt": {
        "resolution",
        "reason",
        "next_step_state",
        "stopped_resource_ids",
        "replacement_assignee",
        "replacement_context",
    },  # attached_context defaults to None
    "reserve_verification": {"step_id", "verifier", "context", "handoff_from", "stopped_reason"},
    "release_resource_use": {"use_id", "resource_ids", "reason", "stopped"},
    "append_late_evidence": {"origin_attempt_id", "evidence", "stopped_resource_ids", "reason"},
    "import_plan": {
        "host_id",
        "source_directory_id",
        "source_directory_claim",
        "original_task_id",
        "original_snapshot",
        "provenance",
        "writers_stopped",
        "known_contexts",
    },
    "resolve_store_hold": {
        "backup_cutoff",
        "inventory_complete",
        "absent_work_inventory",
        "resource_resolutions",
        "reason",
    },
}

_ENVELOPE_ALLOWED_FIELDS: set[str] = {
    "kind",
    "epoch",
    "key",
    "actor",
    "plan_id",
    "generation",
    "expected_revision",
    "ownership_version",
    "orchestrator_token",
    "attempt_id",
    "attempt_token",
    "attempt_version",
    "definition_version",
    "payload",
}

_ALL_KINDS = BOOTSTRAP_KINDS | WORKER_KINDS | ORCHESTRATOR_KINDS


def _freeze_nested(val: Any) -> Any:
    """Recursively freeze lists to tuples and copy mappings for immutability."""
    if isinstance(val, (list, tuple)):
        return tuple(_freeze_nested(item) for item in val)
    if isinstance(val, dict):
        return {k: _freeze_nested(v) for k, v in val.items()}
    return copy.deepcopy(val)


def parse_command(raw: Mapping[str, JSONValue]) -> Command:
    """Validate and parse a raw dictionary into a typed, immutable CommandEnvelope."""
    if not isinstance(raw, Mapping):
        raise DomainError("invalid", "Command must be a JSON mapping")

    # Reject unknown fields in envelope
    unknown_env = set(raw.keys()) - _ENVELOPE_ALLOWED_FIELDS
    if unknown_env:
        raise DomainError("invalid", f"Command envelope contains unknown field(s): {sorted(unknown_env)}")

    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in _ALL_KINDS:
        raise DomainError("invalid", f"Invalid command kind: {kind!r}")

    epoch = raw.get("epoch")
    if not isinstance(epoch, str) or not epoch.strip():
        raise DomainError("invalid", "Field 'epoch' must be a non-empty string")

    key = raw.get("key")
    if not isinstance(key, str) or not key.strip():
        raise DomainError("invalid", "Field 'key' must be a non-empty string")

    actor = raw.get("actor")
    if not isinstance(actor, str) or not actor.strip():
        raise DomainError("invalid", "Field 'actor' must be a non-empty string")

    # Validate envelope optional fields types
    plan_id = raw.get("plan_id")
    if plan_id is not None and (not isinstance(plan_id, str) or not plan_id.strip()):
        raise DomainError("invalid", "Field 'plan_id' must be a non-empty string or null")

    generation = raw.get("generation")
    if generation is not None and (not isinstance(generation, str) or not generation.strip()):
        raise DomainError("invalid", "Field 'generation' must be a non-empty string or null")

    expected_revision = raw.get("expected_revision")
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
        raise DomainError("invalid", "Field 'expected_revision' must be a non-negative integer or null")

    ownership_version = raw.get("ownership_version")
    if ownership_version is not None and (type(ownership_version) is not int or ownership_version < 0):
        raise DomainError("invalid", "Field 'ownership_version' must be a non-negative integer or null")

    orchestrator_token = raw.get("orchestrator_token")
    if orchestrator_token is not None and not isinstance(orchestrator_token, str):
        raise DomainError("invalid", "Field 'orchestrator_token' must be a string or null")

    attempt_id = raw.get("attempt_id")
    if attempt_id is not None and (not isinstance(attempt_id, str) or not attempt_id.strip()):
        raise DomainError("invalid", "Field 'attempt_id' must be a non-empty string or null")

    attempt_token = raw.get("attempt_token")
    if attempt_token is not None and not isinstance(attempt_token, str):
        raise DomainError("invalid", "Field 'attempt_token' must be a string or null")

    attempt_version = raw.get("attempt_version")
    if attempt_version is not None and (type(attempt_version) is not int or attempt_version < 0):
        raise DomainError("invalid", "Field 'attempt_version' must be a non-negative integer or null")

    definition_version = raw.get("definition_version")
    if definition_version is not None and (not isinstance(definition_version, str) or not definition_version.strip()):
        raise DomainError("invalid", "Field 'definition_version' must be a non-empty string or null")

    payload_raw = raw.get("payload")
    if not isinstance(payload_raw, Mapping):
        raise DomainError("invalid", f"Command payload must be a mapping for kind '{kind}'")

    payload: dict[str, Any] = dict(payload_raw)
    allowed_payload_keys = _PAYLOAD_ALLOWED_FIELDS[kind]
    required_payload_keys = _PAYLOAD_REQUIRED_FIELDS[kind]

    # Defaults specified by contract
    if kind == "revise_plan" and "retire_steps" not in payload:
        payload["retire_steps"] = ()
    if kind == "resolve_attempt" and "attached_context" not in payload:
        payload["attached_context"] = None

    # Check unknown payload fields
    unknown_pld = set(payload.keys()) - allowed_payload_keys
    if unknown_pld:
        raise DomainError("invalid", f"Payload for kind '{kind}' contains unknown field(s): {sorted(unknown_pld)}")

    # Check required payload fields
    missing_pld = required_payload_keys - set(payload.keys())
    if missing_pld:
        raise DomainError("invalid", f"Payload for kind '{kind}' is missing required field(s): {sorted(missing_pld)}")

    # Specific kind and payload type validation
    if kind == "import_plan":
        has_id = payload.get("source_directory_id") is not None
        has_claim = payload.get("source_directory_claim") is not None
        if has_id == has_claim:
            raise DomainError(
                "invalid",
                "import_plan requires exactly one of source_directory_id or source_directory_claim",
            )
        if not isinstance(payload.get("host_id"), str):
            raise DomainError("invalid", "import_plan requires string host_id")
        if not isinstance(payload.get("original_task_id"), str):
            raise DomainError("invalid", "import_plan requires string original_task_id")
        if not isinstance(payload.get("writers_stopped"), str):
            raise DomainError("invalid", "import_plan requires string writers_stopped")

    elif kind == "takeover":
        eov = payload.get("expected_ownership_version")
        if type(eov) is not int or eov < 0:
            raise DomainError("invalid", "takeover requires non-negative integer expected_ownership_version")
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise DomainError("invalid", "takeover requires non-empty string reason")

    elif kind == "resolve_attempt":
        resolution = payload.get("resolution")
        valid_resolutions = {"confirm-owner", "replace", "reject", "release", "attach-context"}
        if resolution not in valid_resolutions:
            raise DomainError("invalid", f"resolve_attempt resolution must be one of {sorted(valid_resolutions)}, got {resolution!r}")
        next_step_state = payload.get("next_step_state")
        valid_next_states = {"pending", "blocked", "retired"}
        if next_step_state not in valid_next_states:
            raise DomainError("invalid", f"resolve_attempt next_step_state must be one of {sorted(valid_next_states)}, got {next_step_state!r}")
        if not isinstance(payload.get("reason"), str):
            raise DomainError("invalid", "resolve_attempt requires string reason")
        if not isinstance(payload.get("stopped_resource_ids"), (list, tuple)):
            raise DomainError("invalid", "resolve_attempt requires list or tuple stopped_resource_ids")
        if resolution != "attach-context" and payload.get("attached_context") is not None:
            raise DomainError("invalid", f"resolve_attempt resolution '{resolution}' requires attached_context to be None")

    elif kind == "resolve_reassessment":
        decision = payload.get("decision")
        valid_decisions = {"apply", "reject", "hold"}
        if decision not in valid_decisions:
            raise DomainError("invalid", f"resolve_reassessment decision must be one of {sorted(valid_decisions)}, got {decision!r}")
        if type(payload.get("unbounded")) is not bool:
            raise DomainError("invalid", "resolve_reassessment requires boolean unbounded")
        if not isinstance(payload.get("checkpoint_id"), str):
            raise DomainError("invalid", "resolve_reassessment requires string checkpoint_id")
        if not isinstance(payload.get("reason"), str):
            raise DomainError("invalid", "resolve_reassessment requires string reason")

    elif kind == "resolve_invalidation":
        disposition = payload.get("disposition")
        valid_dispositions = {"revalidated", "superseded", "replanned"}
        if disposition not in valid_dispositions:
            raise DomainError("invalid", f"resolve_invalidation disposition must be one of {sorted(valid_dispositions)}, got {disposition!r}")
        if not isinstance(payload.get("invalidation_id"), str):
            raise DomainError("invalid", "resolve_invalidation requires string invalidation_id")
        if not isinstance(payload.get("affected_step_id"), str):
            raise DomainError("invalid", "resolve_invalidation requires string affected_step_id")
        if not isinstance(payload.get("reason"), str):
            raise DomainError("invalid", "resolve_invalidation requires string reason")

    elif kind == "release_resource_use":
        if type(payload.get("stopped")) is not bool:
            raise DomainError("invalid", "release_resource_use requires boolean stopped")
        if not isinstance(payload.get("use_id"), str):
            raise DomainError("invalid", "release_resource_use requires string use_id")
        if not isinstance(payload.get("reason"), str):
            raise DomainError("invalid", "release_resource_use requires string reason")

    elif kind == "resolve_store_hold":
        if type(payload.get("inventory_complete")) is not bool:
            raise DomainError("invalid", "resolve_store_hold requires boolean inventory_complete")
        if not isinstance(payload.get("backup_cutoff"), str):
            raise DomainError("invalid", "resolve_store_hold requires string backup_cutoff")
        if not isinstance(payload.get("reason"), str):
            raise DomainError("invalid", "resolve_store_hold requires string reason")

    elif kind in ("start_step", "report_activity"):
        if not isinstance(payload.get("origin_worker"), str):
            raise DomainError("invalid", f"{kind} requires string origin_worker")
        if not isinstance(payload.get("observed_activity"), str):
            raise DomainError("invalid", f"{kind} requires string observed_activity")

    elif kind == "create_plan":
        if not isinstance(payload.get("creation_id"), str):
            raise DomainError("invalid", "create_plan requires string creation_id")
        intent = payload.get("new_plan_intent")
        if intent is not None and not isinstance(intent, str):
            raise DomainError("invalid", "create_plan new_plan_intent must be a string or null")

    elif kind == "revise_plan":
        if not isinstance(payload.get("reason"), str):
            raise DomainError("invalid", "revise_plan requires string reason")

    elif kind == "reopen_attempt":
        if not isinstance(payload.get("reason"), str):
            raise DomainError("invalid", "reopen_attempt requires string reason")

    elif kind == "accept_step":
        if not isinstance(payload.get("step_id"), str):
            raise DomainError("invalid", "accept_step requires string step_id")
        if not isinstance(payload.get("stopped_resource_ids"), (list, tuple)):
            raise DomainError("invalid", "accept_step requires list or tuple stopped_resource_ids")

    elif kind == "assign_step":
        if not isinstance(payload.get("step_id"), str):
            raise DomainError("invalid", "assign_step requires string step_id")
        if not isinstance(payload.get("assignee"), str):
            raise DomainError("invalid", "assign_step requires string assignee")

    # Normative capability matrix token validation
    if kind in BOOTSTRAP_KINDS:
        if orchestrator_token is not None or attempt_token is not None:
            raise DomainError("invalid", f"Bootstrap kind '{kind}' forbids orchestrator and attempt tokens")
    elif kind in WORKER_KINDS:
        relay = payload.get("relay")
        if relay is None:
            if orchestrator_token is not None:
                raise DomainError(
                    "invalid",
                    f"Worker kind '{kind}' without relay forbids orchestrator token",
                )
    elif kind in ORCHESTRATOR_KINDS:
        if attempt_token is not None:
            raise DomainError("invalid", f"Orchestrator kind '{kind}' forbids attempt token")

    # Freeze payload contents
    frozen_payload: dict[str, Any] = {}
    for k, v in payload.items():
        frozen_payload[k] = _freeze_nested(v)

    cmd = CommandEnvelope(
        kind=kind,  # type: ignore[arg-type]
        epoch=epoch,
        key=key,
        actor=actor,
        plan_id=plan_id,
        generation=generation,
        expected_revision=expected_revision,
        ownership_version=ownership_version,
        orchestrator_token=orchestrator_token,
        attempt_id=attempt_id,
        attempt_token=attempt_token,
        attempt_version=attempt_version,
        definition_version=definition_version,
        payload=frozen_payload,
    )
    return cmd
