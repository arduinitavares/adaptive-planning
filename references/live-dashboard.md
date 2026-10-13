# Live dashboard

The live dashboard provides real-time visualization of the canonical adaptive plan.
In configured environments, the shared adaptive-planning MCP service provides the
authoritative dashboard at `http://127.0.0.1:8790/`.

The dashboard visualizes the objective, dependencies, active assignments, completion
checks, evidence artifacts, retired steps, and revision history. It updates whenever
a mutation is committed through the service. It does not observe hidden agent activity
or authorize work.

## Service mode default and per-task server policy

In shared service mode, the central service is the single coordination authority.
The live dashboard link (http://127.0.0.1:8790/) is the default for plan visualization and status reporting.
Shared service mode prohibits launching a separate per-task or per-session fallback server.
Provide the dashboard link (`http://127.0.0.1:8790/`) in status updates by default.
ASCII plan visualization requires an explicit request and is never emitted by default.
Do not emit ASCII maps unless the user explicitly requests them.

Distinguish authoritative MCP coordination from other documents:
- **Authoritative MCP service**: Authoritative record of plan graph, step state,
  assignments, resource uses, audit events, and verifiable evidence references.
- **Detailed design and implementation docs**: Repository design documents and
  specifications contain deep technical reasoning but are not plan records.
- **Temporary workflow notes**: Scratch notes and logs remain private working records.

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
maintenance, Mac sleep, or network interruption):

During a service outage, an assigned bounded worker finishes verification and retains unpublished evidence locally while new assignments and dependent steps wait.
Completing a named step and its verification requires publishing a structured checkpoint with evidence and reassessment.

1. **Assigned bounded worker finishes verification:**
   An already assigned bounded worker may complete its current step verification. It
   retains its unpublished evidence locally in its private working record.
2. **Dependents and new assignments wait:**
   Downstream dependent steps and new assignments must wait for service restoration.
3. **No spawned fallback server or competing plans:**
   Shared service mode prohibits launching a separate per-task or per-session fallback server.
   Do not launch a temporary fallback server or write competing local canonical plans.
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
  "changeSummary": "Inspect inputs before transformation.",
  "steps": [
    {
      "id": "inspect",
      "title": "Inspect inputs",
      "status": "pending",
      "dependsOn": [],
      "check": "Required columns and date range checked",
      "evidence": []
    },
    {
      "id": "report",
      "title": "Build and verify report",
      "status": "pending",
      "dependsOn": ["inspect"],
      "check": "Totals reconcile with checked input",
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
