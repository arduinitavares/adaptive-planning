# Maintained planning integration

## Outcome

Invoking Adaptive Planning alone must load the installed
`superpowers:writing-plans` skill for initial plans and material replanning.
The user approved this change on 2026-10-23. Work stays on
`dev/superpowers-rolling-plans` for testing before merge.

## Design

Superpowers owns detailed plan authoring and its self-review. Adaptive Planning
owns the rolling horizon, canonical graph, lifecycle, evidence, and acceptance.
Resolve the companion through the active skill catalog. Do not copy its templates
or fix its installation path or version in the integration.

Represent independently verifiable outcomes as graph steps. Keep action
checkboxes in linked plan documents. Show goal coverage without inventing tasks
to fill the horizon. Detail ready work. Identify uncertainty and expansion
conditions for distant work. A milestone is a verifiable outcome, not a new
schema type or an empty grouping task.

Apply the authoring standard again when observations materially change the
plan. Revise affected pending work. Preserve accepted history and stable IDs.
Synchronize document versions and canonical definitions through the existing
reassessment and hold-resolution workflow before affected work can advance.

Use the maintained software plan structure for software work. For other work,
map files and interfaces to artifacts, inputs, outputs, owners, and observable
checks. Do not require software-only actions for physical or research work.

If Superpowers cannot be resolved or read, report the exact dependency gap.
Preserve existing Adaptive Planning use with a clearly labelled provisional
plan under its current rules. Do not claim maintained Superpowers authoring or
self-review. Apply the companion when it becomes available. An incompatible
installed contract requires the same explicit limitation, not a copied substitute.
An unchanged reassessment does not require rewriting the plan.

The plan owner applies this authoring contract before an initial publication or
a material proposal. Existing `disposition="material"` rules determine the latter.
Corrective work after invalidation also uses the contract. Resuming or taking over
an unchanged existing plan does not require re-authoring accepted work.

One graph step normally maps to one independently verifiable document task.
Small action checkboxes remain inside that task. Outcomes beyond the bounded
graph horizon remain explicit in the linked roadmap, with prerequisites and
expansion conditions. Superpowers self-review covers the whole requested outcome,
but detailed implementation instructions cover the current horizon only.

Keep version-bound plan snapshots immutable. Record execution checkboxes in a
separate progress copy or the selected workflow's ledger. Editing a tracked
document requires its actual new reference and the existing material rules,
even when the edit only changes checkboxes. Never hide a content-hash mismatch.

Save plan files only within existing file-write authorization. Otherwise return
the detail in the requested medium. Preserve mandated Superpowers template wording
and literals. Apply STE to new prose. Plan-only work does not execute planned
implementation or commit actions. Commits retain their applicable authorization.

Retain a selected execution method. For complete-task work with no method selected,
choose a compatible method within the user's authorization and state the choice.
Routine authorized replanning does not repeat the handoff question. Objective
changes, relaxed acceptance criteria, or additional scope still require authorization.

## Preservation requirements

Keep all current authorization modes, service selection and identity checks,
ownership and capability fences, resource obligations, recovery, outage handling,
exact retries, measurement receipts, language rules, review and owner acceptance.
Keep the current dashboard, schema, runtime, deployment, and installed skill.
The feature branch supplies the candidate skill for testing before merge.

Preserve Superpowers checkboxes and the selected execution method's own review
gates, verdicts, fix budgets, and ledger. Transfer required evidence from temporary
workflow files into durable nonsecret references before the workflow removes those files.
Existing implementation authorization remains effective during routine replanning.

## Acceptance

1. Fresh agents using only Adaptive Planning request the maintained companion
   before initial authoring and material replanning.
2. Their plans contain outcome coverage, dependencies, actionable near-term work,
   concrete checks, and links between detailed tasks and graph steps.
3. Missing-companion (initial and revision), unchanged resume, checkbox-version,
   non-software, scope-change, and first-step cases preserve existing boundaries.
4. The existing Python and dashboard tests pass. Any baseline failures or skips
   are recorded explicitly.
5. A fresh cross-family reviewer checks the final change and verification record.

The successful user dashboard is the qualitative benchmark. Its private business
details are not needed in public test fixtures. Agent scenarios use synthetic data.

Behavior test protocol: five fresh `gemini-4-argon-high` / `high` samples per
variant, one for each numbered scenario. Every candidate sample must load the
companion and satisfy its applicable rubric items. Additional boundary cases use
a separate fresh response. Inspect every output manually. Any candidate failure
requires a correction and a rerun of the affected scenario plus the five-sample
candidate set after the final wording change.

The coordinator runs supplied-text simulations through Orca in the task's own
workspace. It injects the exact skill snapshot and a simulated catalog. The model
requests companion content using a text protocol. Runtime tools remain exposed
by AGY, so this is observed no-tool testing, not a claim of tool-level isolation.
Inspect actual events and stop on any real tool action. Record source hashes and
zero real service mutations. Test wrappers and raw logs stay in task-owned scratch.
The repository contains fixtures, dispositions, and reproducible test instructions.
Inspect exposed initialization, delivered input, skill expansion settings, and
tool calls. Ambient provider context remains a confound. Score loading only from
explicit simulated-catalog read requests. A reference to unsupplied catalog content
fails that sample. These samples do not establish behavior on every client.

Language verification: Issue 9-oriented candidate. The official dictionary and
governed termbase are unavailable. Identifiers, commands, paths, and authorization
conditions are protected. Full STE conformance is not claimed.
