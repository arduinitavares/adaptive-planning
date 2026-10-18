"""Release contracts at actual SDK dispatch, registration and public boundaries."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from functools import update_wrapper
import gc
import hashlib
import json
import logging
from threading import Barrier
from typing import Any
import unittest
from unittest.mock import Mock, patch
import weakref

from pydantic import TypeAdapter

from adaptive_planning import plans, transport
from adaptive_planning.types import (
    BOOTSTRAP_KINDS, ORCHESTRATOR_KINDS, WORKER_KINDS, Command, DomainError,
    DocumentRef, Event, Metadata, MutationReceipt, PrivateGrant, ResourceClaim, Review,
)
from tests.service_fixtures import checkpoint, context, draft, make_service
from tests.test_service_http import CapturedLogs


READS = (
    ("read_service_info", "read_service_info", {}),
    ("list_plans", "list_plans", {}),
    ("read_plan", "read_plan", {"plan_id": None}),
    ("read_events", "read_events", {"plan_id": None}),
    ("list_ready", "list_ready", {"plan_id": None}),
    ("read_coordination", "read_coordination", {"plan_id": None}),
)
MUTATIONS = (
    "create_plan", "import_plan", "revise_plan", "takeover", "register_resource",
    "reconcile_resources", "assign_step", "bind_availability", "start_step",
    "report_activity", "record_checkpoint", "resolve_reassessment", "invalidate_result",
    "resolve_invalidation", "record_review", "accept_step", "reserve_verification",
    "release_resource_use", "reopen_attempt", "sweep_stale", "resolve_attempt",
    "append_late_evidence", "resolve_store_hold",
)
SENTINEL = "SYNTHETIC-PRIVATE-PROJECTION-LEAF-DO-NOT-EXPOSE"


@dataclass(frozen=True)
class AddedMetadata(Metadata):
    secret: str = SENTINEL


class AddedString(str):
    pass


class TestService(unittest.TestCase):
    def setUp(self):
        self.f = make_service()
        self.addCleanup(self.f.cleanup)
        self.created = self.f.create()
        self.plan_id = self.created.current_metadata.plan_id
        self.logs = self.capture_logs()

    def test_release_contract_coverage(self):
        validator = getattr(transport, "validate_release_contract", None)
        self.assertTrue(callable(validator), "The production release-contract validator is absent.")
        handlers = transport.registered_handlers(self.f.service)
        view = self.f.service.read_plan(self.plan_id)
        validator(handlers, view)
        self.assertEqual(set(handlers), {*(name for name, _, _ in READS), *MUTATIONS, "read_measurements", "record_measurement"})
        self.assertEqual(set(MUTATIONS), BOOTSTRAP_KINDS | ORCHESTRATOR_KINDS | WORKER_KINDS)
        validator(dict(handlers), view)
        with self.assertRaises(DomainError):
            validator({**handlers, "unguarded": lambda command: None}, view)

    def snapshot(self):
        return self.f.store.read(lambda db: tuple(db.iterdump()))

    def read_arguments(self, arguments):
        return {key: self.plan_id if value is None else value for key, value in arguments.items()}

    def capture_logs(self):
        if hasattr(self, "logs"):
            return self.logs
        captured = CapturedLogs()
        logger = logging.getLogger()
        old_level = logger.level
        old_handlers = logger.handlers[:]
        logger.handlers = [captured]
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.setLevel, old_level)
        self.addCleanup(setattr, logger, "handlers", old_handlers)
        return captured

    def assert_safe_error(self, result, forbidden=(SENTINEL,)):
        self.assertTrue(result.is_error, result.content)
        self.assertIn(result.structured_content["error"]["code"], ("internal_error", "stale"))
        encoded = json.dumps(result.model_dump(mode="json", by_alias=True))
        for value in forbidden:
            self.assertNotIn(value, encoded)
        return result.structured_content["error"]

    def test_registry_rejects_replacement_swaps_copied_attributes_and_mixed_batches_without_calls(self):
        handlers = transport.registered_handlers(self.f.service)
        other = transport.registered_handlers(self.f.service)
        view = self.f.service.read_plan(self.plan_id)
        invoked = Mock(side_effect=AssertionError("Registry validation invoked a handler"))

        def copied(command: Any):
            return invoked(command)

        update_wrapper(copied, handlers["create_plan"])
        missing = dict(handlers)
        missing.pop("record_checkpoint")
        bad = (
            {**handlers, "unguarded": invoked}, missing,
            {**handlers, "create_plan": invoked}, {**handlers, "create_plan": copied},
            {**handlers, "create_plan": handlers["takeover"], "takeover": handlers["create_plan"]},
            {**handlers, "read_plan": handlers["create_plan"], "create_plan": handlers["read_plan"]},
            {**handlers, "read_plan": other["read_plan"]},
            {**handlers, "create_plan": other["create_plan"]},
        )
        before = self.snapshot()
        with patch.object(self.f.service, "execute", invoked), patch.object(self.f.service, "read_plan", invoked):
            for index, candidate in enumerate(bad):
                with self.subTest(candidate=index):
                    with self.assertRaises(DomainError):
                        transport.validate_release_contract(candidate, view)
                    with patch.object(transport, "registered_handlers", return_value=candidate), \
                            patch.object(transport.MCPServer, "tool") as register:
                        with self.assertRaises(DomainError):
                            transport.build_mcp(self.f.service)
                        register.assert_not_called()
        invoked.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_build_consumes_one_genuine_mapping_and_starts_without_a_plan(self):
        with make_service() as empty:
            handlers = transport.registered_handlers(empty.service)
            before = empty.store.read(lambda db: tuple(db.iterdump()))
            with patch.object(transport, "registered_handlers", return_value=handlers) as factory:
                server = transport.build_mcp(empty.service)
            factory.assert_called_once_with(empty.service)
            result = asyncio.run(server.call_tool("list_plans", {}))
            self.assertFalse(result.is_error, result.content)
            self.assertEqual(result.structured_content, {"plans": []})
            self.assertEqual(empty.store.read(lambda db: tuple(db.iterdump())), before)

    def test_actual_sdk_catalog_preserves_all_schemas_annotations_defaults_and_descriptions(self):
        server = transport.build_mcp(self.f.service)
        tools = asyncio.run(server.list_tools())
        # Preserve the original 29-tool contract byte for byte; additions have separate behavioural tests.
        catalog = [tool.model_dump(mode="json", by_alias=True) for tool in sorted(tools, key=lambda tool: tool.name)
                   if tool.name not in {"read_measurements", "record_measurement"}]
        encoded = json.dumps(catalog, sort_keys=True, separators=(",", ":")).encode()
        # Captured from accepted Task25 with MCP2.3.0/Pydantic2.13.5 before this refactor.
        self.assertEqual(hashlib.sha256(encoded).hexdigest(),
                         "8a6f4dbc21d35917326cea812670d52804fc617dcbe3df10bd4dcfd60f90ce9f")
        self.assertEqual({tool.name for tool in tools}, {*(name for name, _, _ in READS), *MUTATIONS, "read_measurements", "record_measurement"})
        added = {tool.name: tool for tool in tools if tool.name in {"read_measurements", "record_measurement"}}
        self.assertTrue(added["read_measurements"].annotations.read_only_hint)
        self.assertFalse(added["record_measurement"].annotations.read_only_hint)
        self.assertTrue(added["record_measurement"].annotations.idempotent_hint)

    def mutation_commands(self, attempt):
        cp = checkpoint()
        claim = ResourceClaim("synthetic-host", "external", None, None, None, False, None, "matrix-external")
        payloads = {
            "create_plan": {"creation_id": "matrix-create", "draft": draft(), "new_plan_intent": "Separate matrix plan"},
            "import_plan": {"host_id": "synthetic-host", "source_directory_id": "matrix-directory",
                            "source_directory_claim": None, "original_task_id": "matrix-legacy",
                            "original_snapshot": {"objective": "Matrix import"},
                            "provenance": DocumentRef("matrix.md", None, None, "v1", None),
                            "writers_stopped": "Synthetic stopped publisher", "known_contexts": {}},
            "revise_plan": {"draft": draft(), "reason": "Matrix revision", "authorization_change": None},
            "takeover": {"expected_ownership_version": 1, "reason": "Matrix takeover"},
            "register_resource": {"claim": claim},
            "reconcile_resources": {"resource_ids": ("matrix-resource",), "canonical_claim": claim, "reason": "Matrix reconcile"},
            "assign_step": {"step_id": "b", "assignee": "worker-b", "context": context(None, writes=False)},
            "bind_availability": {"bindings": (), "checked_base": "matrix-base"},
            "start_step": {"origin_worker": "worker-a", "relay": None, "observed_activity": "Matrix start"},
            "report_activity": {"origin_worker": "worker-a", "relay": None, "observed_activity": "Matrix observation"},
            "record_checkpoint": {"checkpoint": cp},
            "resolve_reassessment": {"checkpoint_id": "matrix-checkpoint", "decision": "reject", "reason": "Matrix classification",
                                     "draft": None, "affected_steps": (), "unbounded": False},
            "invalidate_result": {"step_id": "a", "result_version": "matrix-result", "reason": "Matrix invalidation", "evidence": cp.evidence},
            "resolve_invalidation": {"invalidation_id": "matrix-invalidation", "affected_step_id": "a", "disposition": "revalidated",
                                     "corrective_step_id": None, "corrected_result_version": None, "evidence": cp.evidence, "reason": "Matrix proof"},
            "record_review": {"step_id": "a", "review": Review("matrix-reviewer", cp.artifact, "passed", (), cp.evidence, None)},
            "accept_step": {"step_id": "a", "artifact": cp.artifact, "stopped_resource_ids": (), "stopped_reason": None},
            "reserve_verification": {"step_id": "a", "verifier": "matrix-reviewer", "context": context(None, writes=False),
                                     "handoff_from": None, "stopped_reason": None},
            "release_resource_use": {"use_id": "matrix-use", "resource_ids": ("matrix-resource",), "reason": "Matrix stopped", "stopped": True},
            "reopen_attempt": {"reason": "Matrix reopen"},
            "sweep_stale": {},
            "resolve_attempt": {"resolution": "confirm-owner", "reason": "Matrix confirmation", "next_step_state": "pending",
                                "stopped_resource_ids": (), "replacement_assignee": None, "replacement_context": None},
            "append_late_evidence": {"origin_attempt_id": attempt.committed["attempt_id"], "evidence": cp.evidence,
                                     "stopped_resource_ids": (), "reason": "Matrix retained observation"},
            "resolve_store_hold": {"backup_cutoff": "matrix-cutoff", "inventory_complete": True,
                                   "absent_work_inventory": (), "resource_resolutions": (), "reason": "Matrix inventory"},
        }
        self.assertEqual(set(payloads), set(MUTATIONS))
        adapter = TypeAdapter(transport.CommandInput)
        commands = {}
        for kind in MUTATIONS:
            receipt = None if kind in ("create_plan", "import_plan") else self.created
            parsed = self.f.command(kind, payloads[kind], receipt=receipt,
                                    attempt=attempt if kind not in BOOTSTRAP_KINDS else None, key=f"matrix-{kind}")
            wire = plans.to_jsonable(parsed)
            typed = adapter.validate_python(wire)
            self.assertEqual(typed.kind, kind)
            commands[kind] = (wire, parsed)
        return commands

    def test_every_actual_mutation_parses_once_and_resolves_central_execute_after_build(self):
        attempt = self.f.assign(self.created)
        commands = self.mutation_commands(attempt)  # Setup reads precede the database snapshot/spy.
        server = transport.build_mcp(self.f.service)
        metadata = self.f.service.read_plan(self.plan_id).metadata
        synthetic = MutationReceipt({"dispatch_probe": True}, metadata, metadata, False, None)
        before = self.snapshot()
        with patch.object(self.f.service, "execute", return_value=synthetic) as execute:
            async def exercise():
                for kind, (wire, expected) in commands.items():
                    with self.subTest(kind=kind):
                        execute.reset_mock()
                        result = await server.call_tool(kind, {"command": wire})
                        self.assertFalse(result.is_error, result.content)
                        execute.assert_called_once()
                        actual = execute.call_args.args[0]
                        self.assertIs(type(actual), Command)
                        self.assertEqual(actual, expected)
                        self.assertEqual(actual.kind, kind)
                        self.assertEqual(result.structured_content["committed"], {"dispatch_probe": True})
                        self.assertEqual(self.snapshot(), before)
            asyncio.run(exercise())

    def test_each_semantic_tool_rejects_a_valid_different_kind_without_execute(self):
        attempt = self.f.assign(self.created)
        commands = self.mutation_commands(attempt)
        server = transport.build_mcp(self.f.service)
        before = self.snapshot()
        with patch.object(self.f.service, "execute") as execute:
            async def exercise():
                for kind in MUTATIONS:
                    wrong = "takeover" if kind == "create_plan" else "create_plan"
                    with self.subTest(kind=kind):
                        result = await server.call_tool(kind, {"command": commands[wrong][0]})
                        self.assertTrue(result.is_error)
                        self.assertEqual(result.structured_content["error"]["code"], "invalid")
                        execute.assert_not_called()
                        self.assertEqual(self.snapshot(), before)
            asyncio.run(exercise())

    def test_six_real_reads_keep_issued_grants_private_and_do_not_mutate(self):
        attempt = self.f.assign(self.created)
        self.f.start(self.created, attempt)
        self.f.submit(self.created, attempt)
        tokens = (self.created.private.orchestrator_token, attempt.private.attempt_token)
        self.assertTrue(all(tokens))
        server = transport.build_mcp(self.f.service)
        before = self.snapshot()
        captured = self.capture_logs()
        async def exercise():
            for name, _, arguments in READS:
                with self.subTest(tool=name):
                    result = await server.call_tool(name, self.read_arguments(arguments))
                    self.assertFalse(result.is_error, result.content)
                    encoded = json.dumps(result.model_dump(mode="json", by_alias=True))
                    for token in tokens:
                        self.assertNotIn(token, encoded)
                    self.assertEqual(self.snapshot(), before)
        asyncio.run(exercise())
        for line in captured.lines:
            for token in tokens:
                self.assertNotIn(token, line)

    def test_six_served_reads_reject_nested_typed_private_values_and_receipts(self):
        server = transport.build_mcp(self.f.service)
        metadata = self.f.service.read_plan(self.plan_id).metadata
        forbidden = (PrivateGrant(SENTINEL, SENTINEL),
                     MutationReceipt({"observation": SENTINEL}, metadata, metadata, False, None))
        captured = self.capture_logs()
        before = self.snapshot()
        async def exercise():
            for name, method, arguments in READS:
                for private in forbidden:
                    with self.subTest(tool=name, value=type(private).__name__):
                        contaminated = {"public": [{"nested": private}]}
                        with patch.object(self.f.service, method, return_value=contaminated):
                            result = await server.call_tool(name, self.read_arguments(arguments))
                        self.assert_safe_error(result)
                        self.assertEqual(self.snapshot(), before)
        asyncio.run(exercise())
        self.assertNotIn(SENTINEL, "\n".join(captured.lines))

    def test_actual_plan_draft_coordination_and_event_paths_reject_typed_contamination(self):
        view = self.f.service.read_plan(self.plan_id)
        grant = PrivateGrant(SENTINEL, SENTINEL)
        bad_draft = replace(view.draft)
        object.__setattr__(bad_draft, "workflow", {"allowed-label": grant})
        contaminated = (
            ("read_plan", "read_plan", replace(view, draft=bad_draft)),
            ("read_plan", "read_plan", replace(view, legacy_snapshot={"notes": [grant]})),
            ("read_plan", "read_plan", replace(view, coordination=replace(view.coordination, holds=(grant,)))),
            ("read_events", "read_events", (Event(1, 1, self.plan_id, "observation", "worker-a", None, None,
                                                 "2026-10-09T00:00:00Z", {"notes": [grant]}),)),
        )
        server = transport.build_mcp(self.f.service)
        captured = self.capture_logs()
        before = self.snapshot()
        for tool, method, value in contaminated:
            with self.subTest(tool=tool, shape=type(value).__name__):
                with patch.object(self.f.service, method, return_value=value):
                    self.assert_safe_error(asyncio.run(server.call_tool(tool, {"plan_id": self.plan_id})))
                self.assertEqual(self.snapshot(), before)
        self.assertNotIn(SENTINEL, "\n".join(captured.lines))

    def test_valid_reserved_looking_public_json_and_shared_acyclic_values_are_preserved_and_detached(self):
        view = self.f.service.read_plan(self.plan_id)
        workflow = {"private": "private workflow label", "orchestrator_token": "ordinary prose",
                    "attempt_token": "public coordination terminology"}
        shared = [None, True, 7, 2.5, {"private": "a legitimate label"}]
        public = replace(view, draft=replace(view.draft, workflow=workflow, objective="Explain attempt_token in public prose"),
                         legacy_snapshot={"private": {"orchestrator_token": "ordinary data", "attempt_token": shared},
                                          "also": shared})
        handlers = transport.registered_handlers(self.f.service)
        transport.validate_release_contract(handlers, public)
        server = transport.build_mcp(self.f.service)
        with patch.object(self.f.service, "read_plan", return_value=public):
            result = asyncio.run(server.call_tool("read_plan", {"plan_id": self.plan_id}))
            self.assertFalse(result.is_error, result.content)
            converted = handlers["read_plan"](self.plan_id)
        self.assertEqual(result.structured_content["draft"]["workflow"], workflow)
        self.assertEqual(result.structured_content["legacy_snapshot"]["also"], shared)
        self.assertEqual(converted["legacy_snapshot"]["private"]["attempt_token"], shared)
        converted["legacy_snapshot"]["also"].append("detached")
        converted["draft"]["workflow"]["private"] = "detached"
        self.assertEqual(len(shared), 5)
        self.assertEqual(public.draft.workflow["private"], "private workflow label")
        self.assertEqual(converted["legacy_snapshot"]["private"]["attempt_token"], shared)

    def test_validator_and_served_reads_reject_non_json_internal_subclass_extra_and_cyclic_values(self):
        view = self.f.service.read_plan(self.plan_id)
        handlers = transport.registered_handlers(self.f.service)
        server = transport.build_mcp(self.f.service)
        cycle = []
        cycle.append(cycle)
        mapping_cycle = {}
        mapping_cycle["cycle"] = mapping_cycle
        added = replace(view.metadata)
        object.__setattr__(added, SENTINEL, SENTINEL)
        internal = self.f.command("create_plan", {"creation_id": "internal", "draft": draft(), "new_plan_intent": None})
        values = (PrivateGrant(SENTINEL, None), MutationReceipt({}, view.metadata, view.metadata, False, None),
                  internal, AddedMetadata(**vars(view.metadata)), added, AddedString(SENTINEL),
                  {SENTINEL}, SENTINEL.encode(), object(), float("nan"), float("inf"), -float("inf"),
                  {1: SENTINEL}, cycle, mapping_cycle)
        captured = self.capture_logs()
        before = self.snapshot()
        for value in values:
            with self.subTest(value=type(value).__name__):
                contaminated = replace(view, legacy_snapshot={"notes": value})
                with self.assertRaises(DomainError) as raised:
                    transport.validate_release_contract(handlers, contaminated)
                self.assertNotIn(SENTINEL, str(raised.exception))
                with patch.object(self.f.service, "read_plan", return_value=contaminated):
                    self.assert_safe_error(asyncio.run(server.call_tool("read_plan", {"plan_id": self.plan_id})))
                self.assertEqual(self.snapshot(), before)
        self.assertNotIn(SENTINEL, "\n".join(captured.lines))

    def test_error_current_metadata_is_checked_and_contamination_is_omitted_safely(self):
        metadata = self.f.service.read_plan(self.plan_id).metadata
        bad_metadata = replace(metadata)
        object.__setattr__(bad_metadata, "dashboard_url", {"nested": PrivateGrant(SENTINEL, SENTINEL)})
        server = transport.build_mcp(self.f.service)
        captured = self.capture_logs()
        before = self.snapshot()
        for value, permitted in ((metadata, True), (bad_metadata, False), (AddedMetadata(**vars(metadata)), False),
                                 (PrivateGrant(SENTINEL, None), False)):
            with self.subTest(metadata=type(value).__name__, permitted=permitted):
                error = DomainError("stale", SENTINEL, current_metadata=value)
                with patch.object(self.f.service, "read_plan", side_effect=error):
                    result = asyncio.run(server.call_tool("read_plan", {"plan_id": self.plan_id}))
                payload = self.assert_safe_error(result)
                if permitted:
                    self.assertEqual(payload["current_metadata"], plans.to_jsonable(metadata))
                else:
                    self.assertNotIn("current_metadata", payload)
                self.assertEqual(self.snapshot(), before)
        self.assertNotIn(SENTINEL, "\n".join(captured.lines))

    def test_registry_provenance_does_not_retain_functions_service_or_server(self):
        def issue():
            with make_service() as f:
                handlers = transport.registered_handlers(f.service)
                with patch.object(transport, "registered_handlers", return_value=handlers):
                    server = transport.build_mcp(f.service)
                return tuple((id(handler), weakref.ref(handler)) for handler in handlers.values()), weakref.ref(f.service), weakref.ref(server)
        issued, service, server = issue()
        gc.collect()  # Explicit collection, independent of automatic GC timing.
        self.assertTrue(all(reference() is None for _, reference in issued))
        with transport._HANDLER_LOCK:
            self.assertTrue(all(identity not in transport._HANDLER_ISSUANCE for identity, _ in issued))
        self.assertIsNone(service())
        self.assertIsNone(server())

    def test_concurrent_registry_issue_validation_and_collection_remain_isolated(self):
        view = self.f.service.read_plan(self.plan_id)
        other = transport.registered_handlers(self.f.service)
        barrier = Barrier(4)
        def exercise(_):
            references = []
            barrier.wait(timeout=10)
            for _ in range(8):
                handlers = transport.registered_handlers(self.f.service)
                transport.validate_release_contract(handlers, view)
                transport.validate_release_contract(dict(handlers), view)
                with self.assertRaises(DomainError):
                    transport.validate_release_contract({**handlers, "create_plan": other["create_plan"]}, view)
                references.extend(weakref.ref(handler) for handler in handlers.values())
            return references
        with ThreadPoolExecutor(max_workers=4) as executor:
            references = [reference for batch in executor.map(exercise, range(4)) for reference in batch]
        gc.collect()
        self.assertTrue(all(reference() is None for reference in references))
