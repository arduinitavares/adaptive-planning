from __future__ import annotations

import sqlite3

SCHEMA_VERSION: int = 1

# Identity indexes decode only the strictly shaped v1 attestation. CASE, rather
# than AND evaluation order, keeps historical opaque/malformed text readable.
_RESOURCE_ATTESTATION = "CASE WHEN json_valid(physical_identity) THEN physical_identity ELSE '{}' END"
_DEVICE = f"json_extract({_RESOURCE_ATTESTATION}, '$.device')"
_INODE = f"json_extract({_RESOURCE_ATTESTATION}, '$.inode')"
_EXTERNAL = f"json_extract({_RESOURCE_ATTESTATION}, '$.external_identity')"


def _decimal_identity(expression: str) -> str:
    return (f"length({expression}) BETWEEN 1 AND 1024 AND "
            f"({expression} = '0' OR ({expression} GLOB '[1-9]*' AND {expression} NOT GLOB '*[^0-9]*'))")


_RESOURCE_V1 = f"""
    json_type({_RESOURCE_ATTESTATION}) = 'object'
    AND json_type({_RESOURCE_ATTESTATION}, '$.v') = 'integer'
    AND json_extract({_RESOURCE_ATTESTATION}, '$.v') = 1
    AND json_type({_RESOURCE_ATTESTATION}, '$.case_insensitive') IN ('true', 'false')
    AND ((json_type({_RESOURCE_ATTESTATION}, '$.device') = 'null'
          AND json_type({_RESOURCE_ATTESTATION}, '$.inode') = 'null')
         OR (json_type({_RESOURCE_ATTESTATION}, '$.device') = 'text'
             AND json_type({_RESOURCE_ATTESTATION}, '$.inode') = 'text'
             AND {_decimal_identity(_DEVICE)} AND {_decimal_identity(_INODE)}))
    AND (json_type({_RESOURCE_ATTESTATION}, '$.external_identity') = 'null'
         OR (json_type({_RESOURCE_ATTESTATION}, '$.external_identity') = 'text'
             AND length({_EXTERNAL}) BETWEEN 1 AND 10000 AND instr({_EXTERNAL}, char(0)) = 0))
    AND json_remove({_RESOURCE_ATTESTATION}, '$.v', '$.device', '$.inode',
                    '$.case_insensitive', '$.external_identity') = '{{}}'
"""

SCHEMA_STATEMENTS: tuple[str, ...] = (
    # 1. installation singleton
    """CREATE TABLE IF NOT EXISTS installation (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    epoch TEXT NOT NULL UNIQUE,
    schema_version INTEGER NOT NULL,
    host_id TEXT NOT NULL,
    authority_kind TEXT NOT NULL CHECK (authority_kind IN ('authority', 'backup')),
    dashboard_url TEXT NOT NULL DEFAULT '',
    store_hold INTEGER NOT NULL DEFAULT 0,
    restore_cutoff TEXT,
    created_at TEXT NOT NULL
);""",
    # 2. plans
    """CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    generation TEXT NOT NULL UNIQUE,
    revision INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'draft',
    title TEXT NOT NULL DEFAULT '',
    ownership_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);""",
    # 3. steps
    """CREATE TABLE IF NOT EXISTS steps (
    plan_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'pending',
    definition_version TEXT NOT NULL DEFAULT 'v1',
    payload TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (plan_id, step_id),
    FOREIGN KEY (plan_id) REFERENCES plans (plan_id) ON DELETE CASCADE
);""",
    # 4. attempts
    """CREATE TABLE IF NOT EXISTS attempts (
    plan_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    assignee TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'assigned',
    version INTEGER NOT NULL DEFAULT 1,
    definition_version TEXT NOT NULL DEFAULT 'v1',
    started_at TEXT,
    stopped_at TEXT,
    payload TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (plan_id, attempt_id),
    UNIQUE (plan_id, attempt_id, step_id),
    FOREIGN KEY (plan_id, step_id) REFERENCES steps (plan_id, step_id) ON DELETE CASCADE
);""",
    # 5. capabilities
    """CREATE TABLE IF NOT EXISTS capabilities (
    capability_id TEXT PRIMARY KEY,
    epoch TEXT NOT NULL,
    plan_id TEXT,
    role TEXT NOT NULL,
    secret_hash TEXT,
    valid_until TEXT,
    revoked INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (plan_id) REFERENCES plans (plan_id) ON DELETE CASCADE
);""",
    # 6. resources
    """CREATE TABLE IF NOT EXISTS resources (
    resource_id TEXT PRIMARY KEY,
    host_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('repository', 'workspace', 'external')),
    repository_id TEXT,
    physical_identity TEXT,
    canonical_path TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    CHECK (repository_id IS NULL OR kind = 'workspace'),
    UNIQUE (host_id, resource_id),
    FOREIGN KEY (host_id, repository_id) REFERENCES resources (host_id, resource_id) ON DELETE RESTRICT
);""",
    """CREATE INDEX IF NOT EXISTS idx_resources_host ON resources (host_id);""",
    """CREATE INDEX IF NOT EXISTS idx_resources_host_physical ON resources (host_id, physical_identity);""",
    f"""CREATE UNIQUE INDEX IF NOT EXISTS idx_resources_canonical_physical
ON resources (host_id, {_DEVICE} COLLATE BINARY, {_INODE} COLLATE BINARY)
WHERE status = 'active' AND ({_RESOURCE_V1}) AND json_type({_RESOURCE_ATTESTATION}, '$.device') = 'text';""",
    f"""CREATE UNIQUE INDEX IF NOT EXISTS idx_resources_canonical_external
ON resources (host_id, {_EXTERNAL} COLLATE BINARY)
WHERE status = 'active' AND ({_RESOURCE_V1}) AND json_type({_RESOURCE_ATTESTATION}, '$.external_identity') = 'text';""",
    """CREATE TRIGGER IF NOT EXISTS trg_resources_validate_parent_insert
BEFORE INSERT ON resources
WHEN NEW.repository_id IS NOT NULL
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM resources
            WHERE resource_id = NEW.repository_id
              AND host_id = NEW.host_id
              AND kind = 'repository'
        )
        THEN RAISE(ABORT, 'Parent repository must exist on same host with kind repository')
    END;
END;""",
    """CREATE TRIGGER IF NOT EXISTS trg_resources_validate_parent_update
BEFORE UPDATE OF repository_id, host_id, kind ON resources
WHEN NEW.repository_id IS NOT NULL
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM resources
            WHERE resource_id = NEW.repository_id
              AND host_id = NEW.host_id
              AND kind = 'repository'
        )
        THEN RAISE(ABORT, 'Parent repository must exist on same host with kind repository')
    END;
END;""",
    """CREATE TRIGGER IF NOT EXISTS trg_resources_prevent_parent_kind_change
BEFORE UPDATE OF kind ON resources
WHEN OLD.kind = 'repository' AND NEW.kind != 'repository'
BEGIN
    SELECT CASE
        WHEN EXISTS (SELECT 1 FROM resources WHERE repository_id = OLD.resource_id)
        THEN RAISE(ABORT, 'Cannot change kind of repository with dependent workspace resources')
    END;
END;""",
    # 7. resource_redirects
    """CREATE TABLE IF NOT EXISTS resource_redirects (
    retired_id TEXT PRIMARY KEY,
    survivor_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT '',
    CHECK (retired_id != survivor_id),
    FOREIGN KEY (retired_id) REFERENCES resources (resource_id) ON DELETE CASCADE,
    FOREIGN KEY (survivor_id) REFERENCES resources (resource_id) ON DELETE CASCADE
);""",
    """CREATE TRIGGER IF NOT EXISTS trg_resource_redirects_no_cycle_insert
BEFORE INSERT ON resource_redirects
BEGIN
    SELECT CASE
        WHEN EXISTS (
            WITH RECURSIVE path(node) AS (
                SELECT NEW.survivor_id
                UNION ALL
                SELECT r.survivor_id FROM resource_redirects r JOIN path p ON r.retired_id = p.node
            )
            SELECT 1 FROM path WHERE node = NEW.retired_id
        )
        THEN RAISE(ABORT, 'Cycle detected in resource redirects')
    END;
END;""",
    """CREATE TRIGGER IF NOT EXISTS trg_resource_redirects_no_cycle_update
BEFORE UPDATE ON resource_redirects
BEGIN
    SELECT CASE
        WHEN EXISTS (
            WITH RECURSIVE path(node) AS (
                SELECT NEW.survivor_id
                UNION ALL
                SELECT r.survivor_id FROM resource_redirects r JOIN path p ON r.retired_id = p.node
                WHERE r.retired_id != OLD.retired_id
            )
            SELECT 1 FROM path WHERE node = NEW.retired_id
        )
        THEN RAISE(ABORT, 'Cycle detected in resource redirects')
    END;
END;""",
    # 8. aliases
    """CREATE TABLE IF NOT EXISTS aliases (
    alias_id TEXT PRIMARY KEY,
    host_id TEXT NOT NULL,
    canonical_path TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT '',
    UNIQUE (host_id, canonical_path),
    FOREIGN KEY (host_id, resource_id) REFERENCES resources (host_id, resource_id) ON DELETE CASCADE
);""",
    """CREATE INDEX IF NOT EXISTS idx_aliases_host ON aliases (host_id);""",
    """CREATE INDEX IF NOT EXISTS idx_aliases_host_resource ON aliases (host_id, resource_id);""",
    # 9. absent_host_work
    """CREATE TABLE IF NOT EXISTS absent_host_work (
    absent_work_id TEXT PRIMARY KEY,
    host_id TEXT NOT NULL,
    resource_id TEXT,
    scope TEXT NOT NULL CHECK (scope IN ('repository', 'workspace', 'external')),
    declared_identity TEXT,
    status TEXT NOT NULL DEFAULT 'unresolved',
    created_at TEXT NOT NULL DEFAULT '',
    UNIQUE (absent_work_id, scope),
    FOREIGN KEY (resource_id) REFERENCES resources (resource_id) ON DELETE RESTRICT
);""",
    """CREATE INDEX IF NOT EXISTS idx_absent_host_work_host ON absent_host_work (host_id);""",
    # 10. store_hold_reasons
    """CREATE TABLE IF NOT EXISTS store_hold_reasons (
    reason_id TEXT PRIMARY KEY,
    epoch TEXT NOT NULL,
    kind TEXT NOT NULL,
    details TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT ''
);""",
    # 11. resource_uses
    """CREATE TABLE IF NOT EXISTS resource_uses (
    use_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL,
    plan_id TEXT,
    attempt_id TEXT,
    absent_work_id TEXT,
    scope TEXT NOT NULL CHECK (scope IN ('repository', 'workspace', 'external')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'unresolved', 'released')),
    created_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (resource_id) REFERENCES resources (resource_id) ON DELETE RESTRICT,
    FOREIGN KEY (plan_id, attempt_id) REFERENCES attempts (plan_id, attempt_id) ON DELETE RESTRICT,
    FOREIGN KEY (absent_work_id, scope) REFERENCES absent_host_work (absent_work_id, scope) ON DELETE RESTRICT,
    CHECK (
        (plan_id IS NOT NULL AND attempt_id IS NOT NULL AND absent_work_id IS NULL)
        OR
        (plan_id IS NULL AND attempt_id IS NULL AND absent_work_id IS NOT NULL)
    )
);""",
    """CREATE INDEX IF NOT EXISTS idx_resource_uses_unresolved ON resource_uses (resource_id, status)
WHERE status IN ('active', 'unresolved');""",
    """CREATE INDEX IF NOT EXISTS idx_resource_uses_plan_attempt ON resource_uses (plan_id, attempt_id);""",
    """CREATE INDEX IF NOT EXISTS idx_resource_uses_absent ON resource_uses (absent_work_id);""",
    """CREATE TRIGGER IF NOT EXISTS trg_resource_uses_scope_insert
BEFORE INSERT ON resource_uses
WHEN NEW.absent_work_id IS NOT NULL
BEGIN
    SELECT CASE
        WHEN EXISTS (
            SELECT 1 FROM absent_host_work
            WHERE absent_work_id = NEW.absent_work_id
              AND scope != NEW.scope
        )
        THEN RAISE(ABORT, 'Orphan resource_use scope must match absent_host_work scope')
    END;
END;""",
    """CREATE TRIGGER IF NOT EXISTS trg_resource_uses_scope_update
BEFORE UPDATE OF absent_work_id, scope ON resource_uses
WHEN NEW.absent_work_id IS NOT NULL
BEGIN
    SELECT CASE
        WHEN EXISTS (
            SELECT 1 FROM absent_host_work
            WHERE absent_work_id = NEW.absent_work_id
              AND scope != NEW.scope
        )
        THEN RAISE(ABORT, 'Orphan resource_use scope must match absent_host_work scope')
    END;
END;""",
    # 12. availability
    """CREATE TABLE IF NOT EXISTS availability (
    plan_id TEXT NOT NULL,
    prerequisite TEXT NOT NULL,
    result_version TEXT NOT NULL,
    method TEXT NOT NULL,
    checked_base TEXT NOT NULL,
    artifact_digest TEXT,
    evidence TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (plan_id, prerequisite, result_version),
    FOREIGN KEY (plan_id, result_version, prerequisite) REFERENCES result_versions (plan_id, result_version, step_id) ON DELETE CASCADE
);""",
    # 13. checkpoints
    """CREATE TABLE IF NOT EXISTS checkpoints (
    plan_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    sequence INTEGER NOT NULL DEFAULT 1,
    outcome TEXT NOT NULL,
    check_name TEXT NOT NULL,
    evidence TEXT NOT NULL,
    origin_worker TEXT NOT NULL,
    relay TEXT,
    artifact TEXT,
    reassessment TEXT,
    created_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (plan_id, attempt_id, sequence),
    FOREIGN KEY (plan_id, attempt_id) REFERENCES attempts (plan_id, attempt_id) ON DELETE CASCADE
);""",
    # 14. holds
    """CREATE TABLE IF NOT EXISTS holds (
    hold_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (plan_id) REFERENCES plans (plan_id) ON DELETE CASCADE
);""",
    # 15. reviews
    """CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    verdict TEXT NOT NULL,
    findings TEXT NOT NULL,
    evidence TEXT NOT NULL,
    artifact TEXT NOT NULL,
    verification_use_id TEXT,
    created_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (plan_id, attempt_id, step_id) REFERENCES attempts (plan_id, attempt_id, step_id) ON DELETE CASCADE
);""",
    # 16. result_versions
    """CREATE TABLE IF NOT EXISTS result_versions (
    plan_id TEXT NOT NULL,
    result_version TEXT NOT NULL,
    step_id TEXT NOT NULL,
    artifact_kind TEXT NOT NULL DEFAULT 'code',
    identity TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    digest TEXT,
    created_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (plan_id, result_version),
    UNIQUE (plan_id, result_version, step_id),
    FOREIGN KEY (plan_id, step_id) REFERENCES steps (plan_id, step_id) ON DELETE CASCADE
);""",
    # 17. events
    """CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT,
    plan_revision INTEGER,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    origin_worker TEXT,
    relay TEXT,
    published_at TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (plan_id) REFERENCES plans (plan_id) ON DELETE SET NULL
);""",
    """CREATE INDEX IF NOT EXISTS idx_events_plan ON events (plan_id);""",
    # 18. requests
    """CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY,
    epoch TEXT NOT NULL,
    key TEXT NOT NULL,
    command_digest TEXT NOT NULL CHECK (length(command_digest) = 64),
    plan_id TEXT,
    response_payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT '',
    UNIQUE (epoch, key),
    FOREIGN KEY (plan_id) REFERENCES plans (plan_id) ON DELETE SET NULL
);""",
    # 19. creation_ids
    """CREATE TABLE IF NOT EXISTS creation_ids (
    epoch TEXT NOT NULL,
    creation_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (epoch, creation_id),
    FOREIGN KEY (plan_id) REFERENCES plans (plan_id) ON DELETE CASCADE
);""",
    # 20. imports
    """CREATE TABLE IF NOT EXISTS imports (
    import_id TEXT PRIMARY KEY,
    host_id TEXT NOT NULL,
    source_directory_id TEXT NOT NULL,
    original_task_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT '',
    UNIQUE (host_id, source_directory_id, original_task_id),
    FOREIGN KEY (plan_id) REFERENCES plans (plan_id) ON DELETE CASCADE
);""",
)

SCHEMA_DDL: str = "\n\n".join(SCHEMA_STATEMENTS)


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Initialize all normative database tables, constraints, triggers, and indexes without breaking transactions."""
    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)
