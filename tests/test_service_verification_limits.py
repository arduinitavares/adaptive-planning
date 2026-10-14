"""Safety reduction at the real retained execution-history content limit."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import asyncio
import hashlib
import json
import unittest
import uuid

from adaptive_planning import plans, resources
from adaptive_planning.core import MAX_FILE_BYTES, MAX_TEXT_LEN
from adaptive_planning.transport import build_mcp
from adaptive_planning.types import DomainError, ResourceClaim
from tests.service_fixtures import checkpoint, context, make_service, step, workspace


class TestVerificationHistoryLimit(unittest.TestCase):
    def setUp(self):
        self.f = self.enterContext(make_service())
        # Legal public text inputs make the retained boundary economical to exercise.
        definition = replace(step("a", review_required=True),
                             title="🧪" * MAX_TEXT_LEN, completion_check="🧪" * MAX_TEXT_LEN)
        self.p = self.f.create(steps=(definition, step("b")))
        self.pid = self.p.current_metadata.plan_id
        self.rid = self.f.register(workspace("/synthetic/verifier-limit", 980), self.p)
        self.ext = self.f.register(ResourceClaim("synthetic-host", "external", None, None, None,
                                               False, None, "verifier-limit"), self.p)
        self.execution = context(self.rid, external_ids=(self.ext,))
        self.second_rid = self.f.register(workspace("/synthetic/second-verifier-limit", 981), self.p)
        self.second_ext = self.f.register(ResourceClaim("synthetic-host", "external", None, None, None,
                                                      False, None, "second-verifier-limit"), self.p)
        self.second_execution = context(self.second_rid, external_ids=(self.second_ext,))
        self.a = self.f.start(self.p, self.f.assign(self.p))
        self.f.submit(self.p, self.a, checkpoint())

    def reserve(self, verifier="final-reviewer", *, execution=None):
        return self.f.service.execute(self.f.command("reserve_verification", {
            "step_id": "a", "verifier": verifier, "context": execution or self.execution,
            "handoff_from": None, "stopped_reason": None}, receipt=self.p))

    def release_command(self, anchor, reason, *, key=None, resource_ids=None):
        return self.f.command("release_resource_use", {
            "use_id": anchor, "resource_ids": resource_ids or (self.rid, self.ext), "stopped": True,
            "reason": reason}, receipt=self.p, key=key)

    def data(self):
        return self.f.store.read(lambda db: json.loads(db.execute(
            "SELECT payload FROM attempts WHERE attempt_id=?", (self.a.committed["attempt_id"],)).fetchone()[0]))

    def seed_repeated_public_history(self):
        """Fast-forward repetitions of a real reserve/release pair, without mocked limits.

        Each repetition uses the same submitted target and released resources. Only
        producer-generated identities, times, sequences and the legal stop reason
        vary; every immutable group, physical member and original audit link stays
        complete. The final public reservation validates this entire history.
        """
        first = self.reserve("🧪" * MAX_TEXT_LEN)
        anchor = first.committed["use_id"]
        self.f.service.execute(self.release_command(anchor, "Stopped"))

        def seed(db):
            data = json.loads(db.execute("SELECT payload FROM attempts WHERE attempt_id=?",
                                        (self.a.committed["attempt_id"],)).fetchone()[0])
            namespace = data["execution_uses"]
            template = deepcopy(namespace["groups"][anchor])
            events = [json.loads(raw) for (raw,) in db.execute(
                "SELECT payload FROM events WHERE plan_id=? AND kind IN ('reserve_verification','release_resource_use') "
                "ORDER BY sequence", (self.pid,))]
            revision, previous = db.execute("SELECT revision,updated_at FROM plans WHERE plan_id=?", (self.pid,)).fetchone()
            # Reserve room for two final public groups with short verifier labels.
            final = deepcopy(template)
            final["verifier"] = "final-reviewer"
            final["writer"]["state"] = "active"
            final["writer"]["reason"] = "Declared writable verification"
            addition = len(plans.canonical_bytes({anchor: final})) - 1
            target = MAX_FILE_BYTES - 2 * addition - 8000
            clones = []
            while True:
                group = deepcopy(template)
                new_anchor = str(uuid.uuid4())
                namespace["groups"][new_anchor] = group
                if len(plans.canonical_bytes(data)) > target:
                    del namespace["groups"][new_anchor]
                    break
                # Start with producer-shaped identities; fill the bounded reason below.
                group["anchor"] = new_anchor
                group["members"][0]["use_id"] = new_anchor
                for member in group["members"][1:]:
                    member["use_id"] = str(uuid.uuid4())
                clones.append(group)
            remaining = target - len(plans.canonical_bytes(data))
            for group in clones:
                # Leave character room for the prefix and a UTF-8 remainder.
                extra = min(remaining, 39956)
                quarters, remainder = divmod(extra, 4)
                group["writer"]["reason"] += "🧪" * quarters + "x" * remainder
                self.assertLessEqual(len(group["writer"]["reason"]), MAX_TEXT_LEN)
                remaining -= extra
            self.assertEqual(remaining, 0)
            for group in clones:
                created = (datetime.fromisoformat(previous) + timedelta(microseconds=1)).isoformat()
                stopped = (datetime.fromisoformat(created) + timedelta(microseconds=1)).isoformat()
                members = group["members"]
                ids = [item["use_id"] for item in members]
                for member in members:
                    member["created_at"] = created
                    db.execute("INSERT INTO resource_uses(use_id,resource_id,plan_id,attempt_id,scope,status,created_at) "
                               "VALUES(?,?,?,?,?,'released',?)", (member["use_id"], member["resource_id"], self.pid,
                               self.a.committed["attempt_id"], member["scope"], created))
                origin = deepcopy(events[0])
                origin["use_id"] = group["anchor"]
                origin["execution_transitions"][0].update(anchor=group["anchor"], use_ids=ids)
                plans._insert_event(db, self.pid, revision + 1, "reserve_verification",
                                    group["creation"]["actor"], created, {})
                group["creation"].update(sequence=plans._store_sequence(db), published_at=created)
                origin["verification_group"] = {key: value for key, value in group.items() if key != "writer"}
                db.execute("UPDATE events SET payload=? WHERE sequence=?",
                           (json.dumps(origin, ensure_ascii=False), group["creation"]["sequence"]))
                released = deepcopy(events[1])
                released.update(use_id=group["anchor"], use_ids=ids, reason=group["writer"]["reason"])
                released["execution_transitions"][0].update(anchor=group["anchor"], use_ids=ids,
                                                          reason=group["writer"]["reason"])
                plans._insert_event(db, self.pid, revision + 2, "release_resource_use",
                                    group["writer"]["actor"], stopped, released)
                group["writer"]["sequence"] = plans._store_sequence(db)
                revision, previous = revision + 2, stopped
            resources.save_execution_uses(db, self.pid, self.a.committed["attempt_id"], namespace)
            db.execute("UPDATE plans SET revision=?,updated_at=? WHERE plan_id=?", (revision, previous, self.pid))

        self.f.store.write(seed)

    def authority(self):
        # Hash private-free rows so failed rollback assertions never dump grant material.
        return self.f.store.read(lambda db: tuple(
            (table, hashlib.sha256(plans.canonical_bytes(list(db.execute(query)))).hexdigest())
            for table, query in {
                "plans": "SELECT * FROM plans ORDER BY rowid",
                "attempts": "SELECT * FROM attempts ORDER BY rowid",
                "uses": "SELECT * FROM resource_uses ORDER BY rowid",
                "events": "SELECT * FROM events ORDER BY sequence",
                "requests": "SELECT request_id,key,command_digest FROM requests ORDER BY rowid",
            }.items()))

    def test_admitted_verifier_cohort_releases_at_retained_history_limit(self):
        self.seed_repeated_public_history()
        reserved = self.reserve()
        anchor = reserved.committed["use_id"]
        second = self.reserve(execution=self.second_execution)
        second_anchor = second.committed["use_id"]
        before = self.data()
        self.assertLessEqual(len(plans.canonical_bytes(before)), MAX_FILE_BYTES)
        self.assertGreater(len(plans.canonical_bytes(before)), MAX_FILE_BYTES - MAX_TEXT_LEN)
        frozen = deepcopy(before["execution_uses"])
        old_events = self.f.store.read(lambda db: tuple(db.execute("SELECT * FROM events ORDER BY sequence")))
        reason = "🧪" * MAX_TEXT_LEN
        command = self.release_command(anchor, reason, key="limit-release")
        unchanged = self.authority()
        with self.assertRaises(DomainError) as invalid:
            self.f.service.execute(self.release_command(anchor, reason + "x"))
        self.assertEqual(invalid.exception.code, "limit_exceeded")
        self.assertEqual(self.authority(), unchanged)
        try:
            released = self.f.service.execute(command)
        except DomainError as error:
            self.fail(f"Admitted verifier release rejected: {error.code}: {error}")
        self.assertTrue(released.committed["released"])
        after = self.data()
        self.assertGreater(len(plans.canonical_bytes(after)), MAX_FILE_BYTES)
        after_frozen = deepcopy(after["execution_uses"])
        after_frozen["groups"][anchor]["writer"] = frozen["groups"][anchor]["writer"]
        self.assertEqual(after_frozen, frozen)
        self.assertEqual({key: value for key, value in after.items() if key != "execution_uses"},
                         {key: value for key, value in before.items() if key != "execution_uses"})
        ids = [member["use_id"] for member in frozen["groups"][anchor]["members"]]
        self.assertEqual(self.f.store.read(lambda db: tuple(db.execute(
            "SELECT status FROM resource_uses WHERE use_id IN (?,?) ORDER BY use_id", ids))),
                         (("released",), ("released",)))
        latest = self.f.store.read(lambda db: tuple(db.execute("SELECT * FROM events ORDER BY sequence")))
        self.assertEqual(latest[:-1], old_events)
        event = json.loads(self.f.store.read(lambda db: db.execute(
            "SELECT payload FROM events WHERE sequence=(SELECT MAX(sequence) FROM events)").fetchone()[0]))
        self.assertEqual(event["reason"], reason)
        self.assertEqual(event["execution_transitions"][0]["reason"], reason)
        settled = self.authority()
        self.assertTrue(self.f.service.execute(command).replayed)
        self.assertEqual(self.authority(), settled)
        # Safety reduction does not waive the admission cap for a new writer.
        with self.assertRaises(DomainError) as capped:
            self.reserve()
        self.assertEqual(capped.exception.code, "limit_exceeded")
        self.assertEqual(self.authority(), settled)
        # Even over the admission cap, a release must prove its original writer.
        aid = self.a.committed["attempt_id"]
        corrupt = deepcopy(after)
        corrupt["execution_uses"]["groups"][second_anchor]["writer"]["sequence"] = 1
        self.f.store.write(lambda db: db.execute("UPDATE attempts SET payload=? WHERE attempt_id=?",
                                              (json.dumps(corrupt, ensure_ascii=False), aid)))
        invalid_state = self.authority()
        second_command = self.release_command(second_anchor, reason,
                                              resource_ids=(self.second_rid, self.second_ext))
        with self.assertRaises(DomainError) as invalid_proof:
            self.f.service.execute(second_command)
        self.assertEqual(invalid_proof.exception.code, "invalid")
        self.assertEqual(self.authority(), invalid_state)
        self.f.store.write(lambda db: db.execute("UPDATE attempts SET payload=? WHERE attempt_id=?",
                                              (json.dumps(after, sort_keys=True, ensure_ascii=False), aid)))
        # The first stop enlarged retained history; the other admitted cohort still releases.
        second_release = self.f.service.execute(second_command)
        self.assertTrue(second_release.committed["released"])
        self.assertEqual(self.f.store.read(lambda db: db.execute(
            "SELECT COUNT(*) FROM resource_uses WHERE attempt_id=? AND status!='released'", (aid,)).fetchone()[0]), 0)
        proved = self.f.store.read(lambda db: resources.execution_uses(db, self.pid, aid)[0])
        self.assertEqual(proved["groups"][second_anchor]["writer"]["reason"], reason)
        for released_anchor in (anchor, second_anchor):
            proved["groups"][released_anchor]["writer"] = frozen["groups"][released_anchor]["writer"]
        self.assertEqual(proved, frozen)
        view = self.f.service.read_coordination(self.pid)
        self.assertLessEqual(len(plans.canonical_bytes(view)), MAX_FILE_BYTES)
        self.assertFalse(any(item.value.writer_active for item in view.items if item.kind == "reservation"))
        assigned = self.f.service.execute(self.f.command("assign_step", {
            "step_id": "b", "assignee": "next-writer", "context": self.execution}, receipt=self.p))
        assigned_view = next(item.value for item in self.f.service.read_coordination(self.pid).items
                             if item.kind == "attempt" and item.value.attempt_id == assigned.committed["attempt_id"])
        self.assertEqual(assigned_view.step_id, "b")


class TestReleaseEventSize(unittest.TestCase):
    def setUp(self):
        self.f = self.enterContext(make_service())
        self.p = self.f.create(steps=(step("a", review_required=True), step("b")))
        self.pid = self.p.current_metadata.plan_id
        self.rid = self.f.register(workspace("/synthetic/release-event-size", 982), self.p)
        self.a = self.f.start(self.p, self.f.assign(self.p))
        self.f.submit(self.p, self.a, checkpoint())

    def reserve(self, execution):
        return self.f.service.execute(self.f.command("reserve_verification", {
            "step_id": "a", "verifier": "event-size-reviewer", "context": execution,
            "handoff_from": None, "stopped_reason": None}, receipt=self.p))

    def assert_readable_release(self, command, expected_resources):
        arguments = {"command": plans.to_jsonable(command)}
        original = json.dumps(arguments["command"], sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False, allow_nan=False).encode("utf-8")
        original_digest = hashlib.sha256(original).hexdigest()
        anchor = command.payload["use_id"]
        aid = self.a.committed["attempt_id"]
        before = self.f.store.read(lambda db: resources.execution_uses(db, self.pid, aid)[0])
        group = deepcopy(before["groups"][anchor])
        ids = [member["use_id"] for member in group["members"]]
        server = build_mcp(self.f.service)

        def stored_request():
            return self.f.store.read(lambda db: db.execute(
                "SELECT command_digest,response_payload FROM requests WHERE epoch=? AND key=?",
                (command.epoch, command.key)).fetchone())

        async def exercise():
            released = await server.call_tool("release_resource_use", arguments)
            self.assertFalse(released.is_error, released.structured_content)
            self.assertTrue(released.structured_content["committed"]["released"])
            sequence = self.f.store.read(lambda db: db.execute(
                "SELECT MAX(sequence) FROM events WHERE plan_id=?", (self.pid,)).fetchone()[0])
            stored = stored_request()
            self.assertEqual(stored[0], original_digest)
            proved = self.f.store.read(lambda db: resources.execution_uses(db, self.pid, aid)[0])
            stopped = proved["groups"][anchor]
            self.assertEqual(stopped["writer"]["state"], "stopped")
            self.assertEqual(stopped["writer"]["sequence"], sequence)
            self.assertEqual(stopped["writer"]["reason"], command.payload["reason"])
            self.assertEqual({key: value for key, value in stopped.items() if key != "writer"},
                             {key: value for key, value in group.items() if key != "writer"})
            self.assertEqual(self.f.store.read(lambda db: tuple(db.execute(
                "SELECT status FROM resource_uses WHERE use_id IN (" + ",".join("?" for _ in ids) + ")",
                ids))), (("released",),) * len(ids))
            self.assertFalse(any(item.value.writer_active for item in
                                 self.f.service.read_coordination(self.pid).items if item.kind == "reservation"))

            replay = await server.call_tool("release_resource_use", arguments)
            self.assertFalse(replay.is_error, replay.structured_content)
            self.assertTrue(replay.structured_content["replayed"])
            self.assertEqual(replay.structured_content["committed"], released.structured_content["committed"])
            changed = deepcopy(arguments)
            changed["command"]["payload"]["resource_ids"] = expected_resources
            conflict = await server.call_tool("release_resource_use", changed)
            self.assertTrue(conflict.is_error)
            self.assertEqual(conflict.structured_content["error"]["code"], "conflict")
            self.assertEqual(stored_request(), stored)
            self.assertEqual(self.f.store.read(lambda db: plans._store_sequence(db)), sequence)
            self.assertEqual(hashlib.sha256(plans.canonical_bytes(command)).hexdigest(), original_digest)
            self.assertEqual(hashlib.sha256(plans.canonical_bytes(arguments["command"])).hexdigest(), original_digest)

            read = await server.call_tool("read_events", {"plan_id": self.pid, "after": sequence - 1, "limit": 1})
            self.assertFalse(read.is_error, read.structured_content)
            self.assertLessEqual(sum(len(block.text.encode("utf-8")) for block in read.content), MAX_FILE_BYTES)
            events = read.structured_content["events"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["sequence"], sequence)
            self.assertEqual(events[0]["kind"], "release_resource_use")
            payload = events[0]["payload"]
            self.assertEqual(payload["resource_ids"], expected_resources)
            self.assertEqual(payload["use_ids"], ids)
            self.assertTrue(payload["stopped"])
            self.assertEqual(payload["reason"], command.payload["reason"])
            self.assertEqual(payload["execution_transitions"][0]["reason"], command.payload["reason"])

        asyncio.run(exercise())

    def test_duplicate_heavy_stopped_release_is_readable_at_real_mcp_limit(self):
        # Repeating a legal UUID makes the old public event exceed the actual
        # 10 MiB read limit while the complete request stays below 12 MiB.
        reserved = self.reserve(context(self.rid))
        duplicates = (MAX_FILE_BYTES + 30000) // (len(self.rid) + 4) + 1
        command = self.f.command("release_resource_use", {
            "use_id": reserved.committed["use_id"], "resource_ids": (self.rid,) * duplicates,
            "stopped": True, "reason": "All synthetic verifier processes positively stopped."},
            receipt=self.p, key="duplicate-event-release")
        arguments = {"command": plans.to_jsonable(command)}
        wire_request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "release_resource_use", "arguments": arguments}}
        request_bytes = len(json.dumps(wire_request, ensure_ascii=False).encode("utf-8"))
        self.assertGreater(request_bytes, MAX_FILE_BYTES)
        self.assertLess(request_bytes, 12 * 1024 * 1024)
        self.assert_readable_release(command, [self.rid])

    def test_duplicate_alias_release_event_names_current_canonical_cohort_once(self):
        ext = self.f.register(ResourceClaim("synthetic-host", "external", None, None, None,
                                            False, None, "release-event-external"), self.p)
        alias = "synthetic-retained-release-alias"

        def coherent_alias(db):
            raw = db.execute("SELECT physical_identity FROM resources WHERE resource_id=?", (ext,)).fetchone()[0]
            db.execute("INSERT INTO resources(resource_id,host_id,kind,physical_identity,status,created_at) "
                       "VALUES(?,'synthetic-host','external',?,'retired',?)", (alias, raw, self.f.clock().isoformat()))
            db.execute("INSERT INTO resource_redirects(retired_id,survivor_id,created_at) VALUES(?,?,?)",
                       (alias, ext, self.f.clock().isoformat()))

        self.f.store.write(coherent_alias)
        reserved = self.reserve(context(self.rid, external_ids=(ext, alias)))
        command = self.f.command("release_resource_use", {
            "use_id": reserved.committed["use_id"], "resource_ids": (alias, self.rid, ext, alias, self.rid),
            "stopped": True, "reason": "Both synthetic verifier members positively stopped."},
            receipt=self.p, key="alias-event-release")
        self.assert_readable_release(command, sorted([self.rid, ext]))
