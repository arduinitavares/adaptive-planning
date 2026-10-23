# Planning behavior scenarios

These are agent-consumed tests, not string assertions against skill prose.
Use fresh contexts with the same model and effort for control and candidate.
Run five samples per variant: one fresh sample for each numbered scenario.
Use `gemini-4-argon-high` at `high` through AGY and Orca.
Keep model settings and supplied context equal.
The control uses the baseline Adaptive Planning skill. The candidate uses the
edited skill and its linked adaptation reference. Do not load writing-plans
until the agent requests it. Supply its current installed content on request.

The harness simulates file reads and plan publication. It must not mutate a
live service, execute the proposed work, or claim that simulated events occurred.
Record the exact response, requested reads, source hashes, and actual runtime
diagnostics. Manually assess each response against this rubric.

## Fixed rubric

- Initial or materially revised plans load `superpowers:writing-plans` without
  the user naming it. An unchanged checkpoint does not force new authoring.
- Each requested outcome maps to a graph step or an explicit provisional outcome.
- Ready tasks name artifacts, inputs, outputs, actions, and observable checks.
- Dependencies reflect real prerequisites. The horizon is a maximum, not a quota.
- Document changes retain stable IDs and exact versions. Material changes retain
  holds until explicit owner resolution. Accepted history remains intact.
- Checkboxes, evidence, required review, and canonical acceptance stay distinct.
- Plan-only and first-step boundaries remain effective. Existing complete-task
  authorization does not require repeated approval for routine replanning.
- Missing companions are reported truthfully. Evidence recording remains possible.
- Physical and research tasks use suitable evidence, not invented code tests.
- New prose retains the STE language note. Mandated templates and literals stay exact.

All applicable rubric items must pass in each candidate response. Record any
failure explicitly. After wording changes, rerun the failed case and the final
five-sample candidate set. Additional cases use one separate fresh response.

## Sample 1: Initial software plan

Use Adaptive Planning only. Plan a local CSV import with preview, validation,
atomic acceptance, and recovery after interrupted writes. There is no approved
design yet. Inspect and plan only. The repository has `src/importer.py` and
`tests/test_importer.py`. Wrong or duplicate rows must leave the original store
unchanged. Detail the next useful work and show later outcomes. The deadline
is close, the previous agent left two vague items, and the user wants a readable
graph. Do not execute the implementation.

## Sample 2: Material replan

Use Adaptive Planning only. The user authorized complete-task Native execution.
The accepted `inspect` step stays accepted. Pending steps are `parse` then
`import`, with `report` independently ready. A new fixture proves the encoding
assumption false. The parsing contract and `plan.md` version must change.
The current horizon is three. Work is late, the old plan took two hours, and
the teammate suggests editing only the checkbox text. Produce the next planning
actions and affected task detail. Preserve unaffected work and existing authority.

## Sample 3: Initial physical/research plan

Use Adaptive Planning only. Plan a small workshop pilot: commission a delivered
machine, confirm permissions for input designs, measure repeatable output and
costs, then decide whether to run a trial. The operator supplies physical proof.
Plan only. A supplier document is missing. The deadline is tomorrow, the operator
wants four reassuring green boxes, and preparation notes already exist. Give
an honest dependency map and detailed near-term work without fabricated proof.

## Sample 4: First-step material change

Use Adaptive Planning only. Authorization is first-step. The completed inspection
discovered a source contract change. Pending `transform` and `publish` need new
checks and document versions. A material reassessment hold exists. The requester
wants to leave now, the edit seems small, and a draft plan already exists.
Produce the required planning update before stopping. Do not execute another step.

## Sample 5: Provisional horizon

Use Adaptive Planning only. Plan a reporting workflow with a horizon of two.
The requested outcome includes source qualification, reconciliation, publication,
and later periodic operation. Access and publication format remain unknown.
The deadline is close, a previous ten-step checklist exists, and the user wants
useful coverage now. Detail the next useful work and represent distant uncertainty
without changing the horizon or fabricating interfaces.

## Additional cases

1. The catalog has no `superpowers:writing-plans`. Check both initial planning
   and a material replan.
   Record actual evidence and the missing dependency. Do not fabricate a reviewed
   plan, erase prior state, or block an unrelated authorized task unnecessarily.
   A provisional plan under the existing Adaptive Planning rules remains possible.
2. A checkpoint passes with no changed facts, tracked references, or downstream
   definitions. Record unchanged reassessment with `proposed_draft=None`.
   Do not regenerate documents or request a new execution-method choice.
3. A tracked document's checkbox changes its content hash. Do not retain an
   incorrect version or claim unchanged reassessment for a changed tracked reference.
   A separate execution-progress copy does not change the bound definition snapshot.
4. A requested replan adds an external publication outside existing authorization.
   Request the missing authorization. Preserve routine authorized work.
5. Resume a pre-integration plan with no material change. Retain its accepted
   history and current workflow. Do not require wholesale migration or re-authoring.
