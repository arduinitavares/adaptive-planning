---
name: adaptive-planning
description: Use when a multi-step task has uncertain dependencies or outcomes, new observations could invalidate later steps, or the user requests iterative replanning, a rolling plan, or a receding planning horizon.
---

# Adaptive Planning

Keep the objective stable and the route provisional. Apply this loop
throughout the task, not only when first producing a plan.

## Scope and defaults

Use for uncertain multi-step work, not trivial single-action requests.

Maintain up to 10 upcoming steps unless the user specifies another
horizon. The horizon is a maximum, not a quota.

Derive execution permission from the user's request:
- Planning only: produce the plan and requested plan files; allows
  read-only inspection; stop without executing modifications.
- First step only: execute one step, reassess, publish the updated
  plan, and stop.
- Complete the task: repeat the loop within the authorized scope;
  do not ask between already-authorized steps. Continue independent
  authorized work despite a blocked branch elsewhere in the plan.

Skill invocation does not expand permissions. Preserve required
approvals and applicable testing, debugging, and review workflows.
Takeover of a plan changes coordination authority but is not scope
renewal; execution boundaries remain strictly bound to user authorization.

## Plan language

Use `simplified-technical-english` for all new natural-language planning
and reporting text. Load its installed `SKILL.md` before authoring text.
Resolve the companion through the agent's skill catalog or a supplied
path. Read its applicable references. Do not assume an installation path.

This policy covers objectives, acceptance criteria, constraints,
assumptions, step titles, instructions, completion checks, explanations,
blockers, authored evidence summaries, progress updates, and final reports.
It also covers new dashboard summaries, details, retirement reasons,
history summaries, and authorization or recovery explanations.

Classify each block by its function. Apply procedure rules to instructions
and safety text. Apply descriptive rules to explanations and observations.
Treat headings and short labels as labels, not as incomplete instructions.
Write one instruction per sentence. Keep related work and its verification
in one planning step when appropriate. Sentence splitting does not require
new graph nodes or different dependencies.

Preserve technical meaning, uncertainty, negation, conditions, sequence,
numbers, units, thresholds, safety information, and authorization boundaries.
Keep code, commands, paths, URLs, identifiers, schema keys, enum values,
quoted evidence, error text, and mandated wording exact. Preserve published
history and protected step fields. Apply language review to new authored
text before publication. Do not rewrite previous evidence or protected
completed-step fields to satisfy the new policy.

Use the companion's governing-issue and terminology checks. When the
official dictionary or governed terminology is unavailable, produce only
an Issue 9-oriented candidate. Do not claim full conformance or guess
approved vocabulary. Missing sources block strict verification, not a
useful provisional plan whose meaning is clear.

Give one compact language verification note with the initial plan.
Identify the issue, evidence mode, available or missing terminology
sources, protected-literal checks, and material unresolved items.
Update the note when new unresolved items or a different evidence mode
appear. Keep it beside the canonical plan, not in a competing plan record.
Do not add unsupported metadata to dashboard JSON.

If the companion is unavailable, report the missing dependency.
Do not silently substitute another writing style or claim STE review.
Preserve the existing plan. Continue independent authorized work only
when it does not depend on the missing review. Do not guess unresolved
technical meaning that can change safety, legality, or product behavior.

Preserve the user's requested language and applicable publication rules.
Do not translate text solely to apply STE. Report non-English text as
outside the STE language-review scope. Task deliverables retain their
governing specifications and protected source wording.

## Authoritative state and coordination

In configured environments, the shared adaptive-planning MCP service
is the default authoritative coordination authority (dashboard at
`http://127.0.0.1:8790/`). Shared service mode prohibits launching a separate per-task or per-session fallback server.


Distinguish the authoritative MCP coordination state from other project
artifacts:
- **Authoritative MCP service**: Stores the canonical plan graph, step
  lifecycle, assignments, resource obligations, audit events, and
  verifiable evidence references.
- **Detailed design and implementation docs**: Persisted repository
  specifications, architectural decision records, and design documents
  contain technical detail but are not canonical plan substitutes.
- **Temporary workflow notes**: Scratch files, local logs, and working
  notes remain private and transient; they carry no service authority.

### Domain grants and working records

Persist domain grants (orchestrator tokens, attempt tokens) only in
explicitly private client or session working records outside public
views and repository files. Do not assume automatic client-session
persistence across restarts or new sessions. If a new session starts
without a retained valid grant, use an explicit `takeover` command on the
plan to re-establish orchestrator authority.

### Worktree isolation and resource declarations

Each assigned worker must operate in an isolated worktree or directory.
Declare physical resource claims (repository, workspace, external)
explicitly to prevent file collisions. Retain resource obligations
until explicit stopped-use evidence is recorded.
The service enforces declared coordination and capability fences; it does not enforce filesystem permissions or stop host processes.
Workers and operators must honor the declared context, isolation, and stopped-use
obligations in the actual execution environment.

### Activity and stale thresholds

Workers must report observable progress at least once every five minutes
(`report_activity` or checkpoints). An attempt with no activity for
15 minutes reaches the stale threshold, requiring explicit administrative
sweep and recovery before new progress can occur.

## Execution loop

The framework-neutral instruction contract follows six generic stages:
`publish/resume -> assign/start -> act/verify -> checkpoint/reassess -> resolve/review/accept -> eligible next work`.
Workflow document references and phase metadata are optional; no specific
Superpowers sequence is required.

1. **Publish or resume.**
   - Initial plan: Publish the objective, acceptance criteria, constraints,
     known facts, assumptions, and bounded steps. Keep stable step IDs, explicit
     dependencies, and concrete next checks; keep distant steps coarse.
   - Resume existing plan: Read canonical events, unfinished ownership,
     holds, and reviews. Explicitly sweep stale attempts before dispatch
     or recovery. Only an authorized owner invokes `sweep_stale`; a direct worker requests that owner sweep before recovery.
     If an assigned worker discovers its attempt is overdue, obtain the owner
     sweep first, then reread the latest attempt version. The owner confirms
     ownership, or an unfenced current worker publishes a recovery checkpoint.

2. **Assign and start.**
   - Choose the next action by usefulness, feasibility, risk, cost,
     reversibility, and uncertainty reduced, following the user's outcome
     preferences. Use qualitative comparisons unless an actual scoring model
     is supplied; never fabricate reward estimates.
   - Assign the next ready step to a bounded worker with its declared
     execution context and resource obligations.
   - Worker begins with an explicit `start_step` call recording observed
     activity.
   - Missing-MCP worker relay: When a worker lacks direct MCP capabilities,
     an authorized relay orchestrator submits `start_step` and
     `report_activity` on its behalf, preserving the worker's identity
     in `origin_worker` and the relay's identity in `relay`. Never invent
     checks, heartbeats, or activity signals.

3. **Act and verify.**
   - Execute one bounded step with checkable verification.
   - Run the specified completion check and collect concrete evidence.
   - Do not batch subsequent dependent steps. If material evidence invalidates
     the approach mid-step, pause and reassess before further dependent actions.
   - A successful tool call is not proof of the intended outcome. Failed
     verification means the step is not complete.

4. **Checkpoint and reassess.**
   - Completing a named step and its verification requires publishing a structured checkpoint with evidence and reassessment.
   - Use `record_checkpoint` to publish a structured checkpoint with outcome (`passed`, `failed`,
     or `blocked`), the executed check, full artifact references,
     verifiable evidence, and a comprehensive `reassessment`.
   - Reassess every pending step in the remaining horizon, including indirect
     dependencies, against updated facts. Note any
     material downstream effects, reordering, additions, or retirements.
     Preserve accepted history when later evidence invalidates a result; record
     invalidation and explicit corrective/recheck work instead of presenting
     the disproven result as currently verified. When nothing relevant changes,
     retain the plan and explicitly record the unchanged reassessment.
   - Safe pause relay: If work must pause, the relay records actual
     state truthfully without fabricating completion.

5. **Resolve, review, and accept.**
   - The plan owner explicitly classifies and resolves any provisional
     reassessment holds.
   - Independent review: For steps with `review_required` or final gates,
     an independent reviewer records a `Review` observation. The `Review`
     DTO contains `reviewer`, findings, and evidence; it has no invented
     relay field. Relayed reviews truthfully record the submitting actor
     as orchestrator and the original reviewer in `Review.reviewer`.
   - Owner acceptance: The owner accepts verified work via `accept_step`,
     matching the exact submitted artifact, target attempt, and current
     CAS metadata. Declare stopped resource IDs supported by actual
     evidence; empty stop IDs retain obligations. A submitted checkpoint
     is not accepted work until explicit owner acceptance.

6. **Eligible next work.**
   - Consult `list_ready` to discover newly unblocked steps.
   - Continue only within the authorized execution mode (`first-step` stops
     here; `complete-task` proceeds with the next eligible step).
   - Stop when acceptance criteria are verified, authorization or budget is
     exhausted, or no authorized unblocked work remains. Report blocked branches
     separately while independent authorized work continues. Repeated failure
     without new evidence or a changed hypothesis requires reassessment or a
     blocker report.

## Outage policy and unavailable behavior

When the shared MCP service or dashboard is temporarily unavailable
(e.g., host reboot, Docker maintenance, Mac sleep, or connection drop):

During a service outage, an assigned bounded worker finishes verification and retains unpublished evidence locally while new assignments and dependent steps wait.

1. **Assigned bounded worker finishes verification:**
   An already assigned bounded worker may complete its active execution
   and verification checks. It retains its unpublished evidence locally
   in its private working record.

2. **Dependents and new assignments wait:**
   New step assignments and dependent work must wait for service
   restoration. Do not attempt uncoordinated execution of downstream steps.

3. **No spawned fallback server or competing plans:**
   Shared service mode prohibits launching a separate per-task or per-session fallback server.
   Do not launch a per-task fallback server or create competing canonical
   plan files. Report the last published revision as **LAST KNOWN** and
   offline/stale while the service is unavailable. It remains the
   authoritative public reference. Legacy file mode is an explicit
   compatibility mechanism for isolated environments, never an outage
   fallback.

4. **Recovery, same-key retry, and reconcile:**
   When service resumes, reread service identity, canonical events, ownership, and coordination before publishing retained evidence.
   Retain the original immutable command body and request key; retry the exact saved command without rebuilding its metadata or payload.
   Reconcile the saved command against the current epoch, generation, ownership,
   attempt version, and any holds. If it is fenced or stale, obtain explicit
   owner recovery rather than altering a retry under the same key. Reread after
   publication or exact replay to confirm what committed before dispatching work.

## Reporting

Apply the Plan language policy to progress updates, checkpoints, and
final task reports. Keep complete sentences and explicit relationships.
Do not use Caveman compression for this skill.

If `pstack-plugin:unslop` is available, apply it only when its edits
preserve STE requirements, technical meaning, and protected literals.
It does not replace the required STE companion. Preserve the checkpoint
information required below.

At each meaningful checkpoint, cover the result and evidence, material
downstream impact, plan changes or "unchanged after review", the next
action and its completion check, and whether work continues, stops at
the requested boundary, awaits input, or is complete.

### Dashboard link and ASCII view

- **Default dashboard link**: The live dashboard link (http://127.0.0.1:8790/) is the default for plan visualization and status reporting. By default, include the link to the live dashboard (`http://127.0.0.1:8790/`) in progress updates so users can inspect the real-time plan state.
- **ASCII plan view on explicit request only**: ASCII plan visualization requires an explicit request and is never emitted by default. Render an ASCII plan map only upon explicit request from the user. Do not include ASCII maps by default. When explicitly requested, use plain ASCII in a fenced
  block with standard markers (`[x]` complete, `[>]` in progress,
  `[ ]` pending, `[!]` blocked, `[-]` retired) derived strictly from
  the canonical plan.

## Guardrail

Optimize the agreed outcome, not completion of the original list.
Do not silently change the objective, relax acceptance criteria,
add speculative work to fill the horizon, or use long-term benefits
to justify unapproved scope expansion.
