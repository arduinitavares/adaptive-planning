"""Corrective and baseline regressions. Run with unittest discovery from the repository root."""
from __future__ import annotations

import copy
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import live_plan as lp


def proposal():
    return dict(schemaVersion=2, taskId="corrective", objective="Verify boundaries",
                status="planning", executionMode="first-step", nextStepId="A",
                changeSummary="Initial", steps=[
                    dict(id=sid, title=sid, status="pending", dependsOn=[], check="Checked", evidence=[])
                    for sid in ("A", "B")])


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.state = self.directory / "state"
        self.state.mkdir()
        self.canonical = self.state / "live_plan.json"
        self.input = self.directory / "proposal.json"
        self.write(proposal())

    def write(self, value):
        self.input.write_text(json.dumps(value, ensure_ascii=True), encoding="utf-8")

    def create(self):
        return lp.publish(str(self.state), str(self.input), 0,
                          authorization_note="User requested the first step and its verification")

    def update(self, current):
        return lp.publish(str(self.state), str(self.input), current["revision"], current["generation"])

    def store(self, value):
        self.canonical.write_text(json.dumps(value), encoding="utf-8")

    def read(self):
        return lp.read_published_snapshot(str(self.canonical))


class SnapshotRegressions(Fixture):
    def test_legacy_history_note_extensions_refuse_before_migration_commit(self):
        spec = importlib.util.spec_from_file_location("v1_history_baseline", ROOT / "tests/fixtures/live_plan_v1.py")
        baseline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(baseline)
        current = self.create()
        legacy = {key: copy.deepcopy(value) for key, value in current.items()
                  if key not in ("scope", "generation")}
        legacy["schemaVersion"] = 1
        for field, value in (("authorizationNote", None), ("authorizationNote", []),
                             ("recoveryNote", ""), ("recoveryNote", {})):
            with self.subTest(field=field, value=value):
                state = copy.deepcopy(legacy)
                state["history"][0][field] = value
                self.store(state)
                # Establish genuine legacy validity with the unmodified v1 reader.
                self.assertEqual(baseline.read_published_snapshot(str(self.canonical))[0], state)
                self.assertEqual(self.read()[0], state)
                before = self.canonical.read_bytes()
                with self.assertRaisesRegex(lp.PlanValidationError, "History entry " + field):
                    lp.publish(str(self.state), str(self.input), 1, "none",
                               "User requested the first step and its verification")
                self.assertEqual(self.canonical.read_bytes(), before)
                self.assertEqual(self.read()[0]["history"], state["history"])
                self.assertEqual(list(self.state.glob("*.tmp.*")), [])
                self.assertEqual(list(self.state.glob("live_plan.corrupt.*")), [])
        # Compatible legacy history remains intact on successful migration.
        self.store(legacy)
        migrated = lp.publish(str(self.state), str(self.input), 1, "none",
                              "User requested the first step and its verification")
        self.assertEqual(migrated["history"][:-1], legacy["history"])
        self.assertEqual(self.read()[0], migrated)

    def test_identity_and_literal_migration_generation_boundaries(self):
        good = self.create()
        before = self.canonical.read_bytes()
        for field in ("taskId", "objective"):
            plan = proposal()
            plan[field] = "different"
            self.write(plan)
            with self.assertRaises(lp.PlanValidationError):
                self.update(good)
            self.assertEqual(self.canonical.read_bytes(), before)
        self.write(proposal())
        legacy = {key: value for key, value in good.items() if key not in ("scope", "generation")}
        legacy["schemaVersion"] = 1
        self.store(legacy)
        before = self.canonical.read_bytes()
        for generation in (None, "NONE", " none ", good["generation"]):
            with self.assertRaises(lp.PlanValidationError):
                lp.publish(str(self.state), str(self.input), 1, generation, "User requested first step")
            self.assertEqual(self.canonical.read_bytes(), before)

    def test_plan_only_imported_history_then_new_completion_rejected(self):
        plan = proposal()
        plan["executionMode"] = "plan-only"
        plan["nextStepId"] = "B"
        plan["steps"][0].update(status="complete", evidence=["Imported verification"])
        self.write(plan)
        current = self.create()
        self.assertEqual(current["scope"]["baseline"], ["A"])
        self.assertEqual(self.read()[0], current)
        plan["steps"][1].update(status="complete", evidence=["Unapproved new completion"])
        plan["nextStepId"] = None
        self.write(plan)
        before = self.canonical.read_bytes()
        with self.assertRaisesRegex(lp.PlanValidationError, "outside baseline"):
            self.update(current)
        self.assertEqual(self.canonical.read_bytes(), before)

    def test_common_fields_limits_and_metadata_rejected_in_both_versions(self):
        good = self.create()
        mutations = [("schemaVersion", True), ("schemaVersion", 1.0), ("taskId", ""),
                     ("objective", None), ("status", "unknown"), ("status", []),
                     ("executionMode", "unknown"), ("changeSummary", ""),
                     ("revision", True), ("revision", 1.5), ("updatedAt", "no"),
                     ("updatedAt", "2026-10-03T10:00:00"), ("history", []),
                     ("steps", [None]), ("steps", [dict(proposal()["steps"][0], id=str(i)) for i in range(201)])]
        for version in (1, 2):
            for field, value in mutations:
                with self.subTest(version=version, field=field, value=repr(value)[:60]):
                    bad = copy.deepcopy(good)
                    bad["schemaVersion"] = version
                    bad[field] = value
                    self.store(bad)
                    with self.assertRaises(lp.PlanValidationError):
                        self.read()
        for field, value in (("timestamp", "2099-01-01T00:00:00Z"), ("revision", 3),
                             ("summary", "different"), ("authorizationNote", []), ("recoveryNote", "")):
            bad = copy.deepcopy(good)
            bad["history"][0][field] = value
            self.store(bad)
            with self.assertRaises(lp.PlanValidationError):
                self.read()
        for field in ("unexpected", "authorizationNote", "recoveryNote"):
            bad = dict(good, **{field: "not publisher metadata"})
            self.store(bad)
            with self.assertRaises(lp.PlanValidationError):
                self.read()
        bad = dict(good, generation="not-a-uuid")
        self.store(bad)
        with self.assertRaises(lp.PlanValidationError):
            self.read()

    def test_first_step_snapshot_claim_consistency_and_removed_claim(self):
        good = self.create()
        for claimed, active in ((None, ["A"]), ("A", ["B"]), ("A", ["A", "B"])):
            bad = copy.deepcopy(good)
            bad["status"] = "running"
            bad["scope"]["claimed"] = claimed
            for step in bad["steps"]:
                if step["id"] in active:
                    step["status"] = "in-progress"
            self.store(bad)
            with self.assertRaises(lp.PlanValidationError):
                self.read()
        good["scope"]["claimed"] = "removed-evidence-free-id"
        self.store(good)
        self.assertEqual(self.read()[0]["scope"]["claimed"], "removed-evidence-free-id")
        good["history"][0].pop("authorizationNote")
        self.store(good)
        with self.assertRaises(lp.PlanValidationError):
            self.read()

    def test_legacy_semantics_match_real_baseline_and_migrate(self):
        spec = importlib.util.spec_from_file_location("v1_baseline", ROOT / "tests/fixtures/live_plan_v1.py")
        baseline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(baseline)
        good = self.create()
        good["schemaVersion"] = 1
        for key in ("scope", "generation"):
            good.pop(key)
        variants = []
        blocked = copy.deepcopy(good)
        blocked["steps"][0]["status"] = "blocked"
        variants.append(blocked)
        unmet = copy.deepcopy(good)
        unmet["steps"][1]["dependsOn"] = ["A"]
        unmet["nextStepId"] = "B"
        variants.append(unmet)
        planning_mode = copy.deepcopy(good)
        planning_mode.update(executionMode="plan-only", status="running")
        planning_mode["steps"][0]["status"] = "in-progress"
        variants.append(planning_mode)
        accumulated = copy.deepcopy(good)
        accumulated.update(status="complete", nextStepId=None)
        for step in accumulated["steps"]:
            step.update(status="complete", evidence=["historical verification"])
        variants.append(accumulated)
        for legacy in variants:
            with self.subTest(legacy=legacy):
                self.store(legacy)
                expected, _ = baseline.read_published_snapshot(str(self.canonical))
                self.assertEqual(self.read()[0], expected)
                before = self.canonical.read_bytes()
                with self.assertRaisesRegex(lp.PlanValidationError, "valid"):
                    lp.recover(str(self.state), str(self.input), "No recovery warranted")
                self.assertEqual(self.canonical.read_bytes(), before)
                self.assertEqual(list(self.state.glob("live_plan.corrupt.*")), [])
        self.store(unmet)
        plan = proposal()
        plan.update(executionMode="plan-only")
        self.write(plan)
        migrated = lp.publish(str(self.state), str(self.input), 1, "none", "User asked for planning only")
        self.assertEqual(migrated["schemaVersion"], 2)
        with self.assertRaises(baseline.PlanValidationError):
            baseline.validate_plan(migrated, migrated)

    def test_utf8_nonfinite_oversize_and_deterministic_wire(self):
        good = self.create()
        _, wire = self.read()
        self.store(good)
        self.assertEqual(self.read()[1], wire)
        self.assertEqual(json.loads(wire), good)
        for version in (1, 2):
            for value in (float("nan"), float("inf"), float("-inf"), "\ud800"):
                bad = copy.deepcopy(good)
                bad["schemaVersion"] = version
                bad["steps"][0]["title"] = value
                self.store(bad)
                with self.assertRaises(lp.PlanValidationError):
                    self.read()
        self.canonical.write_bytes(b" " * 1025)
        with patch.object(lp, "MAX_FILE_BYTES", 1024):
            with self.assertRaises(lp.PlanValidationError):
                self.read()

    def test_bounded_history_output_limit_and_failed_update_bytes(self):
        current = self.create()
        before = self.canonical.read_bytes()
        with patch.object(lp, "MAX_FILE_BYTES", len(before)):
            with self.assertRaises(lp.PlanValidationError):
                self.update(current)
        self.assertEqual(self.canonical.read_bytes(), before)
        for revision in range(2, 106):
            current = self.update(current)
        self.assertEqual([e["revision"] for e in current["history"]], list(range(6, 106)))
        self.assertEqual(self.read()[0], current)  # old opening note legitimately fell out of history
        for value in (float("nan"), float("inf"), float("-inf"), "\ud800"):
            before = self.canonical.read_bytes()
            bad = proposal()
            bad["steps"][0]["title"] = value
            self.write(bad)
            with self.assertRaises(lp.PlanValidationError):
                self.update(current)
            self.assertEqual(self.canonical.read_bytes(), before)
        self.write(proposal())
        self.update(current)
        self.assertEqual(list(self.state.glob("*.tmp.*")), [])

    def test_concurrent_publish_exactly_one_cas_winner(self):
        current = self.create()
        barrier = threading.Barrier(4)
        def contender():
            barrier.wait(timeout=5)
            try:
                return self.update(current)
            except lp.PlanValidationError as error:
                return error
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: contender(), range(4)))
        self.assertEqual(sum(isinstance(value, dict) for value in results), 1)
        self.assertEqual(sum(isinstance(value, lp.PlanValidationError) for value in results), 3)
        self.assertEqual(self.read()[0]["revision"], 2)


class RecoveryRegressions(Fixture):
    def test_assembled_recovery_validation_precedes_backup(self):
        original = b"corrupt canonical"
        self.canonical.write_bytes(original)
        plan = proposal()
        plan["executionMode"] = "plan-only"
        self.write(plan)
        # Inject malformed publisher metadata to exercise the final snapshot gate.
        with patch.object(lp.uuid, "uuid4", return_value="invalid-generation"):
            with self.assertRaisesRegex(lp.PlanValidationError, "generation must be a UUID"):
                lp.recover(str(self.state), str(self.input), "Rebuilt planning state")
        self.assertEqual(self.canonical.read_bytes(), original)
        self.assertEqual(list(self.state.glob("live_plan.corrupt.*")), [])
        self.assertEqual(list(self.state.glob("*.tmp.*")), [])

    def test_transient_read_error_is_not_corruption(self):
        self.create()
        before = self.canonical.read_bytes()
        actual = lp.read_published_snapshot
        calls = 0
        def transient(path):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("temporary unavailable reader")
            return actual(path)
        with patch.object(lp, "read_published_snapshot", side_effect=transient):
            with self.assertRaises(OSError):
                lp.recover(str(self.state), str(self.input), "Must not reset valid state")
        self.assertEqual(calls, 1)
        self.assertEqual(self.canonical.read_bytes(), before)
        self.assertEqual(list(self.state.glob("live_plan.corrupt.*")), [])

    def test_retry_backup_metadata_and_partial_backup_failure(self):
        original = b"invalid canonical\x00" * 100000
        self.canonical.write_bytes(original)
        plan = proposal()
        plan["executionMode"] = "plan-only"
        self.write(plan)
        with patch.object(lp, "_atomic_replace_with_retry", side_effect=lp.RetryableStorageError("retryable; canonical state unchanged")):
            with self.assertRaises(lp.RetryableStorageError) as caught:
                lp.recover(str(self.state), str(self.input), "Rebuilt from evidence")
        backup = Path(caught.exception.backup_path)
        self.assertEqual(backup.read_bytes(), original)
        self.assertIn(str(backup), str(caught.exception))
        self.assertEqual(self.canonical.read_bytes(), original)
        with patch.object(lp.shutil, "copyfileobj", side_effect=OSError("partial copy")):
            with self.assertRaisesRegex(RuntimeError, "no completed backup"):
                lp.recover(str(self.state), str(self.input), "Rebuilt from evidence")
        self.assertEqual(list(self.state.glob("live_plan.corrupt.*")), [backup])
        self.assertEqual(list(self.state.glob("*.tmp.*")), [])

    def test_recovery_publish_really_serialize(self):
        self.canonical.write_bytes(b"corrupt")
        plan = proposal()
        plan["executionMode"] = "plan-only"
        self.write(plan)
        reached_replace, release_replace, publisher_started = threading.Event(), threading.Event(), threading.Event()
        actual = lp._atomic_replace_with_retry
        def hold_recovery(*args, **kwargs):
            if kwargs.get("is_recovery"):
                reached_replace.set()
                if not release_replace.wait(5):
                    raise TimeoutError("test did not release recovery")
            return actual(*args, **kwargs)
        def publish_contender():
            publisher_started.set()
            return lp.publish(str(self.state), str(self.input), 0, authorization_note="User asked for planning")
        with patch.object(lp, "_atomic_replace_with_retry", side_effect=hold_recovery):
            with ThreadPoolExecutor(max_workers=2) as pool:
                recovery = pool.submit(lp.recover, str(self.state), str(self.input), "Rebuilt verified facts")
                contender = None
                try:
                    self.assertTrue(reached_replace.wait(5))
                    contender = pool.submit(publish_contender)
                    self.assertTrue(publisher_started.wait(5))
                    time.sleep(0.1)
                    self.assertFalse(contender.done(), "Publisher must wait on the actual recovery lock")
                    self.assertEqual(self.canonical.read_bytes(), b"corrupt")
                finally:
                    release_replace.set()
                recovered = recovery.result(timeout=5)
                with self.assertRaises(lp.PlanValidationError):
                    contender.result(timeout=5)
        self.assertEqual(self.read()[0]["generation"], recovered["generation"])

    def test_cli_reports_committed_recovery_after_stdout_failure(self):
        self.canonical.write_bytes(b"corrupt")
        plan = proposal()
        plan["executionMode"] = "plan-only"
        self.write(plan)
        stderr = io.StringIO()
        argv = ["live_plan", "recover", "--state-dir", str(self.state), "--input", str(self.input),
                "--recovery-note", "Reconstructed planning state"]
        with patch.object(sys, "argv", argv), patch.object(sys, "stderr", stderr), patch("builtins.print", side_effect=BrokenPipeError("closed output")):
            with self.assertRaises(SystemExit):
                lp.main()
        self.assertEqual(self.read()[0]["revision"], 1)
        self.assertIn("Committed recovery revision 1", stderr.getvalue())
        self.assertNotIn("unchanged", stderr.getvalue())
        self.assertIn("completed backup:", stderr.getvalue())


class RuntimeBoundaries(Fixture):
    def test_actual_dashboard_dom_behavior(self):
        import shutil
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node executable unavailable")
        result = subprocess.run([node, str(ROOT / "tests/test_frontend_dom.js")],
                                cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_retry_only_eligible_windows_errors(self):
        # Patch platform only around the replace helper, never around FileLock.
        for platform, code, error_type, retry in (("win32", 5, PermissionError, True),
                ("win32", 32, PermissionError, True), ("win32", 13, PermissionError, False),
                ("linux", 32, PermissionError, False), ("win32", 32, OSError, False)):
            with self.subTest(platform=platform, code=code, error_type=error_type):
                err = error_type("injected")
                err.winerror = code
                with patch.object(sys, "platform", platform), patch.object(lp.os, "replace", side_effect=[err, None]) as replace, patch.object(lp.time, "sleep"):
                    if retry:
                        lp._atomic_replace_with_retry("missing.tmp", "missing.canonical")
                        self.assertEqual(replace.call_count, 2)
                    else:
                        with self.assertRaises(error_type):
                            lp._atomic_replace_with_retry("missing.tmp", "missing.canonical")
                        self.assertEqual(replace.call_count, 1)

    def test_process_crash_releases_lock_without_deleting_file(self):
        lock = self.state / "crash.lock"
        marker = self.directory / "locked"
        code = "from scripts.live_plan import FileLock; from pathlib import Path; import sys,time; lock=FileLock(sys.argv[1]); lock.__enter__(); Path(sys.argv[2]).write_text('locked'); time.sleep(60)"
        child = subprocess.Popen([sys.executable, "-B", "-c", code, str(lock), str(marker)], cwd=ROOT)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline and child.poll() is None:
                time.sleep(0.02)
            self.assertTrue(marker.exists())
            with self.assertRaises(TimeoutError):
                with lp.FileLock(str(lock), timeout=0.1):
                    pass
            child.kill()
            child.wait(timeout=5)
            self.assertTrue(lock.exists())
            with lp.FileLock(str(lock), timeout=2):
                pass
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

    @unittest.skipUnless(sys.platform == "win32", "requires actual Windows share-denying handles")
    def test_actual_windows_reader_300ms_then_cli75_indefinite(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        current = self.create()
        def reader():
            # GENERIC_READ; share READ|WRITE but deliberately deny DELETE/replace.
            handle = create(str(self.canonical), 0x80000000, 3, None, 3, 0x80, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            return handle
        handle = reader()
        timer = threading.Timer(0.3, lambda: close(handle))
        timer.start()
        try:
            current = self.update(current)
            self.assertEqual(current["revision"], 2)
        finally:
            timer.join(timeout=5)
        before = self.canonical.read_bytes()
        handle = reader()
        try:
            result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/live_plan.py"), "publish",
                "--state-dir", str(self.state), "--input", str(self.input), "--expected-revision", "2",
                "--expected-generation", current["generation"]], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 75, result.stderr)
            self.assertIn("retryable; state unchanged at revision 2", result.stderr)
            self.assertEqual(self.canonical.read_bytes(), before)
            self.assertEqual(list(self.state.glob("*.tmp.*")), [])
        finally:
            close(handle)


class ServerRegressions(Fixture):
    def setUp(self):
        super().setUp()
        self.assets = ROOT / "assets/dashboard"
        self.server = lp.ThreadedHTTPServer(("127.0.0.1", 0), lp.LivePlanRequestHandler,
                                          str(self.state), str(self.assets))
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.stop_event.set()
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)
        self.assertFalse(self.worker.is_alive())

    def request(self, path, method="GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            conn.request(method, path)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_invalid_metadata_never_served_and_head_empty(self):
        good = self.create()
        for field, value in (("revision", True), ("revision", "1"), ("history", []),
                             ("updatedAt", "bad"), ("status", []), ("taskId", "")):
            bad = copy.deepcopy(good)
            bad[field] = value
            self.store(bad)
            self.assertEqual(self.request("/api/plan")[0], 500)
            self.assertEqual(self.request("/api/plan", "HEAD"), (500, b""))

    def test_missing_then_pretty_legacy_sse_roundtrip_and_generation_reset(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        conn.request("GET", "/events")
        response = conn.getresponse()
        def event():
            name, data = None, []
            while True:
                line = response.fp.readline()
                if not line:
                    raise EOFError("SSE closed")
                line = line.decode("utf-8").rstrip("\r\n")
                if not line and name:
                    return name, json.loads("\n".join(data))
                if line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: "):
                    data.append(line[6:])
        try:
            self.assertEqual(event()[0], "state-error")
            legacy = proposal()
            legacy.update(schemaVersion=1, revision=1, updatedAt="2026-10-03T10:00:00Z",
                          history=[dict(revision=1, timestamp="2026-10-03T10:00:00Z", summary="Initial")])
            self.canonical.write_text(json.dumps(legacy, indent=2), encoding="utf-8")
            self.assertEqual(event(), ("plan", legacy))
            migrated = lp.publish(str(self.state), str(self.input), 1, "none", "User requested first step")
            self.assertEqual(event(), ("plan", migrated))
            # Replace between observations: no invalid poll needed to discover a fresh generation.
            changed = copy.deepcopy(migrated)
            changed["generation"] = "00000000-0000-4000-8000-000000000001"
            self.store(changed)
            self.assertEqual(event(), ("plan", changed))
        finally:
            response.close()
            conn.close()

    def test_symlink_canonical_assets_and_asset_containment(self):
        current = self.create()
        target = self.directory / "target.json"
        target.write_bytes(self.canonical.read_bytes())
        self.canonical.unlink()
        try:
            self.canonical.symlink_to(target)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"OS does not permit symlink creation: {error}")
        before = target.read_bytes()
        with self.assertRaises(RuntimeError):
            self.update(current)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(self.request("/api/plan")[0], 403)
        with self.assertRaises(PermissionError):
            self.read()
        outside = self.directory / "outside.js"
        outside.write_text("do not serve", encoding="utf-8")
        assets = self.directory / "assets"
        assets.mkdir()
        (assets / "app.js").symlink_to(outside)
        self.server.assets_dir = str(assets)
        self.assertEqual(self.request("/app.js")[0], 403)

    def test_asset_containment_even_without_symlink_flag(self):
        actual = lp.os.path.realpath
        asset = str(self.assets / "app.js")
        escaped = str(self.directory / "outside.js")
        with patch.object(lp.os.path, "realpath", side_effect=lambda path: escaped if path == asset else actual(path)):
            self.assertEqual(self.request("/app.js")[0], 403)


class ReadyOwnershipRegressions(Fixture):
    def test_real_server_cleanup_preserves_new_owner_and_serializes_check_delete(self):
        # Two actual serve() instances share readiness, each on its own port.
        ready = self.directory / "server.json"
        stops = {name: threading.Event() for name in ("A", "B")}
        errors = []
        threads = {}
        def run(name):
            try:
                lp.serve(str(self.state), 0, str(ready), str(ROOT / "assets/dashboard"), stops[name])
            except Exception as error:
                errors.append(error)
        def wait_ready(old_token=None):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    value = json.loads(ready.read_text(encoding="utf-8"))
                    if value["token"] != old_token:
                        return value
                except (OSError, ValueError):
                    pass
                time.sleep(0.02)
            self.fail(f"ready file did not advance: {errors}")
        def start(name):
            threads[name] = threading.Thread(target=run, args=(name,), name="ready-" + name, daemon=True)
            threads[name].start()
        try:
            start("A")
            first = wait_ready()
            start("B")
            second = wait_ready(first["token"])
            self.assertNotEqual(first["token"], second["token"])
            stops["A"].set()
            threads["A"].join(timeout=5)
            self.assertFalse(threads["A"].is_alive())
            self.assertEqual(json.loads(ready.read_text())["token"], second["token"])
            stops["B"].set()
            threads["B"].join(timeout=5)
            self.assertFalse(ready.exists())
            self.assertTrue((self.directory / "server.json.lock").exists())
        finally:
            for stop in stops.values():
                stop.set()
            for thread in threads.values():
                thread.join(timeout=5)
        self.assertEqual(errors, [])

    def test_ready_writer_waits_between_owner_check_and_delete(self):
        ready = self.directory / "server.json"
        checked, release, writer_attempted = threading.Event(), threading.Event(), threading.Event()
        stops = [threading.Event(), threading.Event()]
        errors = []
        real_load = json.load
        real_lock = lp.FileLock
        class ObservedLock(real_lock):
            def __enter__(lock):
                if threading.current_thread().name == "writer-B" and lock.lock_path.endswith("server.json.lock"):
                    writer_attempted.set()
                return super().__enter__()
        def paused_load(stream, *args, **kwargs):
            data = real_load(stream, *args, **kwargs)
            if threading.current_thread().name == "cleanup-A" and str(stream.name) == str(ready):
                checked.set()
                if not release.wait(5):
                    raise TimeoutError("cleanup gate not released")
            return data
        def serve(index):
            try:
                lp.serve(str(self.state), 0, str(ready), str(ROOT / "assets/dashboard"), stops[index])
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=serve, args=(i,), name=name, daemon=True)
                   for i, name in enumerate(("cleanup-A", "writer-B"))]
        def wait_file(predicate):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if predicate():
                    return
                time.sleep(0.02)
            self.fail(f"ready condition timed out: {errors}")
        started_b = False
        try:
            with patch.object(lp.json, "load", side_effect=paused_load), patch.object(lp, "FileLock", ObservedLock):
                threads[0].start()
                wait_file(ready.exists)
                first = json.loads(ready.read_text())
                stops[0].set()
                self.assertTrue(checked.wait(5))
                threads[1].start()
                started_b = True
                self.assertTrue(writer_attempted.wait(5))
                time.sleep(0.1)
                self.assertEqual(json.loads(ready.read_text())["token"], first["token"])
                release.set()
                threads[0].join(timeout=5)
                def replaced():
                    try:
                        return json.loads(ready.read_text())["token"] != first["token"]
                    except (OSError, ValueError):
                        return False
                wait_file(replaced)
                self.assertTrue(threads[1].is_alive())
        finally:
            release.set()
            for stop in stops:
                stop.set()
            threads[0].join(timeout=5)
            if started_b:
                threads[1].join(timeout=5)
        self.assertEqual(errors, [])
        self.assertFalse(ready.exists())

    def test_sigterm_handler_does_not_deadlock_serving_thread(self):
        import signal
        handlers = {}
        original = lp.ThreadedHTTPServer.serve_forever
        def run_and_signal(server, *args, **kwargs):
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            original(server, *args, **kwargs)
        with patch("signal.signal", side_effect=lambda signum, handler: handlers.update({signum: handler})), patch.object(lp.ThreadedHTTPServer, "serve_forever", run_and_signal):
            worker = threading.Thread(target=lp.serve, args=(str(self.state), 0, str(self.directory / "ready.json")), daemon=True)
            worker.start()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive(), "SIGTERM handler deadlocked the serving thread")
        self.assertFalse((self.directory / "ready.json").exists())


if __name__ == "__main__":
    unittest.main()
