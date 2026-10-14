# Instance selection and portable documentation plan review

> Historical pre-implementation record. The authorization boundary and test recipe below describe the plan-review phase. Later execution results belong in the private task report.

Date: 2026-10-14.

Artifact: [implementation plan](2026-10-14-instance-selection-and-portable-docs.md).

Scope: review the written plan. No implementation, source edit, configuration
change, deployment, commit, or memory update is authorized by this review.

Reviewer: `gemini-4-argon-high`, explicit effort `high`, fresh ordinary AGY
conversation through the task's Orca workspace. Read-only plan mode retained
the existing terminal sandbox setting. No permission settings were changed.

## Round 1

Verdict: **With fixes**. No Critical findings. Three Important findings and two
Minor findings.

| Finding | Coordinator disposition |
| --- | --- |
| Selection matrix omitted private preferences, unavailable entries, and the no-service case. Alias identity and outage scope needed exact rules. | Added all selection cases, `(host_id, epoch)` checks, bound-plan reads, the normal recovery path, identity mismatch handling, and bounded-worker outage behavior. Required removal of the inherited global-store sentence and localhost test workaround. |
| A foreign-epoch rejection did not test plan, grant, or reservation isolation. The two-token HTTP helper was implicit. | Added valid-epoch foreign-plan/grant checks and independent reservation checks. Defined a test-local real-app helper with separate directories and synthetic tokens. |
| Same-host volume guidance lacked an owned documentation step. Cleanup targets and protected rollback text boundaries were unclear. | Assigned those edits to Task 4, named the dated sections and client guides, protected exact rollback boundaries, and added post-cleanup instruction tests and private-value scans. |
| LAN file responsibilities and baseline integration state were unclear. | Listed the five runtime/deployment files, one test file, and five documentation files. Recorded that the inspected HEAD does not contain the LAN commit. |
| The isolated Linux test invocation and missing-validator behavior were unspecified. | Added a disposable image/container procedure with a read-only source mount, no production volume, no published port, and no normal service entrypoint. Added a missing-tool fallback and explicit qualification-gap reporting. |

The coordinator accepted all five findings against source evidence. The plan was
revised; no product changes were made. A fresh correction review was requested.

## Round 2

Verdict: **Ready to implement: Yes**. No Critical or Important findings remain.
Argon confirmed the five Round 1 dispositions against the revised plan and the
supplied source interfaces. A fresh conversation performed this correction
review. It does not authorize implementation.

One non-blocking Minor finding remains: make test-image cleanup explicit with a
`finally` block when turning the illustrative command into execution steps, and
check that `work/portability-tests/` records remain ignored. The plan already
requires owned-image cleanup and private records. This note clarifies execution
bookkeeping; the coordinator did not change the reviewed substantive plan.

Additional recommendations: preserve the rollback command substrings covered by
the existing test, and verify that the source bind mount is readable by container
UID `10001`. If either preflight fails, report the gap. Do not broaden permissions
or rewrite unrelated procedures to make a check pass.

## Verification boundaries

The plan's local links were checked, and its source contains no personal
installation address or home-directory path. The original tracked checkout
files remained byte-for-byte unchanged while drafting and reviewing the plan.
Runtime tests described in the plan have not been executed by this planning task.

Both provider runs exited successfully. Their exposed model matched
`gemini-4-argon-high`; effort `high` was passed explicitly. Downstream internal
effort was not exposed. Actual review tool records contain only `view_file`.
No reviewer file edits, commands, helpers, or permission denials were observed.
The inspected source baseline remained unchanged. The only shared-file additions
are the plan and this review record. Neither file was committed or pushed.

STE text is an Issue 9-oriented candidate. Official dictionary conformance is
outside this review. Raw provider results and submission hashes are retained in
ignored task records. No private provider metadata is reproduced here.
