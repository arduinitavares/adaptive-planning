# Maintained Planning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Make Adaptive Planning apply maintained Superpowers planning quality on initial plans and material revisions.

**Architecture:** Add a companion-loading contract and a small adaptation reference. Preserve the service and dashboard implementation.

**Tech Stack:** Markdown skills, Python unittest, Node dashboard tests, bounded agent behavior scenarios.

**Spec:** [Maintained planning integration](../specs/2026-10-23-maintained-planning.md).

## Global Constraints

- Work on `dev/superpowers-rolling-plans`. Leave merge to the user.
- Preserve all current behavior named in the spec.
- Resolve `superpowers:writing-plans` by name through the current catalog.
- Keep the installed skill and shared service unchanged during branch testing.
- Use synthetic scenarios. Do not include private business details in fixtures.

## Review Focus

- A material replan silently skips Superpowers because the initial plan used it.
- A missing companion causes fabricated success or blocks truthful checkpoints.
- A distant outcome appears executable despite unresolved prerequisites.
- Document checkboxes bypass canonical review, holds, or owner acceptance.
- A planning handoff repeats approval already supplied by the user.

## Task 1: Establish behavioral and regression baselines

**Files:** Create `tests/fixtures/planning-quality/scenarios.md` and `docs/verification/2026-10-23-maintained-planning.md`.

**Interfaces:** Consumes the unchanged skill and synthetic scenarios. Produces observed baseline outputs and a fixed scoring rubric.

- [x] Run five fresh-context baseline samples before changing the skill.
- [x] Use `superpowers:writing-skills` for behavioral RED/GREEN verification.
- [x] Verify the AGY version, exact model catalog, and previously qualified read-only transport.
- [x] Record `--mode plan`, `--sandbox`, roots, grants, and effective permission mode.
- [x] Disable slash-command expansion. Inspect delivered inputs and actual tool events.
- [x] Stop the affected launch on failed selection, unavailable controls, or real tool activity.
- [x] Record the packet hashes, simulated reads, actual tools, and shutdown receipts.
- [x] Inspect every response and preserve exact failure excerpts.
- [x] Run `uv run --frozen python -B -m unittest discover -s tests -p 'test_*.py'`.
- [x] Run `node --check assets/dashboard/app.js` and `node --check tests/test_frontend_dom.js`.
- [x] Run `node --test tests/test_frontend_dom.js`.
- [x] Record failures, skips, environment restrictions, and actual exit status.

**Expected:** The missing automatic companion-loading behavior is observed. Existing test results establish the comparison baseline.

## Task 2: Integrate the maintained authoring skill

**Files:** Modify `SKILL.md`, `README.md`, and `references/live-dashboard.md`. Create `references/superpowers-planning.md`.

**Interfaces:** Consumes the current catalog's `superpowers:writing-plans`. Produces skill guidance for task documents linked through existing `DocumentRef` and step definitions.

- [x] Add required companion resolution for initial plans and material revisions.
- [x] Add outcome coverage, horizon, non-software adaptation, and missing-dependency guidance.
- [x] Connect the guidance to publish and reassessment stages.
- [x] Replace the optional authoring sentence while retaining a framework-neutral service.
- [x] Define provisional fallback, horizon coverage, checkbox versions, and handoff precedence.
- [x] Preserve the existing lifecycle and published-history rules.
- [x] Run the same five fresh-context samples against the candidate.
- [x] Exercise missing-companion initial and revision cases in separate fresh responses.
- [x] Exercise unchanged checkpoint, checkbox-version, scope-change, and unchanged-resume cases in separate fresh responses.
- [x] Correct any failed case. Rerun that case and the final five-sample candidate set.

**Expected:** Agents request the maintained skill and apply it to the affected work. Existing acceptance and permission boundaries remain intact.

## Task 3: Verify preservation and prepare branch testing

**Files:** Complete `docs/verification/2026-10-23-maintained-planning.md`.

**Interfaces:** Consumes final diff, test outputs, behavior transcripts, and reviewer findings. Produces a reviewable branch and testing instructions.

- [x] Run the full Python suite and documented Node checks.
- [x] Verify `git diff af207546607d43ffa3b0cb8d23b4dca121f36b43 -- adaptive_planning assets scripts pyproject.toml uv.lock Dockerfile compose.yaml` is empty.
- [x] Check untracked files with `git status --porcelain` for the same protected paths.
- [x] Compare the installed skill hash with its recorded baseline hash.
- [x] Obtain a fresh cross-family whole-change review through Orca.
- [x] Resolve material findings within the review budget. Record deferred minor findings.
- [x] Record the candidate path, test limits, and how to test without replacing the installed skill.

**Expected:** All existing checks pass or have explicit baseline dispositions. The user can test the branch before merging.

Execution: Native implementation with `superpowers:executing-plans` by the OpenAI coordinator. This is one cohesive skill
change with a fresh final reviewer. The user's implementation request supplies
authorization. No repeated handoff approval is required. The branch-local candidate
is the deliverable. Create a local commit to keep the candidate on the requested
test branch. Push and merge remain with the user.

Delegation: ordinary AGY entries through Orca, no descendants, maximum three
workers plus coordinator. Use Argon High for paired behavior samples and fresh
Opus High for plan and final reviews. Exact IDs are `gemini-4-argon-high` and
`claude-opus-5-5-high`, both at `high`. Provider settings and actual cwd are recorded.
