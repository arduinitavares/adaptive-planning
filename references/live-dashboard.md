# Live dashboard

The live dashboard provides real-time visualization of the canonical adaptive plan.
The selected adaptive-planning MCP instance provides the authoritative
dashboard for its plans. Discover its URLs through that connection.
Apply the Plan language policy in [SKILL.md](../SKILL.md#plan-language).
For initial authoring and material replanning, apply the
[maintained Superpowers integration](superpowers-planning.md).
Detailed task documents support the current graph. They do not replace its lifecycle authority.

Review new authored text with `simplified-technical-english` before publishing.
This includes `objective`, step `title`, `check`, `summary`, `details`,
`retiredReason`, `changeSummary`, and new authorization or recovery explanations.
Apply descriptive rules to authored observations in `evidence`.
Preserve quoted evidence and exact literals. Do not rewrite published history,
completed step fields, or an existing evidence prefix for language cleanup.
Language review does not change identifiers, statuses, dependencies, or scope.

Report language verification beside the plan. Do not add schema fields.
Missing dictionary or terminology sources permit an Issue 9-oriented candidate,
not a full-conformance claim. A missing companion is a separate dependency gap.

The dashboard visualizes the objective, dependencies, active assignments, completion
checks, evidence artifacts, retired steps, and revision history. It updates whenever
a mutation is committed through the service. It does not observe hidden agent activity
or authorize work.

## Service mode default and per-task server policy

### Select and retain one instance

Each plan has one selected service instance. Clients of that instance share
its canonical saved state. Independent instances do not automatically share
plans, grants, events, or resource reservations. Operators must coordinate
physical resources that are used through different instances.

For a new plan, apply this selection order:

1. Use the configured MCP entry explicitly selected for the task.
2. Otherwise, use an applicable private project preference.
3. Otherwise, use an applicable private user preference.
4. Otherwise, use the single configured service, whether local or remote.
5. If several services remain eligible, ask the user to select an entry.
   Ask before creating a plan.
6. If no service is configured, report the configuration gap.

If the selected or preferred entry is missing or unavailable, report the gap.
Ask before selecting another instance for a new plan. Do not silently fall back.
Shared service mode prohibits launching a separate per-task or per-session fallback server.

Use `read_service_info` through the selected MCP connection.
Use its `dashboard_url` for service links and its returned plan URLs for plan links.
The URL `http://127.0.0.1:8790/` is a local example. Deployment configuration
owns the actual addresses. Shared instructions state the selection rule.
Private project or user instructions, or memory, can name a preferred
configured entry for new plans. Do not put credentials in those instructions.

Retain the configured MCP entry, `host_id`, last observed `epoch`, and
`plan_id` in private task/session working records. These are working-record
fields, not new service schema fields. Retain an existing plan's binding
when a preference changes. An explicit move needs a separate migration
decision. This workflow does not provide migration or replication.

Before resuming, read service identity through the bound connection and read
the expected `plan_id`. For aliases of one service, compare `(host_id, epoch)`
through trusted configured connections. A URL or `host_id` alone does not
establish identity. These checks do not provide cryptographic proof against
an unauthorized copied database. Do not create duplicate canonical plans
through different aliases.

If the bound service retains its `host_id` but changes its `epoch`, reread
ownership, holds, and events. Follow the existing restore/recovery rules
before publishing. An epoch change does not authorize a move.
If the bound endpoint returns an unexpected `host_id`, stop publication.
Stop plan mutations. Report the identity mismatch. Obtain an explicit
recovery or migration decision before continuing.

During an outage, retain that plan's binding and last-known state.
Do not create a competing authority for that plan. Deliberately configured
independent instances can still serve other plans. An already assigned
bounded worker can finish local verification and retain unpublished evidence.
New assignments and dependents of the unavailable plan wait.

Provide the advertised dashboard or plan link in status updates by default.
ASCII plan visualization requires an explicit request and is never emitted by default.
Do not emit ASCII maps unless the user explicitly requests them.

Distinguish authoritative MCP coordination from other documents:
- **Authoritative MCP service**: Authoritative record of plan graph, step state,
  assignments, resource uses, audit events, and verifiable evidence references.
- **Detailed design and implementation docs**: Repository design documents and
  specifications contain deep technical reasoning but are not plan records.
- **Temporary workflow notes**: Scratch notes and logs remain private working records.

Specification, architecture, ADR, or contract changes that alter downstream dependencies, ordering, scope, deliverables, completion checks, or tracked document references require `disposition="material"`.
A passing producing-step check does not imply an unchanged reassessment.
Identify affected steps or declare unbounded effects; include the actual proposed
revision when available. Tracked references include their recorded path, branch,
commit, and version. Edits that leave downstream definitions and tracked references
unchanged do not by themselves require material reassessment.
An unchanged reassessment must have `proposed_draft=None`.

When accepted specification changes affect downstream definitions or tracked references, synchronize the canonical graph, step definitions, and document metadata before stopping at a `first-step` boundary or consulting `list_ready` for further dispatch.
Apply the accepted specification changes with `resolve_reassessment(decision="apply", draft=...)`,
or use an authorized `revise_plan` followed by explicit reassessment-hold resolution.
Calling `revise_plan` changes the graph but does not clear reassessment holds; resolve those holds explicitly through `resolve_reassessment`.
When a separate revision has already implemented the proposal, an explicit
`decision="reject"` can record that the proposal is superseded by that revision.
Cite the revision in the reason and declare any residual affected steps or
unbounded effects instead of clearing still-needed holds.
An apply decision can retain residual holds; only advance work that the service
reports eligible, preserving permitted unaffected sibling work. If synchronization
is blocked or exceeds authorization, retain the holds and report the blocker
rather than claim synchronization or dispatch affected work.

Resume reads canonical events, unfinished ownership, holds, and reviews before
dispatch or recovery. Only an authorized owner invokes `sweep_stale`; a direct worker requests that owner sweep before recovery.
After the sweep, reread the latest attempt version before owner confirmation or an
unfenced worker recovery checkpoint. Retain grants only in explicitly private
client/session working records; a new session without a valid owner grant uses
explicit takeover, which changes coordination authority without renewing scope.
The service enforces declared coordination and capability fences; it does not enforce filesystem permissions or stop host processes.
Workers and operators remain responsible for actual isolation, resource declarations,
and supported stopped-use evidence.

## Outage policy and unavailable behavior

When the shared service or Docker daemon is temporarily unavailable (e.g., host reboot,
maintenance, host sleep, or network interruption):

During a service outage, an assigned bounded worker finishes verification and retains unpublished evidence locally while new assignments and dependent steps wait.
Completing a named step and its verification requires publishing a structured checkpoint with evidence and reassessment.

1. **Assigned bounded worker finishes verification:**
   An already assigned bounded worker may complete its current step verification. It
   retains its unpublished evidence locally in its private working record.
2. **Dependents and new assignments wait:**
   Downstream dependent steps and new assignments must wait for service restoration.
3. **No spawned fallback server or competing plans:**
   Shared service mode prohibits launching a separate per-task or per-session fallback server.
   Do not launch a temporary fallback server or write competing local canonical plans
   for that plan. Deliberately configured independent instances can serve other plans.
   Report the last published revision as **LAST KNOWN**, offline/stale while the
   service is unavailable; it remains the authoritative public reference.
4. **Reconnection, same-key retry, and reconcile:**
   When service resumes, reread service identity, canonical events, ownership, and coordination before publishing retained evidence.
   Retain the original immutable command body and request key; retry the exact saved command without rebuilding its metadata or payload.
   Check the current epoch, generation, ownership, attempt version, and holds.
   A fenced or stale command requires explicit owner recovery. Reread after
   publication or exact replay to confirm what committed before dispatching work.
5. **Legacy compatibility is not outage fallback:**
   Legacy standalone file mode is an explicit compatibility option for isolated file
   tasks, never an outage fallback.

## Missing-MCP worker relay

When a dispatched worker cannot access the MCP endpoint directly:
- An authorized relay orchestrator submits `start_step`, `report_activity`, and
  `record_checkpoint` commands on the worker's behalf.
- The relay contract requires both orchestrator and attempt tokens. The command
  faithfully preserves the worker's identity in `origin_worker` and the relay's identity
  in `relay`.
- Relays must never invent checks, heartbeats, or activity signals.
- For reviews, the independent reviewer is recorded in `Review.reviewer`; the `Review`
  DTO has no relay field, and the submitting actor is recorded truthfully as orchestrator.

## Explicit legacy handoff and transferred plans

When importing or transferring a legacy file-backed plan (`live_plan.json`) into the
shared MCP service:
1. **Stop legacy publisher beforehand:** Ensure any running standalone publisher process
   is terminated before import.
2. **Canonical single import:** The service records the original provenance, host ID,
   source directory, and original task ID, enforcing uniqueness via
   `imports UNIQUE(host_id, source_directory_id, original_task_id)`.
3. **MCP-only later publication:** Transferred plans use MCP as their only publisher
   authority. The service introduces no polling, mirroring, or write-through back to the
   legacy file. Because the service cannot technically prevent independently launched legacy
   disk writers from touching files on disk, operators must ensure legacy writers remain
   stopped.

## Standalone file mode (legacy compatibility)

For environments without the shared service, standalone file-based publishing is retained
for backward compatibility using Python 3.10+ standard library.

```powershell
python scripts/live_plan.py serve --state-dir work/live-plan --port 0 --ready-file work/live-plan/server.json
```

### Proposal schema (v2)

In standalone file mode, write proposed snapshots to a task-local JSON file:

```json
{
  "schemaVersion": 2,
  "taskId": "report-01",
  "objective": "Produce a verified sales report",
  "status": "planning",
  "executionMode": "first-step",
  "nextStepId": "inspect",
  "changeSummary": "Before you choose the transformation, inspect the inputs.",
  "steps": [
    {
      "id": "inspect",
      "title": "Inspect the inputs",
      "status": "pending",
      "dependsOn": [],
      "check": "Check the required columns. Check the date range.",
      "evidence": []
    },
    {
      "id": "report",
      "title": "Build and verify the report",
      "status": "pending",
      "dependsOn": ["inspect"],
      "check": "The totals reconcile with the checked input.",
      "evidence": []
    }
  ]
}
```

Task statuses: `planning`, `running`, `stopped`, `blocked`, `complete`. Execution modes:
`plan-only`, `first-step`, `complete-task`. Step statuses: `pending`, `in-progress`,
`blocked`, `complete`, `retired`.

### Atomic replacement and scope

In standalone file mode, writes use an atomic temporary file replacement under lock:
- Creation requires revision 0 and an authorization note.
- Regular updates require `--expected-generation` and `--expected-revision`.
- On conflict, reread canonical state and reconcile.
- Scope enforces mode boundaries (`plan-only`, `first-step`, `complete-task`).
