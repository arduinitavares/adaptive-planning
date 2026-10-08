"""Typed semantic tools over the official MCP server and the shared service authority."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
from threading import RLock
from typing import Annotated, Any, Literal, TYPE_CHECKING

from mcp import MCPError
from mcp.server import MCPServer, ServerRequestContext
from mcp.server.context import CallNext, HandlerResult
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult, ErrorData, JSONRPCError, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError
from starlette.types import ASGIApp, Receive, Scope, Send

from adaptive_planning import plans
from adaptive_planning.types import DomainError, Metadata, parse_command

if TYPE_CHECKING:
    from adaptive_planning.app import PlanningService

_LOG = logging.getLogger("adaptive_planning.transport")
_READ_NAMES = frozenset({"read_service_info", "list_plans", "read_plan", "read_events", "list_ready", "read_coordination"})
_ARGUMENTS = {
    "read_service_info": frozenset(), "list_plans": frozenset({"include_terminal"}),
    "read_plan": frozenset({"plan_id"}), "read_events": frozenset({"plan_id", "after", "limit"}),
    "list_ready": frozenset({"plan_id"}), "read_coordination": frozenset({"plan_id", "after", "limit"}),
    "create_plan": frozenset({"command"}), "revise_plan": frozenset({"command"}), "takeover": frozenset({"command"}),
    "register_resource": frozenset({"command"}), "reconcile_resources": frozenset({"command"}),
    "assign_step": frozenset({"command"}),
    "bind_availability": frozenset({"command"}), "start_step": frozenset({"command"}),
    "report_activity": frozenset({"command"}),
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


CommandInput = Annotated[CreatePlanInput | RevisePlanInput | TakeoverInput | RegisterResourceInput | ReconcileResourcesInput | AssignStepInput
                         | BindAvailabilityInput | StartStepInput | ReportActivityInput,
                         Field(discriminator="kind")]
_PUBLIC_FIELDS = frozenset({"command", "kind", "payload", *_Envelope.model_fields,
                           *_CreatePayload.model_fields, *_RevisePayload.model_fields, *_TakeoverPayload.model_fields,
                           *_ResourceClaimInput.model_fields, *_RegisterResourcePayload.model_fields,
                           *_ReconcileResourcesPayload.model_fields,
                           *_ExecutionContextInput.model_fields, *_AssignStepPayload.model_fields,
                           *_ArtifactInput.model_fields, *_EvidenceInput.model_fields, *_AvailabilityInput.model_fields,
                           *_BindAvailabilityPayload.model_fields, *_ActivityPayload.model_fields,
                           "plan_id", "include_terminal", "after", "limit", *_ARGUMENTS})


def _error_payload(code: str, metadata: Metadata | None = None) -> dict[str, Any]:
    code = code if code in _MESSAGES else "internal_error"
    error: dict[str, Any] = {"code": code, "message": _MESSAGES[code]}
    if isinstance(metadata, Metadata):
        error["current_metadata"] = plans.to_jsonable(metadata)
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


def build_mcp(service: PlanningService) -> MCPServer:
    server = _PlanningMCPServer("adaptive-planning", middleware=[_safe_protocol_errors])
    read = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
    write = ToolAnnotations(read_only_hint=False, idempotent_hint=True, open_world_hint=False)

    def read_value(operation):
        try:
            return plans.to_jsonable(operation())
        except DomainError as error:
            raise _ServiceToolError(error) from None

    def execute(kind: str, command: CommandInput):
        try:
            if command.kind != kind:
                raise DomainError("invalid", "The command kind must match its semantic tool.")
            parsed = parse_command(command.model_dump(mode="python"))
            return plans.to_jsonable(service.execute(parsed))
        except DomainError as error:
            raise _ServiceToolError(error) from None

    @server.tool(annotations=read)
    def read_service_info() -> dict[str, Any]:
        """Read the public installation identity and dashboard URL."""
        return read_value(service.read_service_info)

    @server.tool(annotations=read)
    def list_plans(include_terminal: StrictBool = False) -> dict[str, Any]:
        """List complete public plan views within the public response limit."""
        return {"plans": read_value(lambda: service.list_plans(include_terminal))}

    @server.tool(annotations=read)
    def read_plan(plan_id: str) -> dict[str, Any]:
        """Read a public plan, metadata and bounded coordination summary."""
        return read_value(lambda: service.read_plan(plan_id))

    @server.tool(annotations=read)
    def read_events(plan_id: str, after: StrictInt = 0, limit: StrictInt = 100) -> dict[str, Any]:
        """Read a bounded public event page after the given sequence."""
        return {"events": read_value(lambda: service.read_events(plan_id, after, limit))}

    @server.tool(annotations=read)
    def list_ready(plan_id: str) -> dict[str, Any]:
        """Read planning readiness and reasons without assigning execution."""
        return {"steps": read_value(lambda: service.list_ready(plan_id))}

    @server.tool(annotations=read)
    def read_coordination(plan_id: str, after: str | None = None, limit: StrictInt = 100) -> dict[str, Any]:
        """Read a bounded public coordination page using its opaque cursor."""
        return read_value(lambda: service.read_coordination(plan_id, after, limit))

    @server.tool(annotations=write)
    def create_plan(command: CommandInput) -> dict[str, Any]:
        """Create an authorized plan and return its private bootstrap receipt."""
        return execute("create_plan", command)

    @server.tool(annotations=write)
    def revise_plan(command: CommandInput) -> dict[str, Any]:
        """Revise a plan with current metadata and its orchestrator grant."""
        return execute("revise_plan", command)

    @server.tool(annotations=write)
    def takeover(command: CommandInput) -> dict[str, Any]:
        """Take over a plan using the expected ownership version; return the new grant privately."""
        return execute("takeover", command)

    @server.tool(annotations=write)
    def register_resource(command: CommandInput) -> dict[str, Any]:
        """Register current-host identity with the current plan owner's grant; create no execution use."""
        return execute("register_resource", command)

    @server.tool(annotations=write)
    def reconcile_resources(command: CommandInput) -> dict[str, Any]:
        """Reconcile explicitly attested equivalent identities while preserving every unresolved use."""
        return execute("reconcile_resources", command)

    @server.tool(annotations=write)
    def assign_step(command: CommandInput) -> dict[str, Any]:
        """Assign one frozen authorized step after atomic scope, ownership and resource checks."""
        return execute("assign_step", command)

    @server.tool(annotations=write)
    def bind_availability(command: CommandInput) -> dict[str, Any]:
        """Bind accepted prerequisite outputs to one assigned consumer's final checked base."""
        return execute("bind_availability", command)

    @server.tool(annotations=write)
    def start_step(command: CommandInput) -> dict[str, Any]:
        """Start an authenticated worker after final-base input and current execution guards."""
        return execute("start_step", command)

    @server.tool(annotations=write)
    def report_activity(command: CommandInput) -> dict[str, Any]:
        """Record actual authenticated worker observation without changing execution lifecycle."""
        return execute("report_activity", command)

    return server
