# Maintained planning verification

## Current result

The branch candidate is ready for user testing. The post-review five-case set
plus the focused full-horizon case passed the scoped planning rubric. All six
loaded the maintained companion. Seven earlier boundary checks also passed;
their original source snapshots and limits remain recorded below.

The final runtime regression run passed: **894 passed, 1 Windows-only skip**.
JavaScript syntax and dashboard DOM checks passed. Service, dashboard, schema,
deployment, and installed-skill files remain unchanged.

Independent Opus review found one Important readiness gap. The coordinator fixed
it in one pass and verified RED-to-GREEN behavior plus the full suite. Nine Minor
items remain deferred. The provider truncated part of the review evidence appendix;
this is not a claim of a full-packet review, live client discovery, or strict STE
conformance. No re-review was dispatched, per the Native workflow's fix budget.


## Scope and provenance

- Branch: `dev/superpowers-rolling-plans`.
- Base: `af207546607d43ffa3b0cb8d23b4dca121f36b43`.
- Checkout: `/Users/aaat/projects/adaptive-planning`.
- Baseline and installed Adaptive Planning SHA-256:
  `d5d53ffaece909c654f62ee8c478b889f244584cde8fd18fd5290c3cb8fd3664`.
- Current tested Superpowers catalog: `7.0.1`.
- `writing-plans` SHA-256:
  `a6c67c1900064347c2a329990dd3c555657c51c3ec53b259a08aa01a2c26139a`.
  The integration resolves the installed catalog entry. It does not pin this version.
- Native implementation author: OpenAI coordinator. Review family: Claude.

## Test method

Use [the synthetic scenarios](../../tests/fixtures/planning-quality/scenarios.md)
with the fixed rubric. Each numbered scenario gets one fresh response per variant,
for five samples per variant. A response can request the companion using
`READ_SKILL: superpowers:writing-plans`. Only then does the harness supply the
current installed content. The harness retains the same conversation for that
continuation. Each additional boundary check gets its own fresh response.

These are supplied-text simulations of planning, reads, and publication. They
do not exercise real client skill discovery or mutate the live planning service.
AGY exposes runtime tools and ambient provider context. Score only explicit
simulated reads, inspect actual tool events, and reject unsupplied catalog facts.
This limitation prevents a universal client-compliance claim.

The ordinary entries run through Orca in this checkout. The task-owned execution
directory is `/private/tmp/adaptive-planning-integration`. No model descendants
are authorized. The coordinator controls every launch. Raw prompts, responses,
source snapshots, events, and receipts remain there for inspection.

Requested settings: Argon `gemini-4-argon-high`, effort `high`; Opus
`claude-opus-5-5-high`, effort `high`; AGY `1.4.5`; Orca `1.4.237`.
The observed initialization identifies the exact model and `request-review`.
The launcher passes `--mode plan`, `--sandbox`, and `--disable-slash-commands`.
Backend reasoning effort is provider-managed; it is not separately exposed.
Existing global grants and Orca/telemetry hooks remain unchanged. No tool-level
isolation or permission change is claimed. Catalog selection and the existing
qualified supplied-text route were checked before launch.

## Baseline results

The initial software and material-replanning samples both produced a plan without
requesting `writing-plans`. The physical-work sample also omitted the request.
The material sample opened with "# Material Replan: Encoding Contract and
Downstream Steps" and proposed editing `plan.md` directly. It retained material
holds and history correctly, but did not load the maintained authoring standard.
This establishes the missing automatic integration before the skill edit.

The full baseline command was:

```sh
uv run --frozen python -B -m unittest discover -s tests -p 'test_*.py'
```

The restricted run could not bind test sockets: 890 tests, 9 failures,
41 errors, 1 skip. With local test-server access, the baseline ran 895 tests:
893 passed, 1 failed, 1 skipped. The sole failure was
`test_legacy_standard_library_cli_and_service_coexist`. It asserted an obsolete
CSS comment. The server returned the current stylesheet correctly.

The test now compares the HTTP response with the actual stylesheet bytes. This
catches a wrong, empty, or truncated asset without depending on a comment title.
The focused test passed after that test-only correction. Runtime CSS is unchanged.

Both documented JavaScript syntax checks passed. The dashboard DOM test passed
with `node --test tests/test_frontend_dom.js`.

## Plan review dispositions

Two fresh Opus supplied-packet reviews examined the design before implementation.

- Accepted: preserve standalone planning with a disclosed provisional fallback
  when the companion cannot load. Add missing-initial and missing-revision checks.
- Accepted: distinguish complete outcome coverage from detailed horizon work.
- Accepted: preserve existing authorization and the selected execution method.
- Accepted: name authoring ownership, invalidation, unchanged resume, STE, and
  protected literal behavior.
- Accepted: specify simulations, exposed tools, source hashes, exact commands,
  preflight, all boundary cases, pass criteria, and installed-copy verification.
- Rejected: ignore changed content hashes for checkbox edits. That would weaken
  existing tracked-reference rules. Keep bound snapshots immutable and track
  progress separately. Editing a bound snapshot retains material synchronization.
- Qualified: the provider's ambient context cannot be proven absent. Restrict
  scoring to the explicit protocol and disclose that limitation.
- Local commit: the user requested a separate branch for testing before merge.
  A local commit retains the candidate on that branch. Push and merge are outside
  this delivery.

The remaining re-review findings were concrete protocol and documentation gaps.
The coordinator resolved them in the spec, plan, and this record. The final change
review and its dispositions are recorded below.

## Candidate results

The first candidate set loaded the companion in all five cases (baseline: zero
of five). It was not accepted as green: sample 2 proposed implementation with
unknown paths and contracts as ready work, and sample 5 assigned invented item
numbers to an unread checklist. The coordinator added explicit separation of
source facts and proposed designs, discovery readiness, and honest self-review.
The coordinator initially scored the pre-review five-case set as passing. The
independent review correctly challenged cases 2 and 3: their ready graph steps
bundled discovery with unknown or externally gated execution. Those two scores
are superseded by the Important finding below; the pre-review set is not green. All five requested
`READ_SKILL: superpowers:writing-plans` before authoring. The affected samples
were rerun within that final set. The complete responses and observed execution
receipts are retained in [the behavior evidence](2026-10-23-planning-behavior.json).

| Pre-review case | Observed result |
| --- | --- |
| 1. Initial software | Ready source discovery; design and implementation remain provisional; plan-only stop |
| 2. Material revision — FAIL after review | Accepted `inspect` and ready sibling `report` retained; discovery precedes code edits; dependent `import` stays provisional |
| 3. Physical pilot — FAIL after review | Two discovery tracks; supplier prerequisites gate commissioning; no fabricated completion proof or software tests |
| 4. First-step revision | Revised task detail; unknown contracts remain provisional with holds; no further execution |
| 5. Horizon two | Two active tasks; unread checklist stays unknown; later outcomes retain prerequisites and expansion conditions |

These checks assess the fixed planning rubric. Simulated operation snippets are
not API integration tests or verified live calls. The preserved runtime suite is
the separate API and lifecycle regression evidence.

Six boundary simulations passed their named checks: missing initial companion,
missing companion during revision, unchanged checkpoint, changed checkbox digest,
unauthorized publication scope, and unchanged legacy resume. These are narrow
boundary checks, not an endorsement of every incidental statement in a response.
A focused Native check also passed: with `review_required=false` and no final
gate, the agent retained the final whole-branch review and added no per-task reviewer.

The complete candidate regression suite ran with local test-server access:
**895 tests, 894 passed, 1 skipped**. The skip is
`test_actual_windows_reader_300ms_then_cli75_indefinite`, which requires actual
Windows share-denying handles. The command exited zero. Both JavaScript syntax
checks and the dashboard DOM regression test passed.

No service, schema, CLI, dashboard asset, or deployment implementation changed.
The only existing automated-test edit corrects the baseline CSS assertion above.
This provides regression evidence for the preserved implementation. The agent
simulations provide bounded evidence for the changed instructions; they cannot
prove universal compliance across models and clients.

The STE surface scanner produced candidate flags for mixed procedure and
descriptive prose. The official dictionary and governed termbase are unavailable.
This is an Issue 9-oriented candidate, not a strict STE conformance claim.

Pre-review skill SHA-256: `c530abd6faa5d7b2ebc1107c4da66a8f31d3873a706e9e3b572687f1906b7873`.
Pre-review integration-reference SHA-256: `e74b2e80bc7012f7951e1c59a0904f0f7e564a5914006cdd0a23171f6b4b1514`.
The 22 pre-review behavior runs completed with natural exit zero; no real tool activity
was observed. The `native-review-red` case name identifies the diagnostic run;
it passed before any review-gate wording change, so no such change was made.

## Independent change review and disposition

Fresh Opus High reviewed commit `1f75adce13882df57feccc816ed519b1b62e8115`.
The [complete review](2026-10-23-independent-review.md) records no Critical issue,
one Important issue, and nine Minor items. Its verdict was **With fixes**.

Important 1 is accepted. A graph step must not become ready merely because its
first checkbox is discovery. The repair separates the verifiable discovery outcome from provisional
execution, including when the graph horizon is full. A pending task can become
discovery-only; later execution stays in the roadmap until a slot opens, its
details are authored, and owner-controlled reassessment synchronizes the graph.
The original final-2 and final-3 outputs are retained as RED evidence. The explicit full-horizon diagnostic passed even before the fix, showing prompt
sensitivity; it is supplemental, not RED evidence. The fresh post-fix five-case set and the full-horizon case passed.

| Post-fix case | Result |
| --- | --- |
| `fixed-1` initial software | Discovery-only ready task; implementation provisional; all outcomes covered; plan-only stop |
| `fixed-2` material replan | `parse` narrowed to discovery; explicit stop before code edits; `import` held and `report` retained |
| `fixed-3` physical pilot | Separate preparation inspection and commissioning nodes; commissioning blocked on supplier proof |
| `fixed-4` first-step | Affected tasks remain held and provisional; exact versions unknown; no further step executed |
| `fixed-5` horizon two | Two discovery tasks; all four execution outcomes retained outside the horizon |
| `fixed-6` full horizon | Stable `parse`, `import`, `report` retained; discovery checkpoint frees a slot before `parse_impl` promotion |

Exact RED evidence: pre-review `final-2` made the mixed `parse` task eligible and
`final-3` marked mixed discovery/commissioning ready. Exact GREEN evidence:
`fixed-2` says "Do not execute parser code edits inside this discovery task";
`fixed-3` gives commissioning its own blocked graph node; `fixed-6` stops before
code edits and requires checkpoint, acceptance, authoring, and synchronization.

Post-fix integration-reference SHA-256:
`934fcf0968a2f12c9c5b10c7bd427ea9f8adb09e8fbf08f7fcf5b5e1903588b2`.
The `SKILL.md` hash remains the pre-review hash. All 29 behavior runs are preserved
with requested reads, source manifests, final responses, effective model and mode,
and observed process exits. No real tool activity was observed. Final suite:
895 tests in 241.390 seconds, `OK (skipped=1)`, exit zero. Node DOM test: exit zero.

The review packet was truncated by the provider partway through final-4. The
reviewer saw all normative integration source but did not receive all supplied
behavior evidence or the companion appendix. The review is consequently narrower
than the packet file. No full-packet review or second reviewer verdict is claimed.
The coordinator inspected the omitted evidence directly and retains it here.

Deferred Minor items under the Native workflow's one-fix-pass rule:

1. Top-level self-review wording can be clearer about full outcome coverage.
2. Top-level material-proposal wording can name the owner explicitly.
3. Unexposed companion version/hash can have an explicit `not exposed` instruction.
4. Mandated execution-header wording can be clearer for non-software and selected methods.
5. Incompatible-companion and newly available-companion edge cases can be defined more tightly.
6. The short trigger wording can consistently say material change.
7. Simulation language coverage is limited: STE was pre-satisfied, and host policy was supplied in prompts. Full STE conformance is not tested.
8. The physical sample rendered ASCII without a specifically ASCII request; this incidental output error remains recorded.
9. The baseline CSS test correction could be split into a separate commit for selective cherry-picking.

Coordinator dispositions for every behavior the reviewer declined to judge:

| Set-aside item | Coordinator ruling | Cost if wrong |
| --- | --- | --- |
| final-5 | Inspected directly; horizon and unknown-source checks passed before the readiness fix | Another output defect may remain; fresh post-fix sample required |
| Six boundary cases | Inspected all six complete responses; retain narrowly scoped results | Other client behavior remains untested |
| Native diagnostic | Inspected explicit no-per-task-review response | A less explicit prompt may still over-review |
| Baseline and rejected candidates | Inspected complete outputs and retained their failures | Model variance limits causal certainty |
| Maintained writing-plans content | Read installed content and verified its hash; no vendored template added | A future incompatible companion needs explicit fallback |
| Design approval versus complete-task | Existing authority controls; discovery/design remains required when material choices are unresolved | Incorrect boundary interpretation could overstep scope |
| Real client discovery | Leave to explicit candidate-path branch testing; make no end-to-end claim | Catalog differences may prevent automatic loading |
| Simulated API snippets | Do not treat them as executable API examples; runtime suite tests actual contracts | Readers must not copy invented parameters from evidence transcripts |
| Host-policy output | Disclose supplied policy as simulation context, not generic skill behavior | Context can influence results |
| Suite details and unchanged runtime | Coordinator ran and inspected the suite and verified the full diff | macOS cannot exercise the Windows-only skipped case |
| Acceptance item 5 | Fresh independent source review occurred; preserve its partial-packet limitation | Evidence coverage remains partly coordinator-assessed |

## Coordinator rulings and practical costs

| Ruling | Reason | Cost if wrong |
| --- | --- | --- |
| Continue Native work without a repeat handoff question | Implementation is already authorized | A missed scope change could exceed authorization; explicit scope boundaries remain in the contract |
| Allow disclosed provisional planning when the companion is unavailable | Preserve standalone use and evidence recording | Plan detail can remain below Superpowers quality until the companion loads |
| Keep exact tracked hashes and separate progress copies | Preserve material-change detection | Checkbox edits to a bound document still require synchronization |
| Compare served CSS with the actual asset bytes | The old assertion checked an obsolete comment | The test validates asset delivery, not visual appearance; DOM tests provide separate coverage |
| Treat supplied-text tests as bounded evidence | Ambient context and real client discovery are not isolated | Another client or model can behave differently; branch testing is still needed |
| Retain a local commit on the requested branch | Make the candidate testable before merge | No installed behavior changes until the candidate is explicitly loaded or installed |

## Preservation evidence

| Existing feature | Evidence |
| --- | --- |
| Dependency map, plan detail, activity, stable publication, dashboard recovery | Unchanged `assets`; Python dashboard tests and Node DOM regressions passed |
| Horizons, stable step IDs, material holds and owner resolution | Unchanged runtime; plan, checkpoint, holds, and acceptance tests passed |
| Independent review and owner acceptance | Unchanged review implementation; review and acceptance tests passed; focused Native boundary preserved the chosen method |
| Grants, ownership, attempts, resources, retries and recovery | Ownership, attempts, resources, recovery, scope, and store tests passed |
| Measurements and immutable receipts | Measurement and dashboard tests passed; existing skill instructions retained |
| Service identity, HTTP/LAN, availability and legacy compatibility | Instance, HTTP, LAN, availability, interop, and corrected legacy asset test passed |
| Language, first-step, plan-only and complete-task boundaries | Existing instructions retained; synthetic boundary cases assessed with stated limits |
| Installed skill and running service | Installed hash matches baseline; no installed-copy write or service mutation |

## Settlement

All 32 task-owned model processes completed with natural exit zero. The actual
event audit observed no tool activity or delegated children. All five task-owned
Orca terminals were closed after their jobs and process groups settled. No unrelated
session was closed. The original raw evidence remains in task-owned scratch;
the relevant responses, review, rulings, and ledger are retained in this branch.

## Branch testing

Use the candidate path explicitly in a fresh chat:

```text
Use /Users/aaat/projects/adaptive-planning/SKILL.md and its relative references.
Plan this task only. Use the current installed Superpowers planning skill as
the candidate directs. Do not replace the installed Adaptive Planning copy.
```

Then supply a material discovery and ask the agent to revise the plan. Confirm
that it applies the same maintained authoring standard to the affected work.
The installed skill and running service remain unchanged for this branch test.
