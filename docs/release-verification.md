# Release verification records

These are historical source and runtime observations from the release records.
They do not establish a reader's deployment, client configuration, or installed
skill. Personal activation receipts, original source files, exact extracted
blocks and their hashes are retained in ignored local deployment records.
No credential or private installation path is included here.

The records were carried by source `281c882f8fc41f984198b17f63bf5b44008fb5c4`
and the LAN addition `d2ad6a073eeac919fd270b0176df8c5cbc715ff5`.
They were preserved from local base `daf6c94d09843553eb420c46e396cc050c898f9b`
plus that LAN addition. Source provenance does not make these checks fresh.

## Historical source and platform checks

The 2026-10-09 Task 26 source record used accepted base
`bfe4adfe27622231562729c3e25f672f8cc425eb`.
Its scoped matrix passed 14 tests. The combined transport suite passed 75.
The full suite discovered 815 tests: 814 passed and one Windows-only check
skipped, on CPython 3.13.15 / SQLite 3.53.1. Wheel and source distribution
build/install checks used environments outside the checkout. Node 26.7.0
syntax and synthetic DOM checks passed. These results precede later source
and image changes. Expected negative-path diagnostics remain in the logs;
passing does not mean pristine output.

The later Linux Docker exercise used Python 3.13.16 / SQLite 3.53.4.
It covered bounded worker/checkpoint/outage flows, same-volume recreation,
and a headless Chromium view. Its worker roles and review were synthetic.
Earlier admin evidence covered unchanged backup/export/restore and recovery
paths only: 27 of 30 source bindings still matched. It did not qualify changed
transport or the whole later source. Historical browser results retained
14 passes and five unknowns at their original runtime; changed JavaScript
prevented carrying those functional passes to the later candidate.
See the [original source coverage limits](operations.md#task-26-source-contracts-and-release-evidence).

## Source closure and activation checks, 2026-10-13

The first closure snapshot recorded 829 Python tests, exit 0, one platform
skip on host Python 3.13.15, and 11 synthetic JavaScript DOM scenarios.
Five installed Codex 0.160.1 processes discovered 29 tools and made 49 calls,
including eight mutations, with negotiated MCP `2025-06-18`.
Close/reopen, stop/start, restart and same-volume recreation retained state.
Those processes tested the earlier image before an equivalent RUN-layer split.
The split image retained identical backend and asset bytes and ran as UID 10001
with Python 3.13.16 / SQLite 3.53.4. A private existing-volume copy verified
schema 1-to-2 migration without changing the real volume in that check.

The later correction snapshot recorded seven RED cases, then 829 Python tests
with one platform skip and all 14 frontend scenarios. Negative startup-fixture
diagnostics remained in the logs. An independent Argon review approved the
source, with two optional UI polish findings deferred.
The recorded final image was
`sha256:a4f94ef59b2b14513d921ae841404d0c2e9ef2998dba814fd249a08afc2f79ff`.
These observations bind to those source/assets and that image.

Browser coverage included focus/selection retention, literal hostile text,
compact index navigation, a stopped plan, and empty/oversized page rejection
and recovery. Default-25 and maximum-100 boundaries were checked in Node;
the browser result does not cover those boundaries. HTTP capacity checks used
two legal 6 MB synthetic plans: individual responses were 200, full aggregate
413, and compact JSON/root responses 200. These were isolated test plans.

The installation record verified a quiescent backup and v1-to-v2 migration
that retained two original plans, five events, authority tables, rowids, epoch
and credential, adding only the schema event. Fifteen installed-skill files
matched the candidate; unrelated client entries and extra skill files were
preserved. These are observations about that installation at that time.
They do not install or configure anything for another reader.

## Native CLI acceptance, 2026-10-13

Fresh Codex 0.160.1 app-server reads used a configured native entry, discovered
29 tools and read service/plan state without an inference turn or mutation.
Its direct status did not expose downstream MCP negotiation; no protocol
value is inferred from that status. A later process made five native reads
of the same AGY checkpoint before the AGY continuation.

AGY CLI 1.3.4 with `gemini-3.8-flash-high` and explicit `--effort high`
performed native service qualification and two sequential conversations.
The first accepted A through 12 native MCP calls and exited. The second read
saved state, acquired a fresh owner grant, swept that plan, bound A's exact
accepted result and accepted dependent B through 16 calls. The synthetic
acceptance plan ended complete at revision 10 and ownership version 2.
All 11 public events and both checkpoints were checked. Thirty table digests
for the two original plans matched the baseline; their accepted identities
were preserved. Owned client processes, sidecars and terminals settled.

The AGY launch used `--mode accept-edits` and `--sandbox`; native initialization
exposed `request-review`. Backend effort and native negotiated protocol were
not exposed. Prior permission grants were preserved in that activation.
Those settings are historical observations, not required setup permissions.

## Coverage limits

The official SDK source matrix exercised synthetic clients in auto
`2026-07-28` and legacy `2025-11-25`. Actor labels do not prove a named
installed client or model ran. The native AGY observation does not establish
a fresh 29-operation AGY exercise. The Copilot CLI/VS Code acceptance rows
were not run in the recorded release gate and remain unverified.

The LAN tests use isolated synthetic credentials and a loopback listener with
LAN Host/Origin headers. They do not prove physical cross-host reachability,
actual LAN Compose activation or installed remote-client acceptance.
CLI observations do not establish Desktop/IDE compatibility, environment
inheritance, OS sleep/reboot behavior, or long-running service availability.
Earlier lifecycle checks retain their specific image/client limits.
