"""Remote-readable receipt instructions, independent of a client's filesystem."""


def contract():
    return {
        "schema": 1,
        "envelope": ["source_record_id", "epoch", "plan_id", "kind", "observed_at", "step_id", "attempt_id", "checkpoint_sequence", "data"],
        "authentication": "Supply the current orchestrator_token for plan observations or the bound attempt_token for worker observations. Without a domain capability only context is allowed. Tokens are private inputs, never evidence or public receipt data.",
        "idempotency": "Retain the immutable body. source_record_id is unique per epoch (max 160 characters): use client/session/native-event ID plus receipt kind and scope, or a hash of immutable content. Exact retry replays; different content under the same ID conflicts. Never mint a new ID for a retry or resubmit cumulative usage as deltas.",
        "scope": "observed_at is timezone-aware ISO 8601. step_id, attempt_id, checkpoint_sequence may be null. Workers require exact step/attempt scope. Checkpoint attribution requires its attempt. Coverage and case classification are plan scoped.",
        "kinds": {
            "context": {"optional": ["workflow_version", "skill_version", "model_id", "provider", "effort", "client_name", "client_version", "client_os", "collector_version", "case_kind", "evidence"],
                        "rules": "At create/resume/settings change report only settings actually exposed. case_kind is operational, fixture or unknown. Keep a stable context ID for unchanged context; omit unknown model/usage, never guess."},
            "defect": {"required": ["defect_id", "stage", "status", "cause", "evidence"],
                       "rules": "stage caught|escaped, status open|resolved. Stable defect_id links status updates. Cause and stage are immutable. A failed exploration check alone is not a regression."},
            "effort": {"required": ["activity_kind", "corrective_rework", "source", "evidence"],
                       "optional": ["cause_id", "metrics", "started_at", "ended_at", "time_kind", "billing_amount", "estimated_amount", "currency", "billing_scope"],
                       "rules": "activity_kind plan|implement|verify|review|debug|recover; source provider_receipt|client_measured|estimate. corrective_rework is true, false or null (unknown); true requires cause_id referencing a recorded defect in this plan. Unknown classification cannot establish zero or complete rework coverage. Send incremental observed usage once, never a cumulative session total more than once. All interval fields occur together; time_kind active|wait|blocked. Actual billing requires provider_receipt, explicit three-letter currency and isolated|shared billing_scope; shared usage is not attributable task cost."},
            "outcome": {"required": ["status", "evidence"], "optional": ["independent", "verifier", "result_version"],
                        "rules": "status verified|failed|blocked|unresolved. Verified requires orchestrator, independent=true, distinct verifier, exact current accepted attempt, passed checkpoint and result_version. Canonical acceptance and linked reviews are already measured automatically; do not duplicate them to populate lifecycle counts."},
            "coverage": {"required": ["metrics", "evidence"],
                         "rules": "Plan owner only, after reconciliation of all relevant deltas. metrics is a list of metric names or regressions. Missing is unknown, partial sums are lower bounds. Zero needs explicit complete coverage; do not mark coverage complete just because a session ended."},
        },
        "metrics": ["input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "tool_calls", "worker_seconds", "human_seconds", "reviewer_seconds"],
        "limits": "Token/call counts are nonnegative integers; cached_input_tokens is a subset of input_tokens and reasoning_tokens a subset of output_tokens. Do not sum subsets twice. Time metrics use seconds. No credentials, prompts, source code or unrelated conversations in evidence; use a concise nonsecret reference.",
        "context_example": {"source_record_id": "<session>:<plan>:context:<content-hash>", "epoch": "<read_service_info epoch>", "plan_id": "<bound plan>", "kind": "context", "observed_at": "<original observation timestamp>", "step_id": None, "attempt_id": None, "checkpoint_sequence": None,
                            "data": {"skill_version": "sha256:<hash of loaded SKILL.md>", "workflow_version": "adaptive-planning/receipts-v1", "case_kind": "operational"}},
    }
