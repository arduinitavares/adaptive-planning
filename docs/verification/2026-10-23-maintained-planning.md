# Maintained planning verification

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
The coordinator resolved them in the spec, plan, and this record. Final change
review will examine the resulting artifact and observed verification.

## Candidate results

The first candidate set loaded the companion in all five cases (baseline: zero
of five). It was not accepted as green: sample 2 proposed implementation with
unknown paths and contracts as ready work, and sample 5 assigned invented item
numbers to an unread checklist. The coordinator added explicit separation of
source facts and proposed designs, discovery readiness, and honest self-review.
The final five-case set passed the applicable authoring rubric. All five requested
`READ_SKILL: superpowers:writing-plans` before authoring. The affected samples
were rerun within that final set. The complete responses and observed execution
receipts are retained in [the behavior evidence](2026-10-23-planning-behavior.json).

| Final case | Observed result |
| --- | --- |
| 1. Initial software | Ready source discovery; design and implementation remain provisional; plan-only stop |
| 2. Material revision | Accepted `inspect` and ready sibling `report` retained; discovery precedes code edits; dependent `import` stays provisional |
| 3. Physical pilot | Two discovery tracks; supplier prerequisites gate commissioning; no fabricated completion proof or software tests |
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

Final skill SHA-256: `c530abd6faa5d7b2ebc1107c4da66a8f31d3873a706e9e3b572687f1906b7873`.
Final integration-reference SHA-256: `e74b2e80bc7012f7951e1c59a0904f0f7e564a5914006cdd0a23171f6b4b1514`.
All 22 behavior responses completed with natural exit zero; no real tool activity
was observed. The `native-review-red` case name identifies the diagnostic run;
it passed before any review-gate wording change, so no such change was made.

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
