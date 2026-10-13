"""Host-attested resource identity, durable reconciliation and conservative overlap.

Paths are identity evidence, never paths to open. Registration changes identity
only; neither registration nor reconciliation alters an execution use.
"""
from __future__ import annotations

import json
import hashlib
import sqlite3
import uuid
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from adaptive_planning import plans
from adaptive_planning.core import MAX_FILE_BYTES, MAX_TEXT_LEN
from adaptive_planning.ownership import require_kind_capabilities, require_orchestrator
from adaptive_planning.types import (
    Command, DomainError, ExecutionContext, MutationReceipt, ReservationView, ResourceClaim, ResourceId,
    _to_execution_context,
)

_FIELDS = frozenset({"v", "device", "inode", "case_insensitive", "external_identity"})


@dataclass(frozen=True)
class _Attestation:
    device: str | None
    inode: str | None
    case_insensitive: bool
    external_identity: str | None

    @property
    def physical(self) -> tuple[str, str] | None:
        return (self.device, self.inode) if self.device is not None else None

    def encode(self) -> str:
        return json.dumps({"v": 1, "device": self.device, "inode": self.inode,
                           "case_insensitive": self.case_insensitive, "external_identity": self.external_identity},
                          sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _decimal(value: object) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= 1024 and value.isascii() and value.isdecimal()
            and (value == "0" or not value.startswith("0")))


def _canonical_path(path: object) -> bool:
    return (isinstance(path, str) and bool(path) and len(path) <= MAX_TEXT_LEN and "\0" not in path
            and PurePosixPath(path).is_absolute() and not path.startswith("//")
            and str(PurePosixPath(path)) == path and ".." not in path.split("/"))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate attestation field")
        result[key] = value
    return result


def _decode(raw: str | None) -> _Attestation | None:
    """Unknown/legacy evidence is never positively decoded or overwritten by reads."""
    if raw is None:
        return None
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(value, dict) or set(value) != _FIELDS or type(value["v"]) is not int or value["v"] != 1:
        return None
    device, inode, case, external = (value[k] for k in ("device", "inode", "case_insensitive", "external_identity"))
    if type(case) is not bool:
        return None
    if (device is not None or inode is not None) and not (_decimal(device) and _decimal(inode)):
        return None
    if external is not None and (not isinstance(external, str) or not external or "\0" in external
                                 or len(external) > MAX_TEXT_LEN):
        return None
    return _Attestation(device, inode, case, external)


@dataclass(frozen=True)
class _Resource:
    resource_id: str
    host_id: str
    kind: str
    repository_id: str | None
    raw: str | None
    canonical_path: str | None
    status: str


def _resolve_identity(rid: str, host: str, lookup, roots: dict[str, str]) -> ResourceId:
    """Interpret registered redirect metadata without deciding physical identity."""
    if rid in roots:
        return roots[rid]
    origin = lookup(rid)
    if origin is None:
        raise DomainError("not_found", "The resource is not registered")
    if origin[0] != host:
        raise DomainError("invalid", "The resource belongs to a different host")
    visited = set()
    current = rid
    while True:
        row = lookup(current)
        if current in visited or row is None or row[0] != host or row[1] != origin[1]:
            raise DomainError("held", "Resource redirects are not consistent")
        visited.add(current)
        status, target = row[2:]
        if ((status == "active" and target is not None)
                or (status == "retired" and target is None) or status not in ("active", "retired")):
            raise DomainError("held", "Resource canonical status is not consistent")
        if target is None:
            for item in visited:
                roots[item] = current
            return current
        current = target


def resolve_resource_id(connection: sqlite3.Connection, resource_id: ResourceId) -> ResourceId:
    """Resolve a current public reference; preserve unsupported legacy evidence."""
    host = connection.execute("SELECT host_id FROM installation WHERE id=1").fetchone()[0]
    def lookup(rid):
        return connection.execute(
            "SELECT r.host_id, r.kind, r.status, d.survivor_id FROM resources r "
            "LEFT JOIN resource_redirects d ON d.retired_id = r.resource_id WHERE r.resource_id = ?", (rid,)).fetchone()
    return _resolve_identity(resource_id, host, lookup, {})


class _Registry:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.host = db.execute("SELECT host_id FROM installation WHERE id=1").fetchone()[0]
        self.rows = {row[0]: _Resource(*row) for row in db.execute(
            "SELECT resource_id, host_id, kind, repository_id, physical_identity, canonical_path, status FROM resources")}
        self.redirects = dict(db.execute("SELECT retired_id, survivor_id FROM resource_redirects"))
        self.attestations = {rid: _decode(row.raw) for rid, row in self.rows.items()}
        self.roots: dict[str, str] = {}
        self.aliases = tuple(db.execute("SELECT canonical_path, resource_id FROM aliases WHERE host_id=?", (self.host,)))
        for _, rid in self.aliases:
            if rid not in self.rows or self.rows[rid].host_id != self.host:
                raise DomainError("held", "Resource aliases are not consistent with the host namespace")
        for rid, row in self.rows.items():
            if row.host_id == self.host:
                self.resolve(rid)
        grouped = {}
        for rid, root in self.roots.items():
            grouped.setdefault(root, []).append(self.rows[rid])
        self.groups = {root: tuple(rows) for root, rows in grouped.items()}
        # Validate complete inbound histories and repository FKs, not only the
        # starting ID. Contradictory redirects must not hide an unresolved use.
        for root in set(self.roots.values()):
            members = self.members(root)
            known = [self.attestations[row.resource_id] for row in members if self.attestations[row.resource_id] is not None]
            if (len({a.physical for a in known if a.physical is not None}) > 1
                    or len({a.external_identity for a in known if a.external_identity is not None}) > 1
                    or len({a.case_insensitive for a in known}) > 1):
                raise DomainError("held", "Resource identity history is contradictory")
            parents = {self.parent(row.resource_id) for row in members}
            if len(parents) > 1:
                raise DomainError("held", "Resource repository history is contradictory")

    def resolve(self, rid: str) -> str:
        def lookup(current):
            row = self.rows.get(current)
            return None if row is None else (row.host_id, row.kind, row.status, self.redirects.get(current))
        return _resolve_identity(rid, self.host, lookup, self.roots)

    def members(self, root: str) -> tuple[_Resource, ...]:
        return self.groups[root]

    def known(self, rid: str) -> bool:
        row, attestation = self.rows[rid], self.attestations[rid]
        if attestation is None:
            return False
        if ((row.canonical_path is not None and not _canonical_path(row.canonical_path))
                or any(target == rid and not _canonical_path(path) for path, target in self.aliases)):
            return False
        if row.kind == "external":
            return attestation.external_identity is not None
        return (attestation.external_identity is None and (attestation.physical is not None
                or _canonical_path(row.canonical_path)
                or any(target == rid and _canonical_path(path) for path, target in self.aliases)))

    def supported_history(self, root: str) -> bool:
        if not self.known(root):
            return False
        # Explicit reconciliation can attach absent SQL-NULL evidence to a
        # canonical representative. Its retired bytes stay NULL; the committed
        # canonical attestation and durable redirect supply the accepted evidence.
        return all(self.known(row.resource_id) or (row.resource_id != root and row.raw is None)
                   for row in self.members(root))

    def parent(self, rid: str) -> str | None:
        row = self.rows[rid]
        if row.repository_id is None:
            return None
        try:
            parent = self.resolve(row.repository_id)
        except DomainError:
            raise DomainError("held", "The resource repository identity is not consistent") from None
        if row.kind != "workspace" or self.rows[parent].kind != "repository":
            raise DomainError("held", "The resource repository relationship is not consistent")
        return parent

    def paths(self, root: str) -> tuple[tuple[str, bool | None], ...]:
        paths = []
        for row in self.members(root):
            attestation = self.attestations[row.resource_id]
            policy = attestation.case_insensitive if attestation else None
            if policy is None and row.raw is None and self.supported_history(root):
                policy = self.attestations[root].case_insensitive
            if row.canonical_path:
                paths.append((row.canonical_path, policy))
            paths.extend((path, policy) for path, rid in self.aliases if rid == row.resource_id)
        return tuple(paths)

    def matches(self, claim: ResourceClaim, attestation: _Attestation) -> tuple[set[str], set[str], set[str]]:
        aliases, keys, unknown = set(), set(), set()
        for rid, row in self.rows.items():
            if row.host_id != self.host:
                continue
            root = self.resolve(rid)
            old = self.attestations[rid]
            if not self.supported_history(root):
                # A legacy physical format is not established. It may already
                # represent an incoming key; only explicit reconciliation can
                # positively attach missing information.
                if attestation.physical is not None or attestation.external_identity is not None:
                    unknown.add(root)
            if old is not None and ((attestation.physical is not None and attestation.physical == old.physical)
                  or (attestation.external_identity is not None and attestation.external_identity == old.external_identity)):
                keys.add(root)
            if claim.canonical_path is not None:
                for path, policy in self.paths(root):
                    folded = claim.case_insensitive or policy is not False
                    equal = path.casefold() == claim.canonical_path.casefold() if folded else path == claim.canonical_path
                    if equal:
                        aliases.add(root)
                        if not self.supported_history(root):
                            unknown.add(root)
        return aliases, keys, unknown

    def compatible(self, root: str, claim: ResourceClaim, attestation: _Attestation, *, reconcile=False) -> None:
        for row in self.members(root):
            if row.kind != claim.kind:
                raise DomainError("resource_conflict", "The resource kind contradicts an existing identity")
            if self.parent(row.resource_id) != claim.repository_id:
                raise DomainError("resource_conflict", "The resource repository relationship contradicts the claim")
            old = self.attestations[row.resource_id]
            if old is None:
                if row.raw is not None or (not reconcile and not self.supported_history(root)):
                    raise DomainError("held", "Resource identity evidence requires explicit supported reconciliation")
                continue
            if (old.case_insensitive != attestation.case_insensitive
                    or (old.physical is not None and attestation.physical is not None and old.physical != attestation.physical)
                    or (old.external_identity is not None and attestation.external_identity is not None
                        and old.external_identity != attestation.external_identity)):
                raise DomainError("resource_conflict", "The resource claim contradicts registered identity evidence")


def _claim(registry: _Registry, claim: ResourceClaim) -> tuple[ResourceClaim, _Attestation]:
    if not isinstance(claim, ResourceClaim) or claim.host_id != registry.host:
        raise DomainError("invalid", "A claim must attest the current installation host")
    if claim.kind not in ("repository", "workspace", "external") or type(claim.case_insensitive) is not bool:
        raise DomainError("invalid", "The resource claim is not valid")
    if (claim.device is None) != (claim.inode is None):
        raise DomainError("invalid", "Physical identity requires both device and inode")
    values = []
    for value in (claim.device, claim.inode):
        if value is None:
            values.append(None)
            continue
        if type(value) is not int or value < 0:
            raise DomainError("invalid", "Physical identity requires nonnegative integers")
        try:
            decimal = str(value)
        except ValueError:
            raise DomainError("invalid", "Physical identity exceeds the supported precision") from None
        if len(decimal) > 1024:
            raise DomainError("invalid", "Physical identity exceeds the supported precision")
        values.append(decimal)
    path = claim.canonical_path
    if path is not None and not _canonical_path(path):
        raise DomainError("invalid", "A canonical host path must be absolute and unambiguous")
    external = claim.external_identity
    if external is not None and (not isinstance(external, str) or not external or "\0" in external
                                 or len(external) > MAX_TEXT_LEN):
        raise DomainError("invalid", "External identity must be a nonempty bounded string")
    if claim.kind == "external":
        if external is None or claim.repository_id is not None:
            raise DomainError("invalid", "An external claim requires its own external identity")
    elif external is not None or (path is None and values[0] is None):
        raise DomainError("invalid", "A filesystem claim requires host-attested path or physical identity")
    parent = claim.repository_id
    if parent is not None:
        if claim.kind != "workspace" or not isinstance(parent, str):
            raise DomainError("invalid", "Only a workspace may refer to a repository")
        parent = registry.resolve(parent)
        if registry.rows[parent].kind != "repository":
            raise DomainError("invalid", "A workspace parent must be a repository")
        if not registry.supported_history(parent):
            raise DomainError("held", "Repository identity requires reconciliation")
    return replace(claim, repository_id=parent), _Attestation(values[0], values[1], claim.case_insensitive, external)


def _add_alias(db: sqlite3.Connection, host: str, path: str | None, rid: str) -> None:
    if path is not None:
        existing = db.execute("SELECT resource_id FROM aliases WHERE host_id=? AND canonical_path=?", (host, path)).fetchone()
        # Existing targets remain immutable, including retired foreign keys.
        if existing is None:
            db.execute("INSERT INTO aliases (alias_id, host_id, canonical_path, resource_id, created_at) VALUES (?, ?, ?, ?, ?)",
                       (str(uuid.uuid4()), host, path, rid, plans._timestamp()))


def register_claim(connection: sqlite3.Connection, claim: ResourceClaim) -> ResourceId:
    """Private import-bootstrap primitive: identity only, no capabilities or uses."""
    registry = _Registry(connection)
    claim, attestation = _claim(registry, claim)
    aliases, keys, unknown = registry.matches(claim, attestation)
    if unknown:
        raise DomainError("held", "Unknown historical identity requires reconciliation")
    candidates = aliases | keys
    if len(candidates) > 1:
        raise DomainError("resource_conflict", "Physical and alias claims require explicit reconciliation")
    if candidates:
        rid = next(iter(candidates))
        registry.compatible(rid, claim, attestation)
        old = registry.attestations[rid]
        if old.physical is None and attestation.physical is not None:
            raise DomainError("resource_conflict", "Attaching a physical identity requires explicit reconciliation")
    else:
        rid = str(uuid.uuid4())
        connection.execute("INSERT INTO resources (resource_id, host_id, kind, repository_id, physical_identity, "
                           "canonical_path, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
                           (rid, registry.host, claim.kind, claim.repository_id, attestation.encode(),
                            claim.canonical_path, plans._timestamp()))
    _add_alias(connection, registry.host, claim.canonical_path, rid)
    _Registry(connection)
    return rid


def _begin(connection: sqlite3.Connection, command: Command, kind: str):
    if command.kind != kind:
        raise DomainError("invalid", "The command kind does not match the resource operation")
    if command.epoch != connection.execute("SELECT epoch FROM installation WHERE id=1").fetchone()[0]:
        raise DomainError("stale_epoch", "The installation epoch has changed")
    require_kind_capabilities(command.kind, None, command.orchestrator_token, command.attempt_token)
    require_orchestrator(connection, command)
    plan = plans._load_plan(connection, command.plan_id)
    plans._check_preconditions(plan, command)
    return plan, plans.project_plan(connection, plan.plan_id)


def _publish(connection: sqlite3.Connection, command: Command, plan, previous, committed) -> MutationReceipt:
    revision, ts = plan.revision + 1, plans._timestamp(plan.updated_at)
    connection.execute("UPDATE plans SET revision=?, updated_at=? WHERE plan_id=?", (revision, ts, plan.plan_id))
    # Claim paths/physical keys and domain grants never enter public events.
    plans._insert_event(connection, plan.plan_id, revision, command.kind, command.actor, ts, committed)
    view = plans._commit_projection(connection, plan.plan_id, previous)
    return MutationReceipt(committed, view.metadata, view.metadata, False, None)


def register_resource(connection: sqlite3.Connection, command: Command) -> MutationReceipt:
    plan, previous = _begin(connection, command, "register_resource")
    rid = register_claim(connection, command.payload["claim"])
    return _publish(connection, command, plan, previous, {"resource_id": rid})


def reconcile_resources(connection: sqlite3.Connection, command: Command) -> MutationReceipt:
    plan, previous = _begin(connection, command, "reconcile_resources")
    registry = _Registry(connection)
    payload = command.payload
    ids, reason = payload["resource_ids"], payload["reason"]
    if not ids or len(set(ids)) != len(ids) or not reason.strip() or len(reason) > MAX_TEXT_LEN:
        raise DomainError("invalid", "Reconciliation requires distinct resources and an explicit bounded reason")
    claim, attestation = _claim(registry, payload["canonical_claim"])
    if attestation.physical is None and attestation.external_identity is None:
        raise DomainError("invalid", "Reconciliation requires explicit same-physical identity evidence")
    roots = {registry.resolve(rid) for rid in ids}
    aliases, keys, unknown = registry.matches(claim, attestation)
    if (aliases | keys) - roots:
        raise DomainError("resource_conflict", "Reconciliation omits a competing canonical identity")
    if unknown - roots:
        raise DomainError("held", "Unlisted historical identity evidence remains unknown")
    for rid in roots:
        registry.compatible(rid, claim, attestation, reconcile=True)
    if len(aliases) > 1 or (not aliases and len(keys) > 1):
        raise DomainError("resource_conflict", "The canonical claim has ambiguous matches")
    survivor = next(iter(aliases or keys)) if aliases or keys else min(roots)
    previous_attestation = registry.attestations[survivor]
    accepted = attestation if previous_attestation is None else _Attestation(
        previous_attestation.device or attestation.device, previous_attestation.inode or attestation.inode,
        previous_attestation.case_insensitive, previous_attestation.external_identity or attestation.external_identity)
    ts = plans._timestamp()
    for rid in sorted(roots - {survivor}):
        connection.execute("UPDATE resources SET status='retired' WHERE resource_id=?", (rid,))
        connection.execute("INSERT INTO resource_redirects (retired_id, survivor_id, created_at) VALUES (?, ?, ?)",
                           (rid, survivor, ts))
    # Losers leave the active indexes first; their original keys and FKs remain.
    connection.execute("UPDATE resources SET physical_identity=? WHERE resource_id=?", (accepted.encode(), survivor))
    _add_alias(connection, registry.host, claim.canonical_path, survivor)
    _Registry(connection)
    committed = {"resource_id": survivor, "retired_resource_ids": sorted(roots - {survivor}),
                 "identity_was_attested": previous_attestation is not None,
                 "physical_key_was_known": previous_attestation is not None and previous_attestation.physical is not None,
                 "external_key_was_known": previous_attestation is not None and previous_attestation.external_identity is not None}
    return _publish(connection, command, plan, previous, committed)


def resolve_context(connection: sqlite3.Connection, context: ExecutionContext) -> tuple[ResourceId, ...]:
    registry = _Registry(connection)
    if (not isinstance(context, ExecutionContext) or type(context.writes) is not bool
            or type(context.repository_exclusive) is not bool
            or context.kind not in ("repository", "non-repository", "external-only", "read-only")):
        raise DomainError("invalid", "The execution context is not valid")
    def resource(rid, kind):
        if not isinstance(rid, str) or not rid:
            raise DomainError("invalid", "The execution context requires registered identity")
        root = registry.resolve(rid)
        if registry.rows[root].kind != kind:
            raise DomainError("invalid", "The context resource has the wrong kind")
        if not registry.supported_history(root):
            raise DomainError("held", "The context identity requires reconciliation")
        return root
    repository = resource(context.repository_id, "repository") if context.repository_id is not None else None
    workspace = resource(context.workspace_id, "workspace") if context.workspace_id is not None else None
    external = tuple(resource(rid, "external") for rid in context.external_ids)
    if workspace is not None and registry.parent(workspace) != repository:
        raise DomainError("invalid", "The workspace does not match the asserted repository context")
    if context.kind == "read-only":
        if context.writes or context.repository_exclusive:
            raise DomainError("invalid", "Read-only context cannot assert writable use")
    elif context.kind == "repository":
        if repository is None:
            raise DomainError("invalid", "Repository identity is required")
    elif context.kind == "non-repository":
        if repository is not None or workspace is None or context.repository_exclusive:
            raise DomainError("invalid", "A known non-repository workspace is required")
    elif repository is not None or workspace is not None or not external or context.repository_exclusive:
        raise DomainError("invalid", "An external-only context requires external identities")
    selected = repository if context.kind == "repository" and (workspace is None or context.repository_exclusive) else workspace
    return tuple(dict.fromkeys((*((selected,) if selected else ()), *external)))


def _path_overlap(left: tuple[str, bool | None], right: tuple[str, bool | None]) -> bool:
    a, acase = left
    b, bcase = right
    if acase is not False or bcase is not False:
        a, b = a.casefold(), b.casefold()
    a, b = str(PurePosixPath(a)), str(PurePosixPath(b))
    return a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")


def _resource_overlap(registry, left, right, repository_exclusive, scope):
    """The same identity predicate serves active conflicts and retained handoff proof."""
    if left == right:
        return True
    if not registry.supported_history(left) or not registry.supported_history(right):
        return True
    a, b = registry.rows[left], registry.rows[right]
    ar = left if a.kind == "repository" else registry.parent(left)
    br = right if b.kind == "repository" else registry.parent(right)
    if ar is not None and ar == br and (a.kind == "repository" or b.kind == "repository"
                                       or repository_exclusive or scope == "repository"):
        return True
    apaths, bpaths = registry.paths(left), registry.paths(right)
    if any(_path_overlap(a, b) for a in apaths for b in bpaths):
        return True
    # Opaque external keys are distinct from filesystem identity. Unknown
    # filesystem location cannot establish isolation from another writer.
    if a.kind != "external" and b.kind != "external" and (not apaths or not bpaths):
        return True
    return False


def conflicting_uses(connection: sqlite3.Connection, resource_ids: tuple[ResourceId, ...],
                     repository_exclusive: bool) -> tuple[str, ...]:
    registry = _Registry(connection)
    requested = tuple(dict.fromkeys(registry.resolve(rid) for rid in resource_ids))
    conflicts = []
    for use_id, rid, scope in connection.execute(
            "SELECT use_id, resource_id, scope FROM resource_uses WHERE status IN ('active', 'unresolved') ORDER BY use_id"):
        canonical = registry.resolve(rid)
        if any(_resource_overlap(registry, request, canonical, repository_exclusive, scope) for request in requested):
            conflicts.append(use_id)
    return tuple(conflicts)


# Execution roles are independent of resource identity and physical obligations.
_USE_NAMESPACE = "execution_uses"


def canonical_context(connection, execution):
    resolve_context(connection, execution)
    return replace(execution,
                   repository_id=None if execution.repository_id is None else resolve_resource_id(connection, execution.repository_id),
                   workspace_id=None if execution.workspace_id is None else resolve_resource_id(connection, execution.workspace_id),
                   external_ids=tuple(dict.fromkeys(resolve_resource_id(connection, rid) for rid in execution.external_ids)))


def _owned_uses(connection, plan_id, attempt_id):
    return {row[0]: row for row in connection.execute(
        "SELECT use_id,resource_id,plan_id,attempt_id,absent_work_id,scope,status,created_at,rowid "
        "FROM resource_uses WHERE plan_id=? AND attempt_id=? ORDER BY rowid", (plan_id, attempt_id))}


def _member(row):
    return {"use_id": row[0], "resource_id": row[1], "scope": row[5], "created_at": row[7]}


def _source(plan_id, attempt_id, step_id, assignee, definition_version, data):
    return {"plan_id": plan_id, "attempt_id": attempt_id, "step_id": step_id, "assignee": assignee,
            "definition_version": definition_version, "definition": data.get("definition"),
            "origin_epoch": data.get("epoch"), "origin_generation": data.get("generation")}


def _json_object(raw, label):
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, TypeError, RecursionError):
        raise DomainError("invalid", f"The {label} is malformed") from None
    if not isinstance(value, dict):
        raise DomainError("invalid", f"The {label} is malformed")
    return value


def _members(connection, declared, owned):
    if not isinstance(declared, list):
        raise DomainError("invalid", "Execution-use members must be a complete list")
    ids = []
    for item in declared:
        if (not isinstance(item, dict) or set(item) != {"use_id", "resource_id", "scope", "created_at"}
                or not isinstance(item.get("use_id"), str) or item["use_id"] not in owned
                or item != _member(owned[item["use_id"]]) or item["use_id"] in ids):
            raise DomainError("invalid", "Execution-use membership contradicts retained physical rows")
        row = owned[item["use_id"]]
        canonical = resolve_resource_id(connection, row[1])
        kind = connection.execute("SELECT kind FROM resources WHERE resource_id=?", (canonical,)).fetchone()[0]
        if row[4] is not None or row[5] != kind:
            raise DomainError("invalid", "Execution use has contradictory ownership or resource scope")
        ids.append(item["use_id"])
    return ids


def _original_coverage(connection, execution, members, owned):
    expected = set(resolve_context(connection, execution)) if execution.writes else set()
    covered = {resolve_resource_id(connection, owned[item["use_id"]][1]) for item in members}
    if covered != expected:
        raise DomainError("invalid", "The original implementation cohort contradicts its frozen context")


def _writer_proof(connection, source, writer, role, anchor, ids):
    if writer is None:  # Coherently proved pre-namespace implementation history has unknown writer liveness.
        if role != "implementation":
            raise DomainError("invalid", "Verifier writer provenance is missing")
        return
    if (not isinstance(writer, dict) or set(writer) != {"state", "epoch", "generation", "sequence", "reason", "ownership_version", "actor"}
            or writer["state"] not in ("active", "stopped") or type(writer["sequence"]) is not int
            or type(writer["ownership_version"]) is not int or writer["ownership_version"] < 1
            or writer["sequence"] < 1 or any(not isinstance(writer[k], str) or not writer[k].strip()
                                            for k in ("epoch", "generation", "reason", "actor"))):
        raise DomainError("invalid", "Execution writer provenance is malformed")
    row = connection.execute("SELECT kind,payload,actor FROM events WHERE plan_id=? AND sequence=?",
                             (source["plan_id"], writer["sequence"])).fetchone()
    event = None if row is None else _json_object(row[1], "writer audit event")
    transition = {"role": role, "anchor": anchor, "use_ids": ids,
                  **{key: writer[key] for key in ("state", "epoch", "generation", "reason", "ownership_version", "actor")}}
    allowed = ("assign_step", "reopen_attempt") if writer["state"] == "active" and role == "implementation" else (
        ("reserve_verification",) if writer["state"] == "active" else ("reserve_verification", "release_resource_use"))
    if (row is None or row[0] not in allowed or row[2] != writer["actor"] or event.get("attempt_id") != source["attempt_id"]
            or event.get("step_id") != source["step_id"] or event.get("definition") != source["definition"]
            or not isinstance(event.get("execution_transitions"), list)
            or event["execution_transitions"].count(transition) != 1):
        raise DomainError("invalid", "Execution writer transition lacks exact original audit proof")


def _group_proof(connection, source, anchor, group, owned, original_ids):
    fields = {"anchor", "members", "verifier", "context", "artifact", "cycle", "definition", "source_use_ids", "creation", "writer"}
    if (not isinstance(group, dict) or set(group) != fields or group["anchor"] != anchor
            or not isinstance(anchor, str) or not isinstance(group["verifier"], str) or not group["verifier"].strip()
            or group["verifier"] == source["assignee"] or group["definition"] != source["definition"]):
        raise DomainError("invalid", "Verifier group binding is malformed")
    ids = _members(connection, group["members"], owned)
    if not ids or anchor not in ids or len({owned[item][6] for item in ids}) != 1:
        raise DomainError("invalid", "Verifier group has missing or inconsistent physical members")
    execution = _to_execution_context(group["context"])
    resources = resolve_context(connection, execution)
    if not execution.writes or set(resources) != {resolve_resource_id(connection, owned[item][1]) for item in ids}:
        raise DomainError("invalid", "Verifier members contradict their immutable execution context")
    sources = group["source_use_ids"]
    if (not isinstance(sources, list) or any(not isinstance(item, str) for item in sources) or len(sources) != len(set(sources))
            or (sources and sources != original_ids)):
        raise DomainError("invalid", "Verifier handoff does not identify the exact implementation cohort")
    creation = group["creation"]
    if (not isinstance(creation, dict) or set(creation) != {"sequence", "epoch", "generation", "ownership_version", "actor", "published_at"}
            or type(creation["sequence"]) is not int or creation["sequence"] < 1
            or type(creation["ownership_version"]) is not int or creation["ownership_version"] < 1
            or any(not isinstance(creation[key], str) or not creation[key].strip()
                   for key in ("epoch", "generation", "actor", "published_at"))):
        raise DomainError("invalid", "Verifier creation authority is malformed")
    first = None
    for sequence, kind, actor, published, raw in connection.execute(
            "SELECT sequence,kind,actor,published_at,payload FROM events WHERE plan_id=? ORDER BY sequence", (source["plan_id"],)):
        event = _json_object(raw, "verifier audit event")
        if event.get("use_id") == anchor:
            first = (sequence, kind, actor, published, event)
            break
    immutable = {key: value for key, value in group.items() if key != "writer"}
    if (first is None or first[:4] != (creation["sequence"], "reserve_verification", creation["actor"], creation["published_at"])
            or first[4].get("verification_group") != immutable
            or first[4].get("attempt_id") != source["attempt_id"]
            or first[4].get("step_id") != source["step_id"] or first[4].get("definition") != source["definition"]):
        raise DomainError("invalid", "Verifier group lacks coherent original creation proof")
    from adaptive_planning.reviews import _validate_review_cycle
    _validate_review_cycle(connection, source, group["cycle"], group["artifact"], creation["sequence"])
    original = first[4]
    required = {"artifact": group["artifact"], "cycle": group["cycle"], "verification_group": immutable,
                "attempt_id": source["attempt_id"], "step_id": source["step_id"], "definition": source["definition"],
                "definition_version": source["definition_version"], "verifier": group["verifier"]}
    optional = {"plan_id": source["plan_id"], "producer_step_id": source["step_id"],
                "result_version": group["cycle"].get("result_version"),
                "checkpoint_sequence": group["cycle"].get("checkpoint_sequence")}
    try:
        links_match = (all(key in original and plans.canonical_bytes(original[key]) == plans.canonical_bytes(value)
                           for key, value in required.items())
                       and all(key not in original or plans.canonical_bytes(original[key]) == plans.canonical_bytes(value)
                               for key, value in optional.items()))
    except (ValueError, TypeError, RecursionError):
        links_match = False
    if not links_match:
        raise DomainError("invalid", "Original verifier creation links contradict its immutable group")
    transitions = original.get("execution_transitions")
    if not isinstance(transitions, list) or any(not isinstance(item, dict) for item in transitions):
        raise DomainError("invalid", "Original verifier creation transitions are malformed")
    verification = [item for item in transitions if item.get("role") == "verification"]
    if (len(verification) != 1 or not isinstance(verification[0].get("reason"), str)
            or not verification[0]["reason"].strip()):
        raise DomainError("invalid", "Original verifier activation lacks exact creation proof")
    authority = {key: creation[key] for key in ("epoch", "generation", "ownership_version", "actor")}
    expected = [{"role": "verification", "anchor": anchor, "use_ids": ids, "state": "active",
                 "reason": verification[0]["reason"], **authority}]
    handle, reason = original.get("handoff_from"), original.get("stopped_reason")
    if sources:
        if (not isinstance(handle, str) or handle not in sources or not isinstance(reason, str) or not reason.strip()):
            raise DomainError("invalid", "Original handoff lacks its source handle and meaningful stopped assertion")
        registry = _Registry(connection)
        handle_resource = registry.resolve(owned[handle][1])
        if not any(_resource_overlap(registry, registry.resolve(rid), handle_resource,
                                     execution.repository_exclusive, owned[handle][5]) for rid in resources):
            raise DomainError("invalid", "Original handoff handle does not overlap its retained verification context")
        expected.append({"role": "implementation", "anchor": None, "use_ids": original_ids, "state": "stopped",
                         "reason": reason, **authority})
    elif handle is not None or reason is not None:
        raise DomainError("invalid", "Separate verification cannot assert an implementation handoff or stop")
    try:
        transitions_match = (len(transitions) == len(expected)
                             and sorted(plans.canonical_bytes(item) for item in transitions)
                             == sorted(plans.canonical_bytes(item) for item in expected))
    except (ValueError, TypeError, RecursionError):
        transitions_match = False
    if not transitions_match:
        raise DomainError("invalid", "Original verifier creation contradicts its complete cohort and creation authority")
    _writer_proof(connection, source, group["writer"], "verification", anchor, ids)
    return ids


def execution_uses(connection, plan_id, attempt_id):
    """Decode every owned physical row exactly once; absent legacy metadata requires full proof."""
    row = connection.execute("SELECT step_id,assignee,definition_version,payload FROM attempts WHERE plan_id=? AND attempt_id=?",
                             (plan_id, attempt_id)).fetchone()
    if row is None:
        raise DomainError("invalid", "Execution-use owner is missing")
    data = _json_object(row[3], "execution-use attempt")
    source = _source(plan_id, attempt_id, *row[:3], data)
    definition = source["definition"]
    if (not isinstance(definition, dict) or definition.get("step_id") != row[0]
            or definition.get("definition_version") != row[2]
            or not all(isinstance(source[key], str) and source[key].strip()
                       for key in ("assignee", "origin_epoch", "origin_generation"))):
        raise DomainError("invalid", "Execution-use frozen source is malformed")
    execution = _to_execution_context(data.get("context"))
    owned = _owned_uses(connection, plan_id, attempt_id)
    if _USE_NAMESPACE not in data:
        for (raw,) in connection.execute("SELECT payload FROM events WHERE plan_id=? AND kind IN ('reserve_verification','assign_step')", (plan_id,)):
            event = _json_object(raw, "verification history")
            if event.get("attempt_id") == attempt_id and ("use_id" in event or "execution_transitions" in event):
                raise DomainError("invalid", "Missing explicit execution metadata cannot be inferred as legacy implementation")
        members = [_member(item) for item in owned.values()]
        _members(connection, members, owned)
        if len({item["resource_id"] for item in members}) != len(members):
            raise DomainError("invalid", "Legacy implementation membership is ambiguous")
        _original_coverage(connection, execution, members, owned)
        namespace = {"v": 1, "source": source, "implementation": {"members": members, "writer": None}, "groups": {}}
        return namespace, owned
    namespace = data[_USE_NAMESPACE]
    if (not isinstance(namespace, dict) or set(namespace) != {"v", "source", "implementation", "groups"}
            or type(namespace["v"]) is not int or namespace["v"] != 1 or namespace["source"] != source
            or not isinstance(namespace["implementation"], dict)
            or set(namespace["implementation"]) != {"members", "writer"} or not isinstance(namespace["groups"], dict)):
        raise DomainError("invalid", "Execution-use namespace is unknown or contradictory")
    implementation = namespace["implementation"]
    original_ids = _members(connection, implementation["members"], owned)
    _original_coverage(connection, execution, implementation["members"], owned)
    _writer_proof(connection, source, implementation["writer"], "implementation", None, original_ids)
    classified = set(original_ids)
    for anchor, group in namespace["groups"].items():
        ids = _group_proof(connection, source, anchor, group, owned, original_ids)
        if classified.intersection(ids):
            raise DomainError("invalid", "Execution-use roles have overlapping membership")
        classified.update(ids)
    if classified != set(owned):
        raise DomainError("invalid", "Execution-use namespace leaves physical obligations unclassified")
    return namespace, owned


def save_execution_uses(connection, plan_id, attempt_id, namespace, *, releasing_verification=False):
    """Merge role metadata without changing frozen history or worker counters.

    A validated stopped verifier release retains its full writer proof even when
    historical metadata has filled the admission limit. Public pages remain bounded.
    """
    row = connection.execute("SELECT payload FROM attempts WHERE plan_id=? AND attempt_id=?", (plan_id, attempt_id)).fetchone()
    if row is None:
        raise DomainError("invalid", "Execution-use owner is missing")
    data = _json_object(row[0], "execution-use attempt")
    data[_USE_NAMESPACE] = namespace
    if not releasing_verification and len(plans.canonical_bytes(data)) > MAX_FILE_BYTES:
        raise DomainError("limit_exceeded", "Execution-use history exceeds the 10MB content limit")
    connection.execute("UPDATE attempts SET payload=? WHERE plan_id=? AND attempt_id=?",
                       (json.dumps(data, sort_keys=True, ensure_ascii=False), plan_id, attempt_id))


def writer_transition(namespace, role, anchor, state, command, plan, sequence, reason):
    target = namespace["implementation"] if role == "implementation" else namespace["groups"][anchor]
    writer = {"state": state, "epoch": command.epoch, "generation": plan.generation, "sequence": sequence, "reason": reason,
              "ownership_version": command.ownership_version, "actor": command.actor}
    target["writer"] = writer
    return {"role": role, "anchor": anchor, "use_ids": [item["use_id"] for item in target["members"]],
            **{key: writer[key] for key in ("state", "epoch", "generation", "reason", "ownership_version", "actor")}}


def _writer_active(connection, plan_id, writer, rows):
    if all(row[6] == "released" for row in rows):
        return False
    authority = connection.execute("SELECT generation FROM plans WHERE plan_id=?", (plan_id,)).fetchone()[0]
    return (writer is None or writer["state"] != "stopped" or writer["epoch"] != plans._installation_epoch(connection)
            or writer["generation"] != authority)


def _inventory_declaration(connection, producer_id, work_id, actor, host, visited=None):
    from adaptive_planning import recovery
    visited=set() if visited is None else visited
    if producer_id in visited:
        raise DomainError('invalid','Orphan producer ancestry is cyclic')
    visited.add(producer_id)
    saved=connection.execute('SELECT kind,actor,state,payload FROM host_work_declarations WHERE producer_id=? AND absent_work_id=?',(producer_id,work_id)).fetchone()
    if saved is None or saved[0]!='inventory' or saved[1]!=actor:
        raise DomainError('invalid','Orphan producer ancestry is absent or contradictory')
    record=_json_object(saved[3],'orphan inventory producer')
    origin=connection.execute('SELECT new_epoch,host_id FROM restore_origins WHERE restore_id=?',(record['restore_id'],)).fetchone()
    recovery.restore_origin(connection,record['restore_id'])
    if (record.get('format')!=1 or record.get('producer')!='inventory' or origin!=(record['epoch'],host) or
            record['work']['work_id']!=work_id or record['work']['actor']!=actor or record['work']['state']!=saved[2]):
        raise DomainError('invalid','Inventory orphan has no exact restore/source binding')
    matches=[]
    for event_actor,raw in connection.execute("SELECT actor,payload FROM events WHERE plan_id IS NULL AND plan_revision IS NULL AND kind='host_work_declared'"):
        event=_json_object(raw,'host work producer event')
        if event.get('producer_id')==producer_id:
            matches.append((event_actor,event))
    expected=dict(producer_id=producer_id,absent_work_id=work_id,actor=actor,record_digest=hashlib.sha256(saved[3].encode()).hexdigest())
    if matches!=[('local-administrator',expected)]:
        raise DomainError('invalid','Inventory producer contradicts its retained event')
    prior=record.get('prior_producer_id')
    if prior is not None:
        previous=_inventory_declaration(connection,prior,work_id,actor,host,visited)
        if (record['members'][:len(previous['members'])]!=previous['members'] or
                record.get('identity_reason_id')!=previous.get('identity_reason_id')):
            raise DomainError('invalid','Inventory attachment removed its original producer obligation')
    else:
        identity=record.get('identity_reason_id')
        if identity is not None:
            reason=connection.execute('SELECT epoch,kind,details FROM store_hold_reasons WHERE reason_id=?',(identity,)).fetchone()
            expected=json.dumps(dict(restore_id=record['restore_id'],work_id=work_id,actor=actor,producer_id=producer_id),sort_keys=True)
            if reason!=(record['epoch'],'absent-identity',expected):
                raise DomainError('invalid','Orphan identity reason is not its exact original producer obligation')
    return record


def orphan_cohort(connection, work_id):
    """Prove one actual historical work cohort before projection or release."""
    try:
        parent=connection.execute('SELECT absent_work_id,host_id,resource_id,scope,declared_identity,status,created_at,anchor_use_id,producer_id FROM absent_host_work WHERE absent_work_id=?',(work_id,)).fetchone()
        host=connection.execute('SELECT host_id FROM installation').fetchone()[0]
        if parent is None or parent[1]!=host or not parent[4] or not parent[8]:
            raise DomainError('invalid','Orphan parent has no coherent local producer')
        declarations=connection.execute('SELECT kind,actor,state,payload FROM host_work_declarations WHERE producer_id=? AND absent_work_id=?',(parent[8],work_id)).fetchall()
        if len(declarations)!=1 or declarations[0][1]!=parent[4]:
            raise DomainError('invalid','Orphan declaration is absent or contradictory')
        kind,actor,state,raw=declarations[0]
        record=_json_object(raw,'orphan producer')
        if record.get('format')!=1 or record.get('producer')!=kind:
            raise DomainError('invalid','Orphan producer format is unsupported')
        if kind=='migration':
            original=record['original_parent']
            if original[:5]!=list(parent[:5]) or original[6]!=parent[6]:
                raise DomainError('invalid','Migration orphan parent differs from retained history')
        elif kind=='inventory':
            record=_inventory_declaration(connection,parent[8],work_id,actor,host)
        else:
            raise DomainError('invalid','Orphan producer kind is unsupported')
        rows=connection.execute('SELECT use_id,resource_id,plan_id,attempt_id,absent_work_id,scope,status,created_at,rowid FROM resource_uses WHERE absent_work_id=? ORDER BY rowid,use_id',(work_id,)).fetchall()
        actual=[dict(use_id=row[0],resource_id=row[1],scope=row[5],created_at=row[7]) for row in rows]
        if actual!=record['members'] or any(row[2] is not None or row[3] is not None for row in rows):
            raise DomainError('invalid','Orphan physical rows differ from immutable membership')
        members=connection.execute('SELECT resource_id,scope FROM absent_host_work_members WHERE absent_work_id=?',(work_id,)).fetchall()
        if set(members)!={(row[1],row[5]) for row in rows}:
            raise DomainError('invalid','Orphan member relation is incomplete or contradictory')
        for row in rows:
            if connection.execute('SELECT host_id,kind FROM resources WHERE resource_id=?',(row[1],)).fetchone()!=(host,row[5]):
                raise DomainError('invalid','Orphan raw member belongs to another host or scope')
            resolve_resource_id(connection,row[1])
        if (rows and parent[7]!=rows[0][0]) or (not rows and parent[7] is not None):
            raise DomainError('invalid','Orphan stable anchor is not its retained first use')
        remaining=[row for row in rows if row[6]!='released']
        canonical=tuple(sorted({resolve_resource_id(connection,row[1]) for row in remaining}))
        return dict(parent=parent,record=record,rows=rows,remaining=remaining,resource_ids=canonical,
                    anchor=rows[0] if rows else None,actor=actor,identity_reason_id=record.get('identity_reason_id'))
    except DomainError:
        raise
    except (KeyError,TypeError,ValueError,IndexError) as error:
        raise DomainError('invalid','Orphan cohort producer is malformed') from error


def select_orphan_stop(connection,use_id,resource_ids):
    """Select a declared writer's complete remaining cohort by any real member."""
    row=connection.execute('SELECT absent_work_id FROM resource_uses WHERE use_id=?',(use_id,)).fetchone()
    if row is None or row[0] is None:
        raise DomainError('invalid','Stop target has no actual absent work owner')
    work=orphan_cohort(connection,row[0])
    identity=work['identity_reason_id']
    if identity is not None:
        reason=connection.execute('SELECT resolved FROM store_hold_reasons WHERE reason_id=?',(identity,)).fetchone()
        if reason is None or reason[0]!=1:
            raise DomainError('held','Unidentified orphan work cannot be physically released')
    canonical=[resolve_resource_id(connection,rid) for rid in resource_ids]
    if len(set(canonical))!=len(canonical):
        raise DomainError('invalid','Orphan stop selectors must be distinct canonical identities')
    selected=set(canonical)
    if not selected or selected!=set(work['resource_ids']):
        raise DomainError('invalid','Stop requires the full remaining canonical writer cohort')
    return work,[member[0] for member in work['remaining']]


def reservation_projections(connection, plan_id):
    """One coherent verifier anchor or conservative physical fallback, shared by counts and pages."""
    rows = tuple(connection.execute(
        "SELECT u.use_id,u.resource_id,u.plan_id,u.attempt_id,u.absent_work_id,u.scope,u.status,u.created_at,u.rowid,"
        "h.declared_identity FROM resource_uses u LEFT JOIN absent_host_work h ON h.absent_work_id=u.absent_work_id "
        "WHERE u.plan_id=? OR (u.plan_id IS NULL AND u.status!='released') ORDER BY u.rowid", (plan_id,)))
    decoded, failed = {}, set()
    for row in rows:
        if row[2] is not None and row[3] not in decoded and row[3] not in failed:
            try:
                decoded[row[3]] = execution_uses(connection, row[2], row[3])
            except (DomainError, ValueError, TypeError, KeyError, RecursionError):
                failed.add(row[3])
    result = {}
    orphan_work=set()
    for row in rows:
        actor, writer, selected = row[9] or "", row[6] != "released", [row]
        if row[2] is None:
            if row[4] in orphan_work:
                continue
            orphan_work.add(row[4])
            cohort=orphan_cohort(connection,row[4])
            anchor=cohort['anchor']
            result[anchor[8]]=ReservationView(anchor[0],None,None,row[4],cohort['resource_ids'],cohort['actor'],
                                            bool(cohort['remaining']),bool(cohort['remaining']),
                                            any(item[5]=='repository' for item in cohort['remaining']))
            continue
        if row[2] is not None:
            actor = ""
            if row[3] in decoded:
                namespace, owned = decoded[row[3]]
                implementation = namespace["implementation"]
                originals = {item["use_id"] for item in implementation["members"]}
                if row[0] in originals:
                    actor = namespace["source"]["assignee"]
                    writer = _writer_active(connection, row[2], implementation["writer"], [row])
                else:
                    anchor, group = next((anchor, group) for anchor, group in namespace["groups"].items()
                                         if any(item["use_id"] == row[0] for item in group["members"]))
                    if row[0] != anchor:
                        continue
                    selected = [owned[item["use_id"]] for item in group["members"]]
                    actor, writer = group["verifier"], _writer_active(connection, row[2], group["writer"], selected)
        result[row[8]] = ReservationView(
            use_id=row[0], plan_id=row[2], attempt_id=row[3], absent_work_id=row[4],
            resource_ids=tuple(dict.fromkeys(resolve_resource_id(connection, item[1]) for item in selected)),
            actor=actor, writer_active=writer, unresolved=any(item[6] == "unresolved" for item in selected),
            repository_exclusive=any(item[5] == "repository" for item in selected))
    return result, len(rows) - len(result)
