"""Typed semantic tools over the official MCP server and the shared service authority."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import is_dataclass
import json
import logging
import math
import time
from threading import RLock
from types import FunctionType
from typing import Annotated, Any, Literal, TYPE_CHECKING
import weakref

from mcp import MCPError
from mcp.server import MCPServer, ServerRequestContext
from mcp.server.context import CallNext, HandlerResult
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult, ErrorData, JSONRPCError, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError
from starlette.types import ASGIApp, Receive, Scope, Send

from adaptive_planning import plans, types as domain
from adaptive_planning.types import DomainError, Metadata, PlanView, parse_command

if TYPE_CHECKING:
    from adaptive_planning.app import PlanningService

_LOG = logging.getLogger("adaptive_planning.transport")
_READ_NAMES = frozenset({"read_service_info", "list_plans", "read_plan", "read_events", "list_ready", "read_coordination", "read_measurements"})
_OBSERVATION_WRITES = frozenset({"record_measurement"})
_ARGUMENTS = {
    "read_measurements": frozenset({"plan_id", "after", "limit"}),
    "record_measurement": frozenset({"observation"}),
    "read_service_info": frozenset(), "list_plans": frozenset({"include_terminal"}),
    "read_plan": frozenset({"plan_id"}), "read_events": frozenset({"plan_id", "after", "limit"}),
    "list_ready": frozenset({"plan_id"}), "read_coordination": frozenset({"plan_id", "after", "limit"}),
    "create_plan": frozenset({"command"}), "import_plan": frozenset({"command"}), "revise_plan": frozenset({"command"}), "takeover": frozenset({"command"}),
    "register_resource": frozenset({"command"}), "reconcile_resources": frozenset({"command"}),
    "assign_step": frozenset({"command"}),
    "bind_availability": frozenset({"command"}), "start_step": frozenset({"command"}),
    "report_activity": frozenset({"command"}),
    "record_checkpoint": frozenset({"command"}),
    "resolve_reassessment": frozenset({"command"}), "invalidate_result": frozenset({"command"}),
    "resolve_invalidation": frozenset({"command"}),
    "record_review": frozenset({"command"}), "reserve_verification": frozenset({"command"}),
    "accept_step": frozenset({"command"}),
    "release_resource_use": frozenset({"command"}), "reopen_attempt": frozenset({"command"}),
    "sweep_stale": frozenset({"command"}), "resolve_attempt": frozenset({"command"}),
    "append_late_evidence": frozenset({"command"}),
    "resolve_store_hold": frozenset({"command"}),
}
_MESSAGES = {
    "invalid": "Tool arguments are invalid.", "not_found": "The requested planning record was not found.",
    "conflict": "The command conflicts with current state.", "forbidden": "The command is not authorized.",
    "fenced": "The ownership grant is no longer current.", "stale_epoch": "The installation epoch has changed.",
    "stale": "The planning metadata is no longer current.", "held": "The operation is held.",
    "resource_conflict": "The requested resources conflict.", "busy": "The planning store is busy.",
    "schema_newer": "The store schema is newer than this service.", "handoff_conflict": "The handoff conflicts with current state.",
    "limit_exceeded": "The public result exceeds the response limit.", "unsupported": "The operation is not enabled.",
    "duplicate_candidate": "A possible duplicate plan exists; supply an explicit new plan intent.",
    "history_incomplete": "The public planning history is incomplete.",
    "internal_error": "The tool could not be completed.",
}
_PROTOCOL_MESSAGES = {-32700: "Parse error", -32600: "Invalid request", -32601: "Method not found",
                      -32602: "Invalid request parameters", -32603: "Internal error"}
_SDK_LOGGER_NAMES = ("mcp.server._streamable_http_modern", "mcp.server.runner")
_SDK_LOG_SCOPE = ContextVar("planning_sdk_log_scope", default=False)
_SDK_LOG_LOCK = RLock()
_SDK_LOG_USERS = 0


class _OwnedSdkLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # These pinned server channels can log peer notification names before
        # middleware. Suppress their diagnostics only in our owned context.
        return not _SDK_LOG_SCOPE.get()


_SDK_LOG_FILTER = _OwnedSdkLogs()


@contextmanager
def mcp_server_diagnostics():
    """Contain SDK diagnostics across owned requests, background tasks and lifespan."""
    global _SDK_LOG_USERS
    with _SDK_LOG_LOCK:
        if _SDK_LOG_USERS == 0:
            for name in _SDK_LOGGER_NAMES:
                logging.getLogger(name).addFilter(_SDK_LOG_FILTER)
        _SDK_LOG_USERS += 1
    token = _SDK_LOG_SCOPE.set(True)
    try:
        yield
    finally:
        _SDK_LOG_SCOPE.reset(token)
        with _SDK_LOG_LOCK:
            _SDK_LOG_USERS -= 1
            if _SDK_LOG_USERS == 0:
                for name in _SDK_LOGGER_NAMES:
                    logging.getLogger(name).removeFilter(_SDK_LOG_FILTER)


class MCPServerDiagnostics:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        with mcp_server_diagnostics():
            await self.app(scope, receive, send)


class MCPHttpErrors:
    """Project only the official HTTP child's bounded JSON error responses."""

    def __init__(self, app: ASGIApp, *, max_error_body_bytes: int = 64 * 1024) -> None:
        self.app = app
        self.max_error_body_bytes = max_error_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = None
        body = bytearray()
        project = False
        failed = False

        async def projected_send(message):
            nonlocal start, project, failed
            if failed:
                # No fallback from inside the SDK may publish the original diagnostic.
                raise DomainError("internal_error", "The protocol error response could not be completed.") from None
            if message["type"] == "http.response.start":
                headers = dict(message.get("headers", ()))
                content_type = headers.get(b"content-type", b"").split(b";", 1)[0].lower()
                project = message["status"] >= 400 and content_type == b"application/json"
                if project:
                    start = dict(message)
                    return
            elif message["type"] == "http.response.body" and project:
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > self.max_error_body_bytes:
                    failed = True
                    raise DomainError("limit_exceeded", "The protocol error response exceeds the diagnostic limit.") from None
                body.extend(chunk)
                if message.get("more_body", False):
                    return
                try:
                    original = JSONRPCError.model_validate_json(body)
                except ValidationError:
                    failed = True
                    raise DomainError("internal_error", "The protocol error response is not valid.") from None
                projected = JSONRPCError(jsonrpc="2.0", id=original.id, error=ErrorData(
                    code=original.error.code,
                    message=_PROTOCOL_MESSAGES.get(original.error.code, "The protocol request could not be completed.")))
                payload = projected.model_dump_json(by_alias=True, exclude_unset=True).encode("utf-8")
                start["headers"] = [(key, value) for key, value in start["headers"] if key.lower() != b"content-length"]
                start["headers"].append((b"content-length", str(len(payload)).encode("ascii")))
                await send(start)
                await send({"type": "http.response.body", "body": payload})
                return
            await send(message)

        await self.app(scope, receive, projected_send)


async def _safe_protocol_errors(ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
    # The official public middleware seam wraps unknown methods and params validation.
    # Leave IDs, dispatch and error serialization to the SDK; retain codes, omit peer data.
    try:
        return await call_next(ctx)
    except MCPError as error:
        code = error.code
    except ValidationError:
        code = -32602
    except Exception:
        code = -32603
    _LOG.warning("protocol rejected: code=%d", code)
    raise MCPError(code=code, message=_PROTOCOL_MESSAGES.get(code, "The protocol request could not be completed.")) from None


class _InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class _CreatePayload(_InputModel):
    creation_id: str
    draft: dict[str, Any]
    new_plan_intent: str | None


class _RevisePayload(_InputModel):
    draft: dict[str, Any]
    reason: str
    authorization_change: dict[str, Any] | None
    retire_steps: list[dict[str, Any]] = Field(default_factory=list)


class _TakeoverPayload(_InputModel):
    expected_ownership_version: int
    reason: str


class _ResourceClaimInput(_InputModel):
    host_id: str
    kind: Literal["repository", "workspace", "external"]
    device: int | None
    inode: int | None
    canonical_path: str | None = Field(repr=False)
    case_insensitive: bool
    repository_id: str | None
    external_identity: str | None = Field(repr=False)


class _RegisterResourcePayload(_InputModel):
    claim: _ResourceClaimInput


class _ReconcileResourcesPayload(_InputModel):
    resource_ids: list[str]
    canonical_claim: _ResourceClaimInput
    reason: str


class _ExecutionContextInput(_InputModel):
    kind: Literal["repository", "non-repository", "external-only", "read-only"]
    repository_id: str | None
    workspace_id: str | None
    repository_exclusive: bool
    external_ids: list[str]
    writes: bool
    checked_base: str | None


class _ExecutionContextClaimsInput(_InputModel):
    kind: Literal["repository", "non-repository", "external-only", "read-only"]
    repository_claim: _ResourceClaimInput | None
    workspace_claim: _ResourceClaimInput | None
    repository_exclusive: bool
    external_claims: list[_ResourceClaimInput]
    writes: bool
    checked_base: str | None


class _DocumentRefInput(_InputModel):
    path: str
    branch: str | None
    commit: str | None
    version: str
    task_id: str | None


class _ImportPayload(_InputModel):
    host_id: str
    source_directory_id: str | None
    source_directory_claim: _ResourceClaimInput | None
    original_task_id: str
    original_snapshot: dict[str, Any]
    provenance: _DocumentRefInput
    writers_stopped: str
    known_contexts: dict[str, _ExecutionContextInput | _ExecutionContextClaimsInput]


class _AssignStepPayload(_InputModel):
    step_id: str
    assignee: str
    context: _ExecutionContextInput


class _ArtifactInput(_InputModel):
    kind: Literal["code", "document", "data", "observation"]
    identity: str
    version: str
    digest: str | None
    summary: str


class _EvidenceInput(_InputModel):
    summary: str
    artifact: _ArtifactInput | None
    workspace_id: str | None
    check: str
    result: Literal["passed", "failed", "blocked", "observation"]
    limitations: list[str]
    blockers: list[str]


class _AvailabilityInput(_InputModel):
    prerequisite: str
    result_version: str
    method: Literal["ancestor", "merge", "cherry-pick", "equivalent", "observation"]
    checked_base: str
    artifact_digest: str | None
    evidence: _EvidenceInput


class _BindAvailabilityPayload(_InputModel):
    bindings: list[_AvailabilityInput]
    checked_base: str


class _ActivityPayload(_InputModel):
    origin_worker: str
    relay: str | None
    observed_activity: str


class _ReassessmentInput(_InputModel):
    summary: str
    facts: list[str]
    assumptions: list[str]
    disposition: Literal["unchanged", "material"]
    reason: str
    affected_steps: list[str]
    unbounded: StrictBool
    proposed_draft: dict[str, Any] | None


class _CheckpointInput(_InputModel):
    outcome: Literal["passed", "failed", "blocked"]
    check: str
    evidence: list[_EvidenceInput]
    reassessment: _ReassessmentInput
    origin_worker: str
    relay: str | None
    artifact: _ArtifactInput | None


class _RecordCheckpointPayload(_InputModel):
    checkpoint: _CheckpointInput


class _ResolveReassessmentPayload(_InputModel):
    checkpoint_id: str
    decision: Literal["apply", "reject", "hold"]
    reason: str
    draft: dict[str, Any] | None
    affected_steps: list[str]
    unbounded: StrictBool


class _InvalidateResultPayload(_InputModel):
    step_id: str
    result_version: str
    reason: str
    evidence: list[_EvidenceInput]


class _ResolveInvalidationPayload(_InputModel):
    invalidation_id: str
    affected_step_id: str
    disposition: Literal["revalidated", "superseded", "replanned"]
    corrective_step_id: str | None
    corrected_result_version: str | None
    evidence: list[_EvidenceInput]
    reason: str


class _ReviewInput(_InputModel):
    reviewer: str
    artifact: _ArtifactInput
    verdict: Literal["passed", "failed"]
    findings: list[str]
    evidence: list[_EvidenceInput]
    verification_use_id: str | None


class _RecordReviewPayload(_InputModel):
    step_id: str
    review: _ReviewInput


class _AcceptStepPayload(_InputModel):
    step_id: str
    artifact: _ArtifactInput
    stopped_resource_ids: list[str]
    stopped_reason: str | None


class _ReserveVerificationPayload(_InputModel):
    step_id: str
    verifier: str
    context: _ExecutionContextInput
    handoff_from: str | None
    stopped_reason: str | None


class _ReleaseResourceUsePayload(_InputModel):
    use_id: str
    resource_ids: list[str]
    reason: str
    stopped: StrictBool


class _ReopenAttemptPayload(_InputModel):
    reason: str


class _SweepStalePayload(_InputModel):
    pass


class _ResolveAttemptPayload(_InputModel):
    resolution: Literal["confirm-owner", "replace", "reject", "release", "attach-context"]
    reason: str
    next_step_state: Literal["pending", "blocked", "retired"]
    stopped_resource_ids: list[str]
    replacement_assignee: str | None
    replacement_context: _ExecutionContextInput | None
    attached_context: dict[str, Any] | None = None


class _AppendLateEvidencePayload(_InputModel):
    origin_attempt_id: str
    evidence: list[_EvidenceInput]
    stopped_resource_ids: list[str]
    reason: str


class _HostWorkInput(_InputModel):
    work_id: str
    actor: str
    state: Literal['running','stopped','unknown']
    resource_ids: list[str]
    resource_claims: list[_ResourceClaimInput]
    evidence: list[_EvidenceInput]


class _ResourceResolutionInput(_InputModel):
    use_id: str | None
    absent_work_id: str | None
    resource_ids: list[str]
    resolution: Literal['stopped','isolated','unresolved']
    reason: str


class _ResolveStoreHoldPayload(_InputModel):
    backup_cutoff: str
    inventory_complete: StrictBool
    absent_work_inventory: list[_HostWorkInput]
    resource_resolutions: list[_ResourceResolutionInput]
    reason: str


class _Envelope(_InputModel):
    epoch: str
    key: str
    actor: str
    plan_id: str | None = None
    generation: str | None = None
    expected_revision: int | None = None
    ownership_version: int | None = None
    orchestrator_token: str | None = Field(default=None, repr=False)
    attempt_id: str | None = None
    attempt_token: str | None = Field(default=None, repr=False)
    attempt_version: int | None = None
    definition_version: str | None = None


class CreatePlanInput(_Envelope):
    kind: Literal["create_plan"]
    payload: _CreatePayload


class ImportPlanInput(_Envelope):
    kind: Literal["import_plan"]
    payload: _ImportPayload


class RevisePlanInput(_Envelope):
    kind: Literal["revise_plan"]
    payload: _RevisePayload


class TakeoverInput(_Envelope):
    kind: Literal["takeover"]
    payload: _TakeoverPayload


class RegisterResourceInput(_Envelope):
    kind: Literal["register_resource"]
    payload: _RegisterResourcePayload


class ReconcileResourcesInput(_Envelope):
    kind: Literal["reconcile_resources"]
    payload: _ReconcileResourcesPayload


class AssignStepInput(_Envelope):
    kind: Literal["assign_step"]
    payload: _AssignStepPayload


class BindAvailabilityInput(_Envelope):
    kind: Literal["bind_availability"]
    payload: _BindAvailabilityPayload


class StartStepInput(_Envelope):
    kind: Literal["start_step"]
    payload: _ActivityPayload


class ReportActivityInput(_Envelope):
    kind: Literal["report_activity"]
    payload: _ActivityPayload


class RecordCheckpointInput(_Envelope):
    kind: Literal["record_checkpoint"]
    payload: _RecordCheckpointPayload


class ResolveReassessmentInput(_Envelope):
    kind: Literal["resolve_reassessment"]
    payload: _ResolveReassessmentPayload


class InvalidateResultInput(_Envelope):
    kind: Literal["invalidate_result"]
    payload: _InvalidateResultPayload


class ResolveInvalidationInput(_Envelope):
    kind: Literal["resolve_invalidation"]
    payload: _ResolveInvalidationPayload


class RecordReviewInput(_Envelope):
    kind: Literal["record_review"]
    payload: _RecordReviewPayload


class AcceptStepInput(_Envelope):
    kind: Literal["accept_step"]
    payload: _AcceptStepPayload


class ReserveVerificationInput(_Envelope):
    kind: Literal["reserve_verification"]
    payload: _ReserveVerificationPayload


class ReleaseResourceUseInput(_Envelope):
    kind: Literal["release_resource_use"]
    payload: _ReleaseResourceUsePayload


class ReopenAttemptInput(_Envelope):
    kind: Literal["reopen_attempt"]
    payload: _ReopenAttemptPayload


class SweepStaleInput(_Envelope):
    kind: Literal["sweep_stale"]
    payload: _SweepStalePayload


class ResolveAttemptInput(_Envelope):
    kind: Literal["resolve_attempt"]
    payload: _ResolveAttemptPayload


class AppendLateEvidenceInput(_Envelope):
    kind: Literal["append_late_evidence"]
    payload: _AppendLateEvidencePayload


class ResolveStoreHoldInput(_Envelope):
    kind: Literal['resolve_store_hold']
    payload: _ResolveStoreHoldPayload


CommandInput = Annotated[CreatePlanInput | ImportPlanInput | RevisePlanInput | TakeoverInput | RegisterResourceInput | ReconcileResourcesInput | AssignStepInput
                         | BindAvailabilityInput | StartStepInput | ReportActivityInput | RecordCheckpointInput
                         | ResolveReassessmentInput | InvalidateResultInput | ResolveInvalidationInput
                         | RecordReviewInput | AcceptStepInput | ReserveVerificationInput | ReleaseResourceUseInput | ReopenAttemptInput
                         | SweepStaleInput | ResolveAttemptInput | AppendLateEvidenceInput | ResolveStoreHoldInput,
                         Field(discriminator="kind")]
_PUBLIC_FIELDS = frozenset({"command", "kind", "payload", *_Envelope.model_fields,
                           *_CreatePayload.model_fields, *_ImportPayload.model_fields, *_RevisePayload.model_fields, *_TakeoverPayload.model_fields,
                           *_ResourceClaimInput.model_fields, *_RegisterResourcePayload.model_fields,
                           *_ReconcileResourcesPayload.model_fields,
                           *_ExecutionContextInput.model_fields, *_ExecutionContextClaimsInput.model_fields,
                           *_DocumentRefInput.model_fields, *_AssignStepPayload.model_fields,
                           *_ArtifactInput.model_fields, *_EvidenceInput.model_fields, *_AvailabilityInput.model_fields,
                           *_AcceptStepPayload.model_fields,
                           *_BindAvailabilityPayload.model_fields, *_ActivityPayload.model_fields,
                           *_ReassessmentInput.model_fields, *_CheckpointInput.model_fields, *_RecordCheckpointPayload.model_fields,
                           *_ResolveReassessmentPayload.model_fields, *_InvalidateResultPayload.model_fields,
                           *_ResolveInvalidationPayload.model_fields,
                           *_ReviewInput.model_fields, *_RecordReviewPayload.model_fields,
                           *_ReserveVerificationPayload.model_fields, *_ReleaseResourceUsePayload.model_fields,
                           *_ReopenAttemptPayload.model_fields,
                           *_SweepStalePayload.model_fields, *_ResolveAttemptPayload.model_fields,
                           *_AppendLateEvidencePayload.model_fields,
                           *_HostWorkInput.model_fields, *_ResourceResolutionInput.model_fields, *_ResolveStoreHoldPayload.model_fields,
                           "plan_id", "include_terminal", "after", "limit", *_ARGUMENTS,
                           "observation", "source_record_id", "observed_at", "checkpoint_sequence", "data"})


# Explicit public shapes: adding a DTO or field requires a public-contract decision.
# Input-only/internal command envelopes and private receipts are never projected here.
_PUBLIC_DTOS = {
    domain.InstallationView: ("epoch", "schema_version", "host_id", "dashboard_url", "store_hold", "restore_cutoff"),
    Metadata: ("epoch", "plan_id", "generation", "revision", "ownership_version", "dashboard_url"),
    domain.ArtifactRef: ("kind", "identity", "version", "digest", "summary"),
    domain.DocumentRef: ("path", "branch", "commit", "version", "task_id"),
    domain.StepDefinition: ("step_id", "title", "dependencies", "completion_check", "document", "definition_version", "review_required", "final_gate"),
    domain.Authorization: ("mode", "interval", "since_revision", "note", "baseline", "claimed"),
    domain.ReservationView: ("use_id", "plan_id", "attempt_id", "absent_work_id", "resource_ids", "actor", "writer_active", "unresolved", "repository_exclusive"),
    domain.ExecutionContext: ("kind", "repository_id", "workspace_id", "repository_exclusive", "external_ids", "writes", "checked_base"),
    domain.Evidence: ("summary", "artifact", "workspace_id", "check", "result", "limitations", "blockers"),
    domain.PlanDraft: ("objective", "acceptance_criteria", "constraints", "facts", "assumptions", "next_check", "authorization", "steps", "documents", "workspace_refs", "workflow", "horizon", "stop_reason", "block_reason"),
    domain.Reassessment: ("summary", "facts", "assumptions", "disposition", "reason", "affected_steps", "unbounded", "proposed_draft"),
    domain.Checkpoint: ("outcome", "check", "evidence", "reassessment", "origin_worker", "relay", "artifact"),
    domain.Review: ("reviewer", "artifact", "verdict", "findings", "evidence", "verification_use_id"),
    domain.ReviewView: ("plan_id", "step_id", "attempt_id", "definition_version", "recorded_sequence", "review"),
    domain.AttemptView: ("attempt_id", "step_id", "assignee", "state", "prior_state", "version", "definition_version", "last_activity", "context", "recovery_required", "result_version"),
    domain.HoldView: ("hold_id", "kind", "step_id", "source_id", "reason"),
    domain.InvalidationView: ("invalidation_id", "producer_step_id", "result_version", "affected_steps", "resolved_steps", "recorded_sequence"),
    domain.CoordinationItem: ("kind", "value"),
    domain.CoordinationPage: ("plan_id", "revision", "store_sequence", "items", "next_cursor", "total_counts"),
    domain.CoordinationView: ("attempts", "reservations", "holds", "reviews", "result_versions", "store_hold", "restore_cutoff", "ownership_actor", "ownership_version", "store_sequence", "next_cursor", "total_counts", "has_live_attempts", "has_unresolved_uses", "has_plan_holds"),
    PlanView: ("plan_id", "metadata", "draft", "legacy_snapshot", "legacy_publication", "coordination", "status", "published_at"),
    domain.Event: ("sequence", "plan_revision", "plan_id", "kind", "actor", "origin_worker", "relay", "published_at", "payload"),
    domain.ReadyStep: ("step_id", "ready", "reasons"),
}


def _public_json(value: Any) -> Any:
    """Detach known public DTOs and JSON; refuse typed private or unknown shapes.

    Arbitrary valid JSON keys/prose remain user data. Flattened credentials cannot
    be distinguished from identical legitimate JSON without a new domain policy.
    """
    active: set[int] = set()

    def reject():
        raise DomainError("internal_error", "The public result violates the release contract.") from None

    def convert(item):
        item_type = type(item)
        if item_type in (type(None), str, bool, int):
            return item
        if item_type is float:
            if not math.isfinite(item):
                reject()
            return item
        fields = _PUBLIC_DTOS.get(item_type)
        if fields is None and (is_dataclass(item) or not isinstance(item, Mapping) and item_type not in (list, tuple)):
            reject()
        identity = id(item)
        if identity in active:
            reject()
        active.add(identity)
        try:
            if fields is not None:
                if set(vars(item)) != set(fields):
                    reject()
                return {field: convert(getattr(item, field)) for field in fields}
            if isinstance(item, Mapping):
                result = {}
                for key, child in item.items():
                    if type(key) is not str:
                        reject()
                    result[key] = convert(child)
                return result
            return [convert(child) for child in item]
        finally:
            active.remove(identity)

    try:
        return convert(value)
    except Exception:
        # No offending value, key, path, subclass name or recursive diagnostic escapes.
        reject()


def _error_payload(code: str, metadata: Metadata | None = None) -> dict[str, Any]:
    code = code if code in _MESSAGES else "internal_error"
    error: dict[str, Any] = {"code": code, "message": _MESSAGES[code]}
    if type(metadata) is Metadata:
        try:
            error["current_metadata"] = _public_json(metadata)
        except DomainError:
            # Preserve the safe domain code while omitting contaminated public metadata.
            pass
    return {"error": error}


def _validation_details(error: ValidationError) -> dict[str, Any]:
    # Removing input/context does not sanitize custom messages or peer-controlled locations.
    # Only known field names and numeric array positions survive; messages are constant.
    errors = error.errors(include_input=False, include_context=False, include_url=False)
    return {"validation_error_count": len(errors),
            "details": [{"loc": [part if type(part) is int or part in _PUBLIC_FIELDS else "input" for part in item["loc"]],
                         "message": "Invalid value."} for item in errors[:20]]}


class _ServiceToolError(ToolError):
    def __init__(self, error: DomainError) -> None:
        self.payload = _error_payload(error.code, error.current_metadata)
        super().__init__(self.payload["error"]["message"])


class _PlanningMCPServer(MCPServer):
    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> CallToolResult:
        started = time.perf_counter()
        outcome = "internal_error"
        try:
            result = await self._dispatch_tool(name, arguments, context)
            outcome = "tool_error" if result.is_error else "ok"
            return result
        finally:
            sink = getattr(self, "measurement_sink", None)
            if sink is not None:
                sink(name if isinstance(name, str) and name in _ARGUMENTS else "unknown",
                     (time.perf_counter() - started) * 1000, outcome)

    async def _dispatch_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> CallToolResult:
        # The SDK's public dispatch method owns schema validation. Returning an official
        # error result here keeps its wire wrapper from logging exception causes or raw locs.
        public_name = name if isinstance(name, str) and name in _ARGUMENTS else "unknown"
        payload = None
        if public_name == "unknown":
            payload = _error_payload("unsupported")
        elif not isinstance(arguments, dict) or set(arguments) - _ARGUMENTS[public_name]:
            payload = _error_payload("invalid")
        else:
            try:
                result = await super().call_tool(name, arguments, context)
                if public_name in _READ_NAMES:
                    # Count the SDK's actual serialized public text, including aggregate
                    # list envelopes. Reject an oversized complete result without truncation.
                    size = sum(len(block.text.encode("utf-8")) for block in result.content if isinstance(block, TextContent))
                    if size > plans.MAX_PAGE_BYTES:
                        payload = _error_payload("limit_exceeded")
                    else:
                        return result
                else:
                    return result
            except _ServiceToolError as error:
                payload = error.payload
            except UnexpectedToolError:
                payload = _error_payload("internal_error")
            except ToolError as error:
                payload = _error_payload("invalid")
                if isinstance(error.__cause__, _ServiceToolError):
                    payload = error.__cause__.payload
                elif isinstance(error.__cause__, ValidationError):
                    payload["error"].update(_validation_details(error.__cause__))
            except Exception:
                payload = _error_payload("internal_error")
        _LOG.warning("tool failed: name=%s code=%s", public_name, payload["error"]["code"])
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))],
                              structured_content=payload, is_error=True)


# Identity provenance is weak and batch-local; function attributes convey no authority.
# Each callback retains only an integer key and compares the exact expired reference.
_HANDLER_LOCK = RLock()
_HANDLER_ISSUANCE: dict[int, tuple[weakref.ReferenceType, str, str, object]] = {}


def _issue_handler(handler: Callable, name: str, role: str, batch: object) -> None:
    identity = id(handler)

    def collected(reference):
        with _HANDLER_LOCK:
            current = _HANDLER_ISSUANCE.get(identity)
            if current is not None and current[0] is reference:
                del _HANDLER_ISSUANCE[identity]

    reference = weakref.ref(handler, collected)
    with _HANDLER_LOCK:
        _HANDLER_ISSUANCE[identity] = (reference, name, role, batch)


def _validate_registered_handlers(tool_handlers: Mapping[str, Callable]) -> dict[str, Callable]:
    def reject():
        raise DomainError("internal_error", "The registered tools violate the release contract.") from None

    try:
        handlers = dict(tool_handlers)  # Hold genuine functions while validating a stable snapshot.
    except Exception:
        reject()
    mutations = domain.BOOTSTRAP_KINDS | domain.ORCHESTRATOR_KINDS | domain.WORKER_KINDS
    expected = mutations | _READ_NAMES | _OBSERVATION_WRITES
    if (set(handlers) != expected or set(_ARGUMENTS) != expected
            or set(_MUTATION_DESCRIPTIONS) != mutations
            or any(_ARGUMENTS[name] != frozenset({"command"}) for name in mutations)):
        reject()
    batch = None
    with _HANDLER_LOCK:
        for name, handler in handlers.items():
            if type(handler) is not FunctionType:
                reject()
            issued = _HANDLER_ISSUANCE.get(id(handler))
            if issued is None:
                reject()
            reference, issued_name, role, issued_batch = issued
            referent = reference()  # One dereference yields a strong local reference.
            if (referent is not handler or issued_name != name or handler.__name__ != name
                    or role != ("read" if name in _READ_NAMES else "observation" if name in _OBSERVATION_WRITES else "mutation")):
                reject()
            if batch is None:
                batch = issued_batch
            elif batch is not issued_batch:
                reject()
    return handlers


def validate_release_contract(tool_handlers: Mapping[str, Callable], public_view: PlanView) -> None:
    """Audit actual issued handlers and the same projection used by public reads.

    This audits the registration contract; it does not confine malicious trusted
    Python code or claim universal detection of credentials already flattened to JSON.
    """
    _validate_registered_handlers(tool_handlers)
    _public_json(public_view)


_MUTATION_DESCRIPTIONS = {
    'create_plan': 'Create an authorized plan and return its private bootstrap receipt.',
    'import_plan': 'Import an authorized legacy plan with explicit publisher authority handoff.',
    'revise_plan': 'Revise a plan with current metadata and its orchestrator grant.',
    'takeover': 'Take over a plan using the expected ownership version; return the new grant privately.',
    'register_resource': "Register current-host identity with the current plan owner's grant; create no execution use.",
    'reconcile_resources': 'Reconcile explicitly attested equivalent identities while preserving every unresolved use.',
    'assign_step': 'Assign one frozen authorized step after atomic scope, ownership and resource checks.',
    'bind_availability': "Bind accepted prerequisite outputs to one assigned consumer's final checked base.",
    'start_step': 'Start an authenticated worker after final-base input and current execution guards.',
    'report_activity': 'Record actual authenticated worker observation without changing execution lifecycle.',
    'record_checkpoint': 'Submit attributed worker findings and provisional reassessment holds atomically.',
    'resolve_reassessment': 'Classify one checkpoint and its residual holds; relay affected workers at a safe boundary.',
    'invalidate_result': 'Invalidate an accepted output and hold its producer and transitive consumers without deleting history.',
    'resolve_invalidation': 'Resolve one affected hold with exact artifact proof or an actual revised consumer route.',
    'record_review': 'Record an immutable verdict for the exact submitted or invalidated accepted artifact.',
    'accept_step': 'Accept one exact submitted output; stop only explicitly named original implementation resources.',
    'reserve_verification': 'Reserve an attributable writable verifier group without freeing retained implementation use.',
    'release_resource_use': 'Record an exact stopped-use observation and release the complete named use only when confirmed.',
    'reopen_attempt': 'Reopen the same current submitted attempt after scope, hold and retained-use checks.',
    'sweep_stale': 'Explicitly record overdue ownership in one target-plan batch without releasing resources.',
    'resolve_attempt': 'Confirm observed ownership or explicitly cancel, retire or replace an exact swept native attempt.',
    'append_late_evidence': 'Retain attributed historical observations and stop reports without releasing or accepting work.',
    'resolve_store_hold': 'Record explicit local absent-writer inventory and exact resource observations after restore.',
}


def _mutation_handler(service: PlanningService, kind: str) -> Callable:
    def handler(command: CommandInput) -> dict[str, Any]:
        try:
            if command.kind != kind:
                raise DomainError("invalid", "The command kind must match its semantic tool.")
            parsed = parse_command(command.model_dump(mode="python"))
            # Resolve live authority at invocation, after registration and fixture setup.
            return plans.to_jsonable(service.execute(parsed))
        except DomainError as error:
            raise _ServiceToolError(error) from None

    handler.__name__ = kind
    handler.__qualname__ = f"registered_handlers.<locals>.{kind}"
    handler.__doc__ = _MUTATION_DESCRIPTIONS[kind]
    return handler


class MeasurementInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_record_id: str = Field(min_length=1, max_length=160)
    epoch: str = Field(min_length=1, max_length=160)
    plan_id: str = Field(min_length=1, max_length=160)
    kind: Literal["context", "outcome", "defect", "effort", "coverage"]
    observed_at: str
    step_id: str | None = None
    attempt_id: str | None = None
    checkpoint_sequence: StrictInt | None = None
    data: dict[str, Any] = Field(description="Kind-specific receipt data. Call read_measurements and read receipt_contract for required fields, exact scope, idempotency and examples. Missing usage stays unknown.")
    orchestrator_token: str | None = None
    attempt_token: str | None = None


def registered_handlers(service: PlanningService) -> Mapping[str, Callable]:
    """Issue one complete batch of actual read and centrally gated mutation handlers."""
    def read_value(operation):
        try:
            return _public_json(operation())
        except DomainError as error:
            raise _ServiceToolError(error) from None

    def read_service_info() -> dict[str, Any]:
        """Read the public installation identity and dashboard URL."""
        return read_value(service.read_service_info)

    def read_measurements(plan_id: str | None = None, after: StrictInt = 0, limit: StrictInt = 100) -> dict[str, Any]:
        """Read automatic canonical lifecycle KPIs, semantic receipts, versions and bounded traces. Includes receipt_contract with the complete remote-readable record_measurement contract. Missing values are unknown."""
        return read_value(lambda: service.read_measurements(plan_id, after, limit))

    def record_measurement(observation: MeasurementInput) -> dict[str, Any]:
        """Append an idempotent receipt without changing planning state. First read read_measurements.receipt_contract for fields, authentication and examples. Reuse the same source_record_id and immutable body for retries; submit usage deltas once."""
        value = observation.model_dump(mode="python")
        owner, worker = value.pop("orchestrator_token"), value.pop("attempt_token")
        return read_value(lambda: service.record_measurement(value, owner, worker))

    def list_plans(include_terminal: StrictBool = False) -> dict[str, Any]:
        """List complete public plan views within the public response limit."""
        return {"plans": read_value(lambda: service.list_plans(include_terminal))}

    def read_plan(plan_id: str) -> dict[str, Any]:
        """Read a public plan, metadata and bounded coordination summary."""
        return read_value(lambda: service.read_plan(plan_id))

    def read_events(plan_id: str, after: StrictInt = 0, limit: StrictInt = 100) -> dict[str, Any]:
        """Read a bounded public event page after the given sequence."""
        return {"events": read_value(lambda: service.read_events(plan_id, after, limit))}

    def list_ready(plan_id: str) -> dict[str, Any]:
        """Read planning readiness and reasons without assigning execution."""
        return {"steps": read_value(lambda: service.list_ready(plan_id))}

    def read_coordination(plan_id: str, after: str | None = None, limit: StrictInt = 100) -> dict[str, Any]:
        """Read a bounded public coordination page using its opaque cursor."""
        return read_value(lambda: service.read_coordination(plan_id, after, limit))

    handlers = {
        "read_service_info": read_service_info, "list_plans": list_plans,
        "read_plan": read_plan, "read_events": read_events,
        "list_ready": list_ready, "read_coordination": read_coordination,
        "read_measurements": read_measurements, "record_measurement": record_measurement,
    }
    handlers.update((kind, _mutation_handler(service, kind)) for kind in _MUTATION_DESCRIPTIONS)
    batch = object()  # Standalone identity; does not retain service, handlers or SDK server.
    for name, handler in handlers.items():
        _issue_handler(handler, name, "read" if name in _READ_NAMES else "observation" if name in _OBSERVATION_WRITES else "mutation", batch)
    return handlers


def build_mcp(service: PlanningService) -> MCPServer:
    handlers = _validate_registered_handlers(registered_handlers(service))
    server = _PlanningMCPServer("adaptive-planning", middleware=[_safe_protocol_errors])
    server.measurement_sink = service.trace_measurement_call
    read = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
    write = ToolAnnotations(read_only_hint=False, idempotent_hint=True, open_world_hint=False)
    for name, handler in handlers.items():
        server.tool(name=name, annotations=read if name in _READ_NAMES else write)(handler)
    return server
