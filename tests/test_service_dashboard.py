"""Read-only dashboard projections, committed freshness and HTTP safety."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import unittest
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from adaptive_planning import plans
from adaptive_planning.admin import initialize_installation, load_service_token
from adaptive_planning.app import build_app
from adaptive_planning.app import PlanningService
from adaptive_planning.config import Config, http_allowlist
from tests.service_fixtures import checkpoint, draft, make_service, step
from tests.test_service_http import call
from tests.test_service_plans import complete_all_steps, dump, insert_attempt, insert_store_hold, set_step_state


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.text = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


COMPACT_PLAN_KEYS = {"plan_id", "objective", "status", "revision", "published_at", "dashboard_url"}


def root_navigation(html):
    """Plan links and relative navigation queries from root HTML, read by href and visible text only."""
    page = Page(html)
    hrefs = [attrs["href"] for tag, attrs in page.tags if tag == "a" and "href" in attrs]
    navigation = {"after": [], "before": [], "first": []}
    for href in hrefs:
        parts = urlsplit(href)
        if parts.path in ("", "/") and not parts.scheme and not parts.netloc and parts.query:
            query = parse_qs(parts.query, keep_blank_values=True)
            kind = "after" if "after" in query else "before" if "before" in query else "first"
            navigation[kind].append(parts.query)
    plans = [unquote(href[len("/plans/"):]) for href in hrefs if href.startswith("/plans/")]
    return {"hrefs": hrefs, "plans": plans, "text": "".join(page.text), **navigation}


class TestService(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.f = make_service(self.root)
        self.p = self.f.create()
        self.plan_id = self.p.current_metadata.plan_id
        self.config = Config(self.root)
        initialize_installation(self.config)
        self.app = build_app(self.config)
        self.host, self.origin = http_allowlist(self.config)

    def request(self, path, *, query=b"", method="GET", authenticated=False, extra=()):
        headers = [("host", self.host.encode()), *extra]
        if authenticated:
            headers.append(("authorization", ("Bearer " + load_service_token(self.config)).encode()))
        return call(self.app, method, path, headers=headers, query=query)

    def test_multiple_plans_and_committed_freshness(self):
        from adaptive_planning.dashboard import render_plan_index

        other = self.f.create(creation_id="second-plan")
        html = render_plan_index(plans=self.f.service.list_plans())
        self.assertIn("/plans/" + self.plan_id, html)
        self.assertIn("/plans/" + other.current_metadata.plan_id, html)
        self.assertEqual(self.f.service.read_plan(self.plan_id).metadata.revision,
                         self.p.current_metadata.revision)
        self.assertIn("Rev #1", html)
        self.assertIn("2026-10-06T12:00:00+00:00", html)
        self.assertFalse([attrs for _, attrs in Page(html).tags if attrs.get("class") == "workflow-info"])

        def rolled_back(db):
            row = db.execute("SELECT payload FROM plans WHERE plan_id = ?", (self.plan_id,)).fetchone()
            payload = json.loads(row[0])
            payload["draft"]["objective"] = "NEVER COMMITTED"
            db.execute("UPDATE plans SET payload = ?, revision = 77 WHERE plan_id = ?",
                       (json.dumps(payload), self.plan_id))
            raise RuntimeError("synthetic rollback")

        with self.assertRaisesRegex(RuntimeError, "synthetic rollback"):
            self.f.store.write(rolled_back)
        self.assertEqual(render_plan_index(self.f.service.list_plans()), html)
        self.assertNotIn("NEVER COMMITTED", self.request("/")[2].decode())

    def test_composed_index_is_public_html_with_stable_links(self):
        other = self.f.create(creation_id="index-second")
        before = dump(self.f.store)
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", dict(headers)["content-type"])
        page = Page(body.decode())
        body_attrs = next(attrs for tag, attrs in page.tags if tag == "body")
        self.assertEqual(body_attrs["data-mode"], "service")
        self.assertEqual(body_attrs["data-plans-url"], "/api/plan-index")
        links = [attrs.get("href") for tag, attrs in page.tags if tag == "a"]
        self.assertIn("/plans/" + self.plan_id, links)
        self.assertIn("/plans/" + other.current_metadata.plan_id, links)
        self.assertEqual(dump(self.f.store), before)

    def test_index_escapes_public_text_and_omits_absent_workflow(self):
        from adaptive_planning.dashboard import render_plan_index

        view = self.f.service.read_plan(self.plan_id)
        unsafe = '<script>globalThis.executed = true</script> & "quoted"'
        changed = dataclasses.replace(view, draft=dataclasses.replace(
            view.draft, objective=unsafe, workflow={"name": unsafe}))
        html = render_plan_index((changed, view))
        page = Page(html)
        self.assertIn(unsafe, "".join(page.text))
        scripts = [attrs for tag, attrs in page.tags if tag == "script"]
        self.assertEqual(scripts, [{"src": "/app.js"}])
        self.assertEqual(len([attrs for _, attrs in page.tags if attrs.get("class") == "workflow-info"]), 1)
        self.assertNotIn(self.p.private.orchestrator_token, html)

    def test_plan_page_uses_external_assets_and_public_attribute_configuration(self):
        status, headers, body = self.request("/plans/" + self.plan_id)
        self.assertEqual(status, 200)
        page = Page(body.decode())
        attrs = next(attrs for tag, attrs in page.tags if tag == "body")
        self.assertEqual(attrs["data-mode"], "service")
        self.assertEqual(attrs["data-plan-id"], self.plan_id)
        self.assertEqual(attrs["data-plan-url"], "/api/plans/" + self.plan_id)
        self.assertEqual(attrs["data-coordination-url"], "/api/plans/" + self.plan_id + "/coordination")
        self.assertEqual(attrs["data-events-url"], "/api/plans/" + self.plan_id + "/events")
        self.assertEqual(attrs["data-recovery-warnings-url"], "/api/plans/" + self.plan_id + "/recovery-warnings")
        self.assertEqual(len([attrs for _, attrs in page.tags if attrs.get("id") == "recovery-warnings"]), 1)
        self.assertEqual([a for t, a in page.tags if t == "script"], [{"src": "/app.js"}])
        self.assertIn("Host edits are not controlled", body.decode())
        self.assertIn("read-only", body.decode().lower())
        self.assertNotIn(self.p.private.orchestrator_token, body.decode())
        self.assertEqual(dict(headers)["cache-control"], "no-store")
        query_status, _, query_body = self.request("/plans/" + self.plan_id,
                                                  query=b"token=untrusted-query-marker&limit=malicious")
        self.assertEqual(query_status, 200)
        self.assertEqual(query_body, body)
        self.assertNotIn(b"untrusted-query-marker", query_body)

    def warning_app(self, threshold=6):
        from starlette.applications import Starlette
        from adaptive_planning.dashboard import dashboard_routes
        self.f.service = PlanningService(self.f.store, base_url="http://127.0.0.1:8790", clock=self.f.clock,
                                         stale_seconds=threshold)
        self.app = Starlette(routes=dashboard_routes(self.f.service))

    def warnings(self, method="GET"):
        return self.request("/api/plans/" + self.plan_id + "/recovery-warnings", method=method)

    def test_recovery_warnings_are_pure_custom_policy_and_time_only_observations(self):
        self.warning_app()
        assigned = self.f.assign(self.p)
        before = dump(self.f.store)
        self.f.clock.advance(5)
        status, headers, body = self.warnings()
        self.assertEqual(status, 200)
        first = json.loads(body)
        self.assertEqual(set(first), {"metadata", "store_sequence", "observed_at", "threshold_seconds", "overdue", "unassessable_activity"})
        self.assertEqual((first["threshold_seconds"], first["overdue"], first["unassessable_activity"]), (6, [], []))
        self.f.clock.advance(1)
        status, _, body = self.warnings()
        second = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(second["metadata"], first["metadata"])
        self.assertEqual(second["store_sequence"], first["store_sequence"])
        self.assertNotEqual(second["observed_at"], first["observed_at"])
        self.assertEqual(second["overdue"], [{"attempt_id": assigned.committed["attempt_id"], "version": 1,
            "state": "assigned", "last_activity": "2026-10-06T12:00:00+00:00"}])
        self.assertEqual(dict(headers)["cache-control"], "no-store")
        self.assertNotIn(self.p.private.orchestrator_token.encode(), body)
        self.assertNotIn(assigned.private.attempt_token.encode(), body)
        self.assertNotIn(b"context", body)
        status, _, body = self.warnings("HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(dump(self.f.store), before)

    def test_recovery_warnings_cover_all_current_attempts_beyond_summary(self):
        self.warning_app()
        self.p = self.f.create(steps=tuple(step(f"s{i}") for i in range(102)), creation_id="warning-pages")
        self.plan_id = self.p.current_metadata.plan_id
        for i in range(102):
            self.f.assign(self.p, f"s{i}")
        before = dump(self.f.store)
        self.f.clock.advance(6)
        status, _, body = self.warnings()
        self.assertEqual(status, 200)
        value = json.loads(body)
        self.assertEqual(len(value["overdue"]), 102)
        self.assertEqual(len({item["attempt_id"] for item in value["overdue"]}), 102)
        view = self.f.service.read_plan(self.plan_id)
        self.assertLess(len(view.coordination.attempts), 102)
        self.assertEqual(value["metadata"], plans.to_jsonable(view.metadata))
        self.assertEqual(value["store_sequence"], view.coordination.store_sequence)
        self.assertEqual(dump(self.f.store), before)

    def test_recovery_warnings_states_malformed_timestamps_and_foreign_store_watermark(self):
        self.warning_app()
        assigned = self.f.assign(self.p)
        active = self.f.start(self.p, self.f.assign(self.p, "b"))
        self.f.submit(self.p, active, checkpoint())
        self.f.clock.advance(6)
        status, _, body = self.warnings()
        value = json.loads(body)
        self.assertEqual({item["state"] for item in value["overdue"]}, {"assigned", "submitted"})
        first_sequence = value["store_sequence"]
        first_metadata = value["metadata"]
        self.f.create(creation_id="foreign-warning-watermark")
        value = json.loads(self.warnings()[2])
        self.assertEqual(value["metadata"], first_metadata)
        self.assertGreater(value["store_sequence"], first_sequence)
        def malformed(db):
            row = db.execute("SELECT payload FROM attempts WHERE plan_id=? AND attempt_id=?",
                             (self.plan_id, assigned.committed["attempt_id"])).fetchone()
            data = json.loads(row[0])
            data["last_activity"] = "2026-10-06T12:00:00"
            db.execute("UPDATE attempts SET payload=? WHERE plan_id=? AND attempt_id=?",
                       (json.dumps(data), self.plan_id, assigned.committed["attempt_id"]))
            db.execute("UPDATE attempts SET state='accepted' WHERE plan_id=? AND attempt_id=?",
                       (self.plan_id, active.committed["attempt_id"]))
        self.f.store.write(malformed)
        before = dump(self.f.store)
        value = json.loads(self.warnings()[2])
        self.assertEqual(value["overdue"], [])
        self.assertEqual([item["attempt_id"] for item in value["unassessable_activity"]], [assigned.committed["attempt_id"]])
        self.assertEqual(dump(self.f.store), before)

    def test_recovery_warning_read_never_sweeps_and_excludes_recorded_stale(self):
        self.warning_app()
        active = self.f.start(self.p, self.f.assign(self.p))
        self.f.clock.advance(6)
        self.assertEqual(json.loads(self.warnings()[2])["overdue"][0]["state"], "active")
        self.f.service.execute(self.f.command("sweep_stale", {}, receipt=self.p))
        before = dump(self.f.store)
        self.assertEqual(json.loads(self.warnings()[2])["overdue"], [])
        self.assertEqual(self.f.service.read_plan(self.plan_id).coordination.attempts[0].state, "stale")
        self.assertEqual(dump(self.f.store), before)

    def test_recovery_unassessable_activity_keeps_exact_canonical_display_tuple(self):
        self.warning_app()
        active = self.f.start(self.p, self.f.assign(self.p))
        self.f.clock.advance(6)
        for activity in (None, "", "not-a-timestamp", "2026-10-06T12:00:00", "2026-10-06T12:00:07+00:00"):
            with self.subTest(activity=activity):
                def mutate(db):
                    row = db.execute("SELECT payload FROM attempts WHERE plan_id=? AND attempt_id=?",
                                     (self.plan_id, active.committed["attempt_id"])).fetchone()
                    data = json.loads(row[0])
                    if activity is None:
                        data.pop("last_activity", None)
                    else:
                        data["last_activity"] = activity
                    db.execute("UPDATE attempts SET payload=? WHERE plan_id=? AND attempt_id=?",
                               (json.dumps(data), self.plan_id, active.committed["attempt_id"]))
                self.f.store.write(mutate)
                before = hashlib.sha256(repr(dump(self.f.store)).encode()).hexdigest()
                value = json.loads(self.warnings()[2])
                current = self.f.service.read_plan(self.plan_id).coordination.attempts[0]
                self.assertEqual(value["overdue"], [])
                self.assertEqual(value["unassessable_activity"], [{"attempt_id": current.attempt_id,
                    "version": current.version, "state": current.state, "last_activity": current.last_activity}])
                self.assertEqual(hashlib.sha256(repr(dump(self.f.store)).encode()).hexdigest(), before)

    def test_recovery_warning_endpoint_rejects_queries_and_missing_plan(self):
        self.warning_app()
        before = dump(self.f.store)
        status, _, _ = self.request("/api/plans/" + self.plan_id + "/recovery-warnings", query=b"now=1")
        self.assertEqual(status, 400)
        self.assertEqual(self.request("/api/plans/missing/recovery-warnings")[0], 404)
        self.assertEqual(dump(self.f.store), before)

    def test_public_json_matches_committed_projections_without_tokens(self):
        before = dump(self.f.store)
        endpoints = {
            "/api/plans": {"plans": plans.to_jsonable(self.f.service.list_plans(include_terminal=True))},
            "/api/plans/" + self.plan_id: plans.to_jsonable(self.f.service.read_plan(self.plan_id)),
            "/api/plans/" + self.plan_id + "/coordination":
                plans.to_jsonable(self.f.service.read_coordination(self.plan_id)),
            "/api/plans/" + self.plan_id + "/events":
                {"events": plans.to_jsonable(self.f.service.read_events(self.plan_id))},
        }
        for path, expected in endpoints.items():
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), expected)
                self.assertEqual(dict(headers)["cache-control"], "no-store")
                self.assertNotIn(self.p.private.orchestrator_token.encode(), body)
                self.assertFalse(any(k.startswith("access-control-") for k, _ in headers))
        self.assertEqual(dump(self.f.store), before)

    def test_coordination_pages_and_revision_conflicts_are_public(self):
        insert_attempt(self.f.store, self.plan_id, "att-a", "a", "active")
        insert_attempt(self.f.store, self.plan_id, "att-b", "b", "active")
        path = "/api/plans/" + self.plan_id + "/coordination"
        status, _, body = self.request(path, query=b"limit=1")
        self.assertEqual(status, 200)
        first = json.loads(body)
        self.assertEqual(len(first["items"]), 1)
        self.assertEqual(first["total_counts"]["attempts"], 2)
        from urllib.parse import urlencode
        query = urlencode({"after": first["next_cursor"], "limit": 1}).encode()
        status, _, body = self.request(path, query=query)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["items"][0]["value"]["attempt_id"], "att-b")
        self.f.service.execute(self.f.command("revise_plan", {
            "draft": self.f.service.read_plan(self.plan_id).draft,
            "reason": "committed revision", "authorization_change": None,
            "retire_steps": (),
        }, receipt=self.p))
        status, _, body = self.request(path, query=query)
        self.assertEqual(status, 409)
        error = json.loads(body)["error"]
        self.assertEqual(error["code"], "conflict")
        self.assertEqual(error["current_metadata"]["revision"], 2)

    def test_event_after_sequence_does_not_skip_backlog(self):
        path = "/api/plans/" + self.plan_id + "/events"
        published = self.f.service.read_events(self.plan_id)[0]
        self.f.service.execute(self.f.command("takeover", {
            "expected_ownership_version": 1, "reason": "synthetic second event",
        }, receipt=self.p))
        status, _, body = self.request(path, query=b"limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["events"][0]["sequence"], published.sequence)
        status, _, body = self.request(path, query=f"after={published.sequence}&limit=1".encode())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["events"][0]["kind"], "takeover")

    def test_invalid_queries_not_found_and_read_only_methods(self):
        for suffix, query in (("events", b"after=-1"), ("events", b"after=1.5"),
                              ("events", b"limit=0"), ("coordination", b"limit=501"),
                              ("coordination", b"limit=true"), ("events", b"limit=1&limit=2"),
                              ("events", b"unexpected=x")):
            with self.subTest(suffix=suffix, query=query):
                status, _, body = self.request("/api/plans/" + self.plan_id + "/" + suffix, query=query)
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["error"]["code"], "invalid")
        status, _, body = self.request("/api/plans/missing")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")
        before = dump(self.f.store)
        with self.assertLogs("adaptive_planning.http", level="WARNING"):
            self.assertEqual(self.request("/api/plans", method="POST")[0], 401)
        # The root MCP mount may answer 404 after the dashboard's GET-only route
        # declines this method. Neither response enables a mutation handler.
        self.assertIn(self.request("/api/plans", method="POST", authenticated=True)[0], (404, 405))
        self.assertEqual(dump(self.f.store), before)

    def test_dashboard_keeps_host_origin_and_mcp_request_boundaries(self):
        with self.assertLogs("adaptive_planning.http", level="WARNING"):
            for path in ("/", "/app.js", "/api/plans", "/plans/" + self.plan_id):
                self.assertEqual(call(self.app, path=path, headers=(("host", b"evil.example"),))[0], 403)
                self.assertEqual(self.request(path, extra=(("origin", b"https://evil.example"),))[0], 403)
            self.assertEqual(self.request("/mcp")[0], 401)

    def test_packaged_assets_and_csp_disallow_inline_and_framing(self):
        for path, media in (("/", "text/html"), ("/plans/" + self.plan_id, "text/html"),
                            ("/app.js", "javascript"), ("/style.css", "text/css")):
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, 200)
                values = dict(headers)
                self.assertIn(media, values["content-type"])
                csp = values["content-security-policy"]
                self.assertIn("script-src 'self'", csp)
                self.assertIn("style-src 'self'", csp)
                self.assertIn("frame-ancestors 'none'", csp)
                self.assertIn("object-src 'none'", csp)
                self.assertNotIn("unsafe-inline", csp)
                self.assertNotIn("unsafe-eval", csp)
                self.assertEqual(values["x-content-type-options"], "nosniff")
                self.assertTrue(body)

    def test_installed_asset_lookup_precedes_checkout_fallback(self):
        installed = self.root / "synthetic-package"
        assets = installed / "assets" / "dashboard"
        assets.mkdir(parents=True)
        (assets / "style.css").write_text("/* installed stylesheet */")
        with mock.patch("adaptive_planning.dashboard.resources.files", return_value=installed):
            status, _, body = self.request("/style.css")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"/* installed stylesheet */")

    def test_aggregate_public_size_failure_is_not_truncated(self):
        self.f.create(creation_id="aggregate-second")
        one = plans.canonical_bytes(self.f.service.read_plan(self.plan_id))
        with mock.patch("adaptive_planning.plans.MAX_PAGE_BYTES", len(one) + 8):
            self.assertEqual(self.request("/api/plans/" + self.plan_id)[0], 200)
            status, _, body = self.request("/api/plans")
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body)["error"]["code"], "limit_exceeded")
        self.assertNotIn("plans", json.loads(body))

    def test_oversized_coordination_row_is_reported_without_truncation(self):
        insert_attempt(self.f.store, self.plan_id, "large", "a", "active", {"last_activity": "X" * 2000})
        with mock.patch("adaptive_planning.plans.MAX_PAGE_BYTES", 1000):
            status, _, body = self.request("/api/plans/" + self.plan_id + "/coordination")
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body)["error"]["code"], "limit_exceeded")

    def test_corrupt_reads_keep_constant_public_500(self):
        self.f.store.write(lambda db: db.execute("UPDATE plans SET payload = ? WHERE plan_id = ?",
                                                ('{CORRUPT PRIVATE MARKER', self.plan_id)))
        with self.assertLogs("adaptive_planning.http", level="ERROR") as logs:
            status, _, body = self.request("/api/plans/" + self.plan_id)
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body), {"error": {
            "code": "internal_error", "message": "The request could not be completed."}})
        self.assertNotIn("CORRUPT PRIVATE MARKER", body.decode() + " ".join(logs.output))

    def test_actual_aggregate_capacity_keeps_compact_index_and_root_available(self):
        created, tokens = [self.plan_id], [self.p.private.orchestrator_token]
        for n in (1, 2):
            large = dataclasses.replace(draft(), objective="Synthetic independent capacity " + str(n),
                                        facts=("x" * 10000,) * 600)
            receipt = self.f.service.execute(self.f.command("create_plan", {
                "creation_id": "independent-capacity-" + str(n), "draft": large,
                "new_plan_intent": "Synthetic independent capacity plan " + str(n) + " for the compact index"}))
            created.append(receipt.current_metadata.plan_id)
            tokens.append(receipt.private.orchestrator_token)
        self.assertEqual(len(set(created)), 3)
        for plan_id in created:
            with self.subTest(plan_id=plan_id):
                self.assertEqual(self.request("/api/plans/" + plan_id)[0], 200)
        status, _, body = self.request("/api/plans")
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body)["error"]["code"], "limit_exceeded")
        status, _, body = self.request("/api/plan-index")
        self.assertEqual(status, 200)
        self.assertLess(len(body), 100_000)
        self.assertNotIn(b"x" * 1000, body)
        index = json.loads(body)
        self.assertEqual(len(index["plans"]), 3)
        self.assertEqual({item["plan_id"] for item in index["plans"]}, set(created))
        for item in index["plans"]:
            view = self.f.service.read_plan(item["plan_id"])
            self.assertEqual(set(item), COMPACT_PLAN_KEYS)
            self.assertEqual({key: item[key] for key in COMPACT_PLAN_KEYS - {"dashboard_url"}}, {
                "plan_id": view.plan_id, "objective": view.draft.objective, "status": view.status,
                "revision": view.metadata.revision, "published_at": view.published_at})
            url = urlsplit(item["dashboard_url"])
            self.assertTrue(url.scheme and url.netloc)
            self.assertEqual((url.path, url.query, url.fragment), ("/plans/" + quote(item["plan_id"], safe=""), "", ""))
        status, _, html = self.request("/")
        self.assertEqual(status, 200)
        self.assertNotIn(b"x" * 1000, html)
        root = root_navigation(html.decode())
        self.assertEqual(len(root["plans"]), 3)
        self.assertEqual(set(root["plans"]), set(created))
        self.assertFalse([href for href in root["hrefs"] if urlsplit(href).scheme or urlsplit(href).netloc])
        for token in tokens:
            self.assertNotIn(token.encode(), body + html)
        for plan_id in created:
            self.assertIn(plan_id, root["text"])

    def test_compact_plan_index_and_root_pages_cover_all_plans_newest_first(self):
        second = self.f.create(creation_id="index-page-second").current_metadata.plan_id
        third = self.f.create(creation_id="index-page-third").current_metadata.plan_id
        expected = [third, second, self.plan_id]
        stored = dump(self.f.store)

        def read(**query):
            status, _, body = self.request("/api/plan-index", query=urlencode(query).encode())
            self.assertEqual(status, 200)
            value = json.loads(body)
            self.assertLessEqual({"plans", "next_cursor", "previous_cursor"}, set(value))
            metadata = value["metadata"] if isinstance(value.get("metadata"), dict) else value
            self.assertLessEqual({"epoch", "collection_fence", "store_sequence"}, set(metadata))
            for item in value["plans"]:
                self.assertEqual(set(item), COMPACT_PLAN_KEYS)
            return value

        def ids(value):
            return [item["plan_id"] for item in value["plans"]]

        everything = read()
        self.assertEqual(ids(everything), expected)
        self.assertEqual((everything["next_cursor"], everything["previous_cursor"]), (None, None))
        forward = [read(limit=1)]
        for _ in expected:
            if forward[-1]["next_cursor"] is None:
                break
            forward.append(read(after=forward[-1]["next_cursor"], limit=1))
        else:
            self.fail("Forward paging did not reach the final page")
        self.assertEqual([ids(page) for page in forward], [[plan_id] for plan_id in expected])
        self.assertIsNone(forward[0]["previous_cursor"])
        self.assertTrue(all(page["previous_cursor"] for page in forward[1:]))
        reverse = [forward[-1]]
        for _ in expected:
            if reverse[-1]["previous_cursor"] is None:
                break
            reverse.append(read(before=reverse[-1]["previous_cursor"], limit=1))
        else:
            self.fail("Reverse paging did not reach the newest page")
        self.assertEqual([ids(page) for page in reverse], [[plan_id] for plan_id in reversed(expected)])
        self.assertTrue(all(page["next_cursor"] for page in reverse[1:]))
        wide = read(before=forward[-1]["previous_cursor"], limit=2)
        self.assertEqual(ids(wide), [third, second])
        self.assertIsNone(wide["previous_cursor"])
        self.assertTrue(wide["next_cursor"])

        def root(query):
            status, _, body = self.request("/", query=query.encode())
            self.assertEqual(status, 200)
            view = root_navigation(body.decode())
            for key in ("after", "before", "first"):
                for link in view[key]:
                    self.assertEqual(parse_qs(link).get("limit"), ["1"])
            for label, key in (("Older", "after"), ("Newer", "before"), ("First", "first")):
                if view[key]:
                    self.assertIn(label, view["text"])
            return view

        views = [root("limit=1")]
        for _ in expected:
            if not views[-1]["after"]:
                break
            link = views[-1]["after"][0]
            self.assertEqual(parse_qs(link).get("after"), [forward[len(views) - 1]["next_cursor"]])
            views.append(root(link))
        else:
            self.fail("Root Older links did not reach the final page")
        self.assertEqual([view["plans"] for view in views], [[plan_id] for plan_id in expected])
        self.assertTrue(views[0]["after"])
        self.assertEqual(views[0]["before"], [])
        self.assertTrue(all(view["before"] and view["first"] for view in views[1:]))
        walked = [views[-1]]
        for _ in expected:
            if not walked[-1]["before"]:
                break
            link = walked[-1]["before"][0]
            self.assertEqual(parse_qs(link).get("before"), [forward[len(expected) - len(walked)]["previous_cursor"]])
            walked.append(root(link))
        else:
            self.fail("Root Newer links did not reach the first page")
        self.assertEqual([view["plans"] for view in walked], [[plan_id] for plan_id in reversed(expected)])
        first = views[-1]["first"][0]
        self.assertEqual(parse_qs(first), {"limit": ["1"]})
        self.assertEqual(root(first)["plans"], [third])
        self.assertEqual(dump(self.f.store), stored)

    def test_compact_plan_index_rejects_invalid_queries_and_foreign_cursors(self):
        self.f.create(creation_id="index-invalid-second")
        insert_attempt(self.f.store, self.plan_id, "cursor-a", "a", "active")
        insert_attempt(self.f.store, self.plan_id, "cursor-b", "b", "active")
        coordination_cursor = self.f.service.read_coordination(self.plan_id, limit=1).next_cursor
        self.assertTrue(coordination_cursor)
        status, _, body = self.request("/api/plan-index", query=b"limit=1")
        self.assertEqual(status, 200)
        valid = json.loads(body)["next_cursor"]
        self.assertTrue(valid)
        queries = [b"unexpected=x", b"limit=1&limit=2", b"limit=1&unexpected=x", b"limit=",
                   b"limit=0", b"limit=101", b"limit=-1", b"limit=true", b"limit=1.5",
                   urlencode([("after", valid), ("after", valid)]).encode(),
                   urlencode([("before", valid), ("before", valid)]).encode(),
                   urlencode({"after": valid, "before": valid}).encode(),
                   urlencode({"after": valid, "before": valid, "limit": 1}).encode(),
                   urlencode({"after": valid, "unexpected": "x"}).encode(),
                   urlencode({"after": valid, "limit": 0}).encode(),
                   urlencode({"before": valid, "limit": 101}).encode()]
        for name in ("after", "before"):
            for cursor in ("not-a-cursor", "!!!", "A" * 20000, coordination_cursor):
                queries.append(urlencode({name: cursor, "limit": 1}).encode())
        stored = dump(self.f.store)
        for query in queries:
            with self.subTest(query=query[:120]):
                status, _, body = self.request("/api/plan-index", query=query)
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["error"]["code"], "invalid")
        self.assertEqual(dump(self.f.store), stored)
        self.assertEqual(self.request("/api/plan-index", query=urlencode({"after": valid, "limit": 1}).encode())[0], 200)

    def test_plan_index_cursor_survives_existing_changes_but_not_inserts_or_epoch(self):
        other = self.f.create(creation_id="cursor-lifecycle-second").current_metadata.plan_id

        def page(**query):
            status, _, body = self.request("/api/plan-index", query=urlencode(query).encode())
            self.assertNotEqual(status, 404, "/api/plan-index endpoint is missing")
            return status, json.loads(body)

        def identities(value):
            return ([item["plan_id"] for item in value["plans"]], value["next_cursor"],
                    value["previous_cursor"], value["epoch"], value["collection_fence"])

        status, first = page(limit=1)
        self.assertEqual(status, 200)
        self.assertEqual([item["plan_id"] for item in first["plans"]], [other])
        cursor = first["next_cursor"]
        self.assertTrue(cursor)
        status, baseline = page(after=cursor, limit=1)
        self.assertEqual(status, 200)
        self.assertEqual([item["plan_id"] for item in baseline["plans"]], [self.plan_id])
        self.assertEqual(set(baseline["plans"][0]), COMPACT_PLAN_KEYS)
        sequences = [baseline["store_sequence"]]

        def assert_continuation_survives():
            status, current = page(after=cursor, limit=1)
            self.assertEqual(status, 200)
            self.assertEqual(identities(current), identities(baseline))
            self.assertGreater(current["store_sequence"], sequences[-1])
            sequences.append(current["store_sequence"])

        self.f.service.execute(self.f.command("revise_plan", {
            "draft": self.f.service.read_plan(self.plan_id).draft, "reason": "cursor-preserving revision",
            "authorization_change": None, "retire_steps": ()}, receipt=self.p))
        assert_continuation_survives()
        active = self.f.start(self.p, self.f.assign(self.p))
        assert_continuation_survives()
        heartbeat = self.f.command("report_activity", {
            "origin_worker": "worker-a", "relay": None, "observed_activity": "cursor heartbeat",
        }, receipt=self.p, attempt=active)
        self.f.service.execute(dataclasses.replace(heartbeat, expected_revision=None, ownership_version=None))
        assert_continuation_survives()
        newest = self.f.create(creation_id="cursor-lifecycle-inserted").current_metadata.plan_id
        status, error = page(after=cursor, limit=1)
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "conflict")
        status, headers, body = self.request("/", query=urlencode({"after": cursor, "limit": 1}).encode())
        self.assertIn(status, (200, 409))
        self.assertIn("text/html", dict(headers)["content-type"])
        html = body.decode()
        self.assertFalse(html.lstrip().startswith("{"))
        navigation = root_navigation(html)
        visible = navigation["text"].lower()
        self.assertTrue("changed" in visible or "restart" in visible, visible)
        self.assertIn("First", navigation["text"])
        firsts = [link for link in navigation["first"] if parse_qs(link) == {"limit": ["1"]}]
        self.assertEqual(len(firsts), 1)
        status, _, body = self.request("/", query=firsts[0].encode())
        self.assertEqual(status, 200)
        self.assertEqual(root_navigation(body.decode())["plans"], [newest])
        status, fresh = page(limit=1)
        self.assertEqual((status, [item["plan_id"] for item in fresh["plans"]]), (200, [newest]))
        fresh_cursor = fresh["next_cursor"]
        self.assertTrue(fresh_cursor)
        self.assertEqual(page(after=fresh_cursor, limit=1)[0], 200)
        self.f.store.write(lambda db: db.execute(
            "UPDATE installation SET epoch=? WHERE id=1", ("00000000-0000-4000-8000-000000000001",)))
        status, error = page(after=fresh_cursor, limit=1)
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "conflict")

    def test_compact_and_full_status_match_canonical_authority_and_malformed_authority_fails_closed(self):
        def revised(receipt, **changes):
            base = self.f.service.read_plan(receipt.current_metadata.plan_id).draft
            self.f.service.execute(self.f.command("revise_plan", {
                "draft": dataclasses.replace(base, **changes), "reason": "synthetic canonical status",
                "authorization_change": None, "retire_steps": ()}, receipt=receipt))

        running = self.f.create(creation_id="canonical-running").current_metadata.plan_id
        set_step_state(self.f.store, running, "a", "in-progress")
        insert_attempt(self.f.store, running, "canonical-running-attempt", "a", "active")
        blocked_receipt = self.f.create(creation_id="canonical-blocked")
        revised(blocked_receipt, block_reason="needs input")
        stopped_receipt = self.f.create(creation_id="canonical-stopped")
        revised(stopped_receipt, stop_reason="user stopped")
        complete = self.f.create(creation_id="canonical-complete").current_metadata.plan_id
        complete_all_steps(self.f.store, complete)
        blocked = blocked_receipt.current_metadata.plan_id
        stopped = stopped_receipt.current_metadata.plan_id

        def assert_canonical(expected):
            for plan_id, literal in expected.items():
                with self.subTest(plan_id=plan_id, literal=literal):
                    self.assertEqual(self.f.service.read_plan(plan_id).status, literal)
                    status, _, body = self.request("/api/plans/" + plan_id)
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(body)["status"], literal)
            status, _, body = self.request("/api/plan-index")
            self.assertEqual(status, 200)
            self.assertEqual({item["plan_id"]: item["status"] for item in json.loads(body)["plans"]}, expected)

        assert_canonical({self.plan_id: "planning", running: "running", blocked: "blocked",
                          stopped: "stopped", complete: "complete"})
        insert_store_hold(self.f.store, self.f.store.installation_epoch())
        assert_canonical({self.plan_id: "blocked", running: "blocked", blocked: "blocked",
                          stopped: "stopped", complete: "blocked"})
        for stop_reason in ("", 7, ["user stopped"]):
            def corrupt(db):
                row = db.execute("SELECT payload FROM plans WHERE plan_id = ?", (stopped,)).fetchone()
                payload = json.loads(row[0])
                payload["draft"]["stop_reason"] = stop_reason
                db.execute("UPDATE plans SET payload = ? WHERE plan_id = ?", (json.dumps(payload), stopped))
            self.f.store.write(corrupt)
            for path in ("/api/plans/" + stopped, "/api/plan-index"):
                with self.subTest(stop_reason=stop_reason, path=path):
                    status, _, body = self.request(path)
                    self.assertEqual(status, 400)
                    decoded = json.loads(body)
                    self.assertEqual(set(decoded), {"error"})
                    self.assertEqual(decoded["error"]["code"], "invalid")

    def test_plan_index_rejects_out_of_range_cursor_keys(self):
        import base64

        status, _, body = self.request("/api/plan-index")
        self.assertEqual(status, 200)
        data = json.loads(body)
        epoch = data["epoch"]
        fence = data["collection_fence"]
        count, max_rowid = fence["count"], fence["max_rowid"]

        def make_cursor(direction, key):
            raw = json.dumps({"c": count, "d": direction, "e": epoch, "k": key, "m": max_rowid,
                              "t": "plan-index", "v": 1},
                             sort_keys=True, separators=(",", ":")).encode("utf-8")
            return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

        for direction in ("after", "before"):
            for key in (2**63, 2**63 - 1):
                cursor = make_cursor(direction, key)
                status, _, body = self.request("/api/plan-index", query=urlencode({direction: cursor, "limit": 1}).encode())
                self.assertEqual(status, 400)
                decoded = json.loads(body)
                self.assertEqual(set(decoded), {"error"})
                self.assertEqual(decoded["error"]["code"], "invalid")
                self.assertNotIn("Traceback", body.decode())
                self.assertNotIn("sqlite", body.decode().lower())

    def test_plan_index_navigation_accepts_largest_stored_rowid(self):
        second = self.f.create(creation_id="largest-rowid-second").current_metadata.plan_id
        third = self.f.create(creation_id="largest-rowid-third").current_metadata.plan_id
        self.f.store.write(lambda db: db.execute(
            "UPDATE plans SET rowid = ? WHERE plan_id = ?", (9223372036854775807, self.plan_id)))
        status, _, body = self.request("/api/plan-index", query=b"limit=1")
        self.assertEqual(status, 200)
        page1 = json.loads(body)
        self.assertEqual([p["plan_id"] for p in page1["plans"]], [self.plan_id])
        next_cursor = page1["next_cursor"]
        self.assertTrue(next_cursor)
        status, _, body = self.request("/api/plan-index", query=urlencode({"after": next_cursor, "limit": 1}).encode())
        self.assertEqual(status, 200)
        page2 = json.loads(body)
        self.assertEqual([p["plan_id"] for p in page2["plans"]], [third])
        prev_cursor = page2["previous_cursor"]
        self.assertTrue(prev_cursor)
        status, _, body = self.request("/api/plan-index", query=urlencode({"before": prev_cursor, "limit": 1}).encode())
        self.assertEqual(status, 200)
        page_back = json.loads(body)
        self.assertEqual([p["plan_id"] for p in page_back["plans"]], [self.plan_id])
        import base64
        payload = {
            "c": page_back["collection_fence"]["count"],
            "d": "before",
            "e": page_back["epoch"],
            "k": 9223372036854775807,
            "m": page_back["collection_fence"]["max_rowid"],
            "t": "plan-index",
            "v": 1,
        }
        forged = base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).decode("ascii").rstrip("=")
        status, _, body = self.request("/api/plan-index", query=urlencode({"before": forged, "limit": 1}).encode())
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid")

    def test_default_plan_index_navigation_carries_limit(self):
        created = [self.f.create(creation_id=f"default-nav-{i}").current_metadata.plan_id for i in range(26)]
        all_expected = list(reversed(created)) + [self.plan_id]
        self.assertEqual(len(all_expected), 27)
        first_25 = all_expected[:25]
        last_2 = all_expected[25:]
        self.assertEqual(len(first_25), 25)
        self.assertEqual(len(last_2), 2)
        status, _, body = self.request("/")
        self.assertEqual(status, 200)
        page1 = root_navigation(body.decode())
        self.assertEqual(len(page1["plans"]), 25)
        self.assertEqual(page1["plans"], first_25)
        for key in ("after", "before", "first"):
            for link in page1[key]:
                self.assertEqual(parse_qs(link).get("limit"), ["25"])
        self.assertEqual(len(page1["after"]), 1)
        status, _, body = self.request("/", query=page1["after"][0].encode())
        self.assertEqual(status, 200)
        page2 = root_navigation(body.decode())
        self.assertEqual(len(page2["plans"]), 2)
        self.assertEqual(page2["plans"], last_2)
        for key in ("after", "before", "first"):
            for link in page2[key]:
                self.assertEqual(parse_qs(link).get("limit"), ["25"])
        self.assertEqual(len(page2["before"]), 1)
        self.assertEqual(len(page2["first"]), 1)
        status, _, body = self.request("/", query=page2["before"][0].encode())
        self.assertEqual(status, 200)
        newer_page = root_navigation(body.decode())
        self.assertEqual(len(newer_page["plans"]), 25)
        self.assertEqual(newer_page["plans"], first_25)
        status, _, body = self.request("/", query=page2["first"][0].encode())
        self.assertEqual(status, 200)
        first_page = root_navigation(body.decode())
        self.assertEqual(len(first_page["plans"]), 25)
        self.assertEqual(first_page["plans"], first_25)

    def test_conflicting_root_retains_failed_poll_cursor(self):
        self.f.create(creation_id="conflict-poll-second")
        status, _, body = self.request("/api/plan-index", query=b"limit=1")
        self.assertEqual(status, 200)
        cursor = json.loads(body)["next_cursor"]
        self.assertTrue(cursor)
        self.f.create(creation_id="conflict-poll-inserted")
        status, _, body = self.request("/", query=urlencode({"after": cursor, "limit": 1}).encode())
        self.assertEqual(status, 409)
        html = body.decode()
        nav = root_navigation(html)
        self.assertIn("First", nav["text"])
        firsts = [link for link in nav["first"] if parse_qs(link) == {"limit": ["1"]}]
        self.assertEqual(len(firsts), 1)
        page = Page(html)
        body_attrs = next(attrs for tag, attrs in page.tags if tag == "body")
        plans_url = body_attrs["data-plans-url"]
        parts = urlsplit(plans_url)
        self.assertEqual(parts.path, "/api/plan-index")
        query = parse_qs(parts.query)
        self.assertEqual(query.get("after"), [cursor])
        self.assertEqual(query.get("limit"), ["1"])

    def test_plan_index_cursors_enforce_direction_and_reject_blank_or_padded_values(self):
        self.f.create(creation_id="direction-second")

        def page(**query):
            status, _, body = self.request("/api/plan-index", query=urlencode(query).encode())
            self.assertNotEqual(status, 404, "/api/plan-index endpoint is missing")
            return status, json.loads(body)

        status, newest = page(limit=1)
        self.assertEqual(status, 200)
        status, oldest = page(after=newest["next_cursor"], limit=1)
        self.assertEqual(status, 200)
        forward, reverse = newest["next_cursor"], oldest["previous_cursor"]
        self.assertTrue(forward and reverse)
        rejected = [{"before": forward}, {"after": reverse}, {"after": ""}, {"before": ""}]
        for name, cursor in (("after", forward), ("before", reverse)):
            for variant in (" " + cursor, cursor + " ", cursor + "\n"):
                rejected.append({name: variant})
        stored = dump(self.f.store)
        for query in rejected:
            with self.subTest(query=repr(query)[:80]):
                status, error = page(limit=1, **query)
                self.assertEqual(status, 400)
                self.assertEqual(error["error"]["code"], "invalid")
        self.assertEqual(dump(self.f.store), stored)
        status, value = page(after=forward, limit=1)
        self.assertEqual((status, [item["plan_id"] for item in value["plans"]]), (200, [self.plan_id]))
        status, value = page(before=reverse, limit=1)
        self.assertEqual((status, [item["plan_id"] for item in value["plans"]]), (200, [newest["plans"][0]["plan_id"]]))

    def test_compact_reads_stay_available_when_one_plan_exceeds_the_individual_response_cap(self):
        # TEST-ONLY historical oversized-publication seed: an ordinary small plan plus one synthetic
        # oversized event row. Not state accepted by a normal oversized-draft command; real caps stay.
        plan_id = self.f.create(creation_id="oversized-individual-plan").current_metadata.plan_id
        original = self.f.service.read_plan(plan_id)
        objective, revision = original.draft.objective, original.metadata.revision
        self.assertNotEqual(plan_id, self.plan_id)

        def seed_oversized_history(db):
            db.execute("INSERT INTO events (plan_id,plan_revision,kind,actor,published_at,payload) VALUES (?,?,?,?,?,?)",
                       (plan_id, revision, "synthetic_oversized_history", "synthetic-fixture",
                        self.f.clock().isoformat(), json.dumps({"summary": "x" * 11000000})))

        self.f.store.write(seed_oversized_history)
        status, _, body = self.request("/api/plans/" + plan_id)
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body)["error"]["code"], "limit_exceeded")
        status, _, body = self.request("/api/plan-index", query=b"limit=1")
        self.assertEqual(status, 200)
        self.assertLess(len(body), 100_000)
        self.assertNotIn(b"x" * 1000, body)
        index = json.loads(body)
        self.assertEqual([item["plan_id"] for item in index["plans"]], [plan_id])
        item = index["plans"][0]
        self.assertEqual(set(item), COMPACT_PLAN_KEYS)
        self.assertEqual((item["objective"], item["status"]), (objective, "planning"))
        status, _, html = self.request("/", query=b"limit=1")
        self.assertEqual(status, 200)
        self.assertNotIn(b"x" * 1000, html)
        self.assertEqual(root_navigation(html.decode())["plans"], [plan_id])


if __name__ == "__main__":
    unittest.main()
