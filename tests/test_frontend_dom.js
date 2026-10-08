'use strict';
// Execute the actual dashboard in a deliberately small DOM/EventSource harness.
// This checks behavior and node identity, not browser CSS layout or computed color.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.parent = null; this._text = '';
    this.className = ''; this.style = {}; this.dataset = {}; this.attributes = {};
    this.hidden = false; this.open = false; this.listeners = {}; this.clientWidth = 800;
    this.classList = {
      add: (...names) => { this.className = [...new Set(this.className.split(' ').filter(Boolean).concat(names))].join(' '); },
      remove: (...names) => { this.className = this.className.split(' ').filter(n => !names.includes(n)).join(' '); },
      contains: name => this.className.split(' ').includes(name)
    };
  }
  set textContent(value) { this._text = String(value); this.children.forEach(c => c.parent = null); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  appendChild(child) { child.parent = this; this.children.push(child); return child; }
  setAttribute(key, value) { this.attributes[key] = String(value); if (key === 'title') this.title = value; }
  getAttribute(key) { return key === 'title' ? this.title : this.attributes[key]; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); this.parent = null; }
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

function serviceHarness(responder, {index = false} = {}) {
  const elements = new Map();
  const el = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const body = new Element('body');
  body.dataset = index ? {mode: 'service', plansUrl: '/api/plans'} : {mode: 'service', planId: 'service-plan',
    planUrl: '/api/plans/service-plan', coordinationUrl: '/api/plans/service-plan/coordination', eventsUrl: '/api/plans/service-plan/events'};
  const intervals = [], calls = [], errors = [];
  let active = 0, maximumActive = 0;
  const context = {
    document: {body, readyState: 'complete', getElementById: el, createElement: tag => new Element(tag),
      createElementNS: (_, tag) => new Element(tag)},
    EventSource: class {constructor() {assert.fail('Service mode must not subscribe to legacy SSE');}},
    Date, URLSearchParams, encodeURIComponent,
    fetch: async (url, options) => {
      assert.equal(options.cache, 'no-store'); assert.equal(options.credentials, 'same-origin');
      assert.ok(!options.method || options.method === 'GET');
      assert.ok(!options.headers || !options.headers.Authorization);
      calls.push(url); active++; maximumActive = Math.max(maximumActive, active);
      try { return await responder(url, calls.length); } finally {active--;}
    },
    localStorage: {getItem: () => assert.fail('No persisted progress'), setItem: () => assert.fail('No persisted progress')},
    sessionStorage: {getItem: () => assert.fail('No persisted progress'), setItem: () => assert.fail('No persisted progress')},
    console: {error: (...args) => errors.push(args)},
    setInterval: (fn, delay) => {assert.equal(delay, 1000); intervals.push(fn); return intervals.length;},
    setTimeout: () => 0
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../assets/dashboard/app.js'), 'utf8'), context);
  const settle = () => new Promise(resolve => setImmediate(resolve));
  return {el, body, calls, errors, context, settle, maximumActive: () => maximumActive,
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

function detailsFixture() {
  const state = {view: serviceView(), items: JSON.parse(JSON.stringify([...firstItems, ...secondItems])), eventCount: 205};
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
        (_, i) => event(after + i + 1))});
    }
    assert.fail('Unexpected public endpoint ' + url);
  });
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

(async () => {
  if (process.argv[2] === 'expanded-unchanged') return testExpandedDetailsUnchanged();
  if (process.argv[2] === 'expanded-unrelated') return testExpandedDetailsUnrelatedAddition();
  assert.equal(process.argv.length, 2, 'Unknown focused DOM scenario');
  testLegacy(); // Absent body mode remains legacy.
  testLegacy('legacy');
  await testService();
  await testServiceIndex();
  await testExpandedDetailsUnchanged();
  await testExpandedDetailsUnrelatedAddition();
})().catch(error => {console.error(error); process.exitCode = 1;});
