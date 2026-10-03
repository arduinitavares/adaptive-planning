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
  document: {readyState: 'complete', getElementById: el, createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag)},
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
send(initial); // positive valid revision 1 below current revision 2
assert.match(el('reconnect-text').textContent, /consistency conflict/);
const replacement = {...initial, generation: 'g2', objective: 'Replacement task', nextStepId: 'B',
  status: 'planning', steps: [step('A', 'pending'), step('B', 'pending')]};
send(replacement);
assert.equal(el('reconnect-banner').hidden, true);
assert.equal(el('details-container').querySelector('.task-id-code').textContent, 'B');
assert.equal(el('retired-section').hidden, true);

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
