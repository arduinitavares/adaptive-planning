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

## Establish state

Record the objective, verifiable acceptance criteria, constraints,
authorized actions, budgets, known facts, assumptions, and completed
work.

Reuse one canonical plan in the existing task tracker,
user-designated file, or conversation. Preserve completed work and
evidence. Do not create competing plan records.

## Execution loop

1. **Plan.** Maintain a provisional ordered list. Make the next step
   concrete with a completion check; keep distant steps coarse.
   Note important dependencies and assumptions.

2. **Choose.** Evaluate the next action's immediate benefit and
   downstream consequences: usefulness, feasibility, risk, cost,
   reversibility, and uncertainty reduced. Follow the user's outcome
   preferences. Use qualitative comparisons unless an actual
   scoring model is supplied; never fabricate reward estimates.

3. **Execute one meaningful step.** Use a bounded unit of work with
   a checkable outcome, including its necessary verification.
   Do not batch subsequent dependent steps. If material evidence
   invalidates the current approach mid-step, pause and reassess
   before further dependent actions. Continue independent authorized
   work even if another branch is blocked.

4. **Observe.** Compare the actual result with the expected result.
   Record evidence, failures, and remaining uncertainty.
   A successful tool call is not proof of the intended outcome.
   Failed verification means the step is not complete.

5. **Reassess the entire remaining horizon.** Check every pending
   step, including indirect dependencies, against the updated state.
   Keep, reorder, revise, retire, or add steps as justified.
   If later evidence invalidates an earlier result, preserve history,
   reopen with corrective/recheck steps, and repoint unfinished
   dependencies; never claim a disproven result is currently verified.
   When nothing relevant changes, retain the plan and explicitly
   record that outcome. Update the canonical plan before another
   step begins.

6. **Report and continue.** Publish the checkpoint below. Continue
   only within the requested execution mode. Stop when acceptance
   criteria are verified, authorization or budget is exhausted,
   or no authorized unblocked work remains. Report blocked branches
   separately while independent authorized work continues. Repeated failure without new
   evidence or hypothesis requires reassessing the approach or
   reporting a blocker.

## Reporting

For progress updates, checkpoints, and final task reports, apply
`caveman` for compression and `pstack-plugin:unslop` for plain writing.
Read their installed instructions when available. Use the user's
chosen Caveman level, or its default full level. Scope this style to
task reporting; write deliverables in their requested style.

These writing preferences must preserve the reports and ASCII maps
required here. Keep evidence, uncertainty, negations, numbers,
authorization boundaries, and technical meaning intact. Expand
sentences whenever compression makes sequence or dependencies unclear.
Keep the user's language and exact code, commands, and error text.

If either writing skill is unavailable, use short, direct sentences,
remove filler and stock phrases, and preserve the same information.

At each meaningful checkpoint, briefly cover the result and evidence,
material downstream impact, plan changes or "unchanged after review",
the next action and its completion check, and whether work continues,
stops at the requested boundary, awaits input, or is complete.
Combine these into natural prose or short bullets; fixed headings
are unnecessary. Match detail to what changed.

Show the initial plan and subsequent changes. Keep the full current
plan recoverable; avoid reprinting unchanged details.

## Optional live dashboard

When the user requests a live HTML plan, read
[references/live-dashboard.md](references/live-dashboard.md) for the
publisher, local server, schema v2, and launch commands. Keep this mode
optional; ordinary use needs no server.

Use the task's published JSON as the canonical plan in this mode.
Publish when a step starts, a result is verified, a blocker appears,
or reassessment changes the plan. Include evidence and a short reason
for each update. In plan-only mode, retain `planning`, or `blocked` if
required information is missing. When the plan is ready, record
"Plan ready; execution has not started" in `changeSummary`.
In execution modes, publish `stopped` when the requested execution
boundary is reached; use `complete` when acceptance criteria are
verified, or `blocked` when no authorized unblocked work remains.
The dashboard shows published state, not hidden agent activity,
and does not authorize execution.

Open the local URL and confirm a real update arrives before claiming
the dashboard is live. Keep chat updates concise and link the view;
use the ASCII view below when live mode is unavailable. A dashboard
failure must not discard the plan, bypass the first-step stopping
boundary, or prevent other authorized work.

## ASCII plan view

Use this view by default and as the fallback for live mode. Include a
compact ASCII map with the initial plan and each meaningful checkpoint
where structure or markers change. Derive it from the canonical plan;
it is a view of that plan, not a separate record. Summarize distant work
when needed to keep the big picture readable.

Use plain ASCII in a fenced text block. Use the same step identifiers
as the plan. Arrows show actual dependencies. Branch only where steps
can proceed independently; a join means all incoming prerequisites
must be satisfied. ASCII arrows are intentional here even if the
writing style otherwise discourages arrows. Redraw only when structure
or markers change; provide concise unchanged checkpoints otherwise.

Use consistent markers:
- `[x]` completed when verified
- `[>]` in progress
- `[ ]` pending
- `[!]` blocked
- `[-]` retired

The publisher treats historical completed steps as trusted pending a
future invalidation model; the marker does not override contrary evidence.

Mark a pending step `(next)` when useful. Never mark planned work as
in progress before it starts, or failed verification as complete.
If execution has stopped, say why beside the map; a next-step marker
does not grant permission to continue.

Example while filtering is in progress:

```text
Goal: verified sales report

[x] 1 Inspect inputs
    |
    +--> [>] 2 Filter sales --+
    |                        |
    +--> [ ] 3 Check lookup --+--> [ ] 4 Join --> [ ] 5 Produce and verify
```

Update markers and affected dependencies after reassessment. Keep
unchanged maps compact; skip redraws for individual tool calls within
the same meaningful step. Explain consequential changes briefly
beside the map.

## Guardrail

Optimize the agreed outcome, not completion of the original list.
Do not silently change the objective, relax acceptance criteria,
add speculative work to fill the horizon, or use long-term benefits
to justify unapproved scope expansion.
