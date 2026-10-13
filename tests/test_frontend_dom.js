'use strict';
// Execute the actual dashboard in a deliberately small DOM/EventSource harness.
// This checks behavior and node identity, not browser CSS layout or computed color.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.nodeType = 1; this.childNodes = []; this.parent = null;
    this.className = ''; this.style = {}; this.dataset = {}; this.attributes = {};
    this.hidden = false; this.open = false; this.listeners = {}; this.clientWidth = 800;
    this.classList = {
      add: (...names) => { this.className = [...new Set(this.className.split(' ').filter(Boolean).concat(names))].join(' '); },
      remove: (...names) => { this.className = this.className.split(' ').filter(n => !names.includes(n)).join(' '); },
      contains: name => this.className.split(' ').includes(name)
    };
  }
  get children() { return this.childNodes.filter(child => child.nodeType === 1); }
  get firstChild() { return this.childNodes[0] || null; }
  set textContent(value) {
    const text = value == null ? '' : String(value);
    for (const child of this.childNodes) child.parent = null;
    this.childNodes = [];
    if (text) this.appendChild({nodeType: 3, textContent: text, parent: null});
  }
  get textContent() { return this.childNodes.map(child => child.textContent).join(''); }
  appendChild(child) {
    if (child.parent) child.parent.removeChild(child);
    child.parent = this; this.childNodes.push(child); return child;
  }
  removeChild(child) {
    const index = this.childNodes.indexOf(child);
    if (index < 0) throw new Error('Child is not attached');
    this.childNodes.splice(index, 1); child.parent = null; return child;
  }
  setAttribute(key, value) { this.attributes[key] = String(value); if (key === 'title') this.title = value; }
  getAttribute(key) { return key === 'title' ? this.title : this.attributes[key]; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  remove() { if (this.parent) this.parent.removeChild(this); }
  querySelectorAll(selector) {
    const matches = child => selector.startsWith('.') ? child.classList.contains(selector.slice(1)) : child.tagName === selector;
    return this.children.flatMap(child => [...(matches(child) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

function testLegacy(mode) {
const elements = new Map();
const el = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
let source;
class EventSource {
  constructor(url) { assert.equal(url, '/events'); this.readyState = 1; this.listeners = {}; source = this; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  send(name, data) { this.listeners[name]({data}); }
}
let now = Date.parse('2026-10-03T10:00:00Z');
class Clock extends Date { constructor(...args) { super(...(args.length ? args : [now])); } static now() { return now; } }
const intervals = [];
const errors = [];
const context = {
  document: {body: {dataset: mode ? {mode} : {}}, readyState: 'complete', getElementById: el, createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag)},
  EventSource, Date: Clock, console: {error: (...args) => errors.push(args)},
  setInterval: (fn, delay) => { assert.equal(delay, 1000); intervals.push(fn); }, setTimeout: () => 0
};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../assets/dashboard/app.js'), 'utf8'), context);
source.onopen();
const step = (id, status) => ({id, title: id, status, dependsOn: [], check: 'Check ' + id, evidence: []});
const initial = {
  schemaVersion: 2, generation: 'g1', revision: 1, taskId: 't', objective: '<img onerror="fail">',
  status: 'running', executionMode: 'complete-task', nextStepId: 'A', changeSummary: 'Initial',
  updatedAt: '2026-10-03T10:00:00Z',
  scope: {mode: 'complete-task', sinceRevision: 1, baseline: [], claimed: null},
  steps: [step('A', 'in-progress'), step('B', 'pending'), {...step('R', 'retired'), retiredReason: 'No longer required', evidence: ['Inspection evidence'], details: 'Retained details'}],
  history: [{revision: 1, timestamp: '2026-10-03T10:00:00Z', summary: 'Initial', authorizationNote: 'User requested completion'}]
};
const send = plan => source.send('plan', typeof plan === 'string' ? plan : JSON.stringify(plan));
send(initial);
assert.deepEqual(errors, []);
assert.equal(el('goal-heading').textContent, initial.objective); // content stays text
assert.equal(el('goal-heading').children.length, 0);
assert.equal(el('graph-nodes').children.length, 2); // retirement excluded from map
assert.equal(el('exec-mode-val').className, 'badge badge--neutral');
assert.ok(el('graph-nodes').children[0].classList.contains('step-node--in-progress'));
assert.ok(el('graph-nodes').children[1].classList.contains('step-node--pending'));
assert.equal(el('graph-nodes').children[0].querySelector('.status-icon').getAttribute('aria-hidden'), 'true');
assert.equal(el('graph-nodes').children[0].getAttribute('aria-label'), 'Step A (Working): A');
assert.equal(el('retired-count').textContent, '1');
assert.equal(el('retired-section').hidden, false);
assert.equal(el('retired-section').open, false);
el('retired-list').children[0].listeners.click();
assert.match(el('details-container').textContent, /No longer required/);
assert.match(el('details-container').textContent, /Inspection evidence/);
assert.match(el('details-container').textContent, /Retained details/);

const historyNode = el('history-list').children[0];
source.send('state-error');
assert.equal(el('reconnect-banner').hidden, false);
source.onopen();
assert.equal(el('reconnect-banner').hidden, false); // transport alone cannot clear data warning
send(initial);
assert.equal(el('reconnect-banner').hidden, true);
assert.equal(el('history-list').children[0], historyNode); // identical recovery does not rerender

const corruptions = [plan => { plan.steps = [null]; }, plan => { plan.steps[0].evidence = [null]; },
  plan => { plan.history = [null]; }, plan => { plan.history[0].summary = {}; },
  plan => { plan.updatedAt = 'bad'; }, plan => { plan.status = []; },
  plan => { plan.steps[0].dependsOn = {}; }, plan => { plan.steps[0].details = []; }];
for (const mutate of corruptions) {
  const bad = JSON.parse(JSON.stringify(initial)); mutate(bad); send(bad);
  assert.equal(el('history-list').children[0], historyNode);
  assert.equal(el('goal-heading').textContent, initial.objective);
  assert.equal(el('reconnect-banner').hidden, false);
  send(initial);
  assert.equal(el('reconnect-banner').hidden, true);
}
send({...initial, objective: 'conflicting bytes'});
assert.match(el('reconnect-text').textContent, /consistency conflict/);
send(initial);
assert.match(el('reconnect-text').textContent, /consistency conflict/);
const newer = {...initial, revision: 2, status: 'stopped', nextStepId: 'B',
  steps: [step('A', 'blocked'), step('B', 'pending'), initial.steps[2]]};
send(newer);
assert.equal(el('reconnect-banner').hidden, true);
assert.equal(el('task-status-val').className, 'badge badge--stopped');
assert.equal(el('task-status-val').querySelector('.status-icon').getAttribute('aria-hidden'), 'true');
send(initial); // positive valid revision 1 below current revision 2
assert.match(el('reconnect-text').textContent, /consistency conflict/);
const replacement = {...initial, generation: 'g2', objective: 'Replacement task', nextStepId: 'B',
  status: 'planning', steps: [step('A', 'pending'), {...step('B', 'pending'), dependsOn: ['A']}]};
send(replacement);
assert.equal(el('reconnect-banner').hidden, true);
assert.equal(el('details-container').querySelector('.task-id-code').textContent, 'B');
assert.equal(el('retired-section').hidden, true);
const depTag = el('details-container').querySelector('.dep-tag');
assert.equal(depTag.getAttribute('aria-label'), 'Jump to prerequisite step A (Pending)');
assert.equal(depTag.querySelector('.status-icon').getAttribute('aria-hidden'), 'true');
assert.equal(depTag.querySelector('.dep-tag-id').textContent, 'A');
depTag.listeners.click();
assert.equal(el('details-container').querySelector('.task-id-code').textContent, 'A');
assert.ok(el('graph-nodes').children[0].classList.contains('step-node--selected'));
assert.equal(el('graph-nodes').children[0].getAttribute('aria-pressed'), 'true');
assert.ok(el('graph-nodes').children[0].querySelector('.node-status-badge'));

const replacementHistoryNode = el('history-list').children[0];
now += 10000;
intervals[0]();
assert.equal(el('history-list').children[0], replacementHistoryNode);
assert.equal(el('history-list').querySelector('.history-time').textContent, '10s ago');
source.readyState = 2; source.onerror();
assert.equal(el('conn-label').textContent, 'Offline');
assert.ok(el('conn-pill').classList.contains('status-pill--offline'));
source.readyState = 0; source.onerror();
assert.equal(el('conn-label').textContent, 'Reconnecting…');
source.onopen();
send(replacement);
assert.equal(el('reconnect-banner').hidden, true);
assert.equal(el('history-list').children[0], replacementHistoryNode);
assert.deepEqual(errors, []);
console.log('Actual dashboard DOM/event regressions passed');
}

const hostile = '<script>globalThis.executed = true</script>';
const publishedAt = '2026-10-06T12:00:00+00:00';
const serviceStep = (id, status) => ({id, title: hostile + id, status, dependsOn: [],
  check: 'Committed check', evidence: [hostile + ' evidence'], summary: 'Observation'});
const serviceView = () => ({
  plan_id: 'service-plan', metadata: {epoch: 'epoch-1', plan_id: 'service-plan', generation: 'service-generation',
    revision: 1, ownership_version: 1, dashboard_url: 'http://127.0.0.1:8790/plans/service-plan'},
  draft: {objective: hostile, acceptance_criteria: ['Public criterion'], constraints: ['Public constraint'],
    facts: ['Observed fact'], assumptions: ['Assumption'], next_check: 'Observe published changes',
    authorization: {mode: 'complete-task', interval: '', since_revision: 1, note: 'Authorized', baseline: [], claimed: null},
    steps: ['a', 'b', 'c'].map(id => ({step_id: id, title: hostile + id, dependencies: [], completion_check: 'Committed check',
      document: null, definition_version: 'v1', review_required: false, final_gate: false})),
    documents: [], workspace_refs: [], workflow: null, horizon: 10, stop_reason: null, block_reason: null},
  legacy_snapshot: {schemaVersion: 2, generation: 'service-generation', revision: 1, taskId: 'service-plan', objective: hostile,
    updatedAt: publishedAt, status: 'blocked', executionMode: 'complete-task', changeSummary: 'Committed creation', nextStepId: 'a',
    scope: {mode: 'complete-task', sinceRevision: 1, baseline: [], claimed: null},
    steps: [serviceStep('a', 'in-progress'), serviceStep('b', 'in-progress'), serviceStep('c', 'blocked')],
    history: [{revision: 1, timestamp: publishedAt, summary: 'Committed creation'}]},
  legacy_publication: null, status: 'blocked', published_at: publishedAt,
  coordination: {attempts: [], reservations: [], holds: [], reviews: [], result_versions: {}, store_hold: true,
    restore_cutoff: '2026-10-05T12:00:00+00:00', ownership_actor: 'owner', ownership_version: 1,
    store_sequence: 205, next_cursor: 'summary-cursor', total_counts: {attempts: 3, reservations: 1, holds: 1, reviews: 1, invalidations: 1, orphan_uses: 0},
    has_live_attempts: true, has_unresolved_uses: true, has_plan_holds: true}
});
const attempt = (id, stepId, state) => ({kind: 'attempt', value: {attempt_id: id, step_id: stepId, assignee: 'worker-' + id,
  state, prior_state: state === 'stale' ? 'active' : null, version: 1, definition_version: 'v1', last_activity: publishedAt,
  context: {kind: 'read-only', repository_id: null, workspace_id: null, repository_exclusive: false, external_ids: [], writes: false, checked_base: null},
  recovery_required: state === 'stale', result_version: null}});
const firstItems = [attempt('att-a', 'a', 'active'), attempt('att-b', 'b', 'active')];
const secondItems = [attempt('att-c', 'c', 'stale'),
  {kind: 'reservation', value: {use_id: 'use-1', plan_id: 'service-plan', attempt_id: 'att-a', absent_work_id: null,
    resource_ids: ['resource-1'], actor: 'worker-att-a', writer_active: true, unresolved: true, repository_exclusive: false}},
  {kind: 'hold', value: {hold_id: 'hold-1', kind: 'stale', step_id: 'c', source_id: 'att-c', reason: hostile + ' hold'}},
  {kind: 'review', value: {plan_id: 'service-plan', step_id: 'a', attempt_id: 'att-a', definition_version: 'v1', recorded_sequence: 200,
    review: {reviewer: 'reviewer', artifact: {kind: 'code', identity: 'artifact', version: 'v1', digest: null, summary: hostile},
      verdict: 'failed', findings: [hostile + ' finding'], evidence: [{summary: hostile + ' review evidence', artifact: null,
        workspace_id: null, check: 'inspect', result: 'failed', limitations: ['Limited to synthetic fixture'], blockers: []}], verification_use_id: null}}},
  {kind: 'invalidation', value: {invalidation_id: 'invalid-1', producer_step_id: 'a', result_version: 'result-1',
    affected_steps: ['c'], resolved_steps: [], recorded_sequence: 201}}];
const event = sequence => ({sequence, plan_revision: sequence === 1 ? 1 : null, plan_id: 'service-plan',
  kind: 'record_checkpoint', actor: 'orchestrator', origin_worker: 'worker-origin', relay: 'owner-relay', published_at: publishedAt,
  payload: {checkpoint: {outcome: 'passed', check: 'public check', evidence: [{summary: hostile + ' event evidence', artifact: null,
    workspace_id: null, check: 'inspection', result: 'passed', limitations: [], blockers: []}], reassessment: {summary: 'stable', facts: [],
    assumptions: [], disposition: 'unchanged', reason: 'observed', affected_steps: [], unbounded: false, proposed_draft: null},
    origin_worker: 'worker-origin', relay: 'owner-relay', artifact: null}}});

function serviceHarness(responder, {index = false, warnings = null, date = Date, plansUrl = null, setup = null} = {}) {
  const elements = new Map();
  const el = id => {
    if (elements.has(id)) return elements.get(id);
    if (id.startsWith('plan-index-') && id !== 'plan-index-link') return null;
    const element = new Element();
    elements.set(id, element);
    return element;
  };
  const body = new Element('body');
  const targetPlansUrl = plansUrl || (index ? '/api/plans' : null);
  body.dataset = targetPlansUrl ? {mode: 'service', plansUrl: targetPlansUrl} : {mode: 'service', planId: 'service-plan',
    planUrl: '/api/plans/service-plan', coordinationUrl: '/api/plans/service-plan/coordination', eventsUrl: '/api/plans/service-plan/events'};
  if (warnings !== null) body.dataset.recoveryWarningsUrl = '/api/plans/service-plan/recovery-warnings';
  const intervals = [], calls = [], errors = [];
  let active = 0, maximumActive = 0;
  const context = {
    document: {body, readyState: 'complete', getElementById: el, createElement: tag => new Element(tag),
      createElementNS: (_, tag) => new Element(tag)},
    EventSource: class {constructor() {assert.fail('Service mode must not subscribe to legacy SSE');}},
    Date: date, URLSearchParams, encodeURIComponent,
    fetch: async (url, options) => {
      assert.equal(options.cache, 'no-store'); assert.equal(options.credentials, 'same-origin');
      assert.ok(!options.method || options.method === 'GET');
      assert.ok(!options.headers || !options.headers.Authorization);
      calls.push(url); active++; maximumActive = Math.max(maximumActive, active);
      try {
        return await (warnings !== null && url === body.dataset.recoveryWarningsUrl
          ? warnings(url, calls.length) : responder(url, calls.length));
      } finally {active--;}
    },
    localStorage: {getItem: () => assert.fail('No persisted progress'), setItem: () => assert.fail('No persisted progress')},
    sessionStorage: {getItem: () => assert.fail('No persisted progress'), setItem: () => assert.fail('No persisted progress')},
    console: {error: (...args) => errors.push(args)},
    setInterval: (fn, delay) => {assert.equal(delay, 1000); intervals.push(fn); return intervals.length;},
    setTimeout: () => 0
  };
  if (setup) setup({el, elements, body, context});
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../assets/dashboard/app.js'), 'utf8'), context);
  const settle = () => new Promise(resolve => setImmediate(resolve));
  return {el, elements, body, calls, errors, context, settle, maximumActive: () => maximumActive,
    tick: async () => {for (const fn of intervals) await fn(); await settle();}};
}
const response = (value, status = 200) => ({ok: status >= 200 && status < 300, status, json: async () => JSON.parse(JSON.stringify(value))});

async function testService() {
  let view = serviceView(), eventCount = 205;
  let failNext = false, conflictNext = false, pendingResolve = null, deferNext = false;
  const h = serviceHarness(async url => {
    if (failNext) {failNext = false; throw new Error('synthetic disconnect');}
    if (deferNext) {deferNext = false; await new Promise(resolve => {pendingResolve = resolve;});}
    if (url === '/api/plans/service-plan') return response(view);
    if (url.startsWith('/api/plans/service-plan/coordination')) {
      const query = new URLSearchParams(url.split('?')[1]);
      assert.equal(query.get('limit'), '100');
      const after = query.get('after');
      if (after === 'page-2' && conflictNext) {
        conflictNext = false;
        view = JSON.parse(JSON.stringify(view));
        view.metadata.revision = view.legacy_snapshot.revision = 2;
        view.legacy_snapshot.history.push({revision: 2, timestamp: publishedAt, summary: 'Revision after conflict'});
        view.legacy_snapshot.changeSummary = 'Revision after conflict';
        return response({error: {code: 'conflict', message: 'Restart cursor'}}, 409);
      }
      assert.ok(after === null || after === 'page-2'); // Summary cursor must not hide invalidations.
      return response({plan_id: view.plan_id, revision: view.metadata.revision, store_sequence: view.coordination.store_sequence,
        total_counts: view.coordination.total_counts, items: after ? secondItems : firstItems, next_cursor: after ? null : 'page-2'});
    }
    if (url.startsWith('/api/plans/service-plan/events')) {
      const query = new URLSearchParams(url.split('?')[1]);
      assert.equal(query.get('limit'), '100');
      const after = Number(query.get('after'));
      return response({events: Array.from({length: Math.min(100, Math.max(0, eventCount - after))}, (_, i) => event(after + i + 1))});
    }
    assert.fail('Unexpected public endpoint ' + url);
  });
  await h.settle();
  assert.equal(h.el('conn-label').textContent, 'Connected', 'Service mode must poll public committed projections');
  assert.equal(h.el('goal-heading').textContent, hostile);
  assert.equal(h.el('goal-heading').children.length, 0);
  assert.equal(h.el('graph-nodes').querySelectorAll('.step-node--in-progress').length, 2);
  assert.match(h.el('details-container').textContent, /<script>.* evidence/);
  assert.equal(h.el('details-container').querySelectorAll('script').length, 0);
  assert.equal(h.el('coordination-list').children.length, 7);
  assert.match(h.el('coordination-list').textContent, /att-a/);
  assert.match(h.el('coordination-list').textContent, /att-b/);
  assert.match(h.el('coordination-list').textContent, /stale/);
  assert.match(h.el('coordination-list').textContent, /<script>.* finding/);
  assert.match(h.el('coordination-list').textContent, /invalid-1/);
  assert.equal(h.el('coordination-list').querySelectorAll('script').length, 0);
  assert.equal(h.el('service-events').children.length, 205);
  assert.match(h.el('service-events').textContent, /worker-origin/);
  assert.match(h.el('service-events').textContent, /owner-relay/);
  assert.match(h.el('service-events').textContent, /<script>.* event evidence/);
  assert.match(h.el('published-state').textContent, /2026-10-06T12:00:00\+00:00/);
  assert.match(h.el('recovery-state').textContent, /store hold/i);
  assert.match(h.el('recovery-state').textContent, /2026-10-05/);
  assert.ok(h.calls.includes('/api/plans/service-plan/events?after=100&limit=100'));
  assert.ok(h.calls.includes('/api/plans/service-plan/events?after=200&limit=100'));
  assert.equal(h.context.executed, undefined);
  assert.equal(h.maximumActive(), 1);

  // Same semantic revision may acquire new committed coordination; do not apply legacy byte-conflict rules.
  eventCount = 206;
  view.coordination.store_sequence = 206;
  await h.tick();
  assert.ok(h.calls.includes('/api/plans/service-plan/events?after=205&limit=100'));
  assert.equal(h.el('service-events').children.length, 206);
  assert.equal(h.el('reconnect-banner').hidden, true);

  // While a fetch is pending, repeated timer ticks cannot start another polling cycle.
  deferNext = true;
  const pendingTick = h.tick();
  await h.settle();
  const pendingCalls = h.calls.length;
  await h.tick();
  assert.equal(h.calls.length, pendingCalls);
  pendingResolve(); await pendingTick;
  assert.equal(h.maximumActive(), 1);

  // A disconnect retains the last complete view; recovery restarts event history from zero.
  failNext = true;
  await h.tick();
  assert.equal(h.el('reconnect-banner').hidden, false);
  assert.equal(h.el('service-events').children.length, 206);
  const reconnectStart = h.calls.length;
  await h.tick();
  assert.ok(h.calls.slice(reconnectStart).includes('/api/plans/service-plan/events?after=0&limit=100'));
  assert.equal(h.el('reconnect-banner').hidden, true);
  assert.equal(h.el('service-events').children.length, 206);

  // Cursor revision conflicts restart read_plan and coordination, without mixing partial snapshots.
  conflictNext = true;
  const conflictStart = h.calls.length;
  await h.tick();
  assert.ok(h.calls.slice(conflictStart).filter(url => url === '/api/plans/service-plan').length >= 2);
  assert.match(h.el('rev-val').textContent, /Rev #2/);
  assert.equal(h.el('coordination-list').children.length, 7);
  assert.equal(h.el('service-events').children.length, 206);
  assert.equal(h.el('reconnect-banner').hidden, true);
  assert.deepEqual(h.errors, []);
  console.log('Service dashboard safe DOM, two active attempts, all pages, one in-flight, and reconnect/conflict regressions passed');
}

async function testServiceIndex() {
  const first = serviceView(), second = serviceView();
  second.plan_id = second.metadata.plan_id = second.legacy_snapshot.taskId = 'second-plan';
  second.draft.workflow = {name: hostile + ' workflow'};
  const h = serviceHarness(url => {
    assert.equal(url, '/api/plans'); return response({plans: [first, second]});
  }, {index: true});
  await h.settle();
  assert.equal(h.el('plan-list').children.length, 2);
  assert.equal(h.el('plan-list').querySelectorAll('a')[0].getAttribute('href'), '/plans/service-plan');
  assert.equal(h.el('plan-list').querySelectorAll('a')[1].getAttribute('href'), '/plans/second-plan');
  assert.equal(h.el('plan-list').querySelectorAll('.workflow-info').length, 1);
  assert.match(h.el('plan-list').textContent, /Rev #1/);
  assert.match(h.el('plan-list').textContent, /2026-10-06/);
  assert.equal(h.el('plan-list').querySelectorAll('script').length, 0);
  assert.equal(h.context.executed, undefined);
  assert.deepEqual(h.errors, []);
  console.log('Service multi-plan index safe text and optional workflow regressions passed');
}

function detailsFixture(options = {}) {
  const state = {view: serviceView(), items: JSON.parse(JSON.stringify([...firstItems, ...secondItems])), eventCount: 205, event1: event(1)};
  state.view.coordination.next_cursor = null;
  const h = serviceHarness(url => {
    if (url === '/api/plans/service-plan') return response(state.view);
    const query = new URLSearchParams(url.split('?')[1]);
    assert.equal(query.get('limit'), '100');
    if (url.startsWith('/api/plans/service-plan/coordination')) {
      assert.equal(query.get('after'), null);
      return response({plan_id: state.view.plan_id, revision: state.view.metadata.revision,
        store_sequence: state.view.coordination.store_sequence, total_counts: state.view.coordination.total_counts,
        items: state.items, next_cursor: null});
    }
    if (url.startsWith('/api/plans/service-plan/events')) {
      const after = Number(query.get('after'));
      return response({events: Array.from({length: Math.min(100, Math.max(0, state.eventCount - after))},
        (_, i) => after + i + 1 === 1 ? state.event1 : event(after + i + 1))});
    }
    assert.fail('Unexpected public endpoint ' + url);
  }, options);
  return {h, state};
}

function coordinationDetails(h, kind, matches) {
  const details = h.el('coordination-list').children.find(node =>
    node.classList.contains('coordination-item--' + kind) && matches(JSON.parse(node.querySelector('pre').textContent)));
  assert.ok(details, 'Missing coordination record of kind ' + kind);
  return details;
}

function eventDetails(h, sequence) {
  const details = h.el('service-events').querySelectorAll('details').find(node =>
    node.querySelector('summary').textContent.startsWith(`#${sequence} `));
  assert.ok(details, 'Missing event ' + sequence);
  return details;
}

async function testExpandedDetailsUnchanged() {
  const {h} = detailsFixture();
  await h.settle();
  const attemptDetails = coordinationDetails(h, 'attempt', value => value.attempt_id === 'att-a');
  const reviewDetails = coordinationDetails(h, 'review', value => value.recorded_sequence === 200);
  const payloadDetails = eventDetails(h, 1);
  attemptDetails.open = reviewDetails.open = payloadDetails.open = true;
  await h.tick();
  assert.equal(h.el('conn-label').textContent, 'Connected');
  assert.equal(h.el('published-state').dataset.revision, '1');
  assert.equal(h.el('published-state').dataset.storeSequence, '205');
  assert.deepEqual({
    attempt: coordinationDetails(h, 'attempt', value => value.attempt_id === 'att-a').open,
    review: coordinationDetails(h, 'review', value => value.recorded_sequence === 200).open,
    event: eventDetails(h, 1).open
  }, {attempt: true, review: true, event: true}, 'Unchanged successful polls must preserve opened evidence and findings');
  assert.equal(coordinationDetails(h, 'attempt', value => value.attempt_id === 'att-a'), attemptDetails);
  assert.equal(coordinationDetails(h, 'review', value => value.recorded_sequence === 200), reviewDetails);
  assert.equal(eventDetails(h, 1), payloadDetails);
  assert.equal(coordinationDetails(h, 'attempt', value => value.attempt_id === 'att-b').open, false);
  assert.equal(eventDetails(h, 2).open, false);
  assert.deepEqual(h.errors, []);
  console.log('Opened coordination findings and event evidence survive unchanged successful polls; closed records remain closed');
}

async function testExpandedDetailsUnrelatedAddition() {
  const {h, state} = detailsFixture();
  await h.settle();
  const records = [
    ['attempt', value => value.attempt_id === 'att-a'],
    ['reservation', value => value.use_id === 'use-1'],
    ['hold', value => value.hold_id === 'hold-1'],
    ['review', value => value.recorded_sequence === 200],
    ['invalidation', value => value.invalidation_id === 'invalid-1']
  ];
  const opened = records.map(([kind, matches]) => coordinationDetails(h, kind, matches));
  for (const details of opened) details.open = true;
  const payloadDetails = eventDetails(h, 1);
  payloadDetails.open = true;
  const newReview = JSON.parse(JSON.stringify(state.items.find(item => item.kind === 'review')));
  newReview.value.recorded_sequence = 202;
  state.items = [
    {kind: 'hold', value: {hold_id: 'att-a', kind: 'reassessment', step_id: 'b', source_id: 'source-2', reason: 'Unrelated hold'}},
    newReview, attempt('att-new', 'b', 'released'), ...state.items.reverse()
  ];
  Object.assign(state.view.coordination.total_counts, {attempts: 4, holds: 2, reviews: 2});
  state.view.coordination.store_sequence = state.eventCount = 206;
  await h.tick();
  assert.equal(h.el('conn-label').textContent, 'Connected');
  assert.equal(h.el('published-state').dataset.revision, '1');
  assert.equal(h.el('published-state').dataset.storeSequence, '206');
  assert.deepEqual({
    coordination: records.map(([kind, matches]) => coordinationDetails(h, kind, matches).open),
    event: eventDetails(h, 1).open
  }, {coordination: [true, true, true, true, true], event: true},
  'Unrelated committed additions and record reordering must preserve expansions by durable identity');
  records.forEach(([kind, matches], i) => assert.equal(coordinationDetails(h, kind, matches), opened[i]));
  assert.equal(eventDetails(h, 1), payloadDetails);
  assert.equal(coordinationDetails(h, 'attempt', value => value.attempt_id === 'att-b').open, false);
  assert.equal(coordinationDetails(h, 'attempt', value => value.attempt_id === 'att-new').open, false);
  assert.equal(coordinationDetails(h, 'hold', value => value.hold_id === 'att-a').open, false, 'Different kinds cannot share expansion state');
  assert.equal(coordinationDetails(h, 'review', value => value.recorded_sequence === 202).open, false, 'New review of the same attempt starts closed');
  assert.equal(eventDetails(h, 2).open, false);
  assert.equal(eventDetails(h, 206).open, false, 'New event starts closed');

  // Current user choices, including closing old records, survive subsequent polls.
  opened[0].open = opened[3].open = payloadDetails.open = false;
  coordinationDetails(h, 'attempt', value => value.attempt_id === 'att-b').open = eventDetails(h, 2).open = true;
  await h.tick();
  assert.equal(coordinationDetails(h, 'attempt', value => value.attempt_id === 'att-a').open, false);
  assert.equal(coordinationDetails(h, 'review', value => value.recorded_sequence === 200).open, false);
  assert.equal(eventDetails(h, 1).open, false);
  assert.equal(coordinationDetails(h, 'attempt', value => value.attempt_id === 'att-b').open, true);
  assert.equal(eventDetails(h, 2).open, true);

  // A restored epoch can reuse public record IDs without inheriting the previous publication's UI state.
  state.view.metadata.epoch = 'epoch-2';
  await h.tick();
  assert.equal(h.el('conn-label').textContent, 'Connected');
  assert.ok(h.el('coordination-list').querySelectorAll('details').every(details => !details.open));
  assert.ok(h.el('service-events').querySelectorAll('details').every(details => !details.open));
  assert.deepEqual(h.errors, []);
  console.log('Durable record identity preserves expansions through unrelated additions and reordering; closed/new/reused-epoch records remain closed');
}

class StableClock extends Date {
  constructor(...args) {super(...(args.length ? args : [publishedAt]));}
  static now() {return Date.parse(publishedAt);}
}

function snapshotTree(node) {
  assert.ok(node, 'Expected a rendered node');
  return {node, text: node.textContent, children: (node.childNodes || []).map(snapshotTree)};
}

function assertSameTree(actual, saved, label) {
  assert.equal(actual, saved.node, label + ' identity');
  assert.equal(actual.textContent, saved.text, label + ' content');
  const children = actual.childNodes || [];
  assert.equal(children.length, saved.children.length, label + ' descendants');
  saved.children.forEach((child, i) => assertSameTree(children[i], child, label));
}

async function testStablePublication() {
  const {h, state} = detailsFixture({date: StableClock});
  await h.settle();
  const a = coordinationDetails(h, 'attempt', value => value.attempt_id === 'att-a');
  const review = coordinationDetails(h, 'review', value => value.recorded_sequence === 200);
  const firstEvent = eventDetails(h, 1);
  a.open = review.open = firstEvent.open = true;
  const ids = ['graph-nodes', 'details-container', 'history-list', 'goal-heading', 'published-state'];
  const planTrees = ids.map(id => snapshotTree(h.el(id)));
  const records = h.el('coordination-list').children.map(snapshotTree);
  const eventOne = snapshotTree(firstEvent), eventTwo = snapshotTree(eventDetails(h, 2));
  for (const id of ['goal-heading', 'published-state']) assert.equal(h.el(id).firstChild.nodeType, 3);
  const unchanged = (siblings = records) => {
    ids.forEach((id, i) => assertSameTree(h.el(id), planTrees[i], id));
    for (const saved of siblings) {
      assert.ok(h.el('coordination-list').children.includes(saved.node), 'Retained record is attached');
      assertSameTree(saved.node, saved, 'Unchanged coordination');
    }
    assertSameTree(eventDetails(h, 1), eventOne, 'Event 1');
    assertSameTree(eventDetails(h, 2), eventTwo, 'Event 2');
    assert.equal(a.open, true); assert.equal(review.open, true); assert.equal(firstEvent.open, true);
  };
  const poll = async () => {
    const start = h.calls.length;
    await h.tick();
    assert.equal(h.el('conn-label').textContent, 'Connected');
    assert.equal(h.el('reconnect-banner').hidden, true);
    const calls = h.calls.slice(start);
    assert.equal(calls[calls.length - 1], '/api/plans/service-plan', 'Final canonical fence');
    assert.ok(calls.filter(url => url === '/api/plans/service-plan').length >= 2);
    return calls;
  };

  // Unchanged outer details alone are insufficient: retain every captured descendant/text node.
  await poll(); unchanged();
  assert.equal(h.el('published-state').dataset.storeSequence, '205');

  // Add committed content and reorder existing records, with coherent counts and event watermark.
  const originalOrder = h.el('coordination-list').children.slice();
  state.items = [
    {kind: 'hold', value: {hold_id: 'hold-added', kind: 'reassessment', step_id: 'b',
      source_id: 'source-added', reason: 'Unrelated committed observation'}},
    ...state.items.slice().reverse()
  ];
  state.view.coordination.total_counts.holds = 2;
  state.view.coordination.store_sequence = state.eventCount = 206;
  const additionCalls = await poll();
  assert.ok(additionCalls.includes('/api/plans/service-plan/events?after=205&limit=100'));
  unchanged();
  const added = coordinationDetails(h, 'hold', value => value.hold_id === 'hold-added');
  const expectedOrder = [added, ...originalOrder.reverse()];
  assert.equal(h.el('coordination-list').children.length, expectedOrder.length);
  expectedOrder.forEach((node, i) => assert.equal(h.el('coordination-list').children[i], node, 'Public order'));
  assert.match(added.querySelector('summary').textContent, /hold-added/);
  assert.equal(JSON.parse(added.querySelector('pre').textContent).reason, 'Unrelated committed observation');
  assert.match(h.el('coordination-summary').textContent, /\b206\b/);
  assert.match(h.el('coordination-summary').textContent, /holds:\s*2\b/);
  assert.equal(h.el('service-events').children.length, 206);
  assert.equal(h.el('published-state').dataset.storeSequence, '206');
  assert.equal(h.el('published-state').dataset.eventSequence, '206');
  assert.match(h.el('events-summary').textContent, /\b206\b/);
  assert.deepEqual(JSON.parse(eventDetails(h, 206).querySelector('pre').textContent), event(206).payload);
  assert.equal(added.open, false); assert.equal(eventDetails(h, 206).open, false);

  // One heartbeat updates its payload/label and preserves open choice plus untouched sibling subtrees.
  const changed = state.items.find(item => item.kind === 'attempt' && item.value.attempt_id === 'att-a');
  Object.assign(changed.value, {version: 2, last_activity: '2026-10-06T12:00:01+00:00', assignee: 'worker-heartbeat'});
  state.view.coordination.store_sequence = state.eventCount = 207;
  const addedTree = snapshotTree(added);
  await poll(); unchanged([...records.filter(saved => saved.node !== a), addedTree]);
  const updated = coordinationDetails(h, 'attempt', value => value.attempt_id === 'att-a');
  assert.equal(updated, a); assert.equal(updated.open, true);
  const payload = JSON.parse(updated.querySelector('pre').textContent);
  assert.deepEqual([payload.version, payload.last_activity, payload.assignee],
    [2, '2026-10-06T12:00:01+00:00', 'worker-heartbeat']);
  assert.match(updated.querySelector('summary').textContent, /worker-heartbeat/);

  // A genuine objective change must render even at the same canonical revision/metadata.
  state.view.draft.objective = state.view.legacy_snapshot.objective = 'Observe a revised public objective';
  await poll();
  assert.equal(h.el('goal-heading').textContent, 'Observe a revised public objective');
  assert.equal(state.view.metadata.revision, 1); assert.equal(state.view.legacy_snapshot.revision, 1);
  assert.equal(h.el('published-state').dataset.revision, '1');

  // Existing epoch resync reloads changed event 1; immutable incremental siblings stayed retained above.
  state.view.metadata.epoch = 'epoch-2';
  state.event1.actor = 'restored-orchestrator';
  state.event1.payload.checkpoint.check = 'Revalidated restored event evidence';
  const resyncCalls = await poll();
  assert.ok(resyncCalls.includes('/api/plans/service-plan/events?after=0&limit=100'));
  const restored = eventDetails(h, 1);
  assert.equal(JSON.parse(restored.querySelector('pre').textContent).checkpoint.check, 'Revalidated restored event evidence');
  assert.match(restored.querySelector('summary').textContent, /restored-orchestrator/);
  assert.equal(restored.open, false, 'Restored epoch starts fresh expansions');
  assert.equal(h.el('service-events').children.length, 207);
  assert.deepEqual(h.errors, []);
  console.log('Stable publication regressions passed');
}

async function testStablePublicationWarnings() {
  const warning = {metadata: serviceView().metadata, store_sequence: 205, observed_at: publishedAt,
    threshold_seconds: 6, overdue: [], unassessable_activity: []};
  const {h, state} = detailsFixture({date: StableClock, warnings: () => response(warning)});
  await h.settle();
  const a = coordinationDetails(h, 'attempt', value => value.attempt_id === 'att-a');
  const review = coordinationDetails(h, 'review', value => value.recorded_sequence === 200);
  const firstEvent = eventDetails(h, 1);
  a.open = review.open = firstEvent.open = true;
  assert.equal(h.el('recovery-warnings').dataset.status, 'current');
  assert.equal(h.el('recovery-warnings').dataset.observedAt, publishedAt);
  const ids = ['graph-nodes', 'details-container', 'history-list', 'goal-heading', 'published-state',
    'coordination-list', 'service-events'];
  const trees = ids.map(id => snapshotTree(h.el(id)));
  const authority = JSON.stringify(state.view), records = JSON.stringify(state.items);
  for (const [observed, valid] of [
    ['2026-10-06T12:00:05+00:00', true],
    ['2026-10-06T12:00:05', false], // Observation alone changes; missing zone must be validated.
    ['2026-10-06T12:00:05.000001+00:00', true]
  ]) {
    warning.observed_at = observed;
    const previousText = h.el('recovery-warnings').textContent;
    const start = h.calls.length;
    await h.tick();
    const calls = h.calls.slice(start);
    const advisoryIndex = calls.indexOf('/api/plans/service-plan/recovery-warnings');
    assert.ok(advisoryIndex >= 0, 'Unchanged authority still fetches warnings');
    assert.ok(calls.lastIndexOf('/api/plans/service-plan') > advisoryIndex, 'Fence follows advisory validation');
    assert.equal(calls[calls.length - 1], '/api/plans/service-plan');
    assert.ok(calls.filter(url => url === '/api/plans/service-plan').length >= 2);
    assert.equal(h.el('conn-label').textContent, 'Connected');
    assert.equal(h.el('reconnect-banner').hidden, true);
    const region = h.el('recovery-warnings');
    assert.equal(region.dataset.status, valid ? 'current' : 'unavailable');
    assert.equal(region.dataset.observedAt, valid ? observed : '');
    assert.notEqual(region.textContent, previousText, 'Visible observation/status advances');
    if (valid) assert.ok(region.textContent.includes(observed));
    assert.equal(JSON.stringify(state.view), authority, 'Warnings do not mutate authority');
    assert.equal(JSON.stringify(state.items), records); assert.equal(state.eventCount, 205);
    ids.forEach((id, i) => assertSameTree(h.el(id), trees[i], 'Observation-only ' + id));
    assert.equal(a.open, true); assert.equal(review.open, true); assert.equal(firstEvent.open, true);
  }
  assert.deepEqual(h.errors, []);
  assert.equal(h.maximumActive(), 1);
  console.log('Stable publication warning regressions passed');
}

async function testRecoveryWarnings() {
  let view = serviceView();
  const items = Array.from({length: 101}, (_, i) => attempt(i === 100 ? hostile : 'warning-' + i, 'a',
    ['assigned', 'active', 'submitted'][i % 3]));
  view.coordination.total_counts.attempts = 101;
  let observed = publishedAt, overdue = [], unable = [], fail = false, corrupt = null, mutateAfterWarning = false;
  const item = value => ({attempt_id: value.attempt_id, version: value.version, state: value.state,
    last_activity: value.last_activity});
  const warningValue = () => ({metadata: JSON.parse(JSON.stringify(view.metadata)),
    store_sequence: view.coordination.store_sequence, observed_at: observed, threshold_seconds: 6,
    overdue: overdue.map(i => item(items[i].value)), unassessable_activity: unable.map(i => item(items[i].value))});
  const h = serviceHarness(url => {
    if (url === '/api/plans/service-plan') return response(view);
    if (url.startsWith('/api/plans/service-plan/coordination')) {
      const after = new URLSearchParams(url.split('?')[1]).get('after');
      return response({plan_id: view.plan_id, revision: view.metadata.revision, store_sequence: view.coordination.store_sequence,
        items: after ? items.slice(100) : items.slice(0, 100), next_cursor: after ? null : 'last-warning-page'});
    }
    if (url.startsWith('/api/plans/service-plan/events')) return response({events: []});
    assert.fail('Unexpected warning publication endpoint ' + url);
  }, {warnings: () => {
    if (fail) throw new Error('Synthetic advisory unavailable');
    const value = warningValue();
    if (corrupt) corrupt(value);
    if (mutateAfterWarning) {
      mutateAfterWarning = false;
      view.metadata.revision = view.legacy_snapshot.revision = 2;
      view.legacy_snapshot.changeSummary = 'New fenced publication';
    }
    return response(value);
  }});
  await h.settle();
  assert.equal(h.el('recovery-warnings').dataset.status, 'current');
  assert.match(h.el('recovery-warnings').textContent, /No overdue recorded inactivity/);
  assert.ok(h.calls.indexOf('/api/plans/service-plan/recovery-warnings') < h.calls.lastIndexOf('/api/plans/service-plan'));
  const canonical = h.el('published-state').textContent;
  observed = '2026-10-06T12:00:06+00:00';
  overdue = Array.from({length: 101}, (_, i) => i); // All 101 are six seconds old, including the second page.
  await h.tick();
  assert.equal(h.el('published-state').textContent, canonical);
  assert.equal(h.el('recovery-warnings').dataset.observedAt, observed);
  assert.match(h.el('recovery-warnings').textContent, /101 overdue/);
  assert.match(h.el('recovery-warnings').textContent, /<script>globalThis.executed/);
  assert.equal(h.el('recovery-warnings').querySelectorAll('script').length, 0);
  assert.equal(h.context.executed, undefined);

  // Every metadata field and the global store sequence must match the canonical publication.
  const metadataChanges = [
    v => {v.metadata.epoch = 'another-epoch';}, v => {v.metadata.plan_id = 'another-plan';},
    v => {v.metadata.generation = 'another-generation';}, v => {v.metadata.revision++;},
    v => {v.metadata.ownership_version++;}, v => {v.metadata.dashboard_url += '/foreign';},
    v => {v.store_sequence++;}, v => {v.overdue[100].version++;},
    v => {v.overdue[100].state = 'accepted';}, v => {v.overdue[100].last_activity = publishedAt + 'bad';},
    v => {v.overdue.pop();}, v => {v.overdue.push({...v.overdue[0]});},
    v => {v.overdue[0].attempt_id = 'unknown';}
  ];
  for (const change of metadataChanges) {
    corrupt = change;
    const start = h.calls.length;
    await h.tick();
    assert.equal(h.el('recovery-warnings').dataset.status, 'unavailable');
    assert.doesNotMatch(h.el('recovery-warnings').textContent, /101 overdue|<script>/);
    assert.ok(h.calls.slice(start).filter(url => url === '/api/plans/service-plan/recovery-warnings').length <= 2);
    corrupt = null;
    await h.tick();
    assert.equal(h.el('recovery-warnings').dataset.status, 'current');
  }
  for (const change of [v => {v.threshold_seconds = 0;}, v => {v.threshold_seconds = true;},
      v => {v.observed_at = '2026-10-06T12:00:06';}, v => {v.overdue[0].version = Number.MAX_SAFE_INTEGER + 1;},
      v => {v.extra = 'unexpected';}, v => {v.metadata.extra = 'unexpected';}]) {
    corrupt = change;
    await h.tick();
    assert.equal(h.el('conn-label').textContent, 'Connected', 'Advisory shape failure may retain a valid canonical view');
    assert.equal(h.el('recovery-warnings').dataset.status, 'unavailable');
    corrupt = null;
    await h.tick();
  }
  fail = true;
  await h.tick();
  assert.equal(h.el('conn-label').textContent, 'Connected');
  assert.match(h.el('recovery-warnings').textContent, /unavailable/i);
  assert.doesNotMatch(h.el('recovery-warnings').textContent, /101 overdue/);
  fail = false;
  await h.tick();
  // Preserve the service's microsecond boundary rather than rounding it into browser milliseconds.
  for (const current of items) current.value.last_activity = '2026-10-06T12:00:00.000001+00:00';
  view.coordination.store_sequence++;
  observed = '2026-10-06T13:00:06.000000+01:00';
  overdue = [];
  await h.tick();
  assert.equal(h.el('recovery-warnings').dataset.status, 'current');
  assert.match(h.el('recovery-warnings').textContent, /No overdue recorded inactivity/);
  observed = '2026-10-06T13:00:06.000001+01:00';
  overdue = Array.from({length: 101}, (_, i) => i);
  await h.tick();
  assert.match(h.el('recovery-warnings').textContent, /101 overdue/);
  items[0].value.last_activity = '2026-10-06T12:00:00';
  items[1].value.last_activity = '2026-10-06T13:00:07+01:00';
  view.coordination.store_sequence++;
  unable = [0, 1];
  overdue = overdue.slice(2);
  await h.tick();
  assert.equal(h.el('recovery-warnings').dataset.status, 'current');
  assert.match(h.el('recovery-warnings').textContent, /Unable to assess recorded activity: warning-0, warning-1/);
  for (const change of [v => {v.unassessable_activity.pop();},
      v => {v.unassessable_activity[0].version++;}, v => {v.unassessable_activity[0].state = 'accepted';},
      v => {v.unassessable_activity[0].last_activity = publishedAt;},
      v => {v.overdue.push({...v.unassessable_activity[0]});}]) {
    corrupt = change;
    await h.tick();
    assert.equal(h.el('recovery-warnings').dataset.status, 'unavailable');
    corrupt = null;
    await h.tick();
  }
  for (const current of items) current.value.last_activity = publishedAt;
  view.coordination.store_sequence++;
  observed = '2026-10-06T12:00:06+00:00';
  // Canonical activity can be a started_at fallback; service-observed raw absence remains unassessable.
  unable = [0];
  overdue = Array.from({length: 100}, (_, i) => i + 1);
  await h.tick();
  assert.equal(h.el('recovery-warnings').dataset.status, 'current');
  assert.match(h.el('recovery-warnings').textContent, /100 overdue/);
  assert.match(h.el('recovery-warnings').textContent, /Unable to assess recorded activity: warning-0/);
  unable = [];
  overdue = Array.from({length: 101}, (_, i) => i);
  await h.tick();
  // A foreign-plan event changes only the global watermark, and old warnings cannot attach to it.
  view.coordination.store_sequence++;
  corrupt = v => {v.store_sequence--;};
  await h.tick();
  assert.equal(h.el('recovery-warnings').dataset.status, 'unavailable');
  corrupt = null;
  await h.tick();
  mutateAfterWarning = true;
  await h.tick();
  assert.equal(h.el('published-state').dataset.revision, '2', 'The final canonical read must fence after the advisory fetch');
  assert.equal(h.el('recovery-warnings').dataset.status, 'current');
  view.metadata.epoch = 'new-warning-epoch';
  fail = true;
  await h.tick();
  assert.equal(h.el('recovery-warnings').dataset.status, 'unavailable');
  assert.doesNotMatch(h.el('recovery-warnings').textContent, /101 overdue/);
  assert.equal(h.maximumActive(), 1);
  console.log('Pure-time recovery warnings: all pages, exact metadata/store fence, bounded retry, unavailable clearing and escaped text passed');
}

function createSSRCard(item) {
  const card = new Element('article');
  card.className = 'plan-card';
  card.dataset.planId = item.plan_id;
  card.setAttribute('data-plan-id', item.plan_id);
  const heading = new Element('h2');
  const link = new Element('a');
  link.setAttribute('href', '/plans/' + encodeURIComponent(item.plan_id));
  link.textContent = item.objective;
  heading.appendChild(link);
  card.appendChild(heading);
  const meta = new Element('p');
  meta.className = 'plan-card-meta';
  meta.textContent = `${item.status} · Rev #${item.revision}`;
  card.appendChild(meta);
  const published = new Element('p');
  published.textContent = 'Published: ';
  const time = new Element('time');
  time.textContent = item.published_at;
  published.appendChild(time);
  card.appendChild(published);
  const code = new Element('code');
  code.className = 'task-id-code';
  code.textContent = item.plan_id;
  card.appendChild(code);
  return card;
}

if (!Object.getOwnPropertyDescriptor(Element.prototype, 'parentNode')) {
  Object.defineProperty(Element.prototype, 'parentNode', {
    get() { return this.parent; },
    configurable: true
  });
}

function isAttached(node, root) {
  let curr = node;
  while (curr) {
    if (curr === root) return true;
    curr = curr.parentNode || curr.parent;
  }
  return false;
}

function visibleNoticeText(h) {
  const notices = [];
  const banner = h.el('reconnect-banner');
  if (banner && !banner.hidden) {
    notices.push(banner.textContent);
  }
  const collectAlerts = el => {
    if (!el || el.hidden) return;
    if (el !== banner && (el.classList.contains('index-notice') || el.getAttribute('role') === 'alert')) {
      notices.push(el.textContent);
    }
    for (const child of el.children) collectAlerts(child);
  };
  collectAlerts(h.body);
  return notices.join(' ').trim();
}

async function testCompactIndexCore() {
  const plansUrl = '/api/plan-index?after=chosen-cursor&limit=2';
  const hostileObj1 = hostile + ' <script>fail()</script> plan-1';
  const hostileObj2 = hostile + ' plan-2 & "quote"';
  const planId1 = 'plan/one?special=1&user=alice';
  const planId2 = 'plan/two#anchor?x=2';
  const plan1 = {
    plan_id: planId1,
    objective: hostileObj1,
    status: 'running',
    revision: 2,
    published_at: publishedAt,
    dashboard_url: 'http://foreign.example.com:9999/malicious/link1'
  };
  const plan2 = {
    plan_id: planId2,
    objective: hostileObj2,
    status: 'complete',
    revision: 4,
    published_at: publishedAt,
    dashboard_url: 'http://foreign.example.com:9999/malicious/link2'
  };
  const wirePage = {
    plans: [plan1, plan2],
    next_cursor: 'cursor-next-initial',
    previous_cursor: 'cursor-prev-initial',
    epoch: 'epoch-1',
    collection_fence: {count: 6, max_rowid: 10},
    store_sequence: 100
  };

  let card1, card2, nav, firstAnchor, newerAnchor, olderAnchor;
  let cardTree1, cardTree2;
  const h = serviceHarness(url => {
    assert.equal(url, plansUrl);
    return response(wirePage);
  }, {
    plansUrl,
    setup: ({el, elements, body}) => {
      const container = new Element('div');
      container.className = 'app-container';
      body.appendChild(container);

      const header = new Element('header');
      header.className = 'app-header';
      container.appendChild(header);

      const banner = el('reconnect-banner');
      banner.id = 'reconnect-banner';
      banner.className = 'banner banner--warning';
      banner.hidden = true;
      banner.setAttribute('role', 'alert');
      header.appendChild(banner);

      const bannerText = el('reconnect-text');
      bannerText.id = 'reconnect-text';
      banner.appendChild(bannerText);

      const planList = el('plan-list');
      planList.className = 'plan-list';
      planList.setAttribute('aria-label', 'Committed plans');
      container.appendChild(planList);

      card1 = createSSRCard(plan1);
      card2 = createSSRCard(plan2);
      planList.appendChild(card1);
      planList.appendChild(card2);
      cardTree1 = snapshotTree(card1);
      cardTree2 = snapshotTree(card2);

      nav = new Element('nav');
      nav.id = 'plan-index-nav';
      nav.className = 'plan-index-nav';
      nav.setAttribute('aria-label', 'Plan pages');
      elements.set('plan-index-nav', nav);

      firstAnchor = new Element('a');
      firstAnchor.id = 'plan-index-first';
      firstAnchor.textContent = 'First';
      firstAnchor.setAttribute('href', '/?limit=2');
      elements.set('plan-index-first', firstAnchor);
      nav.appendChild(firstAnchor);

      newerAnchor = new Element('a');
      newerAnchor.id = 'plan-index-newer';
      newerAnchor.textContent = 'Newer';
      newerAnchor.setAttribute('href', '/?before=' + encodeURIComponent(wirePage.previous_cursor) + '&limit=2');
      elements.set('plan-index-newer', newerAnchor);
      nav.appendChild(newerAnchor);

      olderAnchor = new Element('a');
      olderAnchor.id = 'plan-index-older';
      olderAnchor.textContent = 'Older';
      olderAnchor.setAttribute('href', '/?after=' + encodeURIComponent(wirePage.next_cursor) + '&limit=2');
      elements.set('plan-index-older', olderAnchor);
      nav.appendChild(olderAnchor);

      container.appendChild(nav);
    }
  });

  assert.equal(h.el('plan-index-absent'), null);
  await h.settle();
  assert.equal(h.el('conn-label').textContent, 'Connected');
  assert.equal(h.el('plan-list').children.length, 2);
  assert.equal(h.context.executed, undefined);
  assert.equal(h.el('plan-list').querySelectorAll('script').length, 0);
  assert.equal(h.el('plan-list').querySelectorAll('a')[0].getAttribute('href'), '/plans/' + encodeURIComponent(plan1.plan_id));
  assert.equal(h.el('plan-list').querySelectorAll('a')[1].getAttribute('href'), '/plans/' + encodeURIComponent(plan2.plan_id));
  assert.notEqual(h.el('plan-list').querySelectorAll('a')[0].getAttribute('href'), plan1.dashboard_url);
  assert.notEqual(h.el('plan-list').querySelectorAll('a')[1].getAttribute('href'), plan2.dashboard_url);
  assert.equal(h.el('plan-list').querySelectorAll('a')[0].textContent, plan1.objective);
  assert.equal(h.el('plan-list').querySelectorAll('a')[1].textContent, plan2.objective);
  assertSameTree(h.el('plan-list').children[0], cardTree1, 'Card 1 SSR retention');
  assertSameTree(h.el('plan-list').children[1], cardTree2, 'Card 2 SSR retention');
  assert.ok(isAttached(nav, h.body), 'Navigation attached to rendered body');
  assert.notEqual(nav.parent, h.el('plan-list'), 'Navigation is outside plan-list');
  assert.equal(nav.children[0], firstAnchor);
  assert.equal(nav.children[1], newerAnchor);
  assert.equal(nav.children[2], olderAnchor);

  wirePage.store_sequence = 101;
  wirePage.previous_cursor = 'cursor-prev-opaque/2&special=yes';
  wirePage.next_cursor = 'cursor-next-opaque/2&special=yes';
  await h.tick();
  assert.equal(h.el('conn-label').textContent, 'Connected');
  assertSameTree(h.el('plan-list').children[0], cardTree1, 'Card 1 retained on cursor change');
  assertSameTree(h.el('plan-list').children[1], cardTree2, 'Card 2 retained on cursor change');
  assert.ok(isAttached(nav, h.body), 'Navigation remains attached to rendered body');
  assert.notEqual(nav.parent, h.el('plan-list'), 'Navigation is outside plan-list');
  assert.equal(h.el('plan-index-nav'), nav);
  assert.equal(nav.children[0], firstAnchor, 'First anchor retained identity in nav');
  assert.equal(nav.children[1], newerAnchor, 'Newer anchor retained identity in nav');
  assert.equal(nav.children[2], olderAnchor, 'Older anchor retained identity in nav');
  assert.equal(firstAnchor.getAttribute('href'), '/?limit=2');
  assert.equal(newerAnchor.getAttribute('href'), '/?before=' + encodeURIComponent(wirePage.previous_cursor) + '&limit=2');
  assert.equal(olderAnchor.getAttribute('href'), '/?after=' + encodeURIComponent(wirePage.next_cursor) + '&limit=2');

  wirePage.store_sequence = 102;
  plan1.objective = hostile + ' modified first objective without revision bump';
  await h.tick();
  assert.equal(h.el('conn-label').textContent, 'Connected');
  const card1Updated = h.el('plan-list').children[0];
  const card1Link = card1Updated.querySelector('a');
  assert.equal(card1Link.textContent, plan1.objective);
  assert.equal(card1Link.getAttribute('href'), '/plans/' + encodeURIComponent(plan1.plan_id));
  assert.notEqual(card1Link.getAttribute('href'), plan1.dashboard_url);
  assert.ok(card1Link.getAttribute('href').startsWith('/plans/'));
  assert.equal(card1Updated.querySelector('.plan-card-meta').textContent, `${plan1.status} · Rev #${plan1.revision}`);
  assert.equal(card1Updated.querySelector('time').textContent, plan1.published_at);
  assert.equal(card1Updated.querySelector('.task-id-code').textContent, plan1.plan_id);
  assert.match(h.el('plan-list').textContent, /modified first objective without revision bump/);
  assertSameTree(h.el('plan-list').children[1], cardTree2, 'Card 2 tree identity preserved');

  assert.ok(!h.calls.includes('/api/plans'));
  assert.ok(h.calls.every(url => url === plansUrl));
  assert.equal(h.maximumActive(), 1);

  wirePage.store_sequence = 103;
  wirePage.plans = [];
  wirePage.collection_fence = {count: 2, max_rowid: 10};
  await h.tick();
  assert.equal(h.el('conn-label').textContent, 'Connected');
  assert.match(h.el('plan-list').textContent, /No plans on this page/);
  assert.doesNotMatch(h.el('plan-list').textContent, /No committed plans/);

  wirePage.store_sequence = 104;
  wirePage.collection_fence = {count: 0, max_rowid: 0};
  wirePage.previous_cursor = null;
  wirePage.next_cursor = null;
  await h.tick();
  assert.equal(h.el('conn-label').textContent, 'Connected');
  assert.match(h.el('plan-list').textContent, /No committed plans\./);

  assert.deepEqual(h.errors, []);
  assert.ok(!h.calls.includes('/api/plans'));
  assert.ok(h.calls.every(url => url === plansUrl));
  assert.equal(h.maximumActive(), 1);
  console.log('Compact plan index core regressions passed');
}

async function testCompactIndexErrors() {
  const plansUrl = '/api/plan-index?after=selected-cursor&limit=2';
  const plan1 = {
    plan_id: 'plan/one',
    objective: 'Initial canonical planning objective 1',
    status: 'planning',
    revision: 1,
    published_at: publishedAt,
    dashboard_url: 'http://foreign.example.com/malicious1'
  };
  const plan2 = {
    plan_id: 'plan/two',
    objective: 'Initial canonical planning objective 2',
    status: 'running',
    revision: 2,
    published_at: publishedAt,
    dashboard_url: 'http://foreign.example.com/malicious2'
  };
  const validPage = {
    plans: [plan1, plan2],
    next_cursor: 'cursor-next-opaque',
    previous_cursor: 'cursor-prev-opaque',
    epoch: 'epoch-1',
    collection_fence: {count: 5, max_rowid: 6},
    store_sequence: 10
  };

  let card1, card2, cardTree1, cardTree2, nav, firstAnchor, newerAnchor, olderAnchor;
  let planListTree, navTree, planHrefs, navHrefs;
  let mode = 'valid';
  let deferredResolve = null;

  const h = serviceHarness(url => {
    assert.equal(url, plansUrl);
    if (mode === '409') {
      return response({error: {code: 'conflict', message: 'The plan collection changed'}}, 409);
    }
    if (mode === 'throw') {
      throw new Error('network disconnected');
    }
    if (mode === 'deferred') {
      return new Promise(resolve => { deferredResolve = resolve; });
    }
    if (typeof mode === 'object' && mode !== null) {
      return response(mode);
    }
    return response(validPage);
  }, {
    plansUrl,
    setup: ({el, elements, body}) => {
      const container = new Element('div');
      container.className = 'app-container';
      body.appendChild(container);

      const header = new Element('header');
      header.className = 'app-header';
      container.appendChild(header);

      const banner = el('reconnect-banner');
      banner.id = 'reconnect-banner';
      banner.className = 'banner banner--warning';
      banner.hidden = true;
      banner.setAttribute('role', 'alert');
      header.appendChild(banner);

      const bannerText = el('reconnect-text');
      bannerText.id = 'reconnect-text';
      banner.appendChild(bannerText);

      const planList = el('plan-list');
      planList.className = 'plan-list';
      planList.setAttribute('aria-label', 'Committed plans');
      container.appendChild(planList);

      card1 = createSSRCard(plan1);
      card2 = createSSRCard(plan2);
      planList.appendChild(card1);
      planList.appendChild(card2);
      cardTree1 = snapshotTree(card1);
      cardTree2 = snapshotTree(card2);

      nav = new Element('nav');
      nav.id = 'plan-index-nav';
      nav.className = 'plan-index-nav';
      nav.setAttribute('aria-label', 'Plan pages');
      elements.set('plan-index-nav', nav);

      firstAnchor = new Element('a');
      firstAnchor.id = 'plan-index-first';
      firstAnchor.textContent = 'First';
      firstAnchor.setAttribute('href', '/?limit=2');
      elements.set('plan-index-first', firstAnchor);
      nav.appendChild(firstAnchor);

      newerAnchor = new Element('a');
      newerAnchor.id = 'plan-index-newer';
      newerAnchor.textContent = 'Newer';
      newerAnchor.setAttribute('href', '/?before=' + encodeURIComponent(validPage.previous_cursor) + '&limit=2');
      elements.set('plan-index-newer', newerAnchor);
      nav.appendChild(newerAnchor);

      olderAnchor = new Element('a');
      olderAnchor.id = 'plan-index-older';
      olderAnchor.textContent = 'Older';
      olderAnchor.setAttribute('href', '/?after=' + encodeURIComponent(validPage.next_cursor) + '&limit=2');
      elements.set('plan-index-older', olderAnchor);
      nav.appendChild(olderAnchor);

      container.appendChild(nav);
    }
  });

  await h.settle();
  assert.equal(h.el('conn-label').textContent, 'Connected');
  assert.equal(h.el('plan-list').children.length, 2);
  assertSameTree(h.el('plan-list').children[0], cardTree1, 'Initial accepted card 1 SSR retention');
  assertSameTree(h.el('plan-list').children[1], cardTree2, 'Initial accepted card 2 SSR retention');
  planListTree = snapshotTree(h.el('plan-list'));
  navTree = snapshotTree(nav);
  planHrefs = h.el('plan-list').querySelectorAll('a').map(a => a.getAttribute('href'));
  navHrefs = nav.children.map(a => a.getAttribute('href'));

  mode = '409';
  const callsBefore409 = h.calls.length;
  await h.tick();
  assert.ok(h.calls.length - callsBefore409 <= 2, 'No endless retry loop on 409');
  assert.ok(h.calls.every(url => url === plansUrl), 'Chosen page URL retained on 409');
  assert.notEqual(h.el('conn-label').textContent, 'Offline', 'Status not offline on 409');
  assertSameTree(h.el('plan-list'), planListTree, 'Accepted card tree unchanged on 409');
  assert.ok(isAttached(nav, h.body), 'Navigation remains attached on 409');
  const attachedFirst = nav.children[0] || h.el('plan-index-first');
  assert.ok(isAttached(attachedFirst, h.body), 'First link attached on 409');
  assert.equal(attachedFirst.getAttribute('href'), '/?limit=2', 'First relative href ?limit=2');
  const notice409 = visibleNoticeText(h);
  assert.match(notice409, /restart.*(?:first|first page)/i, 'Visible notice directs restart from First page');
  assert.ok(!h.calls.includes('/api/plans'), 'Did not autofetch /api/plans');
  assert.ok(!h.calls.includes('/api/plan-index'), 'Did not autofetch bare first page');

  mode = 'throw';
  await h.tick();
  assert.equal(h.el('conn-label').textContent, 'Offline', 'Offline on disconnect');
  assertSameTree(h.el('plan-list'), planListTree, 'Accepted card tree kept on disconnect');

  mode = 'valid';
  await h.tick();
  assert.equal(h.el('conn-label').textContent, 'Connected', 'Connected after recovery');
  assert.equal(visibleNoticeText(h), '', 'All visible conflict and disconnect notices cleared');
  assertSameTree(h.el('plan-list'), planListTree, 'Card tree unchanged after recovery');
  assert.ok(isAttached(nav, h.body), 'Nav identity and attachment retained');
  assert.equal(nav.children[0], firstAnchor, 'First anchor identity retained in nav');
  assert.equal(nav.children[0].getAttribute('href'), '/?limit=2');

  const omitKey = (obj, key) => { const copy = {...obj}; delete copy[key]; return copy; };
  const malformedCases = [
    {label: 'null plans', page: {...validPage, plans: null}},
    {label: 'not array plans', page: {...validPage, plans: 'invalid'}},
    {label: 'missing plans', page: omitKey(validPage, 'plans')},
    {label: 'missing next_cursor', page: omitKey(validPage, 'next_cursor')},
    {label: 'missing previous_cursor', page: omitKey(validPage, 'previous_cursor')},
    {label: 'missing epoch', page: omitKey(validPage, 'epoch')},
    {label: 'empty epoch', page: {...validPage, epoch: ''}},
    {label: 'missing collection_fence', page: omitKey(validPage, 'collection_fence')},
    {label: 'missing store_sequence', page: omitKey(validPage, 'store_sequence')},
    {label: 'next_cursor number', page: {...validPage, next_cursor: 123}},
    {label: 'next_cursor object', page: {...validPage, next_cursor: {cursor: 'abc'}}},
    {label: 'next_cursor array', page: {...validPage, next_cursor: ['abc']}},
    {label: 'empty next_cursor', page: {...validPage, next_cursor: ''}},
    {label: 'previous_cursor number', page: {...validPage, previous_cursor: 123}},
    {label: 'previous_cursor object', page: {...validPage, previous_cursor: {cursor: 'abc'}}},
    {label: 'previous_cursor array', page: {...validPage, previous_cursor: ['abc']}},
    {label: 'empty previous_cursor', page: {...validPage, previous_cursor: ''}},
    {label: 'negative store_sequence', page: {...validPage, store_sequence: -1}},
    {label: 'string store_sequence', page: {...validPage, store_sequence: '10'}},
    {label: 'fractional store_sequence', page: {...validPage, store_sequence: 10.5}},
    {label: 'wrong fence count type', page: {...validPage, collection_fence: {count: 'three', max_rowid: 6}}},
    {label: 'negative fence count', page: {...validPage, collection_fence: {count: -1, max_rowid: 6}}},
    {label: 'negative max_rowid', page: {...validPage, collection_fence: {count: 3, max_rowid: -1}}},
    {label: 'duplicate plan id', page: {...validPage, plans: [plan1, {...plan1}]}},
    {label: 'missing plan_id', page: {...validPage, plans: [omitKey(plan1, 'plan_id'), plan2]}},
    {label: 'missing objective', page: {...validPage, plans: [omitKey(plan1, 'objective'), plan2]}},
    {label: 'missing status', page: {...validPage, plans: [omitKey(plan1, 'status'), plan2]}},
    {label: 'missing revision', page: {...validPage, plans: [omitKey(plan1, 'revision'), plan2]}},
    {label: 'missing published_at', page: {...validPage, plans: [omitKey(plan1, 'published_at'), plan2]}},
    {label: 'missing dashboard_url', page: {...validPage, plans: [omitKey(plan1, 'dashboard_url'), plan2]}},
    {label: 'wrong numeric revision', page: {...validPage, plans: [{...plan1, revision: 'one'}, plan2]}},
    {label: 'non-string plan_id', page: {...validPage, plans: [{...plan1, plan_id: 123}, plan2]}},
    {label: 'non-string objective', page: {...validPage, plans: [{...plan1, objective: 123}, plan2]}},
    {label: 'non-string status', page: {...validPage, plans: [{...plan1, status: 123}, plan2]}},
    {label: 'non-string published_at', page: {...validPage, plans: [{...plan1, published_at: 123}, plan2]}},
    {label: 'non-string dashboard_url', page: {...validPage, plans: [{...plan1, dashboard_url: 123}, plan2]}},
    {label: 'unknown status completed', page: {...validPage, plans: [{...plan1, status: 'completed'}, plan2]}},
    {label: 'unknown status bogus', page: {...validPage, plans: [{...plan1, status: 'bogus'}, plan2]}},
    // Detect unbounded selected pages using only valid, unique summaries.
    {label: 'three valid summaries exceed selected limit 2', page: {...validPage,
      plans: [{...plan1, objective: 'Rejected oversized prefix'}, plan2,
        {...plan1, plan_id: 'plan/three', objective: 'Valid unique third card'}],
      next_cursor: 'rejected-next-three', previous_cursor: 'rejected-prev-three', store_sequence: 11}},
    {label: '101 valid unique summaries exceed global maximum', page: {...validPage,
      plans: Array.from({length: 101}, (_, i) => ({...plan1,
        plan_id: 'oversized-plan-' + i, objective: 'Valid oversized summary ' + i})),
      collection_fence: {count: 101, max_rowid: 101},
      next_cursor: 'rejected-next-101', previous_cursor: 'rejected-prev-101', store_sequence: 11}},
    {label: 'late malformed summary atomic rejection', page: {...validPage, plans: [{...plan1, objective: 'Modified first objective that must not apply'}, omitKey(plan2, 'revision')], next_cursor: 'cursor-next-rejected', previous_cursor: 'cursor-prev-rejected', store_sequence: 11}}
  ];

  for (const {label, page} of malformedCases) {
    mode = page;
    await h.tick();
    const conn = h.el('conn-label').textContent;
    assert.ok(conn === 'Offline' || /unavailable|error/i.test(conn) || /unavailable|error/i.test(h.body.textContent),
      'Error state on: ' + label);
    assert.equal(h.el('plan-list').children.length, 2, 'Card count retained on: ' + label);
    assert.equal(h.el('plan-list').children[0], card1, 'First card identity retained on: ' + label);
    assert.equal(h.el('plan-list').children[1], card2, 'Second card identity retained on: ' + label);
    assert.equal(h.el('plan-list').children[0].querySelector('a').textContent, plan1.objective, 'First card text retained on: ' + label);
    assert.equal(h.el('plan-list').children[1].querySelector('a').textContent, plan2.objective, 'Second card text retained on: ' + label);
    assertSameTree(h.el('plan-list'), planListTree, 'Whole list retained on: ' + label);
    assert.deepEqual(h.el('plan-list').querySelectorAll('a').map(a => a.getAttribute('href')),
      planHrefs, 'All accepted card hrefs retained on: ' + label);
    assert.ok(isAttached(nav, h.body), 'Nav attached on: ' + label);
    assert.equal(h.el('plan-index-nav'), nav, 'Nav retained on: ' + label);
    assertSameTree(nav, navTree, 'Whole nav retained on: ' + label);
    for (let i = 0; i < nav.children.length; i++) {
      assert.equal(nav.children[i].getAttribute('href'), navHrefs[i], 'Nav href ' + i + ' retained on: ' + label);
    }
    assert.ok(!h.calls.includes('/api/plans'), 'No fallback on: ' + label);
    assert.ok(h.calls.every(url => url === plansUrl), 'Chosen URL on: ' + label);

    mode = 'valid';
    await h.tick();
    assert.equal(h.el('conn-label').textContent, 'Connected', 'Restored connected after: ' + label);
    assertSameTree(h.el('plan-list'), planListTree, 'Cards retained after restore: ' + label);
    assert.deepEqual(h.el('plan-list').querySelectorAll('a').map(a => a.getAttribute('href')),
      planHrefs, 'All accepted card hrefs retained after restore: ' + label);
    assert.ok(isAttached(nav, h.body), 'Nav attached after restore: ' + label);
    assertSameTree(nav, navTree, 'Whole nav retained after restore: ' + label);
    for (let i = 0; i < nav.children.length; i++) {
      assert.equal(nav.children[i].getAttribute('href'), navHrefs[i], 'Nav href ' + i + ' retained after restore: ' + label);
    }
  }

  mode = 'deferred';
  const callsBeforeDeferred = h.calls.length;
  const pending1 = h.tick();
  const pending2 = h.tick();
  await h.settle();
  assert.equal(typeof deferredResolve, 'function', 'Deferred resolver function exists');
  assert.equal(h.calls.length - callsBeforeDeferred, 1, 'Only one new fetch began');
  assert.equal(h.maximumActive(), 1, 'Max inflight is 1 while pending');
  deferredResolve(response(validPage));
  await Promise.all([pending1, pending2]);
  await h.settle();
  assert.equal(h.maximumActive(), 1, 'Max inflight is 1 after settle');
  assert.equal(h.el('conn-label').textContent, 'Connected', 'Connected after deferred settle');

  const bareUrl = '/api/plan-index';
  const barePage = {
    plans: [plan1],
    next_cursor: 'opaque-older-cursor',
    previous_cursor: null,
    epoch: 'epoch-1',
    collection_fence: {count: 5, max_rowid: 10},
    store_sequence: 20
  };
  let bareOlderAnchor;
  const hBare = serviceHarness(url => {
    assert.equal(url, bareUrl, 'Default fetch URL is bare');
    return response(barePage);
  }, {
    plansUrl: bareUrl,
    setup: ({el, elements, body}) => {
      const planList = el('plan-list');
      planList.className = 'plan-list';
      body.appendChild(planList);

      const navBare = new Element('nav');
      navBare.id = 'plan-index-nav';
      navBare.className = 'plan-index-nav';
      elements.set('plan-index-nav', navBare);

      bareOlderAnchor = new Element('a');
      bareOlderAnchor.id = 'plan-index-older';
      bareOlderAnchor.textContent = 'Older';
      elements.set('plan-index-older', bareOlderAnchor);
      navBare.appendChild(bareOlderAnchor);

      body.appendChild(navBare);
    }
  });

  await hBare.settle();
  assert.equal(hBare.el('conn-label').textContent, 'Connected');
  assert.equal(bareOlderAnchor.getAttribute('href'), '/?after=opaque-older-cursor&limit=25');
  assert.ok(hBare.calls.every(url => url === bareUrl), 'Default fetch URL remained bare');
  console.log('Compact plan index error and recovery regressions passed');
}

function compactIndexFixture(plansUrl, limit, plans) {
  // Mount real SSR trees and poll the public page, without invoking validator internals.
  const state = {page: {
    plans, next_cursor: 'opaque/9223372036854775807?raw=%2F&x=1'.padEnd(1024, '.'),
    previous_cursor: null, epoch: 'fixture-epoch',
    collection_fence: {count: plans.length + 1, max_rowid: JSON.parse('9223372036854775807')},
    store_sequence: 10
  }};
  const cardTrees = [];
  let nav;
  const h = serviceHarness(url => {
    assert.equal(url, plansUrl, 'Keep the selected compact page URL');
    return response(state.page);
  }, {
    plansUrl,
    setup: ({el, elements, body}) => {
      const banner = el('reconnect-banner');
      banner.hidden = true;
      banner.setAttribute('role', 'alert');
      banner.appendChild(el('reconnect-text'));
      body.appendChild(banner);
      const list = el('plan-list');
      list.className = 'plan-list';
      body.appendChild(list);
      for (const plan of plans) {
        const card = createSSRCard(plan);
        list.appendChild(card);
        cardTrees.push(snapshotTree(card));
      }
      nav = new Element('nav');
      nav.id = 'plan-index-nav';
      nav.className = 'plan-index-nav';
      nav.setAttribute('aria-label', 'Plan pages');
      elements.set(nav.id, nav);
      const older = new Element('a');
      older.id = 'plan-index-older';
      older.textContent = 'Older';
      older.setAttribute('href', '/?after=' + encodeURIComponent(state.page.next_cursor) + '&limit=' + limit);
      elements.set(older.id, older);
      nav.appendChild(older);
      body.appendChild(nav);
    }
  });
  return {h, state, cardTrees, nav};
}

async function testCompactIndexStatuses() {
  // Detect exclusion of stopped on initial acceptance and running-to-stopped updates.
  const plan = {plan_id: 'stopped-plan', objective: 'Canonical stopped objective', status: 'stopped',
    revision: 1, published_at: publishedAt, dashboard_url: 'http://foreign.example.com/stopped'};
  const sibling = {...plan, plan_id: 'unchanged-sibling', objective: 'Unchanged sibling', status: 'complete'};
  const {h, state, cardTrees, nav} = compactIndexFixture('/api/plan-index?limit=2', 2, [plan, sibling]);
  await h.settle();
  assert.equal(h.el('conn-label').textContent, 'Connected', 'Initial stopped plan must be accepted');
  assert.equal(visibleNoticeText(h), '');
  assert.equal(h.el('plan-list').children.length, 2);
  cardTrees.forEach((tree, i) => assertSameTree(h.el('plan-list').children[i], tree, 'Initial stopped-page SSR tree'));
  const firstCard = h.el('plan-list').children[0];
  const siblingHref = h.el('plan-list').children[1].querySelector('a').getAttribute('href');
  const navTree = snapshotTree(nav), navHrefs = nav.children.map(a => a.getAttribute('href'));
  assert.ok(isAttached(nav, h.body));
  assert.equal(navHrefs[0], '/?after=' + encodeURIComponent(state.page.next_cursor) + '&limit=2',
    'Keep a maximum-length opaque cursor and MAX_INT64 collection fence');
  for (const status of ['planning', 'blocked', 'complete', 'running', 'stopped']) {
    plan.status = status;
    plan.revision++;
    state.page.store_sequence++;
    await h.tick();
    assert.equal(h.el('conn-label').textContent, 'Connected', 'Accept canonical status ' + status);
    assert.equal(visibleNoticeText(h), '');
    assert.equal(h.el('plan-list').children.length, 2);
    assert.equal(h.el('plan-list').children[0], firstCard);
    assert.equal(firstCard.querySelector('.plan-card-meta').textContent, `${status} · Rev #${plan.revision}`);
    assert.equal(firstCard.querySelector('a').getAttribute('href'), '/plans/stopped-plan');
    assertSameTree(h.el('plan-list').children[1], cardTrees[1], 'Whole unchanged sibling on ' + status);
    assert.equal(h.el('plan-list').children[1].querySelector('a').getAttribute('href'), siblingHref);
    assert.ok(isAttached(nav, h.body));
    assertSameTree(nav, navTree, 'Unchanged navigation on ' + status);
    assert.deepEqual(nav.children.map(a => a.getAttribute('href')), navHrefs);
  }
  assert.deepEqual(h.errors, []);
  assert.ok(h.calls.every(url => url === '/api/plan-index?limit=2'));
  assert.equal(h.maximumActive(), 1);
  console.log('Compact plan index canonical statuses and whole sibling retention passed');
}

async function testCompactIndexLimit(plansUrl, limit) {
  // Detect missing effective default/requested/global bounds before any DOM mutation.
  const plans = Array.from({length: limit}, (_, i) => ({
    plan_id: 'limit-' + limit + '-' + i, objective: 'Valid boundary summary ' + i, status: 'planning',
    revision: 1, published_at: publishedAt, dashboard_url: 'http://foreign.example.com/boundary/' + i
  }));
  const {h, state, cardTrees, nav} = compactIndexFixture(plansUrl, limit, plans);
  await h.settle();
  assert.equal(h.el('conn-label').textContent, 'Connected', 'Accept valid boundary ' + limit);
  assert.equal(h.el('plan-list').children.length, limit);
  cardTrees.forEach((tree, i) => assertSameTree(h.el('plan-list').children[i], tree, 'Accepted boundary card ' + i));
  assert.ok(isAttached(nav, h.body));
  assert.equal(nav.children.length, 1);
  assert.equal(nav.children[0].getAttribute('href'),
    '/?after=' + encodeURIComponent(state.page.next_cursor) + '&limit=' + limit);
  const listTree = snapshotTree(h.el('plan-list')), navTree = snapshotTree(nav);
  const cardHrefs = h.el('plan-list').querySelectorAll('a').map(a => a.getAttribute('href'));
  const navHrefs = nav.children.map(a => a.getAttribute('href'));
  const acceptedPage = state.page;
  state.page = {...acceptedPage,
    plans: [{...plans[0], objective: 'Rejected oversized prefix must not render'}, ...plans.slice(1),
      {...plans[0], plan_id: 'limit-' + limit + '-extra', objective: 'Valid unique extra summary'}],
    next_cursor: 'rejected-older', previous_cursor: 'rejected-newer', store_sequence: 11
  };
  await h.tick();
  assert.equal(h.el('conn-label').textContent, 'Offline', 'Reject boundary plus one for limit ' + limit);
  assert.match(visibleNoticeText(h), /unavailable/i);
  assertSameTree(h.el('plan-list'), listTree, 'Whole accepted boundary card tree');
  assert.deepEqual(h.el('plan-list').querySelectorAll('a').map(a => a.getAttribute('href')), cardHrefs);
  assert.ok(isAttached(nav, h.body));
  assertSameTree(nav, navTree, 'Whole accepted boundary nav tree');
  assert.deepEqual(nav.children.map(a => a.getAttribute('href')), navHrefs);
  state.page = acceptedPage;
  await h.tick();
  assert.equal(h.el('conn-label').textContent, 'Connected', 'Recover the accepted boundary ' + limit);
  assert.equal(visibleNoticeText(h), '');
  assertSameTree(h.el('plan-list'), listTree, 'Boundary cards retained after recovery');
  assertSameTree(nav, navTree, 'Boundary nav retained after recovery');
  assert.deepEqual(h.el('plan-list').querySelectorAll('a').map(a => a.getAttribute('href')), cardHrefs);
  assert.deepEqual(nav.children.map(a => a.getAttribute('href')), navHrefs);
  assert.deepEqual(h.errors, []);
  assert.ok(h.calls.every(url => url === plansUrl));
  assert.equal(h.maximumActive(), 1);
  console.log('Compact plan index limit ' + limit + ' boundary and atomic rejection passed');
}

(async () => {
  if (process.argv[2] === '--case' && process.argv[3] === 'stable-publication') {
    assert.equal(process.argv.length, 4, 'Unknown focused DOM scenario');
    await testStablePublication();
    return testStablePublicationWarnings();
  }
  if (process.argv[2] === '--case' && process.argv[3] === 'compact-index-core') {
    assert.equal(process.argv.length, 4, 'Unknown focused DOM scenario');
    return testCompactIndexCore();
  }
  if (process.argv[2] === 'compact-index-core') return testCompactIndexCore();
  if (process.argv[2] === '--case' && process.argv[3] === 'compact-index-errors') {
    assert.equal(process.argv.length, 4, 'Unknown focused DOM scenario');
    return testCompactIndexErrors();
  }
  if (process.argv[2] === 'compact-index-errors') return testCompactIndexErrors();
  if (process.argv[2] === '--case' && process.argv[3] === 'compact-index-statuses') {
    assert.equal(process.argv.length, 4, 'Unknown focused DOM scenario');
    return testCompactIndexStatuses();
  }
  if (process.argv[2] === '--case' && process.argv[3] === 'compact-index-default-limit') {
    assert.equal(process.argv.length, 4, 'Unknown focused DOM scenario');
    return testCompactIndexLimit('/api/plan-index', 25);
  }
  if (process.argv[2] === '--case' && process.argv[3] === 'compact-index-max-limit') {
    assert.equal(process.argv.length, 4, 'Unknown focused DOM scenario');
    return testCompactIndexLimit('/api/plan-index?limit=100', 100);
  }
  if (process.argv[2] === 'expanded-unchanged') return testExpandedDetailsUnchanged();
  if (process.argv[2] === 'expanded-unrelated') return testExpandedDetailsUnrelatedAddition();
  if (process.argv[2] === 'recovery-warnings') return testRecoveryWarnings();
  assert.equal(process.argv.length, 2, 'Unknown focused DOM scenario');
  testLegacy(); // Absent body mode remains legacy.
  testLegacy('legacy');
  await testService();
  await testServiceIndex();
  await testExpandedDetailsUnchanged();
  await testExpandedDetailsUnrelatedAddition();
  await testRecoveryWarnings();
  await testStablePublication();
  await testStablePublicationWarnings();
  await testCompactIndexCore();
  await testCompactIndexErrors();
  await testCompactIndexStatuses();
  await testCompactIndexLimit('/api/plan-index', 25);
  await testCompactIndexLimit('/api/plan-index?limit=100', 100);
})().catch(error => {console.error(error); process.exitCode = 1;});
