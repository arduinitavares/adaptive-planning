"""Read-only measurements presentation, using explicitly synthetic reports."""
from __future__ import annotations

from copy import deepcopy
from html.parser import HTMLParser
from importlib.util import find_spec
import unittest

from adaptive_planning.measurements import report


def example_report():
    cases = [{"plan_id": pid, "title": title, "status": "executing",
              "created_at": "2026-10-01T12:00:00+00:00", "steps": []}
             for pid, title in (("op", "Monthly operational work"),
                                ("fixture", "A fixture"), ("unknown", "Unclassified work"))]
    rows = [{"plan_id": pid, "kind": "context", "sequence": index,
             "attempt_id": None, "observed_at": "2026-10-01T12:00:00+00:00",
             "data": {"case_kind": kind, "skill_version": "sha256:test"}}
            for index, (pid, kind) in enumerate((("op", "operational"), ("fixture", "fixture")), 1)]
    data = report(cases, rows, {"code": "abc123", "measurement_schema": 1})
    data.update(observations=rows, next_after=None, canonical_store_sequence=52,
                mcp_calls={"count": 14, "recent": []}, collection_health={})
    for plan in data["plans"]:
        plan["lifecycle"] = {
            "source": "canonical_records", "total_steps": 10, "accepted_steps": 6,
            "accepted_with_linked_review": 2, "checkpoints": {"passed": 8, "failed": 1, "blocked": 0},
            "reassessments": {"material": 1, "unchanged": 2}, "revisions": 2,
            "invalidations": 1, "attempts": 9, "active_holds": 1,
            "event_count": 52,
            "closed_attempt_elapsed_seconds": [60, 120, 180],
            "acceptance_elapsed_seconds": [3600, 7200],
            "first_event_at": "2026-10-01T12:00:00+00:00",
            "last_event_at": "2026-10-18T15:00:00+00:00",
            "trace": [{"sequence": 52, "kind": "checkpoint_recorded",
                       "at": "2026-10-18T15:00:00+00:00", "step_id": "step-1",
                       "outcome": "failed", "disposition": None}],
        }
    return data


class Markup(HTMLParser):
    def __init__(self, value):
        super().__init__()
        self.tags = []
        self.feed(value)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class MeasurementDashboardTests(unittest.TestCase):
    def render(self, data=None, **kwargs):
        from adaptive_planning.measurement_dashboard import render_measurements
        return render_measurements(example_report() if data is None else data, **kwargs)

    def test_acceptance_and_review_are_separate_with_all_cohort_counts(self):
        page = self.render()
        self.assertIn('6 of 10 steps accepted', page)
        self.assertIn('2 of 6 accepted steps have a linked review', page)
        self.assertIn('Cross-family independence is unknown', page)
        markup = Markup(page)
        cohorts = {attrs.get('data-cohort'): attrs for _, attrs in markup.tags if 'data-cohort' in attrs}
        self.assertEqual(set(cohorts), {'operational', 'fixture', 'unknown', 'all'})
        for kind in ('operational', 'fixture', 'unknown'):
            self.assertEqual(cohorts[kind]['data-count'], '1')
        self.assertEqual(cohorts['all']['data-count'], '3')
        self.assertIn('Unclassified', page)
        self.assertIn('All recorded history', page)

    def test_missing_receipts_are_unknown_and_not_zero_cost(self):
        page = self.render()
        self.assertIn('No billing receipts', page)
        self.assertIn('No measured rework effort', page)
        self.assertIn('Unknown', page)
        self.assertNotIn('$0', page)
        self.assertNotIn('0 tokens', page)
        self.assertIn('0 of 1 plans with token receipts', page)

    def test_title_version_trace_and_raw_json_are_escaped(self):
        data = example_report()
        data['plans'][0]['title'] = '<img src=x onerror=alert(1)>'
        data['plans'][2]['versions'] = [{'skill_version': '<script>alert(1)</script>'}]
        data['plans'][0]['lifecycle']['trace'][0]['kind'] = '<script>trace</script>'
        page = self.render(data)
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;', page)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', page)
        self.assertIn('&lt;script&gt;trace&lt;/script&gt;', page)
        self.assertNotIn('<script>', page)
        self.assertNotIn('<img', page)

    def test_fixture_selection_does_not_mix_operational_denominators(self):
        data = example_report()
        data['plans'][1]['lifecycle']['accepted_steps'] = 9
        page = self.render(data, cohort='fixture')
        overview = page.split('id="overview"', 1)[1].split('</section>', 1)[0]
        self.assertIn('9 of 10 steps accepted', overview)
        self.assertNotIn('15 of 20', overview)
        self.assertIn('Fixture', page)

    def test_empty_cohort_explains_classification_and_does_not_make_percentage(self):
        data = example_report()
        data['plans'] = [data['plans'][2]]
        data['cohort'] = {'total_plans': 1, 'operational_plans': 0, 'fixture_plans': 0, 'unknown_plans': 1}
        page = self.render(data)
        self.assertIn('No operational plans in this report', page)
        self.assertIn('Classification requires an explicit context receipt', page)
        self.assertNotIn('0% accepted', page)
        self.assertIn('&lt;', self.render(example_report_with_unknown_version()))

    def test_canonical_trace_and_calendar_span_denominators(self):
        page = self.render()
        self.assertIn('checkpoint recorded', page)
        self.assertIn('Latest 1 canonical events', page)
        self.assertIn('2 min', page)
        self.assertIn('3 closed attempt spans', page)
        self.assertIn('6 attempts without a closed span', page)
        self.assertIn('Global server MCP calls', page)
        self.assertIn('14', page)

    def test_csp_safe_markup_and_report_is_unchanged(self):
        data = example_report()
        original = deepcopy(data)
        page = self.render(data, query={'limit': '1', 'after': '4'})
        self.assertEqual(data, original)
        markup = Markup(page)
        self.assertFalse(any(tag in ('form', 'script', 'style') for tag, _ in markup.tags))
        self.assertFalse(any('style' in attrs or any(key.startswith('on') for key in attrs)
                             for _, attrs in markup.tags))
        self.assertIn('/measurements.css', page)
        self.assertIn('/api/measurements?limit=1&amp;after=4', page)

    def test_partial_quantities_and_currencies_are_not_combined(self):
        data = example_report()
        data['plans'][0]['resources']['input_tokens'] = {'known_sum': 1200, 'coverage': 'partial'}
        data['plans'][0]['money'] = {
            'billing_amount:EUR:isolated': {'known_sum': 2.5, 'rework_known_sum': 0, 'coverage': 'partial'},
            'estimated_amount:USD:shared': {'known_sum': 3, 'rework_known_sum': 0, 'coverage': 'partial'},
        }
        page = self.render(data)
        self.assertIn('1,200', page)
        self.assertIn('EUR', page)
        self.assertIn('USD', page)
        self.assertIn('Lower bound', page)
        self.assertIn('Estimate', page)

    def test_canonical_volume_is_visible_without_usage_receipts(self):
        page = self.render()
        self.assertIn('Canonical activity volume', page)
        self.assertIn('9 attempts', page)
        self.assertIn('9 checkpoints', page)
        self.assertIn('52 events', page)

    def test_partial_defects_preserve_known_evidence_with_coverage(self):
        data = example_report()
        data['plans'][0]['regressions'].update(caught=1, escaped=0, open=1, coverage='partial')
        page = self.render(data, cohort='all')
        self.assertIn('Lower bound · 1 of 3 plans with defect evidence', page)

    def test_missing_lifecycle_remains_unknown(self):
        data = example_report()
        del data['plans'][0]['lifecycle']
        page = self.render(data)
        self.assertIn('Acceptance is unknown', page)
        self.assertIn('Linked review coverage is unknown', page)
        self.assertNotIn('0% accepted', page)

    def test_activity_is_bounded_and_newest_first(self):
        data = example_report()
        event = data['plans'][0]['lifecycle']['trace'][0]
        data['plans'][0]['lifecycle']['trace'] = [{**event, 'sequence': n} for n in range(1, 31)]
        activity = self.render(data).split('id="activity"', 1)[1].split('</section>', 1)[0]
        self.assertIn('Latest 20 canonical events', activity)
        self.assertLess(activity.index('Event #30'), activity.index('Event #11'))
        self.assertNotIn('Event #10', activity)


def example_report_with_unknown_version():
    data = example_report()
    data['plans'][2]['versions'] = [{'skill_version': '<unknown-version>'}]
    return data


@unittest.skipUnless(find_spec('starlette'), 'Declared service dependencies are unavailable in this Python environment')
class MeasurementRouteTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        from starlette.applications import Starlette
        from adaptive_planning.dashboard import dashboard_routes
        self.app = Starlette(routes=dashboard_routes(SimpleNamespace(read_measurements=lambda *args: example_report())))

    def request(self, path='/measurements', query=b'', method='GET'):
        import asyncio
        messages = []

        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        async def send(message):
            messages.append(message)

        scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                 'method': method, 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
                 'query_string': query, 'root_path': '', 'headers': [],
                 'client': ('127.0.0.1', 50000), 'server': ('127.0.0.1', 8790)}
        asyncio.run(self.app(scope, receive, send))
        start = next(message for message in messages if message['type'] == 'http.response.start')
        headers = [(key.decode(), value.decode()) for key, value in start['headers']]
        body = b''.join(message.get('body', b'') for message in messages if message['type'] == 'http.response.body')
        return start['status'], headers, body

    def test_page_supports_only_known_unique_cohorts_and_api_contract_is_unchanged(self):
        status, _, body = self.request(query=b'cohort=fixture')
        self.assertEqual(status, 200)
        self.assertIn(b'/measurements.css', body)
        for query in (b'cohort=garbage', b'cohort=fixture&cohort=unknown', b'limit=1&limit=2'):
            with self.subTest(query=query):
                self.assertEqual(self.request(query=query)[0], 400)
        self.assertEqual(self.request('/api/measurements', b'cohort=fixture')[0], 400)
        self.assertEqual(self.request('/api/measurements', b'limit=1&after=2')[0], 200)

    def test_local_css_is_read_only_and_keeps_csp(self):
        from adaptive_planning.dashboard import CSP
        status, headers, body = self.request('/measurements.css')
        self.assertEqual(status, 200)
        self.assertIn(b'--teal', body)
        self.assertEqual(dict(headers)['content-security-policy'], CSP)
        self.assertEqual(self.request('/measurements', method='POST')[0], 405)
        self.assertEqual(self.request('/measurements.css', method='POST')[0], 405)
        self.assertEqual(self.request('/measurements', method='HEAD')[0], 200)


if __name__ == '__main__':
    unittest.main()
