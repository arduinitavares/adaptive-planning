"""Host-attested resource identity, durable reconciliation and conservative overlap.

Paths are identity evidence, never paths to open. Registration changes identity
only; neither registration nor reconciliation alters an execution use.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from adaptive_planning import plans
from adaptive_planning.core import MAX_TEXT_LEN
from adaptive_planning.ownership import require_kind_capabilities, require_orchestrator
from adaptive_planning.types import Command, DomainError, ExecutionContext, MutationReceipt, ResourceClaim, ResourceId

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


def conflicting_uses(connection: sqlite3.Connection, resource_ids: tuple[ResourceId, ...],
                     repository_exclusive: bool) -> tuple[str, ...]:
    registry = _Registry(connection)
    requested = tuple(dict.fromkeys(registry.resolve(rid) for rid in resource_ids))
    def overlaps(left, right, scope):
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
    conflicts = []
    for use_id, rid, scope in connection.execute(
            "SELECT use_id, resource_id, scope FROM resource_uses WHERE status IN ('active', 'unresolved') ORDER BY use_id"):
        canonical = registry.resolve(rid)
        if any(overlaps(request, canonical, scope) for request in requested):
            conflicts.append(use_id)
    return tuple(conflicts)
