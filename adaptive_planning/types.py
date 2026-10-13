"""Typed data contracts, DTOs, command definitions, and errors for shared adaptive planning.

Standard-library only (Python 3.10+ compatible). Does not require external SDK dependencies.
"""
from __future__ import annotations

import copy
import dataclasses
from dataclasses import dataclass
from datetime import datetime
import math
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
JSONValue = Union[None, bool, int, float, str, list["JSONValue"], dict[str, "JSONValue"]]


class DomainError(ValueError):
    """Domain error exception carrying an error code, message, and optional current metadata."""

    def __init__(self, code: str, message: str, current_metadata: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.current_metadata = current_metadata

    def __repr__(self) -> str:
        return f"DomainError(code={self.code!r}, message={self.message!r}, current_metadata={self.current_metadata!r})"


def _freeze_value(val: Any) -> Any:
    if isinstance(val, FrozenDict):
        return val
    if isinstance(val, Mapping):
        return FrozenDict(val)
    elif isinstance(val, (list, tuple)):
        return tuple(_freeze_value(x) for x in val)
    return val


class FrozenDict(dict):
    """An immutable dictionary that rejects mutation and supports JSON serialization."""

    _initialized = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if getattr(self, "_initialized", False):
            raise TypeError(f"'{self.__class__.__name__}' object is immutable")
        super().__init__(*args, **kwargs)
        for k, v in list(self.items()):
            super().__setitem__(k, _freeze_value(v))
        self._initialized = True

    def __setitem__(self, key: Any, value: Any) -> None:
        raise TypeError(f"'{self.__class__.__name__}' object does not support item assignment")

    def __delitem__(self, key: Any) -> None:
        raise TypeError(f"'{self.__class__.__name__}' object does not support item deletion")

    def clear(self) -> None:
        raise TypeError(f"'{self.__class__.__name__}' object is immutable")

    def update(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError(f"'{self.__class__.__name__}' object is immutable")

    def setdefault(self, key: Any, default: Any = None) -> Any:
        raise TypeError(f"'{self.__class__.__name__}' object is immutable")

    def pop(self, *args: Any) -> Any:
        raise TypeError(f"'{self.__class__.__name__}' object is immutable")

    def popitem(self) -> Any:
        raise TypeError(f"'{self.__class__.__name__}' object is immutable")

    def __ior__(self, other: Any) -> Any:
        raise TypeError(f"'{self.__class__.__name__}' object is immutable")

    def __copy__(self) -> FrozenDict:
        return self

    def __deepcopy__(self, memo: Any) -> FrozenDict:
        return self

    def __reduce__(self) -> Any:
        return (FrozenDict, (dict(self),))

    def __hash__(self) -> int:
        return hash(tuple(sorted((k, v if not isinstance(v, (list, tuple)) else tuple(v)) for k, v in self.items())))


def _validate_and_freeze_json_tree(val: Any, seen: set[int] | None = None) -> Any:
    if seen is None:
        seen = set()
    if val is None or isinstance(val, (bool, int, str)):
        return val
    if isinstance(val, float):
        if not math.isfinite(val):
            raise DomainError("invalid", "Non-finite float values are not permitted in JSON tree")
        return val
    if isinstance(val, Mapping):
        ptr = id(val)
        if ptr in seen:
            raise DomainError("invalid", "Cyclic reference detected in JSON tree")
        seen.add(ptr)
        try:
            res = {}
            for k, v in val.items():
                if not isinstance(k, str):
                    raise DomainError("invalid", f"Mapping keys in JSON tree must be strings, got {type(k).__name__}")
                res[k] = _validate_and_freeze_json_tree(v, seen)
            return FrozenDict(res)
        except RecursionError as e:
            raise DomainError("invalid", f"Maximum nesting depth exceeded or cycle detected: {e}") from e
        finally:
            seen.remove(ptr)
    if isinstance(val, (list, tuple)):
        ptr = id(val)
        if ptr in seen:
            raise DomainError("invalid", "Cyclic reference detected in JSON tree")
        seen.add(ptr)
        try:
            return tuple(_validate_and_freeze_json_tree(item, seen) for item in val)
        except RecursionError as e:
            raise DomainError("invalid", f"Maximum nesting depth exceeded or cycle detected: {e}") from e
        finally:
            seen.remove(ptr)
    raise DomainError("invalid", f"Invalid JSON leaf of type {type(val).__name__}: {val!r}")


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
    workflow: Mapping[str, str] | None = None
    horizon: int = 10
    stop_reason: str | None = None
    block_reason: str | None = None

    def __post_init__(self) -> None:
        if self.workflow is not None:
            if not isinstance(self.workflow, Mapping):
                raise DomainError("invalid", "PlanDraft workflow must be a mapping or null")
            for k, v in self.workflow.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    raise DomainError("invalid", "PlanDraft workflow keys and values must be strings")
            if not isinstance(self.workflow, FrozenDict):
                object.__setattr__(self, "workflow", FrozenDict(self.workflow))


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
    result_versions: Mapping[StepId, ResultVersion]
    store_hold: bool
    restore_cutoff: str | None
    ownership_actor: str
    ownership_version: int
    store_sequence: int
    next_cursor: str | None
    total_counts: Mapping[str, int]
    has_live_attempts: bool
    has_unresolved_uses: bool
    has_plan_holds: bool

    def __post_init__(self) -> None:
        if not isinstance(self.result_versions, FrozenDict):
            object.__setattr__(self, "result_versions", FrozenDict(self.result_versions))
        if not isinstance(self.total_counts, FrozenDict):
            object.__setattr__(self, "total_counts", FrozenDict(self.total_counts))


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
    committed: Mapping[str, JSONValue]
    committed_metadata: Metadata
    current_metadata: Metadata
    replayed: bool
    private: PrivateGrant | None

    def __post_init__(self) -> None:
        if not isinstance(self.committed, Mapping):
            raise DomainError("invalid", f"MutationReceipt committed must be a mapping, got {type(self.committed).__name__}")
        object.__setattr__(self, "committed", _validate_and_freeze_json_tree(self.committed))


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


def _check_finite_floats(val: Any, seen: set[int] | None = None) -> None:
    if seen is None:
        seen = set()
    if isinstance(val, float):
        if not math.isfinite(val):
            raise DomainError("invalid", "Non-finite float values are not permitted")
    elif isinstance(val, Mapping):
        ptr = id(val)
        if ptr in seen:
            raise DomainError("invalid", "Cyclic reference detected")
        seen.add(ptr)
        try:
            for k, v in val.items():
                if not isinstance(k, str):
                    raise DomainError("invalid", "Mapping keys must be strings")
                _check_finite_floats(v, seen)
        finally:
            seen.remove(ptr)
    elif isinstance(val, (list, tuple)):
        ptr = id(val)
        if ptr in seen:
            raise DomainError("invalid", "Cyclic reference detected")
        seen.add(ptr)
        try:
            for item in val:
                _check_finite_floats(item, seen)
        finally:
            seen.remove(ptr)
    elif dataclasses.is_dataclass(val) and not isinstance(val, type):
        ptr = id(val)
        if ptr in seen:
            raise DomainError("invalid", "Cyclic reference detected")
        seen.add(ptr)
        try:
            for f in dataclasses.fields(val):
                _check_finite_floats(getattr(val, f.name), seen)
        finally:
            seen.remove(ptr)


def _freeze_nested(val: Any) -> Any:
    """Recursively freeze lists to tuples and mappings to FrozenDict for immutability."""
    _check_finite_floats(val)
    if isinstance(val, Mapping):
        return FrozenDict({k: _freeze_nested(v) for k, v in val.items()})
    if isinstance(val, (list, tuple)):
        return tuple(_freeze_nested(item) for item in val)
    return val


def _to_artifact_ref(val: Any) -> ArtifactRef:
    if isinstance(val, ArtifactRef):
        if val.kind not in ("code", "document", "data", "observation"):
            raise DomainError("invalid", f"Invalid ArtifactRef kind: {val.kind!r}")
        if not isinstance(val.identity, str) or not isinstance(val.version, str) or not isinstance(val.summary, str):
            raise DomainError("invalid", "ArtifactRef identity, version, and summary must be strings")
        if val.digest is not None and not isinstance(val.digest, str):
            raise DomainError("invalid", "ArtifactRef digest must be a string or null")
        return val
    if isinstance(val, Mapping):
        unknown = set(val.keys()) - {"kind", "identity", "version", "digest", "summary"}
        if unknown:
            raise DomainError("invalid", f"ArtifactRef contains unknown field(s): {sorted(unknown)}")
        missing = {"kind", "identity", "version", "digest", "summary"} - set(val.keys())
        if missing:
            raise DomainError("invalid", f"ArtifactRef missing required field(s): {sorted(missing)}")
        kind = val["kind"]
        if kind not in ("code", "document", "data", "observation"):
            raise DomainError("invalid", f"Invalid ArtifactRef kind: {kind!r}")
        if not isinstance(val["identity"], str) or not isinstance(val["version"], str) or not isinstance(val["summary"], str):
            raise DomainError("invalid", "ArtifactRef identity, version, and summary must be strings")
        digest = val["digest"]
        if digest is not None and not isinstance(digest, str):
            raise DomainError("invalid", "ArtifactRef digest must be a string or null")
        return ArtifactRef(kind=kind, identity=val["identity"], version=val["version"], digest=digest, summary=val["summary"])
    raise DomainError("invalid", f"Expected ArtifactRef or mapping, got {type(val).__name__}")


def _to_document_ref(val: Any) -> DocumentRef:
    if isinstance(val, DocumentRef):
        if not isinstance(val.path, str) or not isinstance(val.version, str):
            raise DomainError("invalid", "DocumentRef path and version must be strings")
        if val.branch is not None and not isinstance(val.branch, str):
            raise DomainError("invalid", "DocumentRef branch must be a string or null")
        if val.commit is not None and not isinstance(val.commit, str):
            raise DomainError("invalid", "DocumentRef commit must be a string or null")
        if val.task_id is not None and not isinstance(val.task_id, str):
            raise DomainError("invalid", "DocumentRef task_id must be a string or null")
        return val
    if isinstance(val, Mapping):
        unknown = set(val.keys()) - {"path", "branch", "commit", "version", "task_id"}
        if unknown:
            raise DomainError("invalid", f"DocumentRef contains unknown field(s): {sorted(unknown)}")
        missing = {"path", "branch", "commit", "version", "task_id"} - set(val.keys())
        if missing:
            raise DomainError("invalid", f"DocumentRef missing required field(s): {sorted(missing)}")
        if not isinstance(val["path"], str) or not isinstance(val["version"], str):
            raise DomainError("invalid", "DocumentRef path and version must be strings")
        branch = val["branch"]
        if branch is not None and not isinstance(branch, str):
            raise DomainError("invalid", "DocumentRef branch must be a string or null")
        commit = val["commit"]
        if commit is not None and not isinstance(commit, str):
            raise DomainError("invalid", "DocumentRef commit must be a string or null")
        task_id = val["task_id"]
        if task_id is not None and not isinstance(task_id, str):
            raise DomainError("invalid", "DocumentRef task_id must be a string or null")
        return DocumentRef(path=val["path"], branch=branch, commit=commit, version=val["version"], task_id=task_id)
    raise DomainError("invalid", f"Expected DocumentRef or mapping, got {type(val).__name__}")


def _to_step_definition(val: Any) -> StepDefinition:
    if isinstance(val, StepDefinition):
        if not isinstance(val.step_id, str) or not isinstance(val.title, str) or not isinstance(val.completion_check, str) or not isinstance(val.definition_version, str):
            raise DomainError("invalid", "StepDefinition step_id, title, completion_check, and definition_version must be strings")
        if type(val.review_required) is not bool or type(val.final_gate) is not bool:
            raise DomainError("invalid", "StepDefinition review_required and final_gate must be booleans")
        if not isinstance(val.dependencies, (tuple, list)) or not all(isinstance(d, str) for d in val.dependencies):
            raise DomainError("invalid", "StepDefinition dependencies must be a sequence of strings")
        doc = _to_document_ref(val.document) if val.document is not None else None
        return StepDefinition(
            step_id=val.step_id,
            title=val.title,
            dependencies=tuple(val.dependencies),
            completion_check=val.completion_check,
            document=doc,
            definition_version=val.definition_version,
            review_required=val.review_required,
            final_gate=val.final_gate,
        )
    if isinstance(val, Mapping):
        unknown = set(val.keys()) - {"step_id", "title", "dependencies", "completion_check", "document", "definition_version", "review_required", "final_gate"}
        if unknown:
            raise DomainError("invalid", f"StepDefinition contains unknown field(s): {sorted(unknown)}")
        missing = {"step_id", "title", "dependencies", "completion_check", "document", "definition_version", "review_required", "final_gate"} - set(val.keys())
        if missing:
            raise DomainError("invalid", f"StepDefinition missing required field(s): {sorted(missing)}")
        if not isinstance(val["step_id"], str) or not isinstance(val["title"], str) or not isinstance(val["completion_check"], str) or not isinstance(val["definition_version"], str):
            raise DomainError("invalid", "StepDefinition step_id, title, completion_check, and definition_version must be strings")
        if type(val["review_required"]) is not bool or type(val["final_gate"]) is not bool:
            raise DomainError("invalid", "StepDefinition review_required and final_gate must be booleans")
        deps = val["dependencies"]
        if not isinstance(deps, (tuple, list)) or not all(isinstance(d, str) for d in deps):
            raise DomainError("invalid", "StepDefinition dependencies must be a sequence of strings")
        doc = _to_document_ref(val["document"]) if val["document"] is not None else None
        return StepDefinition(
            step_id=val["step_id"],
            title=val["title"],
            dependencies=tuple(deps),
            completion_check=val["completion_check"],
            document=doc,
            definition_version=val["definition_version"],
            review_required=val["review_required"],
            final_gate=val["final_gate"],
        )
    raise DomainError("invalid", f"Expected StepDefinition or mapping, got {type(val).__name__}")


def _to_authorization(val: Any) -> Authorization:
    if isinstance(val, Authorization):
        if val.mode not in ("plan-only", "first-step", "complete-task"):
            raise DomainError("invalid", f"Invalid Authorization mode: {val.mode!r}")
        if not isinstance(val.interval, str) or not isinstance(val.note, str):
            raise DomainError("invalid", "Authorization interval and note must be strings")
        if type(val.since_revision) is not int or val.since_revision < 0:
            raise DomainError("invalid", "Authorization since_revision must be a non-negative integer")
        if not isinstance(val.baseline, (tuple, list)) or not all(isinstance(b, str) for b in val.baseline):
            raise DomainError("invalid", "Authorization baseline must be a sequence of strings")
        if val.claimed is not None and not isinstance(val.claimed, str):
            raise DomainError("invalid", "Authorization claimed must be a string or null")
        return Authorization(
            mode=val.mode,
            interval=val.interval,
            since_revision=val.since_revision,
            note=val.note,
            baseline=tuple(val.baseline),
            claimed=val.claimed,
        )
    if isinstance(val, Mapping):
        unknown = set(val.keys()) - {"mode", "interval", "since_revision", "note", "baseline", "claimed"}
        if unknown:
            raise DomainError("invalid", f"Authorization contains unknown field(s): {sorted(unknown)}")
        missing = {"mode", "interval", "since_revision", "note", "baseline", "claimed"} - set(val.keys())
        if missing:
            raise DomainError("invalid", f"Authorization missing required field(s): {sorted(missing)}")
        mode = val["mode"]
        if mode not in ("plan-only", "first-step", "complete-task"):
            raise DomainError("invalid", f"Invalid Authorization mode: {mode!r}")
        if not isinstance(val["interval"], str) or not isinstance(val["note"], str):
            raise DomainError("invalid", "Authorization interval and note must be strings")
        if type(val["since_revision"]) is not int or val["since_revision"] < 0:
            raise DomainError("invalid", "Authorization since_revision must be a non-negative integer")
        baseline = val["baseline"]
        if not isinstance(baseline, (tuple, list)) or not all(isinstance(b, str) for b in baseline):
            raise DomainError("invalid", "Authorization baseline must be a sequence of strings")
        claimed = val["claimed"]
        if claimed is not None and not isinstance(claimed, str):
            raise DomainError("invalid", "Authorization claimed must be a string or null")
        return Authorization(
            mode=mode,
            interval=val["interval"],
            since_revision=val["since_revision"],
            note=val["note"],
            baseline=tuple(baseline),
            claimed=claimed,
        )
    raise DomainError("invalid", f"Expected Authorization or mapping, got {type(val).__name__}")


def _to_authorization_change(val: Any) -> AuthorizationChange:
    if isinstance(val, AuthorizationChange):
        if val.mode not in ("plan-only", "first-step", "complete-task"):
            raise DomainError("invalid", f"Invalid AuthorizationChange mode: {val.mode!r}")
        if not isinstance(val.note, str) or not val.note.strip():
            raise DomainError("invalid", "AuthorizationChange note must be a non-empty string")
        return val
    if isinstance(val, Mapping):
        unknown = set(val.keys()) - {"mode", "note"}
        if unknown:
            raise DomainError("invalid", f"AuthorizationChange contains unknown field(s): {sorted(unknown)}")
        missing = {"mode", "note"} - set(val.keys())
        if missing:
            raise DomainError("invalid", f"AuthorizationChange missing required field(s): {sorted(missing)}")
        mode = val["mode"]
        if mode not in ("plan-only", "first-step", "complete-task"):
            raise DomainError("invalid", f"Invalid AuthorizationChange mode: {mode!r}")
        if not isinstance(val["note"], str) or not val["note"].strip():
            raise DomainError("invalid", "AuthorizationChange note must be a non-empty string")
        return AuthorizationChange(mode=mode, note=val["note"])
    raise DomainError("invalid", f"Expected AuthorizationChange or mapping, got {type(val).__name__}")


def _to_retirement_request(val: Any) -> RetirementRequest:
    if isinstance(val, RetirementRequest):
        if not isinstance(val.step_id, str) or not isinstance(val.reason, str):
            raise DomainError("invalid", "RetirementRequest step_id and reason must be strings")
        return val
    if isinstance(val, Mapping):
        unknown = set(val.keys()) - {"step_id", "reason"}
        if unknown:
            raise DomainError("invalid", f"RetirementRequest contains unknown field(s): {sorted(unknown)}")
        missing = {"step_id", "reason"} - set(val.keys())
        if missing:
            raise DomainError("invalid", f"RetirementRequest missing required field(s): {sorted(missing)}")
        if not isinstance(val["step_id"], str) or not isinstance(val["reason"], str):
            raise DomainError("invalid", "RetirementRequest step_id and reason must be strings")
        return RetirementRequest(step_id=val["step_id"], reason=val["reason"])
    raise DomainError("invalid", f"Expected RetirementRequest or mapping, got {type(val).__name__}")


def _to_resource_claim(val: Any) -> ResourceClaim:
    if isinstance(val, ResourceClaim):
        if not isinstance(val.host_id, str):
            raise DomainError("invalid", "ResourceClaim host_id must be a string")
        if val.kind not in ("repository", "workspace", "external"):
            raise DomainError("invalid", f"Invalid ResourceClaim kind: {val.kind!r}")
        if val.device is not None and type(val.device) is not int:
            raise DomainError("invalid", "ResourceClaim device must be an integer or null")
        if val.inode is not None and type(val.inode) is not int:
            raise DomainError("invalid", "ResourceClaim inode must be an integer or null")
        if val.canonical_path is not None and not isinstance(val.canonical_path, str):
            raise DomainError("invalid", "ResourceClaim canonical_path must be a string or null")
        if type(val.case_insensitive) is not bool:
            raise DomainError("invalid", "ResourceClaim case_insensitive must be a boolean")
        if val.repository_id is not None and not isinstance(val.repository_id, str):
            raise DomainError("invalid", "ResourceClaim repository_id must be a string or null")
        if val.external_identity is not None and not isinstance(val.external_identity, str):
            raise DomainError("invalid", "ResourceClaim external_identity must be a string or null")
        return val
    if isinstance(val, Mapping):
        unknown = set(val.keys()) - {"host_id", "kind", "device", "inode", "canonical_path", "case_insensitive", "repository_id", "external_identity"}
        if unknown:
            raise DomainError("invalid", f"ResourceClaim contains unknown field(s): {sorted(unknown)}")
        missing = {"host_id", "kind", "device", "inode", "canonical_path", "case_insensitive", "repository_id", "external_identity"} - set(val.keys())
        if missing:
            raise DomainError("invalid", f"ResourceClaim missing required field(s): {sorted(missing)}")
        if not isinstance(val["host_id"], str):
            raise DomainError("invalid", "ResourceClaim host_id must be a string")
        kind = val["kind"]
        if kind not in ("repository", "workspace", "external"):
            raise DomainError("invalid", f"Invalid ResourceClaim kind: {kind!r}")
        device = val["device"]
        if device is not None and type(device) is not int:
            raise DomainError("invalid", "ResourceClaim device must be an integer or null")
        inode = val["inode"]
        if inode is not None and type(inode) is not int:
            raise DomainError("invalid", "ResourceClaim inode must be an integer or null")
        canon = val["canonical_path"]
        if canon is not None and not isinstance(canon, str):
            raise DomainError("invalid", "ResourceClaim canonical_path must be a string or null")
        if type(val["case_insensitive"]) is not bool:
            raise DomainError("invalid", "ResourceClaim case_insensitive must be a boolean")
        repo_id = val["repository_id"]
        if repo_id is not None and not isinstance(repo_id, str):
            raise DomainError("invalid", "ResourceClaim repository_id must be a string or null")
        ext_id = val["external_identity"]
        if ext_id is not None and not isinstance(ext_id, str):
            raise DomainError("invalid", "ResourceClaim external_identity must be a string or null")
        return ResourceClaim(
            host_id=val["host_id"],
            kind=kind,
            device=device,
            inode=inode,
            canonical_path=canon,
            case_insensitive=val["case_insensitive"],
            repository_id=repo_id,
            external_identity=ext_id,
        )
    raise DomainError("invalid", f"Expected ResourceClaim or mapping, got {type(val).__name__}")


def _to_execution_context(val: Any) -> ExecutionContext:
    if isinstance(val, ExecutionContext):
        if val.kind not in ("repository", "non-repository", "external-only", "read-only"):
            raise DomainError("invalid", f"Invalid ExecutionContext kind: {val.kind!r}")
        if val.repository_id is not None and not isinstance(val.repository_id, str):
            raise DomainError("invalid", "ExecutionContext repository_id must be a string or null")
        if val.workspace_id is not None and not isinstance(val.workspace_id, str):
            raise DomainError("invalid", "ExecutionContext workspace_id must be a string or null")
        if type(val.repository_exclusive) is not bool or type(val.writes) is not bool:
            raise DomainError("invalid", "ExecutionContext repository_exclusive and writes must be booleans")
        if not isinstance(val.external_ids, (tuple, list)) or not all(isinstance(e, str) for e in val.external_ids):
            raise DomainError("invalid", "ExecutionContext external_ids must be a sequence of strings")
        if val.checked_base is not None and not isinstance(val.checked_base, str):
            raise DomainError("invalid", "ExecutionContext checked_base must be a string or null")
        return ExecutionContext(
            kind=val.kind,
            repository_id=val.repository_id,
            workspace_id=val.workspace_id,
            repository_exclusive=val.repository_exclusive,
            external_ids=tuple(val.external_ids),
            writes=val.writes,
            checked_base=val.checked_base,
        )
    if isinstance(val, Mapping):
        unknown = set(val.keys()) - {"kind", "repository_id", "workspace_id", "repository_exclusive", "external_ids", "writes", "checked_base"}
        if unknown:
            raise DomainError("invalid", f"ExecutionContext contains unknown field(s): {sorted(unknown)}")
        missing = {"kind", "repository_id", "workspace_id", "repository_exclusive", "external_ids", "writes", "checked_base"} - set(val.keys())
        if missing:
            raise DomainError("invalid", f"ExecutionContext missing required field(s): {sorted(missing)}")
        kind = val["kind"]
        if kind not in ("repository", "non-repository", "external-only", "read-only"):
            raise DomainError("invalid", f"Invalid ExecutionContext kind: {kind!r}")
        repo_id = val["repository_id"]
        if repo_id is not None and not isinstance(repo_id, str):
            raise DomainError("invalid", "ExecutionContext repository_id must be a string or null")
        ws_id = val["workspace_id"]
        if ws_id is not None and not isinstance(ws_id, str):
            raise DomainError("invalid", "ExecutionContext workspace_id must be a string or null")
        if type(val["repository_exclusive"]) is not bool or type(val["writes"]) is not bool:
            raise DomainError("invalid", "ExecutionContext repository_exclusive and writes must be booleans")
        ext_ids = val["external_ids"]
        if not isinstance(ext_ids, (tuple, list)) or not all(isinstance(e, str) for e in ext_ids):
            raise DomainError("invalid", "ExecutionContext external_ids must be a sequence of strings")
        checked_base = val["checked_base"]
        if checked_base is not None and not isinstance(checked_base, str):
            raise DomainError("invalid", "ExecutionContext checked_base must be a string or null")
        return ExecutionContext(
            kind=kind,
            repository_id=repo_id,
            workspace_id=ws_id,
            repository_exclusive=val["repository_exclusive"],
            external_ids=tuple(ext_ids),
            writes=val["writes"],
            checked_base=checked_base,
        )
    raise DomainError("invalid", f"Expected ExecutionContext or mapping, got {type(val).__name__}")


def _to_execution_context_claims(val: Any) -> ExecutionContextClaims:
    if isinstance(val, ExecutionContextClaims):
        if val.kind not in ("repository", "non-repository", "external-only", "read-only"):
            raise DomainError("invalid", f"Invalid ExecutionContextClaims kind: {val.kind!r}")
        rep_c = _to_resource_claim(val.repository_claim) if val.repository_claim is not None else None
        ws_c = _to_resource_claim(val.workspace_claim) if val.workspace_claim is not None else None
        if type(val.repository_exclusive) is not bool or type(val.writes) is not bool:
            raise DomainError("invalid", "ExecutionContextClaims repository_exclusive and writes must be booleans")
        ext_c = tuple(_to_resource_claim(c) for c in val.external_claims)
        if val.checked_base is not None and not isinstance(val.checked_base, str):
            raise DomainError("invalid", "ExecutionContextClaims checked_base must be a string or null")
        return ExecutionContextClaims(
            kind=val.kind,
            repository_claim=rep_c,
            workspace_claim=ws_c,
            repository_exclusive=val.repository_exclusive,
            external_claims=ext_c,
            writes=val.writes,
            checked_base=val.checked_base,
        )
    if isinstance(val, Mapping):
        unknown = set(val.keys()) - {"kind", "repository_claim", "workspace_claim", "repository_exclusive", "external_claims", "writes", "checked_base"}
        if unknown:
            raise DomainError("invalid", f"ExecutionContextClaims contains unknown field(s): {sorted(unknown)}")
        missing = {"kind", "repository_claim", "workspace_claim", "repository_exclusive", "external_claims", "writes", "checked_base"} - set(val.keys())
        if missing:
            raise DomainError("invalid", f"ExecutionContextClaims missing required field(s): {sorted(missing)}")
        kind = val["kind"]
        if kind not in ("repository", "non-repository", "external-only", "read-only"):
            raise DomainError("invalid", f"Invalid ExecutionContextClaims kind: {kind!r}")
        rep_c = _to_resource_claim(val["repository_claim"]) if val["repository_claim"] is not None else None
        ws_c = _to_resource_claim(val["workspace_claim"]) if val["workspace_claim"] is not None else None
        if type(val["repository_exclusive"]) is not bool or type(val["writes"]) is not bool:
            raise DomainError("invalid", "ExecutionContextClaims repository_exclusive and writes must be booleans")
        ext_claims_raw = val["external_claims"]
        if not isinstance(ext_claims_raw, (tuple, list)):
            raise DomainError("invalid", "ExecutionContextClaims external_claims must be a sequence")
        ext_c = tuple(_to_resource_claim(c) for c in ext_claims_raw)
        checked_base = val["checked_base"]
        if checked_base is not None and not isinstance(checked_base, str):
            raise DomainError("invalid", "ExecutionContextClaims checked_base must be a string or null")
        return ExecutionContextClaims(
            kind=kind,
            repository_claim=rep_c,
            workspace_claim=ws_c,
            repository_exclusive=val["repository_exclusive"],
            external_claims=ext_c,
            writes=val["writes"],
            checked_base=checked_base,
        )
    raise DomainError("invalid", f"Expected ExecutionContextClaims or mapping, got {type(val).__name__}")


def _to_evidence(val: Any) -> Evidence:
    if isinstance(val, Evidence):
        if not isinstance(val.summary, str) or not isinstance(val.check, str):
            raise DomainError("invalid", "Evidence summary and check must be strings")
        art = _to_artifact_ref(val.artifact) if val.artifact is not None else None
        if val.workspace_id is not None and not isinstance(val.workspace_id, str):
            raise DomainError("invalid", "Evidence workspace_id must be a string or null")
        if val.result not in ("passed", "failed", "blocked", "observation"):
            raise DomainError("invalid", f"Invalid Evidence result: {val.result!r}")
        if not isinstance(val.limitations, (tuple, list)) or not all(isinstance(l, str) for l in val.limitations):
            raise DomainError("invalid", "Evidence limitations must be a sequence of strings")
        if not isinstance(val.blockers, (tuple, list)) or not all(isinstance(b, str) for b in val.blockers):
            raise DomainError("invalid", "Evidence blockers must be a sequence of strings")
        return Evidence(
            summary=val.summary,
            artifact=art,
            workspace_id=val.workspace_id,
            check=val.check,
            result=val.result,
            limitations=tuple(val.limitations),
            blockers=tuple(val.blockers),
        )
    if isinstance(val, Mapping):
        unknown = set(val.keys()) - {"summary", "artifact", "workspace_id", "check", "result", "limitations", "blockers"}
        if unknown:
            raise DomainError("invalid", f"Evidence contains unknown field(s): {sorted(unknown)}")
        missing = {"summary", "artifact", "workspace_id", "check", "result", "limitations", "blockers"} - set(val.keys())
        if missing:
            raise DomainError("invalid", f"Evidence missing required field(s): {sorted(missing)}")
        if not isinstance(val["summary"], str) or not isinstance(val["check"], str):
            raise DomainError("invalid", "Evidence summary and check must be strings")
        art = _to_artifact_ref(val["artifact"]) if val["artifact"] is not None else None
        ws_id = val["workspace_id"]
        if ws_id is not None and not isinstance(ws_id, str):
            raise DomainError("invalid", "Evidence workspace_id must be a string or null")
        result = val["result"]
        if result not in ("passed", "failed", "blocked", "observation"):
            raise DomainError("invalid", f"Invalid Evidence result: {result!r}")
        lims = val["limitations"]
        if not isinstance(lims, (tuple, list)) or not all(isinstance(l, str) for l in lims):
            raise DomainError("invalid", "Evidence limitations must be a sequence of strings")
        blks = val["blockers"]
        if not isinstance(blks, (tuple, list)) or not all(isinstance(b, str) for b in blks):
            raise DomainError("invalid", "Evidence blockers must be a sequence of strings")
        return Evidence(
            summary=val["summary"],
            artifact=art,
            workspace_id=ws_id,
            check=val["check"],
            result=result,
            limitations=tuple(lims),
            blockers=tuple(blks),
        )
    raise DomainError("invalid", f"Expected Evidence or mapping, got {type(val).__name__}")


def _to_plan_draft(val: Any) -> PlanDraft:
    if isinstance(val, PlanDraft):
        if not isinstance(val.objective, str) or not isinstance(val.next_check, str):
            raise DomainError("invalid", "PlanDraft objective and next_check must be strings")
        if not isinstance(val.acceptance_criteria, (tuple, list)) or not all(isinstance(a, str) for a in val.acceptance_criteria):
            raise DomainError("invalid", "PlanDraft acceptance_criteria must be a sequence of strings")
        if not isinstance(val.constraints, (tuple, list)) or not all(isinstance(c, str) for c in val.constraints):
            raise DomainError("invalid", "PlanDraft constraints must be a sequence of strings")
        if not isinstance(val.facts, (tuple, list)) or not all(isinstance(f, str) for f in val.facts):
            raise DomainError("invalid", "PlanDraft facts must be a sequence of strings")
        if not isinstance(val.assumptions, (tuple, list)) or not all(isinstance(a, str) for a in val.assumptions):
            raise DomainError("invalid", "PlanDraft assumptions must be a sequence of strings")
        auth = _to_authorization(val.authorization)
        if not isinstance(val.steps, (tuple, list)):
            raise DomainError("invalid", "PlanDraft steps must be a sequence")
        steps = tuple(_to_step_definition(s) for s in val.steps)
        if not isinstance(val.documents, (tuple, list)):
            raise DomainError("invalid", "PlanDraft documents must be a sequence")
        docs = tuple(_to_document_ref(d) for d in val.documents)
        if not isinstance(val.workspace_refs, (tuple, list)) or not all(isinstance(w, str) for w in val.workspace_refs):
            raise DomainError("invalid", "PlanDraft workspace_refs must be a sequence of strings")
        if type(val.horizon) is not int or val.horizon < 0:
            raise DomainError("invalid", "PlanDraft horizon must be a non-negative integer")
        if val.stop_reason is not None and not isinstance(val.stop_reason, str):
            raise DomainError("invalid", "PlanDraft stop_reason must be a string or null")
        if val.block_reason is not None and not isinstance(val.block_reason, str):
            raise DomainError("invalid", "PlanDraft block_reason must be a string or null")
        if val.workflow is not None:
            if not isinstance(val.workflow, Mapping):
                raise DomainError("invalid", "PlanDraft workflow must be a mapping or null")
            for k, v in val.workflow.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    raise DomainError("invalid", "PlanDraft workflow keys and values must be strings")
            wf = FrozenDict(val.workflow)
        else:
            wf = None
        return PlanDraft(
            objective=val.objective,
            acceptance_criteria=tuple(val.acceptance_criteria),
            constraints=tuple(val.constraints),
            facts=tuple(val.facts),
            assumptions=tuple(val.assumptions),
            next_check=val.next_check,
            authorization=auth,
            steps=steps,
            documents=docs,
            workspace_refs=tuple(val.workspace_refs),
            workflow=wf,
            horizon=val.horizon,
            stop_reason=val.stop_reason,
            block_reason=val.block_reason,
        )
    if isinstance(val, Mapping):
        unknown = set(val.keys()) - {
            "objective", "acceptance_criteria", "constraints", "facts", "assumptions",
            "next_check", "authorization", "steps", "documents", "workspace_refs",
            "workflow", "horizon", "stop_reason", "block_reason"
        }
        if unknown:
            raise DomainError("invalid", f"PlanDraft contains unknown field(s): {sorted(unknown)}")
        missing = {
            "objective", "acceptance_criteria", "constraints", "facts", "assumptions",
            "next_check", "authorization", "steps", "documents", "workspace_refs",
            "workflow", "stop_reason", "block_reason"
        } - set(val.keys())
        if missing:
            raise DomainError("invalid", f"PlanDraft missing required field(s): {sorted(missing)}")
        if not isinstance(val["objective"], str) or not isinstance(val["next_check"], str):
            raise DomainError("invalid", "PlanDraft objective and next_check must be strings")
        ac = val["acceptance_criteria"]
        if not isinstance(ac, (tuple, list)) or not all(isinstance(a, str) for a in ac):
            raise DomainError("invalid", "PlanDraft acceptance_criteria must be a sequence of strings")
        constraints = val["constraints"]
        if not isinstance(constraints, (tuple, list)) or not all(isinstance(c, str) for c in constraints):
            raise DomainError("invalid", "PlanDraft constraints must be a sequence of strings")
        facts = val["facts"]
        if not isinstance(facts, (tuple, list)) or not all(isinstance(f, str) for f in facts):
            raise DomainError("invalid", "PlanDraft facts must be a sequence of strings")
        assumptions = val["assumptions"]
        if not isinstance(assumptions, (tuple, list)) or not all(isinstance(a, str) for a in assumptions):
            raise DomainError("invalid", "PlanDraft assumptions must be a sequence of strings")
        auth = _to_authorization(val["authorization"])
        steps_raw = val["steps"]
        if not isinstance(steps_raw, (tuple, list)):
            raise DomainError("invalid", "PlanDraft steps must be a sequence")
        steps = tuple(_to_step_definition(s) for s in steps_raw)
        docs_raw = val["documents"]
        if not isinstance(docs_raw, (tuple, list)):
            raise DomainError("invalid", "PlanDraft documents must be a sequence")
        docs = tuple(_to_document_ref(d) for d in docs_raw)
        ws_refs = val["workspace_refs"]
        if not isinstance(ws_refs, (tuple, list)) or not all(isinstance(w, str) for w in ws_refs):
            raise DomainError("invalid", "PlanDraft workspace_refs must be a sequence of strings")
        horizon = val.get("horizon", 10)
        if type(horizon) is not int or horizon < 0:
            raise DomainError("invalid", "PlanDraft horizon must be a non-negative integer")
        wf_raw = val["workflow"]
        if wf_raw is not None:
            if not isinstance(wf_raw, Mapping):
                raise DomainError("invalid", "PlanDraft workflow must be a mapping or null")
            for k, v in wf_raw.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    raise DomainError("invalid", "PlanDraft workflow keys and values must be strings")
            wf = FrozenDict(wf_raw)
        else:
            wf = None
        stop_r = val["stop_reason"]
        if stop_r is not None and not isinstance(stop_r, str):
            raise DomainError("invalid", "PlanDraft stop_reason must be a string or null")
        block_r = val["block_reason"]
        if block_r is not None and not isinstance(block_r, str):
            raise DomainError("invalid", "PlanDraft block_reason must be a string or null")
        return PlanDraft(
            objective=val["objective"],
            acceptance_criteria=tuple(ac),
            constraints=tuple(constraints),
            facts=tuple(facts),
            assumptions=tuple(assumptions),
            next_check=val["next_check"],
            authorization=auth,
            steps=steps,
            documents=docs,
            workspace_refs=tuple(ws_refs),
            workflow=wf,
            horizon=horizon,
            stop_reason=stop_r,
            block_reason=block_r,
        )
    raise DomainError("invalid", f"Expected PlanDraft or mapping, got {type(val).__name__}")


def _to_reassessment(val: Any) -> Reassessment:
    if isinstance(val, Reassessment):
        if not isinstance(val.summary, str) or not isinstance(val.reason, str):
            raise DomainError("invalid", "Reassessment summary and reason must be strings")
        if not isinstance(val.facts, (tuple, list)) or not all(isinstance(f, str) for f in val.facts):
            raise DomainError("invalid", "Reassessment facts must be a sequence of strings")
        if not isinstance(val.assumptions, (tuple, list)) or not all(isinstance(a, str) for a in val.assumptions):
            raise DomainError("invalid", "Reassessment assumptions must be a sequence of strings")
        if val.disposition not in ("unchanged", "material"):
            raise DomainError("invalid", f"Invalid Reassessment disposition: {val.disposition!r}")
        if not isinstance(val.affected_steps, (tuple, list)) or not all(isinstance(s, str) for s in val.affected_steps):
            raise DomainError("invalid", "Reassessment affected_steps must be a sequence of strings")
        if type(val.unbounded) is not bool:
            raise DomainError("invalid", "Reassessment unbounded must be a boolean")
        pd = _to_plan_draft(val.proposed_draft) if val.proposed_draft is not None else None
        return Reassessment(
            summary=val.summary,
            facts=tuple(val.facts),
            assumptions=tuple(val.assumptions),
            disposition=val.disposition,
            reason=val.reason,
            affected_steps=tuple(val.affected_steps),
            unbounded=val.unbounded,
            proposed_draft=pd,
        )
    if isinstance(val, Mapping):
        unknown = set(val.keys()) - {"summary", "facts", "assumptions", "disposition", "reason", "affected_steps", "unbounded", "proposed_draft"}
        if unknown:
            raise DomainError("invalid", f"Reassessment contains unknown field(s): {sorted(unknown)}")
        missing = {"summary", "facts", "assumptions", "disposition", "reason", "affected_steps", "unbounded", "proposed_draft"} - set(val.keys())
        if missing:
            raise DomainError("invalid", f"Reassessment missing required field(s): {sorted(missing)}")
        if not isinstance(val["summary"], str) or not isinstance(val["reason"], str):
            raise DomainError("invalid", "Reassessment summary and reason must be strings")
        disp = val["disposition"]
        if disp not in ("unchanged", "material"):
            raise DomainError("invalid", f"Invalid Reassessment disposition: {disp!r}")
        facts = val["facts"]
        if not isinstance(facts, (tuple, list)) or not all(isinstance(f, str) for f in facts):
            raise DomainError("invalid", "Reassessment facts must be a sequence of strings")
        assumptions = val["assumptions"]
        if not isinstance(assumptions, (tuple, list)) or not all(isinstance(a, str) for a in assumptions):
            raise DomainError("invalid", "Reassessment assumptions must be a sequence of strings")
        aff = val["affected_steps"]
        if not isinstance(aff, (tuple, list)) or not all(isinstance(s, str) for s in aff):
            raise DomainError("invalid", "Reassessment affected_steps must be a sequence of strings")
        if type(val["unbounded"]) is not bool:
            raise DomainError("invalid", "Reassessment unbounded must be a boolean")
        pd = _to_plan_draft(val["proposed_draft"]) if val["proposed_draft"] is not None else None
        return Reassessment(
            summary=val["summary"],
            facts=tuple(facts),
            assumptions=tuple(assumptions),
            disposition=disp,
            reason=val["reason"],
            affected_steps=tuple(aff),
            unbounded=val["unbounded"],
            proposed_draft=pd,
        )
    raise DomainError("invalid", f"Expected Reassessment or mapping, got {type(val).__name__}")


def _to_checkpoint(val: Any) -> Checkpoint:
    if isinstance(val, Checkpoint):
        if val.outcome not in ("passed", "failed", "blocked"):
            raise DomainError("invalid", f"Invalid Checkpoint outcome: {val.outcome!r}")
        if not isinstance(val.check, str) or not isinstance(val.origin_worker, str):
            raise DomainError("invalid", "Checkpoint check and origin_worker must be strings")
        if not isinstance(val.evidence, (tuple, list)):
            raise DomainError("invalid", "Checkpoint evidence must be a sequence")
        evs = tuple(_to_evidence(e) for e in val.evidence)
        reassess = _to_reassessment(val.reassessment)
        if val.relay is not None and not isinstance(val.relay, str):
            raise DomainError("invalid", "Checkpoint relay must be a string or null")
        art = _to_artifact_ref(val.artifact) if val.artifact is not None else None
        return Checkpoint(
            outcome=val.outcome,
            check=val.check,
            evidence=evs,
            reassessment=reassess,
            origin_worker=val.origin_worker,
            relay=val.relay,
            artifact=art,
        )
    if isinstance(val, Mapping):
        unknown = set(val.keys()) - {"outcome", "check", "evidence", "reassessment", "origin_worker", "relay", "artifact"}
        if unknown:
            raise DomainError("invalid", f"Checkpoint contains unknown field(s): {sorted(unknown)}")
        missing = {"outcome", "check", "evidence", "reassessment", "origin_worker", "relay", "artifact"} - set(val.keys())
        if missing:
            raise DomainError("invalid", f"Checkpoint missing required field(s): {sorted(missing)}")
        outcome = val["outcome"]
        if outcome not in ("passed", "failed", "blocked"):
            raise DomainError("invalid", f"Invalid Checkpoint outcome: {outcome!r}")
        if not isinstance(val["check"], str) or not isinstance(val["origin_worker"], str):
            raise DomainError("invalid", "Checkpoint check and origin_worker must be strings")
        ev_raw = val["evidence"]
        if not isinstance(ev_raw, (tuple, list)):
            raise DomainError("invalid", "Checkpoint evidence must be a sequence")
        evs = tuple(_to_evidence(e) for e in ev_raw)
        reassess = _to_reassessment(val["reassessment"])
        relay = val["relay"]
        if relay is not None and not isinstance(relay, str):
            raise DomainError("invalid", "Checkpoint relay must be a string or null")
        art = _to_artifact_ref(val["artifact"]) if val["artifact"] is not None else None
        return Checkpoint(
            outcome=outcome,
            check=val["check"],
            evidence=evs,
            reassessment=reassess,
            origin_worker=val["origin_worker"],
            relay=relay,
            artifact=art,
        )
    raise DomainError("invalid", f"Expected Checkpoint or mapping, got {type(val).__name__}")


def _to_availability(val: Any) -> Availability:
    if isinstance(val, Availability):
        if not isinstance(val.prerequisite, str) or not isinstance(val.result_version, str) or not isinstance(val.checked_base, str):
            raise DomainError("invalid", "Availability prerequisite, result_version, and checked_base must be strings")
        if val.method not in ("ancestor", "merge", "cherry-pick", "equivalent", "observation"):
            raise DomainError("invalid", f"Invalid Availability method: {val.method!r}")
        if val.artifact_digest is not None and not isinstance(val.artifact_digest, str):
            raise DomainError("invalid", "Availability artifact_digest must be a string or null")
        ev = _to_evidence(val.evidence)
        return Availability(
            prerequisite=val.prerequisite,
            result_version=val.result_version,
            method=val.method,
            checked_base=val.checked_base,
            artifact_digest=val.artifact_digest,
            evidence=ev,
        )
    if isinstance(val, Mapping):
        unknown = set(val.keys()) - {"prerequisite", "result_version", "method", "checked_base", "artifact_digest", "evidence"}
        if unknown:
            raise DomainError("invalid", f"Availability contains unknown field(s): {sorted(unknown)}")
        missing = {"prerequisite", "result_version", "method", "checked_base", "artifact_digest", "evidence"} - set(val.keys())
        if missing:
            raise DomainError("invalid", f"Availability missing required field(s): {sorted(missing)}")
        if not isinstance(val["prerequisite"], str) or not isinstance(val["result_version"], str) or not isinstance(val["checked_base"], str):
            raise DomainError("invalid", "Availability prerequisite, result_version, and checked_base must be strings")
        method = val["method"]
        if method not in ("ancestor", "merge", "cherry-pick", "equivalent", "observation"):
            raise DomainError("invalid", f"Invalid Availability method: {method!r}")
        ad = val["artifact_digest"]
        if ad is not None and not isinstance(ad, str):
            raise DomainError("invalid", "Availability artifact_digest must be a string or null")
        ev = _to_evidence(val["evidence"])
        return Availability(
            prerequisite=val["prerequisite"],
            result_version=val["result_version"],
            method=method,
            checked_base=val["checked_base"],
            artifact_digest=ad,
            evidence=ev,
        )
    raise DomainError("invalid", f"Expected Availability or mapping, got {type(val).__name__}")


def _to_review(val: Any) -> Review:
    if isinstance(val, Review):
        if not isinstance(val.reviewer, str):
            raise DomainError("invalid", "Review reviewer must be a string")
        art = _to_artifact_ref(val.artifact)
        if val.verdict not in ("passed", "failed"):
            raise DomainError("invalid", f"Invalid Review verdict: {val.verdict!r}")
        if not isinstance(val.findings, (tuple, list)) or not all(isinstance(f, str) for f in val.findings):
            raise DomainError("invalid", "Review findings must be a sequence of strings")
        if not isinstance(val.evidence, (tuple, list)):
            raise DomainError("invalid", "Review evidence must be a sequence")
        evs = tuple(_to_evidence(e) for e in val.evidence)
        if val.verification_use_id is not None and not isinstance(val.verification_use_id, str):
            raise DomainError("invalid", "Review verification_use_id must be a string or null")
        return Review(
            reviewer=val.reviewer,
            artifact=art,
            verdict=val.verdict,
            findings=tuple(val.findings),
            evidence=evs,
            verification_use_id=val.verification_use_id,
        )
    if isinstance(val, Mapping):
        unknown = set(val.keys()) - {"reviewer", "artifact", "verdict", "findings", "evidence", "verification_use_id"}
        if unknown:
            raise DomainError("invalid", f"Review contains unknown field(s): {sorted(unknown)}")
        missing = {"reviewer", "artifact", "verdict", "findings", "evidence", "verification_use_id"} - set(val.keys())
        if missing:
            raise DomainError("invalid", f"Review missing required field(s): {sorted(missing)}")
        if not isinstance(val["reviewer"], str):
            raise DomainError("invalid", "Review reviewer must be a string")
        art = _to_artifact_ref(val["artifact"])
        verdict = val["verdict"]
        if verdict not in ("passed", "failed"):
            raise DomainError("invalid", f"Invalid Review verdict: {verdict!r}")
        findings = val["findings"]
        if not isinstance(findings, (tuple, list)) or not all(isinstance(f, str) for f in findings):
            raise DomainError("invalid", "Review findings must be a sequence of strings")
        ev_raw = val["evidence"]
        if not isinstance(ev_raw, (tuple, list)):
            raise DomainError("invalid", "Review evidence must be a sequence")
        evs = tuple(_to_evidence(e) for e in ev_raw)
        v_use_id = val["verification_use_id"]
        if v_use_id is not None and not isinstance(v_use_id, str):
            raise DomainError("invalid", "Review verification_use_id must be a string or null")
        return Review(
            reviewer=val["reviewer"],
            artifact=art,
            verdict=verdict,
            findings=tuple(findings),
            evidence=evs,
            verification_use_id=v_use_id,
        )
    raise DomainError("invalid", f"Expected Review or mapping, got {type(val).__name__}")


def _to_host_work(val: Any) -> HostWork:
    if isinstance(val, HostWork):
        if not isinstance(val.work_id, str) or not isinstance(val.actor, str):
            raise DomainError("invalid", "HostWork work_id and actor must be strings")
        if val.state not in ("running", "stopped", "unknown"):
            raise DomainError("invalid", f"Invalid HostWork state: {val.state!r}")
        if not isinstance(val.resource_ids, (tuple, list)) or not all(isinstance(r, str) for r in val.resource_ids):
            raise DomainError("invalid", "HostWork resource_ids must be a sequence of strings")
        claims = tuple(_to_resource_claim(c) for c in val.resource_claims)
        evs = tuple(_to_evidence(e) for e in val.evidence)
        return HostWork(
            work_id=val.work_id,
            actor=val.actor,
            state=val.state,
            resource_ids=tuple(val.resource_ids),
            resource_claims=claims,
            evidence=evs,
        )
    if isinstance(val, Mapping):
        unknown = set(val.keys()) - {"work_id", "actor", "state", "resource_ids", "resource_claims", "evidence"}
        if unknown:
            raise DomainError("invalid", f"HostWork contains unknown field(s): {sorted(unknown)}")
        missing = {"work_id", "actor", "state", "resource_ids", "resource_claims", "evidence"} - set(val.keys())
        if missing:
            raise DomainError("invalid", f"HostWork missing required field(s): {sorted(missing)}")
        if not isinstance(val["work_id"], str) or not isinstance(val["actor"], str):
            raise DomainError("invalid", "HostWork work_id and actor must be strings")
        state = val["state"]
        if state not in ("running", "stopped", "unknown"):
            raise DomainError("invalid", f"Invalid HostWork state: {state!r}")
        res_ids = val["resource_ids"]
        if not isinstance(res_ids, (tuple, list)) or not all(isinstance(r, str) for r in res_ids):
            raise DomainError("invalid", "HostWork resource_ids must be a sequence of strings")
        claims_raw = val["resource_claims"]
        if not isinstance(claims_raw, (tuple, list)):
            raise DomainError("invalid", "HostWork resource_claims must be a sequence")
        claims = tuple(_to_resource_claim(c) for c in claims_raw)
        ev_raw = val["evidence"]
        if not isinstance(ev_raw, (tuple, list)):
            raise DomainError("invalid", "HostWork evidence must be a sequence")
        evs = tuple(_to_evidence(e) for e in ev_raw)
        return HostWork(
            work_id=val["work_id"],
            actor=val["actor"],
            state=state,
            resource_ids=tuple(res_ids),
            resource_claims=claims,
            evidence=evs,
        )
    raise DomainError("invalid", f"Expected HostWork or mapping, got {type(val).__name__}")


def _to_resource_resolution(val: Any) -> ResourceResolution:
    if isinstance(val, ResourceResolution):
        if val.use_id is not None and not isinstance(val.use_id, str):
            raise DomainError("invalid", "ResourceResolution use_id must be a string or null")
        if val.absent_work_id is not None and not isinstance(val.absent_work_id, str):
            raise DomainError("invalid", "ResourceResolution absent_work_id must be a string or null")
        if not isinstance(val.resource_ids, (tuple, list)) or not all(isinstance(r, str) for r in val.resource_ids):
            raise DomainError("invalid", "ResourceResolution resource_ids must be a sequence of strings")
        if val.resolution not in ("stopped", "isolated", "unresolved"):
            raise DomainError("invalid", f"Invalid ResourceResolution resolution: {val.resolution!r}")
        if not isinstance(val.reason, str):
            raise DomainError("invalid", "ResourceResolution reason must be a string")
        return ResourceResolution(
            use_id=val.use_id,
            absent_work_id=val.absent_work_id,
            resource_ids=tuple(val.resource_ids),
            resolution=val.resolution,
            reason=val.reason,
        )
    if isinstance(val, Mapping):
        unknown = set(val.keys()) - {"use_id", "absent_work_id", "resource_ids", "resolution", "reason"}
        if unknown:
            raise DomainError("invalid", f"ResourceResolution contains unknown field(s): {sorted(unknown)}")
        missing = {"use_id", "absent_work_id", "resource_ids", "resolution", "reason"} - set(val.keys())
        if missing:
            raise DomainError("invalid", f"ResourceResolution missing required field(s): {sorted(missing)}")
        use_id = val["use_id"]
        if use_id is not None and not isinstance(use_id, str):
            raise DomainError("invalid", "ResourceResolution use_id must be a string or null")
        absent_work_id = val["absent_work_id"]
        if absent_work_id is not None and not isinstance(absent_work_id, str):
            raise DomainError("invalid", "ResourceResolution absent_work_id must be a string or null")
        res_ids = val["resource_ids"]
        if not isinstance(res_ids, (tuple, list)) or not all(isinstance(r, str) for r in res_ids):
            raise DomainError("invalid", "ResourceResolution resource_ids must be a sequence of strings")
        resolution = val["resolution"]
        if resolution not in ("stopped", "isolated", "unresolved"):
            raise DomainError("invalid", f"Invalid ResourceResolution resolution: {resolution!r}")
        if not isinstance(val["reason"], str):
            raise DomainError("invalid", "ResourceResolution reason must be a string")
        return ResourceResolution(
            use_id=use_id,
            absent_work_id=absent_work_id,
            resource_ids=tuple(res_ids),
            resolution=resolution,
            reason=val["reason"],
        )
    raise DomainError("invalid", f"Expected ResourceResolution or mapping, got {type(val).__name__}")


def _is_execution_context_claims(val: Any) -> bool:
    if isinstance(val, ExecutionContextClaims):
        return True
    if isinstance(val, ExecutionContext):
        return False
    if isinstance(val, Mapping):
        has_claim_fields = any(k in val for k in ("repository_claim", "workspace_claim", "external_claims"))
        has_id_fields = any(k in val for k in ("repository_id", "workspace_id", "external_ids"))
        if has_claim_fields and not has_id_fields:
            return True
        if has_id_fields and not has_claim_fields:
            return False
        return has_claim_fields
    return False


def parse_command(raw: Mapping[str, JSONValue]) -> Command:
    """Validate and parse a raw dictionary into a typed, immutable CommandEnvelope."""
    try:
        return _parse_command_impl(raw)
    except DomainError:
        raise
    except (RecursionError, TypeError) as err:
        raise DomainError("invalid", f"Malformed command input: {err}") from err


def _parse_command_impl(raw: Mapping[str, JSONValue]) -> Command:
    """Validate and parse a raw dictionary into a typed, immutable CommandEnvelope."""
    if not isinstance(raw, Mapping):
        raise DomainError("invalid", "Command must be a JSON mapping")

    # Reject non-finite floats in entire raw command
    _check_finite_floats(raw)

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

    # Validate each kind's payload fields and convert nested DTOs
    validated_payload: dict[str, Any] = {}

    if kind == "create_plan":
        cid = payload.get("creation_id")
        if not isinstance(cid, str) or not cid.strip():
            raise DomainError("invalid", "create_plan requires non-empty string creation_id")
        intent = payload.get("new_plan_intent")
        if intent is not None and not isinstance(intent, str):
            raise DomainError("invalid", "create_plan new_plan_intent must be a string or null")
        draft_val = _to_plan_draft(payload.get("draft"))
        validated_payload = {"creation_id": cid, "draft": draft_val, "new_plan_intent": intent}

    elif kind == "revise_plan":
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise DomainError("invalid", "revise_plan requires non-empty string reason")
        draft_val = _to_plan_draft(payload.get("draft"))
        ac_val = _to_authorization_change(payload.get("authorization_change")) if payload.get("authorization_change") is not None else None
        retire_steps_raw = payload.get("retire_steps", ())
        if not isinstance(retire_steps_raw, (tuple, list)):
            raise DomainError("invalid", "revise_plan retire_steps must be a list or tuple")
        retire_steps = tuple(_to_retirement_request(r) for r in retire_steps_raw)
        validated_payload = {
            "draft": draft_val,
            "reason": reason,
            "authorization_change": ac_val,
            "retire_steps": retire_steps,
        }

    elif kind == "takeover":
        eov = payload.get("expected_ownership_version")
        if type(eov) is not int or eov < 0:
            raise DomainError("invalid", "takeover requires non-negative integer expected_ownership_version")
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise DomainError("invalid", "takeover requires non-empty string reason")
        validated_payload = {"expected_ownership_version": eov, "reason": reason}

    elif kind == "register_resource":
        claim = _to_resource_claim(payload.get("claim"))
        validated_payload = {"claim": claim}

    elif kind == "reconcile_resources":
        rids = payload.get("resource_ids")
        if not isinstance(rids, (tuple, list)) or not all(isinstance(r, str) for r in rids):
            raise DomainError("invalid", "reconcile_resources requires list or tuple of string resource_ids")
        canonical_claim = _to_resource_claim(payload.get("canonical_claim"))
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise DomainError("invalid", "reconcile_resources requires non-empty string reason")
        validated_payload = {
            "resource_ids": tuple(rids),
            "canonical_claim": canonical_claim,
            "reason": reason,
        }

    elif kind == "invalidate_result":
        step_id = payload.get("step_id")
        if not isinstance(step_id, str) or not step_id.strip():
            raise DomainError("invalid", "invalidate_result requires non-empty string step_id")
        rv = payload.get("result_version")
        if not isinstance(rv, str) or not rv.strip():
            raise DomainError("invalid", "invalidate_result requires non-empty string result_version")
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise DomainError("invalid", "invalidate_result requires non-empty string reason")
        ev_raw = payload.get("evidence")
        if not isinstance(ev_raw, (tuple, list)):
            raise DomainError("invalid", "invalidate_result requires list or tuple evidence")
        evidence = tuple(_to_evidence(e) for e in ev_raw)
        validated_payload = {
            "step_id": step_id,
            "result_version": rv,
            "reason": reason,
            "evidence": evidence,
        }

    elif kind == "resolve_invalidation":
        invalidation_id = payload.get("invalidation_id")
        if not isinstance(invalidation_id, str) or not invalidation_id.strip():
            raise DomainError("invalid", "resolve_invalidation requires non-empty string invalidation_id")
        aff_step_id = payload.get("affected_step_id")
        if not isinstance(aff_step_id, str) or not aff_step_id.strip():
            raise DomainError("invalid", "resolve_invalidation requires non-empty string affected_step_id")
        disposition = payload.get("disposition")
        if disposition not in ("revalidated", "superseded", "replanned"):
            raise DomainError("invalid", f"resolve_invalidation disposition must be one of ('revalidated', 'superseded', 'replanned'), got {disposition!r}")
        corr_step = payload.get("corrective_step_id")
        if corr_step is not None and not isinstance(corr_step, str):
            raise DomainError("invalid", "resolve_invalidation corrective_step_id must be a string or null")
        corr_rv = payload.get("corrected_result_version")
        if corr_rv is not None and not isinstance(corr_rv, str):
            raise DomainError("invalid", "resolve_invalidation corrected_result_version must be a string or null")
        ev_raw = payload.get("evidence")
        if not isinstance(ev_raw, (tuple, list)):
            raise DomainError("invalid", "resolve_invalidation requires list or tuple evidence")
        evidence = tuple(_to_evidence(e) for e in ev_raw)
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise DomainError("invalid", "resolve_invalidation requires non-empty string reason")
        validated_payload = {
            "invalidation_id": invalidation_id,
            "affected_step_id": aff_step_id,
            "disposition": disposition,
            "corrective_step_id": corr_step,
            "corrected_result_version": corr_rv,
            "evidence": evidence,
            "reason": reason,
        }

    elif kind == "assign_step":
        step_id = payload.get("step_id")
        if not isinstance(step_id, str) or not step_id.strip():
            raise DomainError("invalid", "assign_step requires non-empty string step_id")
        assignee = payload.get("assignee")
        if not isinstance(assignee, str) or not assignee.strip():
            raise DomainError("invalid", "assign_step requires non-empty string assignee")
        ctx = _to_execution_context(payload.get("context"))
        validated_payload = {"step_id": step_id, "assignee": assignee, "context": ctx}

    elif kind == "bind_availability":
        b_raw = payload.get("bindings")
        if not isinstance(b_raw, (tuple, list)):
            raise DomainError("invalid", "bind_availability requires list or tuple bindings")
        bindings = tuple(_to_availability(b) for b in b_raw)
        cb = payload.get("checked_base")
        if not isinstance(cb, str) or not cb.strip():
            raise DomainError("invalid", "bind_availability requires non-empty string checked_base")
        validated_payload = {"bindings": bindings, "checked_base": cb}

    elif kind in ("start_step", "report_activity"):
        ow = payload.get("origin_worker")
        if not isinstance(ow, str) or not ow.strip():
            raise DomainError("invalid", f"{kind} requires non-empty string origin_worker")
        relay = payload.get("relay")
        if relay is not None and not isinstance(relay, str):
            raise DomainError("invalid", f"{kind} relay must be a string or null")
        act = payload.get("observed_activity")
        if not isinstance(act, str) or not act.strip():
            raise DomainError("invalid", f"{kind} requires non-empty string observed_activity")
        validated_payload = {"origin_worker": ow, "relay": relay, "observed_activity": act}

    elif kind == "record_checkpoint":
        cp = _to_checkpoint(payload.get("checkpoint"))
        validated_payload = {"checkpoint": cp}

    elif kind == "resolve_reassessment":
        cpid = payload.get("checkpoint_id")
        if not isinstance(cpid, str) or not cpid.strip():
            raise DomainError("invalid", "resolve_reassessment requires non-empty string checkpoint_id")
        decision = payload.get("decision")
        if decision not in ("apply", "reject", "hold"):
            raise DomainError("invalid", f"resolve_reassessment decision must be one of ('apply', 'reject', 'hold'), got {decision!r}")
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise DomainError("invalid", "resolve_reassessment requires non-empty string reason")
        draft_val = _to_plan_draft(payload.get("draft")) if payload.get("draft") is not None else None
        aff_steps = payload.get("affected_steps")
        if not isinstance(aff_steps, (tuple, list)) or not all(isinstance(s, str) for s in aff_steps):
            raise DomainError("invalid", "resolve_reassessment affected_steps must be a list or tuple of strings")
        unbounded = payload.get("unbounded")
        if type(unbounded) is not bool:
            raise DomainError("invalid", "resolve_reassessment unbounded must be a boolean")
        validated_payload = {
            "checkpoint_id": cpid,
            "decision": decision,
            "reason": reason,
            "draft": draft_val,
            "affected_steps": tuple(aff_steps),
            "unbounded": unbounded,
        }

    elif kind == "reopen_attempt":
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise DomainError("invalid", "reopen_attempt requires non-empty string reason")
        validated_payload = {"reason": reason}

    elif kind == "sweep_stale":
        validated_payload = {}

    elif kind == "record_review":
        step_id = payload.get("step_id")
        if not isinstance(step_id, str) or not step_id.strip():
            raise DomainError("invalid", "record_review requires non-empty string step_id")
        review = _to_review(payload.get("review"))
        validated_payload = {"step_id": step_id, "review": review}

    elif kind == "accept_step":
        step_id = payload.get("step_id")
        if not isinstance(step_id, str) or not step_id.strip():
            raise DomainError("invalid", "accept_step requires non-empty string step_id")
        art = _to_artifact_ref(payload.get("artifact"))
        s_rids = payload.get("stopped_resource_ids")
        if not isinstance(s_rids, (tuple, list)) or not all(isinstance(r, str) for r in s_rids):
            raise DomainError("invalid", "accept_step stopped_resource_ids must be a list or tuple of strings")
        s_reason = payload.get("stopped_reason")
        if s_reason is not None and not isinstance(s_reason, str):
            raise DomainError("invalid", "accept_step stopped_reason must be a string or null")
        validated_payload = {
            "step_id": step_id,
            "artifact": art,
            "stopped_resource_ids": tuple(s_rids),
            "stopped_reason": s_reason,
        }

    elif kind == "resolve_attempt":
        resolution = payload.get("resolution")
        if resolution not in ("confirm-owner", "replace", "reject", "release", "attach-context"):
            raise DomainError("invalid", f"resolve_attempt resolution must be one of ('confirm-owner', 'replace', 'reject', 'release', 'attach-context'), got {resolution!r}")
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise DomainError("invalid", "resolve_attempt requires non-empty string reason")
        nss = payload.get("next_step_state")
        if nss not in ("pending", "blocked", "retired"):
            raise DomainError("invalid", f"resolve_attempt next_step_state must be one of ('pending', 'blocked', 'retired'), got {nss!r}")
        s_rids = payload.get("stopped_resource_ids")
        if not isinstance(s_rids, (tuple, list)) or not all(isinstance(r, str) for r in s_rids):
            raise DomainError("invalid", "resolve_attempt stopped_resource_ids must be a list or tuple of strings")
        rep_asg = payload.get("replacement_assignee")
        if rep_asg is not None and not isinstance(rep_asg, str):
            raise DomainError("invalid", "resolve_attempt replacement_assignee must be a string or null")
        rep_ctx = _to_execution_context(payload.get("replacement_context")) if payload.get("replacement_context") is not None else None
        att_ctx = payload.get("attached_context")
        if resolution != "attach-context":
            if att_ctx is not None:
                raise DomainError("invalid", f"resolve_attempt resolution '{resolution}' requires attached_context to be None")
            att_ctx_val = None
        else:
            if att_ctx is None:
                att_ctx_val = None
            elif _is_execution_context_claims(att_ctx):
                att_ctx_val = _to_execution_context_claims(att_ctx)
            else:
                att_ctx_val = _to_execution_context(att_ctx)
        validated_payload = {
            "resolution": resolution,
            "reason": reason,
            "next_step_state": nss,
            "stopped_resource_ids": tuple(s_rids),
            "replacement_assignee": rep_asg,
            "replacement_context": rep_ctx,
            "attached_context": att_ctx_val,
        }

    elif kind == "reserve_verification":
        step_id = payload.get("step_id")
        if not isinstance(step_id, str) or not step_id.strip():
            raise DomainError("invalid", "reserve_verification requires non-empty string step_id")
        verifier = payload.get("verifier")
        if not isinstance(verifier, str) or not verifier.strip():
            raise DomainError("invalid", "reserve_verification requires non-empty string verifier")
        ctx = _to_execution_context(payload.get("context"))
        handoff_from = payload.get("handoff_from")
        if handoff_from is not None and not isinstance(handoff_from, str):
            raise DomainError("invalid", "reserve_verification handoff_from must be a string or null")
        s_reason = payload.get("stopped_reason")
        if s_reason is not None and not isinstance(s_reason, str):
            raise DomainError("invalid", "reserve_verification stopped_reason must be a string or null")
        validated_payload = {
            "step_id": step_id,
            "verifier": verifier,
            "context": ctx,
            "handoff_from": handoff_from,
            "stopped_reason": s_reason,
        }

    elif kind == "release_resource_use":
        use_id = payload.get("use_id")
        if not isinstance(use_id, str) or not use_id.strip():
            raise DomainError("invalid", "release_resource_use requires non-empty string use_id")
        rids = payload.get("resource_ids")
        if not isinstance(rids, (tuple, list)) or not all(isinstance(r, str) for r in rids):
            raise DomainError("invalid", "release_resource_use resource_ids must be a list or tuple of strings")
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise DomainError("invalid", "release_resource_use requires non-empty string reason")
        stopped = payload.get("stopped")
        if type(stopped) is not bool:
            raise DomainError("invalid", "release_resource_use stopped must be a boolean")
        validated_payload = {
            "use_id": use_id,
            "resource_ids": tuple(rids),
            "reason": reason,
            "stopped": stopped,
        }

    elif kind == "append_late_evidence":
        att_id = payload.get("origin_attempt_id")
        if not isinstance(att_id, str) or not att_id.strip():
            raise DomainError("invalid", "append_late_evidence requires non-empty string origin_attempt_id")
        ev_raw = payload.get("evidence")
        if not isinstance(ev_raw, (tuple, list)):
            raise DomainError("invalid", "append_late_evidence requires list or tuple evidence")
        evidence = tuple(_to_evidence(e) for e in ev_raw)
        s_rids = payload.get("stopped_resource_ids")
        if not isinstance(s_rids, (tuple, list)) or not all(isinstance(r, str) for r in s_rids):
            raise DomainError("invalid", "append_late_evidence stopped_resource_ids must be a list or tuple of strings")
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise DomainError("invalid", "append_late_evidence requires non-empty string reason")
        validated_payload = {
            "origin_attempt_id": att_id,
            "evidence": evidence,
            "stopped_resource_ids": tuple(s_rids),
            "reason": reason,
        }

    elif kind == "import_plan":
        host_id = payload.get("host_id")
        if not isinstance(host_id, str) or not host_id.strip():
            raise DomainError("invalid", "import_plan requires non-empty string host_id")
        s_dir_id = payload.get("source_directory_id")
        s_dir_claim = payload.get("source_directory_claim")
        has_id = s_dir_id is not None
        has_claim = s_dir_claim is not None
        if has_id == has_claim:
            raise DomainError("invalid", "import_plan requires exactly one of source_directory_id or source_directory_claim")
        if s_dir_id is not None and not isinstance(s_dir_id, str):
            raise DomainError("invalid", "import_plan source_directory_id must be a string or null")
        claim_val = _to_resource_claim(s_dir_claim) if s_dir_claim is not None else None
        orig_task = payload.get("original_task_id")
        if not isinstance(orig_task, str) or not orig_task.strip():
            raise DomainError("invalid", "import_plan requires non-empty string original_task_id")
        orig_snap = payload.get("original_snapshot")
        if not isinstance(orig_snap, Mapping):
            raise DomainError("invalid", "import_plan requires mapping original_snapshot")
        orig_snap_val = _validate_and_freeze_json_tree(orig_snap)
        prov = _to_document_ref(payload.get("provenance"))
        ws_stopped = payload.get("writers_stopped")
        if not isinstance(ws_stopped, str) or not ws_stopped.strip():
            raise DomainError("invalid", "import_plan requires non-empty string writers_stopped")
        known_ctx_raw = payload.get("known_contexts")
        if not isinstance(known_ctx_raw, Mapping):
            raise DomainError("invalid", "import_plan requires mapping known_contexts")
        known_ctx = {}
        for k, v in known_ctx_raw.items():
            if not isinstance(k, str):
                raise DomainError("invalid", "known_contexts keys must be strings")
            if _is_execution_context_claims(v):
                known_ctx[k] = _to_execution_context_claims(v)
            else:
                known_ctx[k] = _to_execution_context(v)
        validated_payload = {
            "host_id": host_id,
            "source_directory_id": s_dir_id,
            "source_directory_claim": claim_val,
            "original_task_id": orig_task,
            "original_snapshot": orig_snap_val,
            "provenance": prov,
            "writers_stopped": ws_stopped,
            "known_contexts": known_ctx,
        }

    elif kind == "resolve_store_hold":
        cutoff = payload.get("backup_cutoff")
        if not isinstance(cutoff, str) or not cutoff.strip():
            raise DomainError("invalid", "resolve_store_hold requires non-empty string backup_cutoff")
        ic = payload.get("inventory_complete")
        if type(ic) is not bool:
            raise DomainError("invalid", "resolve_store_hold inventory_complete must be a boolean")
        awi_raw = payload.get("absent_work_inventory")
        if not isinstance(awi_raw, (tuple, list)):
            raise DomainError("invalid", "resolve_store_hold requires list or tuple absent_work_inventory")
        awi = tuple(_to_host_work(w) for w in awi_raw)
        rr_raw = payload.get("resource_resolutions")
        if not isinstance(rr_raw, (tuple, list)):
            raise DomainError("invalid", "resolve_store_hold requires list or tuple resource_resolutions")
        rr = tuple(_to_resource_resolution(r) for r in rr_raw)
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise DomainError("invalid", "resolve_store_hold requires non-empty string reason")
        validated_payload = {
            "backup_cutoff": cutoff,
            "inventory_complete": ic,
            "absent_work_inventory": awi,
            "resource_resolutions": rr,
            "reason": reason,
        }

    # Normative capability matrix token validation
    if kind in BOOTSTRAP_KINDS:
        if orchestrator_token is not None or attempt_token is not None:
            raise DomainError("invalid", f"Bootstrap kind '{kind}' forbids orchestrator and attempt tokens")
    elif kind in WORKER_KINDS:
        if kind == "record_checkpoint":
            cp_val = validated_payload.get("checkpoint")
            relay = cp_val.relay if isinstance(cp_val, Checkpoint) else None
        else:
            relay = validated_payload.get("relay")

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
    frozen_payload = _freeze_nested(validated_payload)

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
