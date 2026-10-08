"""Read-only dashboard projections, committed freshness and HTTP safety."""
from __future__ import annotations

import dataclasses
import json
import unittest
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from adaptive_planning import plans
from adaptive_planning.admin import initialize_installation, load_service_token
from adaptive_planning.app import build_app
from adaptive_planning.config import Config, http_allowlist
from tests.service_fixtures import make_service
from tests.test_service_http import call
from tests.test_service_plans import dump, insert_attempt


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
        self.assertEqual(body_attrs["data-plans-url"], "/api/plans")
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


if __name__ == "__main__":
    unittest.main()
