# Preserved v1 compatibility fixture

`live_plan_v1.py` is an exact byte-for-byte copy of the frozen pre-v2
`scripts/live_plan.py`, taken from the staged `adaptive-planning-v1-backup`
on 2026-10-03. It is test data imported by the compatibility tests; production
code never imports it. Preserve it unchanged so tests compare against the actual
legacy reader/validator rather than a reimplementation.

SHA-256 of both original and copied fixture, checked using file hashing only:

```text
A81BDED1487AC1BAA901A1D3311EADFF9192B370FB85BDC3BCA32A6ADD35E820
```

The compatibility tests import this fixture to check that the v1 reader rejects
v2 state and that migration preserves valid legacy history. Run the test suite
using the commands in the [repository README](../../README.md#verification).
