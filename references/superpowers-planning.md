# Maintained Superpowers planning

This contract connects maintained plan authoring to the existing Adaptive Planning lifecycle.
It does not replace the service, dashboard, or selected execution workflow.

## Load the current companion

The plan owner uses `superpowers:writing-plans` before an initial plan or material revision.
Resolve its current entry through the active skill catalog or the user's supplied path.
Read the resolved `SKILL.md` before authoring. Reuse a loaded copy only while that source remains current.
Record its resolved source and exposed version or content hash in the planning note.
Keep this note beside the plan. Do not invent service schema fields.

Follow the installed companion's structure, task sizing, and self-review.
Keep its templates in the companion. Do not copy, vendor, or hardcode a versioned path here.
The adaptation rules below govern the rolling horizon and canonical coordination.
Explicit user instructions retain precedence over both skills.

## Produce a useful bounded plan

1. Identify the requested outcome, acceptance criteria, constraints, and current authorization.
2. Use the approved specification when one exists.
   If material design choices remain unresolved, plan the next verifiable discovery or design task.
   Follow the applicable design workflow before treating uncertain implementation work as ready.
3. Map every requested outcome to a graph task or a provisional outcome in the roadmap.
4. Detail the current horizon with the maintained companion.
5. Apply its self-review before publishing the plan.

Each ready task identifies its owner or role, prerequisites, inputs, outputs,
actions, verification check, and expected result. Use exact files, interfaces,
tests, and commands when the source establishes them. If required facts are
unknown, give the discovery task an observable check. Do not fabricate interfaces
or present unknown implementation instructions as ready work.

Separate observed source facts from proposed designs and artifact names.
Do not invent the contents of an unread document, checklist item numbers, or an existing API.
Label proposed names as proposals until the source or approved design establishes them.
A placeholder path or "run the repository tests" does not make an implementation task ready.
Keep that work provisional. Make source inspection or contract discovery the next verifiable action.
The self-review must report unresolved facts; it must not certify placeholder instructions as executable.

Assess readiness for the whole graph task, not only its first checkbox.
Do not mark a task ready when it combines discovery with implementation or physical actions
that still depend on unknown facts or missing external prerequisites.
Separate discovery into its own verifiable graph task.
If the horizon is full, narrow the affected pending task to discovery only.
Retain the later execution outcome in the provisional roadmap until a slot opens.
Keep accepted history and unaffected steps intact. Synchronize the changed pending definitions
and dependencies through the owner's material reassessment before discovery starts.
After discovery, record its checkpoint and reassess the provisional execution work.
Use the companion to author concrete execution details from that evidence.
Synchronize the new definitions and document references before that execution becomes eligible.
Do not continue into implementation or commissioning within the discovery task.

One graph step normally corresponds to one independently verifiable document task.
Keep the task's action checkboxes inside its detailed document.
Do not create graph nodes for every sentence, command, or checkbox.
A milestone can be a graph step when it has a verifiable outcome.
A display group alone is not executable work. No new milestone schema is required.

The existing horizon remains a maximum, not a task-count target.
When coverage exceeds that horizon, list later outcomes in the linked roadmap.
Give each provisional outcome its known prerequisites, uncertainty, and expansion condition.
Use acceptance criteria and assumptions to retain that coverage when no document is authorized.
Promote work into the graph within the horizon limit as evidence makes it useful.
Full outcome coverage does not require speculative implementation detail for distant work.

Save detailed documents only within existing file-write authorization.
Otherwise return the detail in the requested medium.
For saved documents, map stable step IDs to the relevant task sections.
Use existing `DocumentRef` fields for exact path and version information.
Retain branch and commit references when applicable. Do not invent unpublished commit hashes.

## Adapt evidence to the domain

For software, use the companion's software task structure and applicable test workflow.
For physical, research, or operating work, map that structure to the actual deliverable:

| Planning element | Non-software interpretation |
| --- | --- |
| Files | Documents, records, measurements, or other evidence artifacts |
| Interfaces | Inputs consumed, outputs produced, and the responsible actor |
| Action steps | Observable research, preparation, or authorized operator actions |
| Verification | Source checks, measurements, demonstrations, or acceptance observations |

Preserve checklist tracking and concrete expected results.
Do not invent code, automated tests, commits, or machine operations for a non-software task.
Preparation records do not prove physical or commercial completion.

## Apply the same quality during replanning

Use the existing material-reassessment rules in [SKILL.md](../SKILL.md#execution-loop).
They include changed downstream definitions and changed tracked document references.
Workers record observed evidence and affected work promptly.
The owner applies the companion before authoring the revised plan or accepting a revision proposal.
This also applies to corrective or recheck work after result invalidation.

Revise the affected pending tasks and their interfaces.
Reassess all remaining horizon steps, including indirect dependencies.
Preserve unaffected definitions, stable IDs, and accepted history.
Record why work changed, what evidence caused the change, and what remains provisional.
Run the companion's self-review against the revised tasks and whole outcome coverage.

Put the actual proposed revision in `proposed_draft` when available.
If facts or authorization prevent a valid proposal, retain the relevant holds.
Do not clear a hold merely because a document has the required template sections.
A valid revision can make discovery eligible while keeping implementation provisional.
Do not delay a truthful failed or blocked checkpoint until a new plan exists.
Resolve the proposal through the existing owner-controlled reassessment mechanism.
Synchronize accepted document references and graph definitions before affected work advances.
`revise_plan` alone does not clear reassessment holds.
Keep the existing synchronization requirement before a `first-step` stop.

When nothing material changes, record unchanged reassessment with `proposed_draft=None`.
Do not regenerate the plan merely because a checkpoint, resume, or takeover occurred.
Existing plans adopt this authoring contract at their next material revision.
An installed companion update alone does not invalidate accepted work or require migration.

## Keep progress and acceptance distinct

Keep version-bound plan snapshots immutable.
Name a separate progress-copy file or the selected workflow's ledger for checkbox updates.
Preserve the companion's checkbox structure in that progress record.
If a tracked document changes, record its actual new reference through the existing material rules.
Never retain an incorrect content hash to avoid reassessment.

Record each meaningful completed action and its check as evidence for its parent task.
Use activity reports for partial progress and structured checkpoints for verified outcomes or blockers.
Document checkboxes and workflow ledgers do not change canonical completion.
Keep checkpoint, reassessment, required independent review, and owner `accept_step` as separate obligations.
Retain the selected execution workflow's verdicts, fix budgets, and ledger requirements.
Before that workflow removes temporary files, retain required nonsecret evidence in durable artifacts.
Keep grants and tokens private. Do not turn temporary workflow notes into competing plan authority.

## Preserve authorization and language rules

Retain the user's execution method and existing authorization through routine replanning.
If complete-task authorization names no method, select a compatible method and state the choice.
Apply the selected method's workflow and applicable local policy.
Do not repeat the companion's handoff question for already-authorized work.
Planning-only work stops after its authorized planning output.
First-step work stops after verification, reassessment, and required synchronization.
Changes to the objective, relaxed acceptance criteria, and additional scope require their own authorization.
Planned implementation, commit, publication, and external actions retain their applicable permission boundaries.

Superpowers supplies the plan structure. STE governs new natural-language prose.
Preserve mandated template wording, code, identifiers, paths, and other protected literals.
Retain the existing language verification note and its evidence limitations.

## Handle an unavailable companion

If the companion is missing, unreadable, or incompatible, report the exact gap.
Do not claim it loaded or substitute a copied template as maintained guidance.
Preserve existing Adaptive Planning use with a clearly labelled provisional plan.
Apply all existing horizon, evidence, review, authorization, and synchronization rules.
Record that Superpowers authoring and self-review remain unverified.
Continue truthful checkpoints and independent authorized work.
Apply the maintained companion when it becomes available for the pending authoring work.
Synchronize resulting material changes through the normal lifecycle.
