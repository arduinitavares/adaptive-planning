"""Host-attested identities remain conservative across aliases, plans and redirects."""
from __future__ import annotations
from contextlib import ExitStack

import asyncio
import copy
import importlib
import io
import json
import logging
import socket
import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, ExitStack
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from adaptive_planning import plans
from adaptive_planning.types import DomainError, ExecutionContext, ResourceClaim
from tests import service_fixtures
from tests.service_fixtures import context, make_service, workspace


class TestService(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.f = self.enterContext(make_service(Path(self.tmp.name)))
        self.p = self.f.create()

    def register(self, claim, receipt=None):
        try:
            return self.f.register(claim, receipt or self.p)
        except DomainError as error:
            if error.code == "unsupported":
                self.fail("register_resource is not enabled: host-attested identity registration is missing")
            raise

    def resources(self):
        try:
            return importlib.import_module("adaptive_planning.resources")
        except ModuleNotFoundError as error:
            if error.name == "adaptive_planning.resources":
                self.fail("the physical identity and overlap registry is missing")
            raise

    def orphan(self, resource_id):
        return service_fixtures.seed_orphan_use(self.f, resource_id)

    def conflicts(self, ids, exclusive=False, fixture=None):
        f = fixture or self.f
        return f.store.read(lambda db: self.resources().conflicting_uses(db, ids, exclusive))

    def resolve(self, value, fixture=None):
        f = fixture or self.f
        return f.store.read(lambda db: self.resources().resolve_context(db, value))

    def rows(self):
        return self.f.store.read(lambda db: tuple(
            (name, tuple(db.execute(f"SELECT * FROM {name} ORDER BY rowid")))
            for name in ("resources", "aliases", "resource_redirects", "resource_uses",
                         "absent_host_work", "plans", "events", "requests")))

    def reconcile(self, ids, claim, receipt=None, reason="host-attested physical equivalence"):
        return self.f.service.execute(self.f.command(
            "reconcile_resources", {"resource_ids": ids, "canonical_claim": claim, "reason": reason},
            receipt=receipt or self.p))

    def repository(self, path="/synthetic/repo", inode=700):
        return self.register(replace(workspace(path, inode), kind="repository"))

    def external(self, identity="synthetic-external-shared"):
        return self.register(ResourceClaim("synthetic-host", "external", None, None, None, False, None, identity))

    def owned_use(self, resource_id, receipt, use_id="normal-use", scope="workspace", status="unresolved"):
        plan_id = receipt.committed_metadata.plan_id
        def insert(db):
            db.execute("INSERT INTO attempts (plan_id, attempt_id, step_id, assignee, state, version, "
                       "definition_version, payload) VALUES (?, ?, 'a', 'worker-a', 'accepted', 1, 'v1', '{}')",
                       (plan_id, "attempt-" + use_id))
            db.execute("INSERT INTO resource_uses (use_id, resource_id, plan_id, attempt_id, scope, status) "
                       "VALUES (?, ?, ?, ?, ?, ?)",
                       (use_id, resource_id, plan_id, "attempt-" + use_id, scope, status))
        self.f.store.write(insert)
        return use_id

    def test_alias_device_change_and_path_reuse(self):
        first = self.register(workspace("/synthetic/A", 100))
        alias = self.register(workspace("/synthetic/alias", 100))
        self.assertEqual(first, alias)
        lower = self.register(workspace("/synthetic/a", 101))
        self.assertNotEqual(first, lower)
        use = self.orphan(first)
        before = self.rows()
        for changed in (replace(workspace("/synthetic/A", 102), device=7),
                        replace(workspace("/synthetic/A", 100), device=8)):
            with self.subTest(change=changed.device):
                with self.assertRaises(DomainError) as raised:
                    self.register(changed)
                self.assertEqual(raised.exception.code, "resource_conflict")
                self.assertEqual(self.rows(), before)
        self.assertIn(use.use_id, self.conflicts((first,)))
        insensitive = self.register(workspace("/synthetic/Case", 110, case_insensitive=True))
        self.assertEqual(insensitive, self.register(workspace("/synthetic/cASE", 110, case_insensitive=True)))
        with self.assertRaises(DomainError) as raised:
            self.register(workspace("/synthetic/CASE", 111, case_insensitive=True))
        self.assertEqual(raised.exception.code, "resource_conflict")

    def test_resource_cannot_invent_host_namespace(self):
        before = self.rows()
        with self.assertRaises(DomainError) as raised:
            self.register(replace(workspace("/synthetic/foreign", 120), host_id="invented-host"))
        self.assertEqual(raised.exception.code, "invalid")
        self.assertEqual(self.rows(), before)

    def test_case_kind_and_repository_attestations_cannot_contradict_alias(self):
        repo = self.repository()
        ws = self.register(workspace("/synthetic/member", 121, repository_id=repo))
        before = self.rows()
        claims = (workspace("/synthetic/member", 121, repository_id=repo, case_insensitive=True),
                  replace(workspace("/synthetic/member", 121), kind="repository"),
                  workspace("/synthetic/member", 121))
        for claim in claims:
            with self.subTest(kind=claim.kind, case=claim.case_insensitive):
                with self.assertRaises(DomainError) as raised:
                    self.register(claim)
                self.assertEqual(raised.exception.code, "resource_conflict")
                self.assertEqual(self.rows(), before)
        self.assertEqual(ws, self.register(workspace("/synthetic/another-member-alias", 121, repository_id=repo)))

    def test_partial_unknown_and_malformed_identity_rejected_without_residue(self):
        before = self.rows()
        claims = (replace(workspace("/synthetic/partial", 122), device=None),
                  replace(workspace("/synthetic/partial", 122), inode=None),
                  replace(workspace("/synthetic/unknown", 122), device=None, inode=None, canonical_path=None),
                  workspace("relative/path", 122),
                  replace(workspace("/synthetic/parent", 122), repository_id="unknown-repository"),
                  ResourceClaim("synthetic-host", "external", None, None, None, False, None, ""))
        for claim in claims:
            with self.subTest(path=claim.canonical_path):
                with self.assertRaises(DomainError) as raised:
                    self.register(claim)
                self.assertIn(raised.exception.code, ("invalid", "not_found"))
                self.assertEqual(self.rows(), before)

    def test_repository_workspace_external_non_git_overlap(self):
        repo = self.repository()
        left = self.register(workspace("/synthetic/tree-left", 701, repository_id=repo))
        right = self.register(workspace("/synthetic/tree-right", 702, repository_id=repo))
        other = self.f.create(creation_id="other-plan")
        use = self.owned_use(left, other)
        self.assertEqual(self.conflicts((right,)), ())
        self.assertEqual(self.conflicts((repo,), True), (use,))
        self.assertEqual(self.conflicts((right,), True), (use,))
        self.assertEqual(self.resolve(context(left, repository_id=repo)), (left,))
        self.assertEqual(self.resolve(ExecutionContext("repository", repo, None, False, (), True, None)), (repo,))
        self.assertEqual(self.resolve(replace(context(right, repository_id=repo), repository_exclusive=True)), (repo,))
        repo_use = self.owned_use(repo, self.p, use_id="repository-use", scope="repository")
        self.assertIn(repo_use, self.conflicts((right,)))
        external = self.external()
        external_use = self.owned_use(external, other, use_id="external-use", scope="external")
        self.assertEqual(self.conflicts((external,)), (external_use,))
        nongit = self.register(workspace("/synthetic/known-non-git", 703))
        self.assertEqual(self.resolve(context(nongit)), (nongit,))
        self.assertEqual(self.resolve(context(None, external_ids=(external,))), (external,))
        for value in (ExecutionContext("repository", None, left, False, (), True, None),
                      ExecutionContext("non-repository", None, None, False, (), True, None),
                      ExecutionContext("external-only", None, None, False, (), True, None),
                      ExecutionContext("read-only", None, None, False, (), True, None),
                      context(left), context(nongit, repository_id=repo)):
            with self.subTest(kind=value.kind, workspace=value.workspace_id):
                with self.assertRaises(DomainError):
                    self.resolve(value)
        self.assertEqual(self.resolve(context(None, writes=False)), ())

    def test_parent_and_child_paths_overlap_without_repository_metadata(self):
        parent = self.register(workspace("/synthetic/non-git", 710))
        child = self.register(workspace("/synthetic/non-git/subdir", 711))
        sibling = self.register(workspace("/synthetic/non-git-extra", 712))
        use = self.orphan(parent)
        self.assertIn(use.use_id, self.conflicts((child,)))
        self.assertEqual(self.conflicts((sibling,)), ())

    def test_active_normal_orphan_and_retired_fk_uses_are_all_conservative(self):
        a = self.register(replace(workspace("/synthetic/chain-a", 720), device=None, inode=None))
        b = self.register(replace(workspace("/synthetic/chain-b", 720), device=None, inode=None))
        c = self.register(replace(workspace("/synthetic/chain-c", 720), device=None, inode=None))
        active = self.owned_use(a, self.p, use_id="active-normal", status="active")
        unresolved = self.owned_use(b, self.p, use_id="unresolved-normal")
        self.owned_use(c, self.p, use_id="released-normal", status="released")
        orphan = self.orphan(a)
        def history(db):
            db.execute("INSERT INTO resource_redirects (retired_id, survivor_id) VALUES (?, ?)", (a, b))
            db.execute("INSERT INTO resource_redirects (retired_id, survivor_id) VALUES (?, ?)", (b, c))
            db.execute("UPDATE resources SET status='retired' WHERE resource_id IN (?, ?)", (a, b))
        self.f.store.write(history)
        for rid in (a, b, c):
            self.assertEqual(set(self.conflicts((rid,))), {active, unresolved, orphan.use_id})
            self.assertEqual(self.resolve(context(rid)), (c,))
        self.assertEqual(self.register(replace(workspace("/synthetic/chain-a", 720), device=None, inode=None)), c)

    def test_reconciliation_prefers_canonical_alias_and_rejects_unlisted_collision(self):
        left_claim = replace(workspace("/synthetic/prefer-left", 725), device=None, inode=None)
        right_claim = replace(workspace("/synthetic/prefer-right", 725), device=None, inode=None)
        left = self.register(left_claim)
        right = self.register(right_claim)
        selected = max(left, right)
        selected_claim = left_claim if selected == left else right_claim
        canonical = replace(selected_claim, device=7, inode=725)
        receipt = self.reconcile((left, right), canonical)
        self.assertEqual(receipt.committed["resource_id"], selected)
        outside = self.register(workspace("/synthetic/outside", 726))
        unknown_a = self.register(replace(workspace("/synthetic/outside-a", 726), device=None, inode=None))
        unknown_b = self.register(replace(workspace("/synthetic/outside-b", 726), device=None, inode=None))
        before = self.rows()
        with self.assertRaises(DomainError) as raised:
            self.reconcile((unknown_a, unknown_b), workspace("/synthetic/outside", 726))
        self.assertEqual(raised.exception.code, "resource_conflict")
        self.assertEqual(self.rows(), before)
        self.assertNotIn(outside, (unknown_a, unknown_b))

    def test_unattested_historical_writer_is_not_discarded_as_disjoint(self):
        known = self.register(workspace("/synthetic/known-current", 727))
        self.f.store.write(lambda db: db.execute(
            "INSERT INTO resources (resource_id, host_id, kind, physical_identity, created_at) "
            "VALUES ('historical-unknown', 'synthetic-host', 'workspace', 'unversioned-identity', 'ts')"))
        use = self.owned_use("historical-unknown", self.p, use_id="historical-writer")
        self.assertIn(use, self.conflicts((known,)))
        with self.assertRaises(DomainError) as raised:
            self.resolve(context("historical-unknown"))
        self.assertEqual(raised.exception.code, "held")
        self.assertEqual(self.resolve(context(None, writes=False)), ())

    def test_explicit_null_legacy_reconciliation_preserves_unknown_history_and_resolves_lineage(self):
        self.register(workspace("/synthetic/null-legacy-enabled", 728))
        def legacy(db):
            for rid, path in (("legacy-null-a", "/synthetic/null-a"), ("legacy-null-b", "/synthetic/null-b")):
                db.execute("INSERT INTO resources (resource_id, host_id, kind, canonical_path, created_at) "
                           "VALUES (?, 'synthetic-host', 'workspace', ?, 'ts')", (rid, path))
                db.execute("INSERT INTO aliases (alias_id, host_id, canonical_path, resource_id) "
                           "VALUES (?, 'synthetic-host', ?, ?)", (rid, path, rid))
        self.f.store.write(legacy)
        use = self.owned_use("legacy-null-b", self.p)
        before_aliases = self.f.store.read(lambda db: tuple(db.execute("SELECT * FROM aliases ORDER BY alias_id")))
        self.reconcile(("legacy-null-a", "legacy-null-b"), workspace("/synthetic/null-canonical", 729))
        self.assertIsNone(self.f.store.read(lambda db: db.execute(
            "SELECT physical_identity FROM resources WHERE resource_id='legacy-null-b'").fetchone()[0]))
        self.assertTrue(set(before_aliases).issubset(set(self.f.store.read(
            lambda db: tuple(db.execute("SELECT * FROM aliases ORDER BY alias_id"))))))
        self.assertEqual(self.resolve(context("legacy-null-b")), ("legacy-null-a",))
        self.assertIn(use, self.conflicts(("legacy-null-a",)))
        self.assertEqual(self.register(workspace("/synthetic/null-canonical", 729)), "legacy-null-a")

    def test_v1_metadata_without_any_usable_identity_still_blocks_write_context(self):
        known = self.register(workspace("/synthetic/usable-identity", 730))
        raw = '{"v":1,"device":null,"inode":null,"case_insensitive":false,"external_identity":null}'
        self.f.store.write(lambda db: db.execute(
            "INSERT INTO resources (resource_id, host_id, kind, physical_identity, created_at) "
            "VALUES ('no-identity', 'synthetic-host', 'workspace', ?, 'ts')", (raw,)))
        use = self.owned_use("no-identity", self.p, use_id="no-identity-writer")
        self.assertIn(use, self.conflicts((known,)))
        with self.assertRaises(DomainError) as raised:
            self.resolve(context("no-identity"))
        self.assertEqual(raised.exception.code, "held")

    def test_reconcile_keeps_old_unresolved_uses(self):
        with ExitStack() as _owned_store_lifetimes:
            left = replace(workspace("/synthetic/alias-left", 800), device=None, inode=None)
            right = replace(left, canonical_path="/synthetic/alias-right")
            a = self.register(left)
            b = self.register(right)
            use = self.orphan(a)
            canonical = workspace("/synthetic/canonical", 800)
            command = self.f.command("reconcile_resources", {"resource_ids": (a, b), "canonical_claim": canonical,
                "reason": "host-attested both paths refer to same physical workspace"}, receipt=self.p)
            receipt = self.f.service.execute(command)
            self.assertEqual(receipt.committed["resource_id"], min(a, b))
            for rid in (a, b, self.register(canonical), self.register(left), self.register(right)):
                self.assertIn(use.use_id, self.conflicts((rid,)))
                self.assertEqual(self.resolve(context(rid)), (min(a, b),))
            current = self.f.service.read_plan(self.p.committed_metadata.plan_id)
            reservation = next(item for item in current.coordination.reservations if item.use_id == use.use_id)
            self.assertTrue(reservation.unresolved)
            self.assertTrue(reservation.writer_active)
            self.assertIsNone(reservation.plan_id)
            other = self.register(workspace("/synthetic/contradictory", 801))
            before = self.rows()
            with self.assertRaises(DomainError):
                self.reconcile((a, other), canonical, reason="contradictory physical identity")
            self.assertEqual(self.rows(), before)
            reopened = _owned_store_lifetimes.enter_context(make_service(self.f.directory, self.f.clock))
            self.assertIn(use.use_id, self.conflicts((b,), fixture=reopened))

    def test_reconcile_prefers_matching_physical_survivor_and_keeps_fk_history(self):
        repo_old = self.repository("/synthetic/repo-old", 810)
        alias = replace(workspace("/synthetic/repo-alias", 810), kind="repository", device=None, inode=None)
        repo_alias = self.register(alias)
        ws = self.register(workspace("/synthetic/tree", 811, repository_id=repo_alias))
        normal = self.owned_use(ws, self.p)
        orphan = self.orphan(repo_alias)
        before_uses = self.f.store.read(lambda db: tuple(db.execute("SELECT * FROM resource_uses ORDER BY use_id")))
        receipt = self.reconcile((repo_alias, repo_old), replace(workspace("/synthetic/repo-old", 810), kind="repository"))
        self.assertEqual(receipt.committed["resource_id"], repo_old)
        self.assertEqual(self.f.store.read(lambda db: tuple(db.execute("SELECT * FROM resource_uses ORDER BY use_id"))), before_uses)
        self.assertEqual(set(self.conflicts((repo_old,), True)), {normal, orphan.use_id})
        self.assertEqual(self.resolve(context(ws, repository_id=repo_old)), (ws,))
        self.assertEqual(ws, self.register(workspace("/synthetic/tree-alias", 811, repository_id=repo_old)))
        self.assertEqual(repo_old, self.register(alias))

    def test_ambiguous_physical_and_alias_claim_waits_for_explicit_reconciliation(self):
        physical = self.register(workspace("/synthetic/physical", 820))
        unknown_claim = replace(workspace("/synthetic/unknown-alias", 820), device=None, inode=None)
        unknown = self.register(unknown_claim)
        use = self.orphan(unknown)
        combined = workspace("/synthetic/unknown-alias", 820)
        before = self.rows()
        with self.assertRaises(DomainError) as raised:
            self.register(combined)
        self.assertEqual(raised.exception.code, "resource_conflict")
        self.assertEqual(self.rows(), before)
        self.reconcile((physical, unknown), combined)
        self.assertEqual(self.register(combined), unknown)
        self.assertIn(use.use_id, self.conflicts((physical,)))

    def test_second_reconciliation_preserves_historical_keys_aliases_and_losing_use(self):
        with ExitStack() as _owned_store_lifetimes:
            left = replace(workspace("/synthetic/merge-left", 822), device=None, inode=None)
            right = replace(left, canonical_path="/synthetic/merge-right")
            a, b = self.register(left), self.register(right)
            use = self.orphan(max(a, b))
            initial_aliases = self.f.store.read(lambda db: tuple(db.execute("SELECT * FROM aliases ORDER BY alias_id")))
            canonical = workspace("/synthetic/merge-canonical", 822)
            first = self.reconcile((a, b), canonical).committed["resource_id"]
            before_key = self.f.store.read(lambda db: db.execute(
                "SELECT physical_identity FROM resources WHERE resource_id=?", (first,)).fetchone()[0])
            third_claim = replace(left, canonical_path="/synthetic/merge-third")
            third = self.register(third_claim)
            second = self.reconcile((first, third), replace(canonical, canonical_path=third_claim.canonical_path))
            self.assertEqual(second.committed["resource_id"], third)
            self.assertEqual(self.f.store.read(lambda db: db.execute(
                "SELECT physical_identity FROM resources WHERE resource_id=?", (first,)).fetchone()[0]), before_key)
            current_aliases = self.f.store.read(lambda db: tuple(db.execute("SELECT * FROM aliases ORDER BY alias_id")))
            self.assertTrue(set(initial_aliases).issubset(set(current_aliases)))
            reopened = _owned_store_lifetimes.enter_context(make_service(self.f.directory, self.f.clock))
            for rid in (a, b, first, third):
                self.assertIn(use.use_id, self.conflicts((rid,), fixture=reopened))
                self.assertEqual(self.resolve(context(rid), fixture=reopened), (third,))
            self.assertEqual(self.register(canonical), third)
            self.assertEqual(self.register(left), third)

    def test_case_collision_in_both_orders_and_cross_kind_identity_are_rejected(self):
        with ExitStack() as _owned_store_lifetimes:
            self.register(workspace("/synthetic/test-enabled", 823))
            for index, (first_case, second_case) in enumerate(((False, True), (True, False))):
                f = _owned_store_lifetimes.enter_context(make_service(self.f.directory / f"case-order-{index}", self.f.clock))
                p = f.create()
                f.register(workspace("/synthetic/CaseOrder", 824, case_insensitive=first_case), p)
                before = f.store.read(lambda db: tuple(db.iterdump()))
                with self.assertRaises(DomainError) as raised:
                    f.register(workspace("/synthetic/caseorder", 825, case_insensitive=second_case), p)
                self.assertEqual(raised.exception.code, "resource_conflict")
                self.assertEqual(f.store.read(lambda db: tuple(db.iterdump())), before)
            value = workspace("/synthetic/cross-kind", 826)
            self.register(value)
            for changed in (replace(value, kind="repository"),
                            replace(value, kind="repository", canonical_path="/synthetic/other-role-alias")):
                with self.assertRaises(DomainError) as raised:
                    self.register(changed)
                self.assertEqual(raised.exception.code, "resource_conflict")

    def test_identity_unique_indexes_ignore_json_order_case_and_kind(self):
        rid = self.register(workspace("/synthetic/index-source", 827))
        original = self.f.store.read(lambda db: db.execute(
            "SELECT physical_identity FROM resources WHERE resource_id=?", (rid,)).fetchone()[0])
        reordered = dict(reversed(list(json.loads(original).items())))
        reordered["case_insensitive"] = True
        with self.assertRaises(sqlite3.IntegrityError):
            self.f.store.write(lambda db: db.execute(
                "INSERT INTO resources (resource_id, host_id, kind, physical_identity, created_at) "
                "VALUES ('index-bypass', 'synthetic-host', 'repository', ?, 'ts')", (json.dumps(reordered),)))
        external = self.external("opaque-External")
        self.assertNotEqual(external, self.external("opaque-external"))
        original = self.f.store.read(lambda db: db.execute(
            "SELECT physical_identity FROM resources WHERE resource_id=?", (external,)).fetchone()[0])
        reordered = dict(reversed(list(json.loads(original).items())))
        reordered["case_insensitive"] = True
        with self.assertRaises(sqlite3.IntegrityError):
            self.f.store.write(lambda db: db.execute(
                "INSERT INTO resources (resource_id, host_id, kind, physical_identity, created_at) "
                "VALUES ('external-bypass', 'synthetic-host', 'workspace', ?, 'ts')", (json.dumps(reordered),)))
        large = 10 ** 80 + 123
        physical = self.register(workspace("/synthetic/large-integer", large))
        self.assertEqual(physical, self.register(workspace("/synthetic/large-integer-alias", large)))
        for bad in (True, 1.5, -1):
            with self.assertRaises(DomainError):
                self.register(replace(workspace("/synthetic/invalid-scalar", 828), inode=bad))

    def test_legacy_null_malformed_wrong_shape_and_scalar_encodings_remain_held(self):
        with ExitStack() as _owned_store_lifetimes:
            self.register(workspace("/synthetic/test-legacy-enabled", 829))
            unknowns = (None, "opaque-legacy", "{malformed", '{"v":2}',
                        '{"v":1,"device":"07","inode":"829","case_insensitive":false,"external_identity":null}',
                        '{"v":1,"device":7,"inode":"829","case_insensitive":false,"external_identity":null}',
                        '{"v":1,"v":1,"device":"7","inode":"829","case_insensitive":false,"external_identity":null}',
                        '{"v":1,"device":null,"inode":null,"case_insensitive":false,"external_identity":null,"extra":true}')
            for index, opaque in enumerate(unknowns):
                f = _owned_store_lifetimes.enter_context(make_service(self.f.directory / f"legacy-{index}", self.f.clock))
                p = f.create()
                known = f.register(workspace("/synthetic/legacy-known", 829), p)
                f.store.write(lambda db: db.execute(
                    "INSERT INTO resources (resource_id, host_id, kind, physical_identity, canonical_path, created_at) "
                    "VALUES ('legacy-row', 'synthetic-host', 'workspace', ?, '/synthetic/legacy-alias', 'ts')", (opaque,)))
                f.store.write(lambda db: db.execute(
                    "INSERT INTO aliases (alias_id, host_id, canonical_path, resource_id) "
                    "VALUES ('legacy-alias', 'synthetic-host', '/synthetic/legacy-alias', 'legacy-row')"))
                f.store.write(lambda db: db.execute(
                    "INSERT INTO absent_host_work (absent_work_id, host_id, resource_id, scope,declared_identity,producer_id) "
                    "VALUES ('legacy-work', 'synthetic-host', 'legacy-row', 'workspace','synthetic-legacy-worker','migration:legacy-work')"))
                f.store.write(lambda db: db.execute("INSERT INTO absent_host_work_members VALUES('legacy-work','legacy-row','workspace')"))
                f.store.write(lambda db: db.execute(
                    "INSERT INTO resource_uses (use_id, resource_id, absent_work_id, scope, status) "
                    "VALUES ('legacy-use', 'legacy-row', 'legacy-work', 'workspace', 'unresolved')"))
                def origin(db):
                    db.execute("UPDATE absent_host_work SET anchor_use_id='legacy-use' WHERE absent_work_id='legacy-work'")
                    value=dict(format=1,producer='migration',original_parent=['legacy-work','synthetic-host','legacy-row','workspace','synthetic-legacy-worker','unresolved',''],members=[dict(use_id='legacy-use',resource_id='legacy-row',scope='workspace',created_at='')])
                    db.execute("INSERT INTO host_work_declarations VALUES('migration:legacy-work','legacy-work','migration','synthetic-legacy-worker','unknown',?,'')",(json.dumps(value),))
                f.store.write(origin)
                reopened = _owned_store_lifetimes.enter_context(make_service(f.directory, self.f.clock))
                self.assertIn("legacy-use", self.conflicts((known,), fixture=reopened))
                with self.assertRaises(DomainError) as raised:
                    self.resolve(context("legacy-row"), fixture=reopened)
                self.assertEqual(raised.exception.code, "held")
                before = f.store.read(lambda db: tuple(db.iterdump()))
                with self.assertRaises(DomainError) as raised:
                    f.register(workspace("/synthetic/legacy-alias", 830), p)
                self.assertEqual(raised.exception.code, "held")
                self.assertEqual(f.store.read(lambda db: tuple(db.iterdump())), before)

    def test_redirect_status_host_and_kind_corruption_fails_closed(self):
        with ExitStack() as _owned_store_lifetimes:
            self.register(workspace("/synthetic/test-status-enabled", 831))
            for index, corruption in enumerate(("active-edge", "retired-without-edge", "unknown-status", "cross-host", "cross-kind")):
                f = _owned_store_lifetimes.enter_context(make_service(self.f.directory / f"status-{index}", self.f.clock))
                p = f.create()
                a = f.register(workspace("/synthetic/status-a", 832), p)
                b = f.register(workspace("/synthetic/status-b", 833), p)
                def corrupt(db):
                    if corruption == "retired-without-edge":
                        db.execute("UPDATE resources SET status='retired' WHERE resource_id=?", (a,))
                    elif corruption == "unknown-status":
                        db.execute("UPDATE resources SET status='other' WHERE resource_id=?", (a,))
                    else:
                        if corruption == "cross-host":
                            db.execute("UPDATE resources SET host_id='foreign-host' WHERE resource_id=?", (b,))
                        if corruption == "cross-kind":
                            db.execute("UPDATE resources SET kind='repository' WHERE resource_id=?", (b,))
                        db.execute("INSERT INTO resource_redirects (retired_id, survivor_id) VALUES (?, ?)", (a, b))
                        if corruption != "active-edge":
                            db.execute("UPDATE resources SET status='retired' WHERE resource_id=?", (a,))
                # Host FK backstops aliases; remove no data: use a foreign unaliased
                # resource for the corrupt edge instead of changing an aliased host.
                if corruption == "cross-host":
                    def corrupt(db):
                        db.execute("INSERT INTO resources (resource_id, host_id, kind, created_at) "
                                   "VALUES ('foreign-target', 'foreign-host', 'workspace', 'ts')")
                        db.execute("UPDATE resources SET status='retired' WHERE resource_id=?", (a,))
                        db.execute("INSERT INTO resource_redirects (retired_id, survivor_id) VALUES (?, 'foreign-target')", (a,))
                f.store.write(corrupt)
                with self.assertRaises(DomainError) as raised:
                    self.resolve(context(a), fixture=f)
                self.assertEqual(raised.exception.code, "held")

    def test_initialization_checks_v1_duplicates_without_repairing_legacy_rows(self):
        from adaptive_planning.schema import SCHEMA_STATEMENTS, initialize_schema
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        for statement in SCHEMA_STATEMENTS:
            if "CREATE UNIQUE INDEX IF NOT EXISTS idx_resources_canonical_" not in statement:
                db.execute(statement)
        for index, raw in enumerate((None, "opaque", "{invalid", '{"v":2}')):
            db.execute("INSERT INTO resources (resource_id, host_id, kind, physical_identity, created_at) "
                       "VALUES (?, 'synthetic-host', 'workspace', ?, 'ts')", (f"legacy-{index}", raw))
        initialize_schema(db)
        db2 = sqlite3.connect(":memory:")
        self.addCleanup(db2.close)
        for statement in SCHEMA_STATEMENTS:
            if "CREATE UNIQUE INDEX IF NOT EXISTS idx_resources_canonical_" not in statement:
                db2.execute(statement)
        value = '{"v":1,"device":"7","inode":"834","case_insensitive":false,"external_identity":null}'
        for rid in ("duplicate-a", "duplicate-b"):
            db2.execute("INSERT INTO resources (resource_id, host_id, kind, physical_identity, created_at) "
                        "VALUES (?, 'synthetic-host', 'workspace', ?, 'ts')", (rid, value))
        before = tuple(db2.execute("SELECT * FROM resources ORDER BY resource_id"))
        with self.assertRaises(sqlite3.IntegrityError):
            initialize_schema(db2)
        self.assertEqual(tuple(db2.execute("SELECT * FROM resources ORDER BY resource_id")), before)

    def identity_database(self):
        from adaptive_planning.schema import SCHEMA_STATEMENTS
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        for statement in SCHEMA_STATEMENTS:
            if "CREATE UNIQUE INDEX IF NOT EXISTS idx_resources_canonical_" not in statement:
                db.execute(statement)
        return db

    def test_initialization_excludes_nul_labels_and_decimals_without_repairing_legacy(self):
        from adaptive_planning.schema import initialize_schema
        supported = {"v": 1, "device": "7", "inode": "901", "case_insensitive": False,
                     "external_identity": None}
        cases = [("label-" + label, {key + ("\0junk" if key == label else ""): value
                                   for key, value in supported.items()}) for label in supported]
        cases.extend(("decimal-" + label, {**supported, label: supported[label] + "\0junk"})
                     for label in ("device", "inode"))
        cases.append(("external-nul", {**supported, "external_identity": "opaque\0junk"}))
        for name, unsupported in cases:
            with self.subTest(case=name):
                db = self.identity_database()
                raw = json.dumps(unsupported)
                self.assertIsNone(self.resources()._decode(raw))
                for rid, value in (("supported", json.dumps(supported)),
                                   ("legacy-a", raw), ("legacy-b", raw)):
                    db.execute("INSERT INTO resources (resource_id, host_id, kind, physical_identity, created_at) "
                               "VALUES (?, 'synthetic-host', 'workspace', ?, 'ts')", (rid, value))
                before = tuple(db.execute("SELECT * FROM resources ORDER BY resource_id"))
                initialize_schema(db)
                self.assertEqual(tuple(db.execute("SELECT * FROM resources ORDER BY resource_id")), before)
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("INSERT INTO resources (resource_id, host_id, kind, physical_identity, created_at) "
                               "VALUES ('supported-duplicate', 'synthetic-host', 'workspace', ?, 'ts')",
                               (json.dumps(dict(reversed(list(supported.items())))),))

    def test_supported_reordered_keys_and_literal_nul_escape_still_fail_duplicate_initialization(self):
        from adaptive_planning.schema import initialize_schema
        physical = {"v": 1, "device": "7", "inode": "902", "case_insensitive": False,
                    "external_identity": None}
        for name, supported in (("physical", physical),
                                ("literal-escape", {**physical, "device": None, "inode": None,
                                                    "external_identity": r"opaque\u0000-label"}),
                                ("two-literal-slashes", {**physical, "device": None, "inode": None,
                                                        "external_identity": r"opaque\\u0000-label"})):
            with self.subTest(case=name):
                db = self.identity_database()
                raw = json.dumps(supported)
                self.assertIsNotNone(self.resources()._decode(raw))
                reordered = dict(reversed(list(supported.items())))
                reordered["case_insensitive"] = True
                for rid, value in (("supported-a", raw), ("supported-b", json.dumps(reordered, indent=2))):
                    db.execute("INSERT INTO resources (resource_id, host_id, kind, physical_identity, created_at) "
                               "VALUES (?, 'synthetic-host', ?, ?, 'ts')",
                               (rid, "external" if supported["external_identity"] is not None else "workspace", value))
                before = tuple(db.execute("SELECT * FROM resources ORDER BY resource_id"))
                with self.assertRaises(sqlite3.IntegrityError):
                    initialize_schema(db)
                self.assertEqual(tuple(db.execute("SELECT * FROM resources ORDER BY resource_id")), before)

    def test_current_reservation_ids_resolve_losing_fk_after_reopen_and_second_merge(self):
        with ExitStack() as _owned_store_lifetimes:
            left = replace(workspace("/synthetic/current-left", 903), device=None, inode=None)
            right = replace(left, canonical_path="/synthetic/current-right")
            a, b = self.register(left), self.register(right)
            losing = max(a, b)
            normal = self.owned_use(losing, self.p, use_id="current-normal", status="active")
            orphan = self.orphan(losing)
            peer = self.f.create(creation_id="current-peer")
            plan_id, peer_id = self.p.committed_metadata.plan_id, peer.committed_metadata.plan_id
            original = {view.use_id: view for view in self.f.service.read_plan(plan_id).coordination.reservations}

            def immutable_rows():
                return self.f.store.read(lambda db: {name: tuple(db.execute(f"SELECT * FROM {name} ORDER BY rowid"))
                                                    for name in ("resource_uses", "absent_host_work", "aliases", "events", "requests")})

            def assert_preserved(before):
                after = immutable_rows()
                for name in before:
                    self.assertEqual(after[name][:len(before[name])], before[name], name)

            def assert_current(fixture, canonical, stage):
                for selected, expected_ids in ((plan_id, (normal, orphan.use_id)), (peer_id, (orphan.use_id,))):
                    views = fixture.service.read_plan(selected).coordination.reservations
                    page = fixture.service.read_coordination(selected)
                    for reader, reservations in (("plan", views),
                                                 ("coordination", tuple(item.value for item in page.items
                                                                        if item.kind == "reservation"))):
                        with self.subTest(stage=stage, plan=selected, reader=reader):
                            actual = {view.use_id: view for view in reservations}
                            self.assertEqual(set(actual), set(expected_ids))
                            for use_id in expected_ids:
                                self.assertEqual(actual[use_id], replace(original[use_id], resource_ids=(canonical,)))

            before = immutable_rows()
            canonical = workspace("/synthetic/current-canonical", 903)
            command = self.f.command("reconcile_resources", {"resource_ids": (a, b), "canonical_claim": canonical,
                                     "reason": "host-attested current view equivalence"}, receipt=self.p)
            first_receipt = self.f.service.execute(command)
            first = first_receipt.committed["resource_id"]
            self.assertEqual(first, min(a, b))
            assert_current(self.f, first, "first")
            assert_current(_owned_store_lifetimes.enter_context(make_service(self.f.directory, self.f.clock)), first, "first-reopen")
            assert_preserved(before)
            first_key = self.f.store.read(lambda db: db.execute(
                "SELECT physical_identity FROM resources WHERE resource_id=?", (first,)).fetchone()[0])
            third_claim = replace(left, canonical_path="/synthetic/current-third")
            third = self.register(third_claim)
            before_second = immutable_rows()
            second = self.reconcile((first, third), replace(canonical, canonical_path=third_claim.canonical_path))
            self.assertEqual(second.committed["resource_id"], third)
            self.assertEqual(self.f.store.read(lambda db: db.execute(
                "SELECT physical_identity FROM resources WHERE resource_id=?", (first,)).fetchone()[0]), first_key)
            assert_current(self.f, third, "second")
            assert_current(_owned_store_lifetimes.enter_context(make_service(self.f.directory, self.f.clock)), third, "second-reopen")
            assert_preserved(before_second)
            replay = self.f.service.execute(command)
            self.assertTrue(replay.replayed)
            self.assertEqual(replay.committed, first_receipt.committed)
            self.assertEqual(replay.committed["resource_id"], first)
            self.assertEqual(immutable_rows()["resource_uses"], before["resource_uses"])

    def test_current_unknown_legacy_reservations_remain_visible_without_write_validation(self):
        with ExitStack() as _owned_store_lifetimes:
            for index, raw in enumerate((None, "opaque-legacy", "{unsupported")):
                rid = f"visible-legacy-{index}"
                self.f.store.write(lambda db: db.execute(
                    "INSERT INTO resources (resource_id, host_id, kind, physical_identity, created_at) "
                    "VALUES (?, 'synthetic-host', 'workspace', ?, 'ts')", (rid, raw)))
                use = self.owned_use(rid, self.p, use_id=f"visible-use-{index}")
                before = self.rows()
                for fixture in (self.f, _owned_store_lifetimes.enter_context(make_service(self.f.directory, self.f.clock))):
                    current = fixture.service.read_plan(self.p.committed_metadata.plan_id).coordination.reservations
                    reservation = next(view for view in current if view.use_id == use)
                    self.assertEqual(reservation.resource_ids, (rid,))
                    self.assertTrue(reservation.writer_active)
                    self.assertTrue(reservation.unresolved)
                    page = fixture.service.read_coordination(self.p.committed_metadata.plan_id)
                    self.assertIn(reservation, tuple(item.value for item in page.items if item.kind == "reservation"))
                self.assertEqual(self.rows(), before)
                with self.assertRaises(DomainError) as raised:
                    self.resolve(context(rid))
                self.assertEqual(raised.exception.code, "held")

    def test_current_reservation_projection_fails_closed_for_corrupt_redirect_chains(self):
        with ExitStack() as _owned_store_lifetimes:
            for index, corruption in enumerate(("active-edge", "retired-without-edge", "cross-kind", "cross-host")):
                with self.subTest(corruption=corruption):
                    f = _owned_store_lifetimes.enter_context(make_service(self.f.directory / f"current-corrupt-{index}", self.f.clock))
                    p = f.create()
                    rid = f.register(workspace("/synthetic/current-corrupt", 904), p)
                    def seed(db):
                        db.execute("INSERT INTO absent_host_work (absent_work_id, host_id, resource_id, scope,declared_identity,producer_id) "
                                   "VALUES ('current-corrupt-work', 'synthetic-host', ?, 'workspace','synthetic-corrupt-worker','migration:current-corrupt-work')", (rid,))
                        db.execute("INSERT INTO absent_host_work_members VALUES('current-corrupt-work',?,'workspace')",(rid,))
                        db.execute("INSERT INTO resource_uses (use_id, resource_id, absent_work_id, scope, status) "
                                   "VALUES ('current-corrupt-use', ?, 'current-corrupt-work', 'workspace', 'unresolved')", (rid,))
                        db.execute("UPDATE absent_host_work SET anchor_use_id='current-corrupt-use' WHERE absent_work_id='current-corrupt-work'")
                        value=dict(format=1,producer='migration',original_parent=['current-corrupt-work','synthetic-host',rid,'workspace','synthetic-corrupt-worker','unresolved',''],members=[dict(use_id='current-corrupt-use',resource_id=rid,scope='workspace',created_at='')])
                        db.execute("INSERT INTO host_work_declarations VALUES('migration:current-corrupt-work','current-corrupt-work','migration','synthetic-corrupt-worker','unknown',?,'')",(json.dumps(value),))
                        if corruption != "active-edge":
                            db.execute("UPDATE resources SET status='retired' WHERE resource_id=?", (rid,))
                        if corruption != "retired-without-edge":
                            db.execute("INSERT INTO resources (resource_id, host_id, kind, created_at) VALUES "
                                       "('current-corrupt-target', ?, ?, 'ts')",
                                       ("foreign-host" if corruption == "cross-host" else "synthetic-host",
                                        "repository" if corruption == "cross-kind" else "workspace"))
                            db.execute("INSERT INTO resource_redirects (retired_id, survivor_id) "
                                       "VALUES (?, 'current-corrupt-target')", (rid,))
                    f.store.write(seed)
                    before = f.store.read(lambda db: tuple(db.iterdump()))
                    for reader in (f.service.read_plan, f.service.read_coordination):
                        with self.subTest(reader=reader.__name__):
                            with self.assertRaises(DomainError) as raised:
                                reader(p.committed_metadata.plan_id)
                            self.assertEqual(raised.exception.code, "held")
                    self.assertEqual(f.store.read(lambda db: tuple(db.iterdump())), before)

    def test_reconcile_rejects_missing_equivalence_contradictions_and_cycles_atomically(self):
        a = self.register(replace(workspace("/synthetic/a-unknown", 830), device=None, inode=None))
        b = self.register(replace(workspace("/synthetic/b-unknown", 830), device=None, inode=None))
        physical = workspace("/synthetic/canonical-valid", 830)
        for ids, claim, reason in (((a, b), replace(physical, device=None, inode=None), "no physical evidence"),
                                  ((a, b), physical, " "),
                                  ((a, b), replace(physical, host_id="other-host"), "foreign"),
                                  ((a, b), replace(physical, case_insensitive=True), "contradictory case"),
                                  ((a, b), replace(physical, kind="repository"), "contradictory kind"),
                                  ((a, "not-registered"), physical, "missing"),
                                  ((a, a), physical, "duplicate")):
            with self.subTest(reason=reason):
                before = self.rows()
                with self.assertRaises(DomainError):
                    self.reconcile(ids, claim, reason=reason)
                self.assertEqual(self.rows(), before)
        result = self.reconcile((a, b), physical)
        canonical_id = result.committed["resource_id"]
        retired = b if canonical_id == a else a
        with self.assertRaises(sqlite3.IntegrityError):
            self.f.store.write(lambda db: db.execute("INSERT INTO resource_redirects (retired_id, survivor_id) VALUES (?, ?)",
                                                    (canonical_id, retired)))
        self.assertIn(self.register(physical), (a, b))

    def test_ordinary_commands_authenticate_before_replay_and_allow_identity_during_hold(self):
        claim = workspace("/synthetic/current-owner", 840)
        command = self.f.command("register_resource", {"claim": claim}, receipt=self.p)
        first = self.f.service.execute(command)
        replay = self.f.service.execute(command)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.committed, replay.committed)
        self.assertIsNone(first.private)
        for altered, code in ((replace(command, orchestrator_token=None), "forbidden"),
                              (replace(command, attempt_token="synthetic-forbidden-extra"), "invalid"),
                              (replace(command, epoch="stale-epoch"), "stale_epoch")):
            with self.subTest(code=code):
                before = self.rows()
                with self.assertRaises(DomainError) as raised:
                    self.f.service.execute(altered)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(self.rows(), before)
        self.f.service.execute(self.f.command("takeover", {"expected_ownership_version": 1, "reason": "new owner"}, receipt=self.p))
        with self.assertRaises(DomainError) as raised:
            self.f.service.execute(command)
        self.assertEqual(raised.exception.code, "fenced")
        holder = self.f.create(creation_id="hold-owner")
        self.f.store.write(lambda db: db.execute("UPDATE installation SET store_hold=1 WHERE id=1"))
        a = self.register(replace(workspace("/synthetic/hold-left", 841), device=None, inode=None), holder)
        b = self.register(replace(workspace("/synthetic/hold-right", 841), device=None, inode=None), holder)
        reconcile = self.f.command("reconcile_resources", {"resource_ids": (a, b),
            "canonical_claim": workspace("/synthetic/hold-canonical", 841), "reason": "identity only"}, receipt=holder)
        self.f.service.execute(reconcile)
        self.assertTrue(self.f.service.execute(reconcile).replayed)
        with self.assertRaises(DomainError) as raised:
            self.f.service.execute(replace(reconcile, attempt_token="synthetic-forbidden-extra"))
        self.assertEqual(raised.exception.code, "invalid")
        self.assertEqual(self.f.store.read(lambda db: db.execute("SELECT COUNT(*) FROM resource_uses").fetchone()[0]), 0)

    def test_private_import_identity_helper_never_creates_capabilities_or_execution_use(self):
        registry = self.resources()
        before = self.f.store.read(lambda db: tuple(db.execute("SELECT * FROM capabilities")))
        with plans.service_context("http://127.0.0.1:8790", self.f.clock):
            rid = self.f.store.write(lambda db: registry.register_claim(db, workspace("/synthetic/import", 850)))
        self.assertEqual(rid, self.register(workspace("/synthetic/import-alias", 850)))
        self.assertEqual(self.f.store.read(lambda db: tuple(db.execute("SELECT * FROM capabilities"))), before)
        self.assertEqual(self.f.store.read(lambda db: db.execute("SELECT COUNT(*) FROM resource_uses").fetchone()[0]), 0)
        with plans.service_context("http://127.0.0.1:8790", self.f.clock):
            with self.assertRaises(DomainError) as raised:
                self.f.store.write(lambda db: registry.register_claim(db, replace(workspace("/synthetic/import", 851), host_id="other-host")))
        self.assertEqual(raised.exception.code, "invalid")

    def test_duplicate_race_database_uniqueness_and_fault_rollback(self):
        with ExitStack() as _owned_store_lifetimes:
            gate = threading.Barrier(2)
            registry = self.resources()
            def register(index):
                local = _owned_store_lifetimes.enter_context(make_service(self.f.directory, self.f.clock))
                gate.wait(timeout=10)
                with plans.service_context("http://127.0.0.1:8790", self.f.clock):
                    return local.store.write(lambda db: registry.register_claim(db, workspace(f"/synthetic/race-{index}", 860)))
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(register, i) for i in (1, 2)]
                values = [future.result(timeout=15) for future in futures]
            self.assertEqual(values[0], values[1])
            with self.assertRaises(sqlite3.IntegrityError):
                self.f.store.write(lambda db: db.execute(
                    "INSERT INTO resources (resource_id, host_id, kind, repository_id, physical_identity, canonical_path, status, created_at) "
                    "SELECT 'duplicate-physical', host_id, kind, repository_id, physical_identity, '/synthetic/duplicate', status, created_at "
                    "FROM resources WHERE resource_id=?", (values[0],)))
            before = self.rows()
            def fault(db):
                registry.register_claim(db, workspace("/synthetic/rolled-back", 861))
                raise RuntimeError("synthetic fault after registration")
            with plans.service_context("http://127.0.0.1:8790", self.f.clock):
                with self.assertRaises(RuntimeError):
                    self.f.store.write(fault)
            self.assertEqual(self.rows(), before)

    def test_registration_does_not_stat_or_open_attested_host_paths(self):
        claim = workspace("/host-only-path-that-does-not-exist/resource", 870)
        original_open = Path.open
        original_stat = Path.stat
        def checked_open(path, *args, **kwargs):
            if str(path).startswith("/host-only-path-that-does-not-exist"):
                raise AssertionError("service tried to open a host-attested path")
            return original_open(path, *args, **kwargs)
        def checked_stat(path, *args, **kwargs):
            if str(path).startswith("/host-only-path-that-does-not-exist"):
                raise AssertionError("service tried to stat a host-attested path")
            return original_stat(path, *args, **kwargs)
        with mock.patch.object(Path, "open", checked_open), mock.patch.object(Path, "stat", checked_stat):
            self.assertIsInstance(self.register(claim), str)

    def test_sdk_resource_tools_validate_kind_capabilities_and_safe_public_outputs(self):
        from adaptive_planning.transport import build_mcp
        server = build_mcp(self.f.service)
        raw = plans.to_jsonable(self.f.command("register_resource", {
            "claim": workspace("/synthetic/private-claim-marker", 880)}, receipt=self.p))
        before = self.rows()
        async def call(name, command):
            return await server.call_tool(name, {"command": command})
        result = asyncio.run(call("register_resource", raw))
        self.assertFalse(result.is_error, "the resource semantic tool must be enabled")
        public = json.dumps(result.structured_content)
        self.assertNotIn(raw["orchestrator_token"], public)
        self.assertNotIn("private-claim-marker", public)
        self.assertIn("resource_id", result.structured_content["committed"])
        self.assertIsNone(result.structured_content["private"])
        events = plans.to_jsonable(self.f.service.read_events(self.p.committed_metadata.plan_id))
        self.assertNotIn(raw["orchestrator_token"], json.dumps(events))
        self.assertNotIn("private-claim-marker", json.dumps(events))
        a = self.register(replace(workspace("/synthetic/sdk-a", 881), device=None, inode=None))
        b = self.register(replace(workspace("/synthetic/sdk-b", 881), device=None, inode=None))
        reconcile = plans.to_jsonable(self.f.command("reconcile_resources", {"resource_ids": (a, b),
            "canonical_claim": workspace("/synthetic/private-reconcile-marker", 881),
            "reason": "host-attested same physical identity"}, receipt=self.p))
        merged = asyncio.run(call("reconcile_resources", reconcile))
        self.assertFalse(merged.is_error)
        self.assertNotIn("private-reconcile-marker", json.dumps(merged.structured_content))
        for name, command in (("register_resource", reconcile), ("reconcile_resources", raw)):
            frozen = self.rows()
            invalid = asyncio.run(call(name, command))
            self.assertTrue(invalid.is_error)
            self.assertEqual(invalid.structured_content["error"]["code"], "invalid")
            self.assertEqual(self.rows(), frozen)
        for name, source in (("register_resource", raw), ("reconcile_resources", reconcile)):
            for change, expected in (({"attempt_token": "synthetic-extra-secret-marker"}, "invalid"),
                                     ({"peer-secret-marker": "synthetic-extra-secret-marker"}, "invalid"),
                                     ({"epoch": "stale-resource-epoch"}, "stale_epoch"),
                                     ({"orchestrator_token": None}, "forbidden")):
                command = copy.deepcopy(source)
                command.update(change)
                frozen = self.rows()
                rejected = asyncio.run(call(name, command))
                self.assertTrue(rejected.is_error)
                self.assertEqual(rejected.structured_content["error"]["code"], expected)
                self.assertNotIn("synthetic-extra-secret-marker", json.dumps(rejected.structured_content))
                self.assertNotIn("peer-secret-marker", json.dumps(rejected.structured_content))
                self.assertEqual(self.rows(), frozen)
        self.assertNotEqual(self.rows(), before)


class TestOwnedListenerRestoration(unittest.TestCase):
    def exercise(self, failure=None):
        from starlette.applications import Starlette
        from tests import test_service_sdk as sdk

        access = logging.getLogger("uvicorn.access")
        old_handlers = tuple(access.handlers)
        old_propagate = access.propagate
        marker = logging.Filter()
        handler_marker = logging.Filter()
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(handler_marker)
        access.addHandler(handler)
        access.addFilter(marker)
        access.propagate = True

        def state():
            return (access.level, access.disabled, access.propagate,
                    tuple(access.handlers), tuple(access.filters),
                    tuple((item, tuple(item.filters), item.level, item.formatter)
                          for item in access.handlers))

        expected = state()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)

        @asynccontextmanager
        async def failed_startup(app):
            raise RuntimeError("owned fixture startup failed")
            yield

        app = Starlette(lifespan=failed_startup) if failure == "startup" else Starlette()
        original_shutdown = sdk.shutdown_probe_server

        async def failed_settlement(server, task):
            await original_shutdown(server, task)
            raise RuntimeError("owned fixture settlement failed")

        async def run():
            initial_tasks = set(asyncio.all_tasks())
            with ExitStack() as stack:
                if failure == "construction":
                    stack.enter_context(mock.patch.object(sdk.uvicorn, "Server", side_effect=
                        RuntimeError("owned fixture construction failed")))
                if failure == "settlement":
                    stack.enter_context(mock.patch.object(sdk, "shutdown_probe_server", failed_settlement))
                try:
                    async with sdk.running_service(app, sock) as server:
                        self.assertTrue(server.started)
                        if failure == "body":
                            raise RuntimeError("owned fixture body failed")
                finally:
                    if failure != "startup":
                        self.assertEqual(set(asyncio.all_tasks()), initial_tasks)

        try:
            if failure is None:
                asyncio.run(run())
            elif failure == "startup":
                # Uvicorn exits on a failed ASGI lifespan startup; the fixture
                # still owns logger restoration and listener closure.
                with self.assertRaises(SystemExit):
                    asyncio.run(run())
            else:
                expected_error = f"owned fixture {failure} failed"
                with self.assertRaisesRegex(RuntimeError, expected_error):
                    asyncio.run(run())
            self.assertEqual(state(), expected)
            self.assertEqual(sock.fileno(), -1)
        finally:
            sock.close()
            for item in tuple(access.handlers):
                access.removeHandler(item)
            for item in old_handlers:
                access.addHandler(item)
            access.propagate = old_propagate
            access.removeFilter(marker)
            handler.close()
            stream.close()

    def test_owned_listener_preserves_access_logging_after_success(self):
        self.exercise()

    def test_owned_listener_preserves_access_logging_after_body_failure(self):
        self.exercise("body")

    def test_owned_listener_preserves_access_logging_after_construction_failure(self):
        self.exercise("construction")

    def test_owned_listener_preserves_access_logging_after_startup_failure(self):
        self.exercise("startup")

    def test_owned_listener_preserves_access_logging_after_settlement_failure(self):
        self.exercise("settlement")


if __name__ == "__main__":
    unittest.main()
