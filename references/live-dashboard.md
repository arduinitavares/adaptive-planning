# Live dashboard

Optional companion for `adaptive-planning`. Use when the user requests a live or HTML plan. Keep Caveman and Unslop reporting. The page shows the objective, dependencies, current work, checks, evidence, retired steps, and revision history. It updates when the agent publishes a snapshot. It does not observe hidden activity or authorize work.

Requires Python 3.10+ with its standard library. No packages, cloud service, or startup registration. Bind only to `127.0.0.1`. Do not expose the server publicly. The examples use an installation under `.agents/skills`.

## Start in the current task

Resolve `scripts/live_plan.py` relative to this skill. Keep state in the task's own folder, never the shared skill directory. Each task gets a separate state directory and server. The published `live_plan.json` is the canonical plan; input JSON files are temporary proposals.

PowerShell example, run from the task directory:

```powershell
$planScript = Join-Path $env:USERPROFILE '.agents\skills\adaptive-planning\scripts\live_plan.py'
$planState = [IO.Path]::GetFullPath((Join-Path (Get-Location) 'work\live-plan'))
$planReady = Join-Path $planState 'server.json'
New-Item -ItemType Directory -Force -Path $planState | Out-Null
$planPython = (Get-Command python).Source
$planArguments = '-u "{0}" serve --state-dir "{1}" --port 0 --ready-file "{2}"' -f $planScript, $planState, $planReady
Start-Process -FilePath $planPython -ArgumentList $planArguments -WindowStyle Hidden -RedirectStandardOutput (Join-Path $planState 'server.out.log') -RedirectStandardError (Join-Path $planState 'server.err.log')
```

Before starting, inspect any existing readiness file and matching process. Reuse a healthy server for this task. A readiness file alone does not prove the process is alive. After launching, wait briefly for readiness (bounded retries), read `server.json`, and open its `url` using the available browser tool. Do not assume a fixed port. If startup fails, read the logs and use the ASCII fallback while resolving it. A dashboard failure must not bypass the first-step stopping boundary or prevent other authorized work.

Foreground form:

```text
python <skill>/scripts/live_plan.py serve --state-dir <task>/work/live-plan --port 0 --ready-file <task>/work/live-plan/server.json
```

## Proposal schema (v2)

Write a proposed snapshot to a task-local JSON file. When editing a published snapshot, first remove publisher-managed metadata (`scope`, `generation`, `revision`, `history`, `updatedAt`). Unknown top-level proposal fields are rejected. Authorization and recovery notes are CLI arguments (`--authorization-note`, `--recovery-note`), or the Python API arguments `authorization_note` and `recovery_note`; do not put them in proposal JSON.

Minimal initial proposal:

```json
{
  "schemaVersion": 2,
  "taskId": "report-01",
  "objective": "Produce a verified sales report",
  "status": "planning",
  "executionMode": "first-step",
  "nextStepId": "inspect",
  "changeSummary": "Inspect the inputs before choosing the transformation.",
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
      "check": "Totals reconcile with the checked input",
      "evidence": []
    }
  ]
}
```

Task statuses: `planning`, `running`, `stopped`, `blocked`, `complete`. Execution modes: `plan-only`, `first-step`, `complete-task`. Step statuses: `pending`, `in-progress`, `blocked`, `complete`, `retired`. Optional step strings: `summary`, `details`. Retired steps require a bounded non-empty `retiredReason`.

IDs must stay stable. Dependencies must reference existing IDs and form no cycles. A started or completed step requires completed prerequisites. Completion requires concrete evidence. Evidence must be ordered prefix append-only on all surviving steps. `nextStepId` must point to an incomplete, unblocked, non-retired step whose prerequisites are complete, or be `null`.

Completed nodes retain ID, status, title, check, and dependencies. Evidence may only append: its previous ordered prefix, including duplicates, must remain. Any step with evidence retains its check and cannot disappear. Unfinished titles and dependencies may change within graph rules. Evidence-free pending/blocked work can be removed, or retired with a reason; retirement is allowed only from pending/blocked and is terminal. Retired steps cannot be deleted or reactivated, and non-retired steps cannot depend on them. Retired steps count toward the node limit but are excluded from task completion.

If later evidence invalidates a result, record it and add an explicit corrective/recheck step; revise unfinished dependencies accordingly. The publisher currently trusts historical completed steps. Preserve their history while clearly stating that the disproven result is not currently verified; a future invalidation model remains deferred.

Limits: 200 total nodes, 100 history entries, 50 evidence items per step, 10 MB per snapshot.

## Publish and CAS

Publish proposals using the atomic publisher:

### 1. Initial creation (revision 0)

Requires revision 0, no generation flag, and an authorization note quoting or paraphrasing existing user instructions:

```powershell
& $planPython $planScript publish --state-dir $planState --input 'work\plan-init.json' --expected-revision 0 --authorization-note "User asked: plan the sales report, inspect the inputs as the first step, then stop"
```

### 2. Regular updates

Requires both `--expected-generation` and `--expected-revision` matching the current canonical state under lock:

```powershell
& $planPython $planScript publish --state-dir $planState --input 'work\plan-update.json' --expected-generation 'c3a9f0bb-...' --expected-revision 1
```

On a conflict, reread the canonical state and reconcile the proposal with its current evidence, scope and tuple before retrying. Do not blindly increment the expected revision. To renew an interval or change execution mode, include `--authorization-note` grounded in the actual requested work:

```powershell
& $planPython $planScript publish --state-dir $planState --input 'work\plan-update.json' --expected-generation 'c3a9f0bb-...' --expected-revision 2 --authorization-note "User authorized step 2 execution"
```

### 3. Migration from v1 to v2

Migrating legacy v1 canonical state requires literal `--expected-generation none`, current revision, and an authorization note. Schema migration alone does not authorize more execution. For this executing example, use a v2 proposal consistent with the user's actual request to execute the next step:

```powershell
& $planPython $planScript publish --state-dir $planState --input 'work\plan-v2.json' --expected-generation none --expected-revision 3 --authorization-note "User asked to execute the next sales report step and verify it, then stop"
```

### 4. Recovery from corrupt state

If canonical state is invalid, run `recover` under the publisher lock. Absent, symlinked, and valid canonical states are refused; I/O failure does not establish corruption and aborts recovery. A valid replacement plan and a `--recovery-note` explaining imported, reverified, or omitted items are required. Executing modes also require `--authorization-note` based on actual execution authorization. A plan-only recovery needs only the recovery note. The original is preserved byte-for-byte in `live_plan.corrupt.<UTC>.<random>.json` before replacement:

```powershell
& $planPython $planScript recover --state-dir $planState --input 'work\plan-recovery.json' --recovery-note "Imported steps 1-2 using their recorded verification evidence; step 3 remains pending" --authorization-note "User asked to execute step 3, verify it, and stop"
```

These notes record the agent's assertion about existing instructions; they neither prove nor grant human permission. Reuse authorization already given. If only planning is authorized, migrate with `executionMode: "plan-only"`, status `planning` or `blocked`, and a note paraphrasing that planning request. Do not infer permission or completed claims from damaged or legacy metadata.

## Persistent execution scope

The publisher maintains `scope = {mode, sinceRevision, baseline, claimed}`. Creation/recovery import incoming completed IDs as the baseline. Migration and renewal use completed IDs from the previous canonical snapshot, so new incoming completions consume the new interval. A same-mode authorization note renews the interval; ordinary updates without a note retain it. Mode changes require a note.

In first-step mode, the first nonbaseline step to become active or complete is claimed. Blocking, returning to pending, retiring, or removing an evidence-free claimed step never releases that claim. Retrying the same logical step is allowed; another step requires justified renewal. A step initially pending and completed later is claimed; an initially imported completion belongs to the baseline. IDs must continue to identify the same logical work.

Plan-only permits retained baseline completions and planning/blocked state, with no active or newly completed work. Migration or recovery must not silently open a fresh executing interval. Stop at the first-step boundary even if dashboard publishing fails.

## Atomic replacement and retries

Writes use a temp file flushed and fsynced before `os.replace` under `live_plan.lock`. On Windows, transient `PermissionError` (winerror 5/32) is retried with bounded backoff (~2s monotonic time). All other errors fail immediately with zero retries. On retry exhaustion, the temporary file is removed, canonical state remains unchanged, and the CLI exits with code 75 (`retryable; state unchanged at revision N`). Recovery uses `retryable; canonical state unchanged` because the corrupt revision is unknown, and reports the completed backup path. A failed partial backup is never reported as complete. A successful replacement followed by a reporting failure is reported as committed, not unchanged; reread the canonical tuple before further work.

## Upgrade procedure

When upgrading an existing task to schema v2:
1. Stop the running v1 server process.
2. Publish migration to v2 using `--expected-generation none --expected-revision <rev> --authorization-note "<note>"`.
3. Restart the server with the updated `live_plan.py`.
4. Reload the dashboard in the available browser tool.
