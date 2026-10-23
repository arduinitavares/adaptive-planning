# Independent review: maintained Superpowers planning (af20754..1f75adc)

**Reviewer:** `claude-opus-5-5-high`. This is a text-only, read-only review of the supplied packet. I ran no tools, commands or file operations. I reviewed the content in two passes: first the source guidance, then the evidence.

**Packet limit:** The packet cut off partway through final-4 (`<truncated 50878 bytes>`). I could not see final-5, the six boundary cases, or the Native review-gate check. I judge those only through the coordinator's claims, and I list them under Declined to judge.

## Strengths

- **Clear split of ownership.** Superpowers writes the detailed plan. Adaptive Planning keeps the graph, the lifecycle, evidence and acceptance (SKILL.md:34-52, superpowers-planning.md:3-17). The skill finds the companion through the active catalog and does not copy its templates or pin a path. This avoids version drift.
- **The companion is required at both trigger points.** It is loaded at initial publication (SKILL.md:209-210) and at material reassessment (SKILL.md:263-265). That second trigger also covers corrective work after an invalidation. This directly addresses Review Focus #1: a material replan can't skip Superpowers just because the first plan used it.
- **The missing-companion fallback keeps standalone use working.** It requires a clearly labelled provisional plan and forbids claiming Superpowers authoring or self-review (SKILL.md:49-52, ref:137-146). This meets "without feature loss" honestly.
- **Graph steps map to document tasks, not checkboxes** (ref:42-46). This lets Superpowers' small checkbox steps live alongside a horizon of at most 10 graph steps, without flooding the graph. "Horizon is a maximum, not a quota" is restated (ref:48), and distant outcomes keep their prerequisites and expansion conditions (ref:50).
- **The checkbox/hash ruling is correct.** Keeping exact content hashes and using a separate progress copy (ref:108-112) protects the existing material-change detection. The rejected review suggestion would have let tracked documents drift without anyone noticing.
- **Precedence of the handoff question is handled.** The companion's handoff question is suppressed for work that is already authorized. Changes to scope, objective or acceptance criteria still need authorization (ref:124-131).
- **Runtime and installed copy are preserved.** The diff of the protected runtime paths is empty, the installed skill's hash matches the baseline, and the suite result is 895 tests, 894 passed and 1 skip (Windows-only). The tail shows `OK (skipped=1)`.
- **The behavior test has a real control.** The companion was requested in 0/5 baseline samples and 5/5 candidate samples. Both ran the same READ_SKILL protocol, so the difference comes from the skill text and not from the harness priming the model. The coordinator also caught two failures in the first candidate set, fixed the wording and reran, instead of accepting a 5/5 load rate as success.
- **The CSS test is now stronger.** It compares the served bytes with the actual asset (test_service_core.py:241), so it catches empty, wrong or truncated responses without depending on a comment title.
- **Limitations are disclosed honestly** (verification:25-29, 118-120, 136-139).

## Issues

### Critical

None.

### Important

**1. Readiness rule conflicts with "one graph step = one task" when a step bundles discovery and implementation. The final evidence shows the exact failure the RED fix was meant to remove, and it was scored as a pass.**

- **Where:** references/superpowers-planning.md:38-39 together with :42 and :94. Evidence: final-2 (Task 1 `parse`) and final-3 (Task 1 `step-1`). Claimed as passing at docs/verification/2026-10-23-maintained-planning.md:110-116.
- **What's wrong:** ref:38 says *"A placeholder path or 'run the repository tests' does not make an implementation task ready. Keep that work provisional."*
  - In final-2, `parse` is one graph step. Step 1 is discovery, but Steps 2-6 implement and commit against `[PROPOSAL] src/parser.<ext>` and "the discovered test command". The response then says "Consult `list_ready` to start `parse`." So an implementation step with placeholder paths is made eligible.
  - final-3 marks `step-1` "Ready" even though its commissioning substep depends on a supplier document that is missing.
  - Neither response puts a reassessment point between discovery and implementation.
- **Why it matters:**
  - The cause is a real gap in the guidance, not just a sloppy output. With a horizon of 3 that is already full (`parse`, `import`, `report`), the agent cannot add a separate discovery step. Nothing tells it how to gate implementation inside an existing step.
  - Under complete-task authority, an executor would go straight from discovering the fixture's encoding into implementation that the step document describes with placeholders. Discovery could change that tracked document's definitions, and nothing would trigger synchronization in the middle of the step.
  - This is Review Focus #3 and the core defect class from the first candidate set. Calling final-2 "discovery precedes code edits" (verification:113) is technically true only inside one step. It is not true at the canonical graph level.
- **Fix (one pass):** Add a rule to ref:38-40 along these lines:
  > When discovery and implementation share one graph step, end the discovery substeps with a checkpoint. If the discovered facts change the step's tracked definitions, treat that as material reassessment before running the implementation substeps. Otherwise, add a separate discovery step when the horizon permits.

  Then either:
  - **(a)** rerun case 2 and the five-sample set, as spec:96-98 requires after a wording change, or
  - **(b)** if the coordinator decides that gated discovery inside a step already satisfies the current text, codify that decision and record final-2/final-3 as conditional passes instead of clean passes.

  Leaving the text and the scoring in contradiction is the part that must change.

### Minor

1. **Self-review scope is inconsistent.** SKILL.md:40 limits the self-review to "the current planning horizon". ref:89 and spec:47 apply it to "whole outcome coverage". An agent reading only SKILL.md would review horizon tasks only, which weakens the coverage check. Reword SKILL.md:40 so that detail stays limited to the horizon while the self-review covers the whole outcome.

2. **Who loads the companion for a material revision is unclear.** SKILL.md:263 ("Before proposing a material revision, apply…") names no actor. Workers submit `proposed_draft` through `record_checkpoint`, so it could read as a worker duty. ref:81-82 and spec:39 assign it to the owner. Align SKILL.md:263 with the owner role. Make explicit that a worker without the companion submits evidence and affected steps, and leaves `proposed_draft` empty unless it was written to the standard.

3. **No rule for a version or hash that isn't exposed.** ref:11 asks for the "exposed version or content hash". final-2 invented `version/hash: supplied-session-catalog-writing-plans`. final-3 correctly said "unexposed; uncomputed". Add: "If neither is exposed, record `not exposed`. Do not invent a label."

4. **Template wording versus domain and selected method.** ref:134 ("Preserve mandated template wording") makes agents put "REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended)…" and "Implementation Plan" on a physical workshop pilot (final-3). It also appears on a plan the user authorized for Native execution (final-2). The domain table (ref:66-75) and method retention (ref:124) don't say how this header ranks against them. The impact is confusion, not a broken gate. Add one line: the template's execution header gives way to the selected method, and its software-specific wording may be adapted for non-software deliverables.

5. **Fallback edge cases are undefined.** "Incompatible" (ref:139, spec:36) has no definition, for example a missing self-review or missing task structure. "Apply the maintained companion when it becomes available for the pending authoring work" (ref:145) could be read as requiring a full re-author of a fallback-authored plan, which conflicts with ref:102. State that the companion becoming available is not a material trigger by itself. It applies at the next initial authoring or material revision.

6. **Trigger wording drift.** SKILL.md:46 ("when evidence changes the remaining plan") is broader than the `disposition="material"` trigger at SKILL.md:256/263 and spec:40. Use the material wording at line 46.

7. **The verification record overstates language and boundary coverage.** Every final response says "STE review is pre-satisfied for this simulation", so the harness waived the actual STE companion. The language notes in the responses are template fields, not evidence that STE was applied. Every response also shows the host model policy, Orca paths and model IDs leaking into plan text, which means the packet contained host-policy context. The row at verification:172 ("Language … assessed with stated limits") and the rubric line at scenarios.md:31 should disclose that STE was pre-satisfied and that host policy was present in the prompts.

8. **final-3 renders an ASCII map** after reading "honest dependency map" as an explicit ASCII request. SKILL.md:376 allows ASCII only on explicit request. This is a minor output mistake, not a guidance defect, but the clean-pass row for case 3 hides it. Note it in the record.

9. **The CSS test fix is unrelated to the feature.** It is correct and disclosed (ledger ruling). Consider a separate commit so the feature can be reverted or cherry-picked without it. The spec did not explicitly authorize test edits. I accept the ruling because the baseline failure was in a test, not in runtime behavior.

## Challenges to coordinator scoring and rulings

- **Case 2 and case 3 "pass"**: challenged. See Important #1.
- **Missing-companion provisional fallback ruling**: upheld. It is the only option that keeps standalone use without fabrication.
- **Checkbox-hash ruling**: upheld.
- **Native work without a repeated handoff question**: upheld. ref:130 keeps the scope boundary.
- **Supplied-text simulations as bounded evidence**: upheld. Add the STE and host-policy points from Minor 7.
- **Incidental mistakes in the simulations**: these are not guidance defects and I did not score them against the source. Examples:
  - final-1 decides `disposition="material"` for a discovery checkpoint before anything is observed.
  - final-4 invents parameters (`rework_observed`, `hold_id`, `cas_metadata`).
  - final-4 mixes up the simulation's "no-write" constraint with the plan's authorization.

## Recommendations

- Apply the Important #1 fix and Minor items 1-3 together. They are short wording changes in two files. If you rerun, add one case with a full horizon where an existing step must contain discovery.
- During branch testing, run the README candidate-path test on a real client with the real catalog. Include a material replan and a physical-domain plan. That is the only evidence for actual client discovery.
- Measurement receipts hash only `SKILL.md`, so edits to the references alone don't change `skill_version`. This was true before this change. Consider it before relying on receipts to compare variants.

## Declined to judge

- final-5 responses: cut off in the supplied packet.
- The six boundary responses (missing companion initial/revision, unchanged checkpoint, checkbox digest, publication scope, legacy resume): cut off. I relied only on the claim at verification:122-125.
- The Native review-gate diagnostic (`native-review-red`): cut off. I relied only on the claim at verification:126-127 and 147-149.
- Baseline and rejected-candidate responses: not supplied; they are only summarized in the verification record.
- The content of `superpowers:writing-plans`: only its hash was supplied, so I can't check how faithfully the outputs follow it.
- Interaction between brainstorming's design-approval gate and complete-task authority (via ref:22-24): speculative without the companion text.
- Real client skill discovery: explicitly out of scope and disclosed.
- Simulated API parameter names in the outputs: the simulations are not API tests, as the record states.
- Host-policy content in the outputs (cross-family review lines, paths): a harness confound, not a skill behavior.
- Test suite details beyond the supplied output tails, and the empty diff of protected paths: accepted as reported.
- Spec acceptance item 5: fulfilled by this review.

## Assessment

**Ready to merge?** With fixes.

**Reasoning:** The integration is well built. The runtime is untouched and the material-replan trigger is wired correctly at both lifecycle points. The baseline-versus-candidate evidence supports automatic companion loading. One Important gap remains: the guidance on readiness when discovery and implementation share a step. The final evidence shows that gap, so it should be fixed in the wording, or the decision codified, before merge. The current branch is suitable for the user's branch testing now. Merging and pushing stay with the user.
