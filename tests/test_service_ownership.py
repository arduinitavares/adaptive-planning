"""Orchestrator grants, takeover, capability matrix and fenced bootstrap replay."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import stat
import threading
import traceback
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from adaptive_planning import ownership, plans
from adaptive_planning.types import BOOTSTRAP_KINDS, ORCHESTRATOR_KINDS, WORKER_KINDS, DomainError
from tests.service_fixtures import checkpoint, draft, make_service
from tests.test_service_plans import dump, insert_attempt, insert_hold, insert_owned_use, insert_store_hold

OWNED_TABLES = ("attempts", "resource_uses", "holds", "reviews", "result_versions", "steps", "resources")


class TestService(unittest.TestCase):
    def setUp(self):
        self._directory = TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name)
        self.f = make_service(self.path)
        self.p = self.f.create()
        self.plan_id = self.p.current_metadata.plan_id

    # ------------------------------------------------------------------ helpers
    def takeover_command(self, receipt, *, key=None, expected=None, fixture=None, reason="synthetic explicit takeover"):
        return (fixture or self.f).command(
            "takeover",
            {"expected_ownership_version": expected if expected is not None else receipt.committed_metadata
             .ownership_version, "reason": reason},
            receipt=receipt, key=key)

    def takeover(self, receipt, **kwargs):
        return self.f.service.execute(self.takeover_command(receipt, **kwargs))

    def revise(self, receipt, reason="revision", **kwargs):
        base = self.f.service.read_plan(self.plan_id).draft
        return self.f.command(
            "revise_plan",
            {"draft": base, "reason": reason, "authorization_change": None, "retire_steps": ()},
            receipt=receipt, **kwargs)

    def metadata(self):
        return self.f.service.read_plan(self.plan_id).metadata

    def events(self):
        return self.f.service.read_events(self.plan_id, limit=1000)

    def raises(self, command):
        with self.assertRaises(DomainError) as ctx:
            self.f.service.execute(command)
        return ctx.exception

    def orchestrator_rows(self):
        return self.f.store.read(lambda db: db.execute(
            "SELECT secret_hash, revoked FROM capabilities WHERE plan_id = ? AND role = 'orchestrator' "
            "ORDER BY rowid", (self.plan_id,)).fetchall())

    def assert_absent(self, secret, text, where):
        self.assertFalse(secret in text, f"a synthetic secret leaked into {where}")

    # ------------------------------------------------------------------ approved acceptance checks
    def test_concurrent_takeover_one_winner(self):
        other = make_service(self.path)
        version = self.p.current_metadata.ownership_version
        commands = [
            (self.f, dataclasses.replace(self.takeover_command(self.p, key="race-a"), actor="contender-a")),
            (other, dataclasses.replace(self.takeover_command(self.p, key="race-b", fixture=other),
                                        actor="contender-b")),
        ]
        barrier = threading.Barrier(2, timeout=30)

        def contend(fixture, command):
            barrier.wait()
            try:
                return fixture.service.execute(command)
            except DomainError as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = [f.result(timeout=60) for f in [pool.submit(contend, fx, cmd) for fx, cmd in commands]]
        winners = [o for o in outcomes if not isinstance(o, DomainError)]
        losers = [o for o in outcomes if isinstance(o, DomainError)]
        self.assertEqual((len(winners), len(losers)), (1, 1))
        self.assertEqual(losers[0].code, "conflict")
        self.assertEqual(losers[0].current_metadata.ownership_version, version + 1)

        winner = winners[0]
        self.assertEqual(winner.committed_metadata.ownership_version, version + 1)
        self.assertEqual(self.metadata().ownership_version, version + 1)
        owner = self.f.service.read_plan(self.plan_id).coordination.ownership_actor
        self.assertEqual(owner, "contender-a" if outcomes[0] is winner else "contender-b")
        live = [row for row in self.orchestrator_rows() if row[1] == 0]
        self.assertEqual(live, [(ownership.hash_token(winner.private.orchestrator_token), 0)])
        self.assertEqual([e.kind for e in self.events()].count("takeover"), 1)
        self.f.service.execute(self.revise(winner, "winner can revise"))
        self.assertEqual(self.raises(self.revise(self.p, "original owner")).code, "fenced")

    def test_lost_bootstrap_response_current_grant_only(self):
        original = self.f.create(key="lost-create")
        again = self.f.create(key="lost-create")
        self.assertTrue(again.replayed)
        self.assertEqual(again.private, original.private)
        self.assertEqual(again.private.orchestrator_token, original.private.orchestrator_token)

        takeover = self.takeover_command(original)
        owner = self.f.service.execute(takeover)
        retry = self.f.create(key="lost-create")
        self.assertIsNone(retry.private)
        self.assertEqual(retry.committed_metadata.plan_id, original.committed_metadata.plan_id)

        lost_takeover = self.f.service.execute(takeover)
        self.assertTrue(lost_takeover.replayed)
        self.assertEqual(lost_takeover.private, owner.private)
        self.assertNotEqual(owner.private.orchestrator_token, original.private.orchestrator_token)
        self.takeover(owner, key="second-takeover")
        superseded = self.f.service.execute(takeover)
        self.assertTrue(superseded.replayed)
        self.assertIsNone(superseded.private)
        self.assertEqual(superseded.committed, owner.committed)
        self.assertEqual(superseded.committed_metadata, owner.committed_metadata)

    def test_fenced_mutation_cannot_replay(self):
        committed = self.revise(self.p, "committed before takeover")
        self.f.service.execute(committed)
        owner = self.takeover(self.p)
        before = dump(self.f.store)
        old_revise = self.revise(self.p, "after takeover")
        error = self.raises(old_revise)
        self.assertEqual(error.code, "fenced")
        self.assertEqual(self.raises(committed).code, "fenced")
        self.assertEqual(dump(self.f.store), before)
        self.f.service.execute(self.revise(owner, "new owner continues"))

    def test_read_event_repr_secret_redaction(self):
        owner = self.takeover(self.p)
        old_token, new_token = self.p.private.orchestrator_token, owner.private.orchestrator_token
        revise = self.revise(owner, "visible revision")
        with self.assertNoLogs():
            errors = [self.raises(self.revise(self.p, "fenced attempt")),
                      self.raises(dataclasses.replace(revise, orchestrator_token="synthetic-bogus-token")),
                      self.raises(dataclasses.replace(revise, attempt_token=new_token))]
            receipt = self.f.service.execute(revise)
            view = self.f.service.read_plan(self.plan_id)
            surfaces = {
                "plan view": repr(view), "plan list": repr(self.f.service.list_plans()),
                "events": repr(self.events()), "coordination": repr(self.f.service.read_coordination(self.plan_id)),
                "ready": repr(self.f.service.list_ready(self.plan_id)),
                "service info": repr(self.f.service.read_service_info()),
                "dashboard url": view.metadata.dashboard_url,
                "receipts": repr((self.p, owner, receipt)),
                "committed": json.dumps(dict(self.p.committed)) + json.dumps(dict(owner.committed)),
                "commands": repr((revise, self.takeover_command(owner, key="repr-takeover"))),
                "grants": repr((self.p.private, owner.private)),
            }
            for index, error in enumerate(errors):
                surfaces[f"error {index}"] = (repr(error) + str(error) + repr(error.current_metadata)
                                              + "".join(traceback.format_exception(error)))
        for secret in (old_token, new_token):
            for where, text in surfaces.items():
                self.assert_absent(secret, text, where)

        def scan(db):
            leaks = []
            for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
                if name.startswith("sqlite_"):
                    continue
                text = json.dumps(db.execute(f"SELECT * FROM {name}").fetchall(), default=str)
                leaks += [name for secret in (old_token, new_token) if secret in text]
            return sorted(set(leaks))
        self.assertEqual(self.f.store.read(scan), ["requests"])

    # ------------------------------------------------------------------ exact capability matrix
    def test_kind_capability_matrix_checks_presence_and_extras(self):
        def code(kind, relay, orchestrator, attempt):
            try:
                ownership.require_kind_capabilities(kind, relay, orchestrator, attempt)
            except DomainError as error:
                return error.code
            return None

        for kind in sorted(BOOTSTRAP_KINDS):
            self.assertIsNone(code(kind, None, None, None), kind)
            for tokens in (("o", None), (None, "a"), ("o", "a"), ("", None)):
                self.assertEqual(code(kind, None, *tokens), "invalid", (kind, tokens))
        for kind in sorted(WORKER_KINDS):
            self.assertIsNone(code(kind, None, None, "a"), kind)
            self.assertEqual(code(kind, None, None, None), "forbidden", kind)
            self.assertEqual(code(kind, None, None, ""), "forbidden", kind)
            self.assertEqual(code(kind, None, "o", "a"), "invalid", kind)
            self.assertEqual(code(kind, None, "o", None), "invalid", kind)
            self.assertIsNone(code(kind, "relay-1", "o", "a"), kind)
            self.assertEqual(code(kind, "relay-1", None, "a"), "forbidden", kind)
            self.assertEqual(code(kind, "relay-1", "o", None), "forbidden", kind)
            self.assertEqual(code(kind, "relay-1", None, None), "forbidden", kind)
        for kind in sorted(ORCHESTRATOR_KINDS):
            self.assertIsNone(code(kind, None, "o", None), kind)
            self.assertEqual(code(kind, None, None, None), "forbidden", kind)
            self.assertEqual(code(kind, None, "", None), "forbidden", kind)
            self.assertEqual(code(kind, None, "o", "a"), "invalid", kind)
            self.assertEqual(code(kind, None, None, "a"), "invalid", kind)
        self.assertEqual(code("unknown_kind", None, "o", None), "invalid")

    def test_relay_is_read_from_worker_payloads_and_missing_attempt_grant_is_forbidden(self):
        direct = {"origin_worker": "worker-a", "relay": None, "observed_activity": "synthetic"}
        relayed = dict(direct, relay="relay-1")
        for kind in ("start_step", "report_activity"):
            self.assertIsNone(ownership.command_relay(self.f.command(kind, direct, receipt=self.p)), kind)
            self.assertEqual(ownership.command_relay(self.f.command(kind, relayed, receipt=self.p)), "relay-1")
        for relay in (None, "relay-2"):
            command = self.f.command("record_checkpoint", {"checkpoint": checkpoint(relay=relay)}, receipt=self.p)
            self.assertEqual(ownership.command_relay(command), relay)
        self.assertIsNone(ownership.command_relay(self.revise(self.p)))
        before = dump(self.f.store)
        self.assertEqual(self.raises(self.f.command("start_step", relayed, receipt=self.p)).code, "forbidden")
        self.assertEqual(dump(self.f.store), before)

    def test_service_rejects_matrix_violations_before_any_state_change(self):
        before = dump(self.f.store)
        revise = self.revise(self.p)
        cases = {
            "orchestrator kind with attempt token": (dataclasses.replace(revise, attempt_token="a"), "invalid"),
            "orchestrator kind without token": (dataclasses.replace(revise, orchestrator_token=None), "forbidden"),
            "takeover with orchestrator token": (dataclasses.replace(
                self.takeover_command(self.p), orchestrator_token=self.p.private.orchestrator_token), "invalid"),
            "takeover with attempt token": (dataclasses.replace(
                self.takeover_command(self.p), attempt_token="a"), "invalid"),
            "create with orchestrator token": (dataclasses.replace(
                self.f.command("create_plan", {"creation_id": "x", "draft": draft(), "new_plan_intent": "x"}),
                orchestrator_token=self.p.private.orchestrator_token), "invalid"),
        }
        for label, (command, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(self.raises(command).code, expected)
        self.assertEqual(dump(self.f.store), before)

    def test_ordinary_mutators_require_ownership_version_before_replay(self):
        committed = self.revise(self.p, "committed")
        self.f.service.execute(committed)
        before = dump(self.f.store)
        for command in (dataclasses.replace(committed, ownership_version=None),
                        dataclasses.replace(self.revise(self.p, "fresh"), ownership_version=None)):
            error = self.raises(command)
            self.assertEqual(error.code, "invalid")
        self.assertEqual(dump(self.f.store), before)

    def test_authentication_and_generation_precede_exact_replay_which_precedes_new_command_checks(self):
        command = self.revise(self.p, "first")
        first = self.f.service.execute(command)
        self.f.service.execute(self.revise(self.p, "second"))
        events_before = len(self.events())

        replay = self.f.service.execute(command)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.committed, first.committed)
        self.assertEqual(replay.committed_metadata.revision, 2)
        self.assertEqual(replay.current_metadata.revision, 3)
        self.assertEqual(command.expected_revision, 1)

        self.assertEqual(self.raises(dataclasses.replace(command, orchestrator_token="synthetic-bogus")).code,
                         "forbidden")
        self.assertEqual(self.raises(dataclasses.replace(command, generation="other-generation")).code, "stale")
        self.assertEqual(self.raises(dataclasses.replace(command, generation=None)).code, "invalid")
        self.assertEqual(self.raises(dataclasses.replace(command, epoch="epoch-other")).code, "stale_epoch")
        changed = self.revise(self.p, "different content", key=command.key)
        self.assertEqual(self.raises(changed).code, "conflict")
        self.assertEqual(len(self.events()), events_before)

    # ------------------------------------------------------------------ takeover semantics
    def test_takeover_checks_target_generation_and_expected_version_atomically(self):
        before = dump(self.f.store)
        stale_version = self.takeover_command(self.p, expected=self.p.committed_metadata.ownership_version + 5)
        error = self.raises(stale_version)
        self.assertEqual(error.code, "conflict")
        self.assertEqual(error.current_metadata.ownership_version, 1)
        wrong_generation = dataclasses.replace(self.takeover_command(self.p), generation="other-generation")
        error = self.raises(wrong_generation)
        self.assertEqual(error.code, "stale")
        self.assertEqual(error.current_metadata.ownership_version, 1)
        self.assertEqual(self.raises(dataclasses.replace(self.takeover_command(self.p), generation=None)).code,
                         "invalid")
        self.assertEqual(self.raises(dataclasses.replace(self.takeover_command(self.p), plan_id=None)).code,
                         "invalid")
        self.assertEqual(self.raises(dataclasses.replace(self.takeover_command(self.p), plan_id="plan-missing")).code,
                         "not_found")
        self.assertEqual(self.raises(dataclasses.replace(self.takeover_command(self.p), epoch="epoch-other")).code,
                         "stale_epoch")
        self.assertEqual(dump(self.f.store), before)

    def test_takeover_issues_current_grant_event_and_request_receipt_together(self):
        sequence = self.f.store.get_store_sequence()
        self.assertIsNotNone(self.f.service.read_plan(self.plan_id).legacy_publication)
        owner = self.takeover(self.p, key="take-1")
        self.assertFalse(owner.replayed)
        self.assertEqual(owner.committed_metadata.ownership_version, 2)
        self.assertEqual(owner.current_metadata, owner.committed_metadata)
        self.assertEqual(owner.committed_metadata.revision, self.p.committed_metadata.revision)
        self.assertEqual(owner.committed_metadata.generation, self.p.committed_metadata.generation)
        self.assertNotEqual(owner.private.orchestrator_token, self.p.private.orchestrator_token)
        self.assertIsNone(owner.private.attempt_token)
        self.assertEqual(len(owner.private.orchestrator_token), 43)
        self.assertEqual([row[1] for row in self.orchestrator_rows()], [1, 0])
        self.assertEqual(self.orchestrator_rows()[1][0], ownership.hash_token(owner.private.orchestrator_token))
        self.assertEqual(self.f.store.get_store_sequence(), sequence + 1)
        event = self.events()[-1]
        self.assertEqual((event.kind, event.actor), ("takeover", "synthetic-orchestrator"))
        self.assertEqual(event.payload["reason"], "synthetic explicit takeover")
        self.assertEqual(event.payload["ownership_version"], 2)
        stored = self.f.store.read(lambda db: db.execute(
            "SELECT plan_id FROM requests WHERE key = 'take-1'").fetchone())
        self.assertEqual(stored, (self.plan_id,))
        view = self.f.service.read_plan(self.plan_id)
        self.assertEqual(view.coordination.ownership_version, 2)
        self.assertEqual(view.coordination.ownership_actor, "synthetic-orchestrator")
        self.assertIsNotNone(view.legacy_publication)
        self.assertEqual(view.metadata.revision, self.p.committed_metadata.revision)

    def test_takeover_rolls_back_every_effect_on_failure(self):
        before = dump(self.f.store)
        with mock.patch.object(plans, "issue_orchestrator", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.takeover(self.p)
        self.assertEqual(dump(self.f.store), before)
        self.assertEqual(self.takeover(self.p).committed_metadata.ownership_version, 2)

    def test_takeover_preserves_claims_attempts_and_authority(self):
        insert_attempt(self.f.store, self.plan_id, "att-live", "a", "active")
        insert_owned_use(self.f.store, self.plan_id, "b")
        insert_hold(self.f.store, self.plan_id, "hold-1", "c")
        worker_secret = hashlib.sha256(b"synthetic-worker-secret").hexdigest()
        self.f.store.write(lambda db: db.execute(
            "INSERT INTO capabilities (capability_id, epoch, plan_id, role, secret_hash, valid_until, revoked, "
            "payload) VALUES ('cap-worker', ?, ?, 'attempt', ?, NULL, 0, '{\"attempt_id\": \"att-live\"}')",
            (self.f.epoch, self.plan_id, worker_secret)))

        def claims():
            return self.f.store.read(lambda db: (
                [(n, db.execute(f"SELECT * FROM {n} ORDER BY rowid").fetchall()) for n in OWNED_TABLES],
                db.execute("SELECT * FROM capabilities WHERE role != 'orchestrator'").fetchall(),
                db.execute("SELECT revision, generation, payload, updated_at FROM plans").fetchall()))
        before, authorization = claims(), self.f.service.read_plan(self.plan_id).draft.authorization
        owner = self.takeover(self.p)
        self.assertEqual(claims(), before)
        view = self.f.service.read_plan(self.plan_id)
        self.assertEqual(view.draft.authorization, authorization)
        self.assertIn("att-live", [a.attempt_id for a in view.coordination.attempts])
        self.assertEqual(len(view.coordination.reservations), 1)
        self.assertEqual(len(view.coordination.holds), 1)
        self.assertEqual(owner.committed_metadata.revision, 1)

    def test_new_owner_must_supply_current_ownership_version_and_old_owner_is_fenced(self):
        owner = self.takeover(self.p)
        stale = dataclasses.replace(self.revise(owner, "stale ownership"), ownership_version=1)
        error = self.raises(stale)
        self.assertEqual(error.code, "conflict")
        self.assertEqual(error.current_metadata.ownership_version, 2)
        fenced = self.raises(self.revise(self.p, "old owner"))
        self.assertEqual(fenced.code, "fenced")
        self.assertEqual(self.raises(dataclasses.replace(
            self.revise(owner, "forged"), orchestrator_token="synthetic-unknown")).code, "forbidden")
        self.assertEqual(self.f.service.execute(self.revise(owner, "valid")).committed_metadata.revision, 2)

    def test_second_takeover_fences_previous_grant_but_keeps_exact_history(self):
        first = self.takeover(self.p, key="first")
        second = self.takeover(first, key="second")
        self.assertEqual(second.committed_metadata.ownership_version, 3)
        self.assertEqual([row[1] for row in self.orchestrator_rows()], [1, 1, 0])
        self.assertEqual(self.raises(self.revise(first, "superseded owner")).code, "fenced")
        self.assertEqual(self.raises(self.takeover_command(self.p, key="third")).code, "conflict")

    # ------------------------------------------------------------------ replay, restart, epochs, permissions
    def test_restart_preserves_lost_response_current_token_and_protects_storage(self):
        original = self.f.create(key="restart-create")
        reopened = make_service(self.path)
        retry = reopened.create(key="restart-create")
        self.assertTrue(retry.replayed)
        self.assertEqual(retry.private.orchestrator_token, original.private.orchestrator_token)
        self.assertEqual(retry.committed, original.committed)

        owner = reopened.service.execute(self.takeover_command(original, fixture=reopened, key="restart-take"))
        third = make_service(self.path)
        self.assertIsNone(third.create(key="restart-create").private)
        again = third.service.execute(self.takeover_command(original, fixture=third, key="restart-take"))
        self.assertEqual(again.private.orchestrator_token, owner.private.orchestrator_token)

        db_path = self.path / "planning.sqlite"
        self.assertEqual(stat.S_IMODE(os.stat(db_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o700)
        public = repr(third.service.read_plan(self.plan_id)) + repr(third.service.read_events(self.plan_id))
        self.assertFalse(owner.private.orchestrator_token in public)

    def test_creation_identity_with_new_key_follows_current_grant_only_policy(self):
        current = self.f.create(key="other-key")
        self.assertTrue(current.replayed)
        self.assertEqual(current.private, self.p.private)
        self.takeover(self.p)
        superseded = self.f.create(key="yet-another-key")
        self.assertTrue(superseded.replayed)
        self.assertIsNone(superseded.private)
        self.assertEqual(superseded.committed_metadata, self.p.committed_metadata)
        self.assertEqual(superseded.current_metadata.ownership_version, 2)
        self.assertIsNone(self.f.create(key="other-key").private)

    def test_superseded_public_receipt_keeps_committed_and_current_metadata_separate(self):
        original = self.f.create(key="metadata-create")
        self.f.service.execute(self.revise(original, "advance"))
        owner = self.takeover(original, key="metadata-take", expected=1)
        retry = self.f.create(key="metadata-create")
        self.assertIsNone(retry.private)
        self.assertEqual(retry.committed_metadata, original.committed_metadata)
        self.assertEqual(retry.committed_metadata.revision, 1)
        self.assertEqual(retry.committed_metadata.ownership_version, 1)
        self.assertEqual(retry.current_metadata, self.metadata())
        self.assertEqual((retry.current_metadata.revision, retry.current_metadata.ownership_version), (2, 2))
        self.assertEqual(owner.committed_metadata.ownership_version, 2)

    def test_old_epoch_never_receives_or_authorizes_grant_material(self):
        create = self.f.command("create_plan", {
            "creation_id": "epoch-create", "draft": dataclasses.replace(draft(), objective="Epoch plan"),
            "new_plan_intent": "synthetic epoch plan"}, key="epoch-create")
        other = self.f.service.execute(create)
        revise = self.revise(self.p, "before rotation")
        secrets = (self.p.private.orchestrator_token, other.private.orchestrator_token)
        self.f.store.write(lambda db: db.execute("UPDATE installation SET epoch = 'epoch-rotated'"))
        for command in (create, revise):
            error = self.raises(command)
            self.assertEqual(error.code, "stale_epoch")
            for secret in secrets:
                self.assert_absent(secret, repr(error) + str(error), "stale epoch error")
        self.assertEqual(self.raises(dataclasses.replace(revise, epoch="epoch-rotated")).code, "forbidden")
        fresh = self.f.service.execute(dataclasses.replace(create, epoch="epoch-rotated"))
        self.assertFalse(fresh.replayed)
        self.assertNotIn(fresh.private.orchestrator_token, secrets)

    def test_takeover_and_authenticated_replay_remain_available_during_store_hold(self):
        committed = self.revise(self.p, "before hold")
        self.f.service.execute(committed)
        insert_store_hold(self.f.store, self.f.epoch)
        self.assertTrue(self.f.service.read_service_info().store_hold)
        owner = self.takeover(self.p, key="hold-take")
        self.assertEqual(owner.committed_metadata.ownership_version, 2)
        self.assertEqual(self.raises(committed).code, "fenced")
        replay = self.f.service.execute(self.takeover_command(self.p, key="hold-take"))
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.private, owner.private)

    def test_reads_never_mutate_authority(self):
        owner = self.takeover(self.p)
        before = dump(self.f.store)
        self.f.service.read_service_info()
        self.f.service.list_plans(include_terminal=True)
        self.f.service.read_plan(self.plan_id)
        self.f.service.read_events(self.plan_id)
        self.f.service.list_ready(self.plan_id)
        self.f.service.read_coordination(self.plan_id)
        self.assertEqual(dump(self.f.store), before)
        self.assertEqual(self.orchestrator_rows()[-1][0], ownership.hash_token(owner.private.orchestrator_token))


if __name__ == "__main__":
    unittest.main()
