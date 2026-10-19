/**
 * Live Plan Dashboard Application Script
 * Local-only, zero dependencies. Legacy mode consumes SSE /events; service
 * mode polls committed public projections without persisting browser progress.
 */

(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.LivePlan = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // Rendering safety only; canonical DAG and transition validation stays on the server.
  function renderablePlan(plan) {
    const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
    const text = value => typeof value === 'string' && value.length <= 10000;
    const nonempty = value => text(value) && value.trim().length > 0;
    const timestamp = value => text(value) && Number.isFinite(Date.parse(value));
    const taskStatuses = ['planning', 'running', 'stopped', 'blocked', 'complete'];
    const modes = ['plan-only', 'first-step', 'complete-task'];
    const stepStatuses = ['pending', 'in-progress', 'blocked', 'complete', 'retired'];
    if (!object(plan) || !nonempty(plan.taskId) || !nonempty(plan.objective) ||
        !Number.isSafeInteger(plan.revision) || plan.revision < 1 ||
        !timestamp(plan.updatedAt) || !taskStatuses.includes(plan.status) ||
        !modes.includes(plan.executionMode) || !nonempty(plan.changeSummary) ||
        !(plan.nextStepId === null || nonempty(plan.nextStepId)) ||
        !Array.isArray(plan.steps) || plan.steps.length > 200 ||
        !Array.isArray(plan.history) || plan.history.length > 100) return false;
    if (!plan.steps.every(step => object(step) && nonempty(step.id) && nonempty(step.title) &&
        stepStatuses.includes(step.status) && text(step.check) &&
        Array.isArray(step.dependsOn) && step.dependsOn.every(nonempty) &&
        Array.isArray(step.evidence) && step.evidence.length <= 50 && step.evidence.every(text) &&
        (step.summary == null || text(step.summary)) && (step.details == null || text(step.details)) &&
        (step.status !== 'retired' || nonempty(step.retiredReason)))) return false;
    if (new Set(plan.steps.map(step => step.id)).size !== plan.steps.length) return false;
    return plan.history.every(entry => object(entry) && Number.isSafeInteger(entry.revision) &&
      entry.revision >= 1 && timestamp(entry.timestamp) && nonempty(entry.summary) &&
      (entry.authorizationNote === undefined || nonempty(entry.authorizationNote)) &&
      (entry.recoveryNote === undefined || nonempty(entry.recoveryNote)));
  }

  // Pure event/state decision helper for testing and runtime consistency management
  function evaluatePlanUpdate(rawText, currentState) {
    let plan;
    try {
      plan = JSON.parse(rawText);
    } catch (e) {
      return { accept: false, plan: null, rawText, availabilityError: true, reason: 'unparseable' };
    }

    if (!plan || typeof plan !== 'object') {
      return { accept: false, plan: null, rawText, availabilityError: true, reason: 'invalid_object' };
    }

    const isV1 = plan.schemaVersion === 1;
    const isV2 = plan.schemaVersion === 2;

    if (!isV1 && !isV2) {
      return { accept: false, plan: null, rawText, availabilityError: true, reason: 'unsupported_schema' };
    }

    const hasBasicFields = renderablePlan(plan);

    if (!hasBasicFields) {
      return { accept: false, plan: null, rawText, availabilityError: true, reason: 'invalid_fields' };
    }

    if (isV2) {
      if (typeof plan.generation !== 'string' || !plan.generation.trim() || typeof plan.scope !== 'object' || !plan.scope || Array.isArray(plan.scope) ||
          plan.scope.mode !== plan.executionMode || !Number.isSafeInteger(plan.scope.sinceRevision) ||
          plan.scope.sinceRevision < 1 || plan.scope.sinceRevision > plan.revision ||
          !Array.isArray(plan.scope.baseline) || !plan.scope.baseline.every(id => typeof id === 'string') ||
          !(plan.scope.claimed === null || typeof plan.scope.claimed === 'string')) {
        return { accept: false, plan: null, rawText, availabilityError: true, reason: 'invalid_v2_fields' };
      }
    }

    // Valid plan arrival clears availability error even if rejected for consistency
    if (!currentState || !currentState.currentPlan) {
      return { accept: true, plan, rawText, clearAvailability: true, clearConsistency: true, reason: 'initial' };
    }

    const curr = currentState.currentPlan;
    const currIsV1 = curr.schemaVersion === 1;
    const currIsV2 = curr.schemaVersion === 2;

    if (currIsV1) {
      if (isV2) {
        // v1 -> v2 migration accepts
        return { accept: true, plan, rawText, clearAvailability: true, clearConsistency: true, reason: 'migration_v1_to_v2' };
      }
      // Both v1
      if (plan.taskId !== curr.taskId) {
        return { accept: true, plan, rawText, clearAvailability: true, clearConsistency: true, reason: 'new_task' };
      }
      if (plan.revision > curr.revision) {
        return { accept: true, plan, rawText, clearAvailability: true, clearConsistency: true, reason: 'newer_revision' };
      }
      if (plan.revision === curr.revision) {
        if (rawText === currentState.lastRawText) {
          return { accept: false, plan, rawText, clearAvailability: true, keepConsistency: true, reason: 'identical' };
        }
        return { accept: false, plan, rawText, clearAvailability: true, consistencyWarning: true, reason: 'equal_key_changed_bytes' };
      }
      // Rollback
      return { accept: false, plan, rawText, clearAvailability: true, consistencyWarning: true, reason: 'rollback' };
    }

    if (currIsV2) {
      if (isV1) {
        // v1 arriving after v2 is rollback/conflict
        return { accept: false, plan, rawText, clearAvailability: true, consistencyWarning: true, reason: 'v1_rollback' };
      }
      // Both v2
      if (plan.generation !== curr.generation) {
        // Different generation = replacement
        return { accept: true, plan, rawText, clearAvailability: true, clearConsistency: true, reason: 'new_generation' };
      }
      // Same generation
      if (plan.revision > curr.revision) {
        return { accept: true, plan, rawText, clearAvailability: true, clearConsistency: true, reason: 'newer_revision' };
      }
      if (plan.revision === curr.revision) {
        if (rawText === currentState.lastRawText) {
          return { accept: false, plan, rawText, clearAvailability: true, keepConsistency: true, reason: 'identical' };
        }
        return { accept: false, plan, rawText, clearAvailability: true, consistencyWarning: true, reason: 'equal_key_changed_bytes' };
      }
      // Rollback
      return { accept: false, plan, rawText, clearAvailability: true, consistencyWarning: true, reason: 'rollback' };
    }

    return { accept: false, plan, rawText, clearAvailability: true, consistencyWarning: true, reason: 'unknown_state' };
  }

  // Service snapshots are renderer-compatible, even when multiple active attempts
  // prevent a strict legacy publication. Coordination may change at the same revision.
  function serviceSnapshot(view) {
    if (!view || !view.metadata || !view.coordination || !view.draft ||
        typeof view.plan_id !== 'string' || view.metadata.plan_id !== view.plan_id ||
        typeof view.metadata.epoch !== 'string' || typeof view.metadata.generation !== 'string' ||
        !Number.isSafeInteger(view.coordination.store_sequence) || view.coordination.store_sequence < 0 ||
        !renderablePlan(view.legacy_snapshot) || view.legacy_snapshot.schemaVersion !== 2 ||
        view.legacy_snapshot.taskId !== view.plan_id || view.legacy_snapshot.revision !== view.metadata.revision ||
        view.legacy_snapshot.generation !== view.metadata.generation || view.legacy_snapshot.status !== view.status ||
        view.legacy_snapshot.updatedAt !== view.published_at) {
      throw new Error('Invalid public plan projection');
    }
    return view.legacy_snapshot;
  }

  // If running in browser environment, initialize UI logic
  if (typeof document !== 'undefined') {
    const bodyData = document.body ? document.body.dataset : {};
    const serviceMode = bodyData.mode === 'service';
    // DOM Elements
    const connPill = document.getElementById('conn-pill');
    const connLabel = document.getElementById('conn-label');
    const taskStatusVal = document.getElementById('task-status-val');
    const execModeVal = document.getElementById('exec-mode-val');
    const revVal = document.getElementById('rev-val');
    const timeAgoVal = document.getElementById('time-ago-val');
    const goalHeading = document.getElementById('goal-heading');
    const taskIdRow = document.getElementById('task-id-row');
    const taskIdVal = document.getElementById('task-id-val');
    const nextStepMeta = document.getElementById('next-step-meta');
    const nextStepVal = document.getElementById('next-step-val');
    const reconnectBanner = document.getElementById('reconnect-banner');
    const reconnectText = document.getElementById('reconnect-text');

    const graphEmpty = document.getElementById('graph-empty');
    const graphViewport = document.getElementById('graph-viewport');
    const graphSvg = document.getElementById('graph-svg');
    const graphNodes = document.getElementById('graph-nodes');

    const retiredSection = document.getElementById('retired-section');
    const retiredCount = document.getElementById('retired-count');
    const retiredList = document.getElementById('retired-list');

    const detailsContainer = document.getElementById('details-container');
    const historyEmpty = document.getElementById('history-empty');
    const historyList = document.getElementById('history-list');

    // Application State
    let currentPlan = null;
    let lastRawText = null;
    let selectedStepId = null;
    let nodeElementsMap = new Map(); // stepId -> HTMLElement
    let lastUpdatedAt = null;
    let transportStatus = 'connecting'; // 'connected' | 'connecting' | 'offline'
    let availabilityWarning = false;
    let consistencyWarning = false;
    let renderedPublicationIdentity = null;
    let renderedGraphContent = null;
    let renderedDetailsContent = null;
    let renderedHistoryContent = null;
    let renderedRetiredContent = null;
    const renderedBadges = new WeakMap();

    // Status visual configurations
    const STATUS_CONFIG = {
      complete: { label: 'Complete', iconType: 'complete', class: 'complete' },
      'in-progress': { label: 'Working', iconType: 'working', class: 'in-progress' },
      running: { label: 'Working', iconType: 'working', class: 'running' },
      blocked: { label: 'Blocked', iconType: 'blocked', class: 'blocked' },
      pending: { label: 'Pending', iconType: 'pending', class: 'pending' },
      planning: { label: 'Planning', iconType: 'planning', class: 'planning' },
      stopped: { label: 'Stopped', iconType: 'stopped', class: 'stopped' },
      retired: { label: 'Retired', iconType: 'retired', class: 'retired' }
    };

    const SVG_NS = 'http://www.w3.org/2000/svg';

    function createSvgIcon(type) {
      const svg = document.createElementNS(SVG_NS, 'svg');
      svg.setAttribute('viewBox', '0 0 16 16');
      svg.setAttribute('width', '16');
      svg.setAttribute('height', '16');
      svg.setAttribute('fill', 'none');
      svg.setAttribute('stroke', 'currentColor');
      svg.setAttribute('stroke-width', '1.7');
      svg.setAttribute('stroke-linecap', 'round');
      svg.setAttribute('stroke-linejoin', 'round');
      svg.setAttribute('aria-hidden', 'true');
      svg.setAttribute('focusable', 'false');
      svg.classList.add('status-icon');

      if (type === 'complete') {
        const path = document.createElementNS(SVG_NS, 'path');
        path.setAttribute('d', 'm4 8 3 3 5-6');
        svg.appendChild(path);
      } else if (type === 'working' || type === 'in-progress' || type === 'running') {
        const circle = document.createElementNS(SVG_NS, 'circle');
        circle.setAttribute('cx', '8');
        circle.setAttribute('cy', '8');
        circle.setAttribute('r', '6');
        const path = document.createElementNS(SVG_NS, 'path');
        path.setAttribute('d', 'M8 4v4l3 2');
        svg.appendChild(circle);
        svg.appendChild(path);
      } else if (type === 'blocked') {
        const triangle = document.createElementNS(SVG_NS, 'path');
        triangle.setAttribute('d', 'm8 2 7 12H1Z');
        const mark = document.createElementNS(SVG_NS, 'path');
        mark.setAttribute('d', 'M8 6v3m0 2v.1');
        svg.appendChild(triangle);
        svg.appendChild(mark);
      } else if (type === 'pending') {
        const circle = document.createElementNS(SVG_NS, 'circle');
        circle.setAttribute('cx', '8');
        circle.setAttribute('cy', '8');
        circle.setAttribute('r', '6');
        svg.appendChild(circle);
      } else if (type === 'stopped' || type === 'pause') {
        const path = document.createElementNS(SVG_NS, 'path');
        path.setAttribute('d', 'M5 3v10M11 3v10');
        svg.appendChild(path);
      } else if (type === 'planning') {
        const path = document.createElementNS(SVG_NS, 'path');
        path.setAttribute('d', 'M4 2h8v12H4zM6 5h4M6 8h4M6 11h2');
        svg.appendChild(path);
      } else if (type === 'retired') {
        const path = document.createElementNS(SVG_NS, 'path');
        path.setAttribute('d', 'M2 8h12');
        svg.appendChild(path);
      }
      return svg;
    }

    function renderStatusBadge(container, statusKey, customLabel) {
      const conf = STATUS_CONFIG[statusKey] || STATUS_CONFIG.pending;
      const label = customLabel || conf.label;
      const content = JSON.stringify([conf.iconType, label]);
      if (renderedBadges.get(container) === content) return container;
      container.textContent = '';
      const icon = createSvgIcon(conf.iconType);
      container.appendChild(icon);
      const labelSpan = document.createElement('span');
      setText(labelSpan, label);
      container.appendChild(labelSpan);
      renderedBadges.set(container, content);
      return container;
    }

    function setText(el, text) {
      if (!el) return;
      const value = text != null ? String(text) : '';
      if (el.textContent !== value) el.textContent = value;
    }

    function formatTimeAgo(isoString) {
      if (!isoString) return '—';
      const date = new Date(isoString);
      if (isNaN(date.getTime())) return '—';

      const now = new Date();
      const diffSec = Math.max(0, Math.floor((now.getTime() - date.getTime()) / 1000));

      if (diffSec < 5) return 'just now';
      if (diffSec < 60) return `${diffSec}s ago`;
      const diffMin = Math.floor(diffSec / 60);
      if (diffMin < 60) return `${diffMin}m ago`;
      const diffHours = Math.floor(diffMin / 60);
      if (diffHours < 24) return `${diffHours}h ago`;
      return `${Math.floor(diffHours / 24)}d ago`;
    }

    function updateTimeAgoDisplay() {
      if (lastUpdatedAt) {
        setText(timeAgoVal, formatTimeAgo(lastUpdatedAt));
      } else {
        setText(timeAgoVal, '—');
      }

      // Update relative times in change history list without re-rendering DOM
      if (historyList) {
        const timeEls = historyList.querySelectorAll('.history-time');
        timeEls.forEach(el => {
          const raw = el.getAttribute('title');
          if (raw) setText(el, formatTimeAgo(raw));
        });
      }
    }
    setInterval(updateTimeAgoDisplay, 1000);

    function updateBanner() {
      if (consistencyWarning) {
        setText(reconnectText, 'Plan consistency conflict: unexpected rollback or modified revision bytes. Retaining last confirmed state.');
        reconnectBanner.hidden = false;
      } else if (availabilityWarning) {
        setText(reconnectText, 'Published plan unavailable or invalid. Retaining last confirmed state.');
        reconnectBanner.hidden = false;
      } else if (transportStatus !== 'connected') {
        if (currentPlan) {
          setText(
            reconnectText,
            transportStatus === 'offline'
              ? 'Server offline. Displaying last confirmed state.'
              : 'Disconnected from server. Reconnecting… Displaying last confirmed state.'
          );
          reconnectBanner.hidden = false;
        } else {
          reconnectBanner.hidden = true;
        }
      } else {
        reconnectBanner.hidden = true;
      }
    }

    function setTransportStatus(status) {
      transportStatus = status;
      connPill.className = 'status-pill';
      if (status === 'connected') {
        connPill.classList.add('status-pill--connected');
        setText(connLabel, 'Connected');
      } else if (status === 'connecting') {
        connPill.classList.add('status-pill--connecting');
        setText(connLabel, currentPlan ? 'Reconnecting…' : 'Connecting…');
      } else {
        connPill.classList.add('status-pill--offline');
        setText(connLabel, 'Offline');
      }
      updateBanner();
    }

    function renderHeader(plan) {
      const objText = plan.objective || 'Untitled Plan';
      setText(goalHeading, objText);
      if (goalHeading && typeof goalHeading.setAttribute === 'function') {
        goalHeading.setAttribute('data-caveman', cavemanUltra(objText, 8));
        goalHeading.setAttribute('data-full', objText);
      }

      if (plan.taskId) {
        taskIdRow.hidden = false;
        setText(taskIdVal, plan.taskId);
      } else {
        taskIdRow.hidden = true;
      }

      // Task status badge (stopped has own distinct style)
      const tStatus = plan.status || 'planning';
      taskStatusVal.className = `badge badge--${tStatus}`;
      renderStatusBadge(taskStatusVal, tStatus);

      // Execution mode badge (mode badge is neutral)
      const eMode = plan.executionMode || '—';
      execModeVal.className = 'badge badge--neutral';
      setText(execModeVal, eMode);

      // Revision and updated time
      const revText = plan.revision != null
        ? (plan.generation ? `Rev #${plan.revision} (${plan.generation.slice(0, 8)})` : `Rev #${plan.revision}`)
        : '—';
      setText(revVal, revText);
      lastUpdatedAt = plan.updatedAt;
      timeAgoVal.dateTime = plan.updatedAt;
      timeAgoVal.title = new Date(plan.updatedAt).toLocaleString();
      updateTimeAgoDisplay();

      // Next step indicator
      if (nextStepMeta) {
        if (plan.nextStepId) {
          nextStepMeta.hidden = false;
          setText(nextStepVal, plan.nextStepId);
        } else {
          nextStepMeta.hidden = true;
        }
      }
    }

    function computeDagLayout(steps) {
      const stepsById = new Map();
      steps.forEach(s => stepsById.set(s.id, s));

      const layerMemo = new Map();

      function getLayer(id, visited = new Set()) {
        if (layerMemo.has(id)) return layerMemo.get(id);
        if (visited.has(id)) return 0;
        visited.add(id);

        const step = stepsById.get(id);
        if (!step || !step.dependsOn || step.dependsOn.length === 0) {
          layerMemo.set(id, 0);
          return 0;
        }

        let maxDepLayer = -1;
        for (const depId of step.dependsOn) {
          // If dep is retired or missing, don't consider for layout layer
          if (!stepsById.has(depId)) continue;
          const depLayer = getLayer(depId, new Set(visited));
          if (depLayer > maxDepLayer) {
            maxDepLayer = depLayer;
          }
        }

        const layer = maxDepLayer + 1;
        layerMemo.set(id, layer);
        return layer;
      }

      steps.forEach(s => getLayer(s.id));

      const layers = [];
      steps.forEach(s => {
        const l = layerMemo.get(s.id) || 0;
        while (layers.length <= l) {
          layers.push([]);
        }
        layers[l].push(s);
      });

      const containerEl = document.getElementById('graph-container');
      const available = containerEl ? containerEl.clientWidth : 800;
      const padding = 24;
      const gap = 36;
      const horizontal = layers.length * 200 + (layers.length - 1) * gap + padding * 2 <= available;
      const maxSiblings = layers.length > 0 ? Math.max(...layers.map(layer => layer.length)) : 1;
      const nodeWidth = horizontal
        ? Math.min(248, (available - padding * 2 - gap * (Math.max(1, layers.length) - 1)) / Math.max(1, layers.length))
        : Math.max(138, Math.min(240, (available - padding * 2 - 16 * (maxSiblings - 1)) / maxSiblings));
      const nodeHeight = 128;
      const positions = new Map();
      const totalWidth = horizontal ? available : Math.max(available, maxSiblings * (nodeWidth + 16) - 16 + padding * 2);
      const totalHeight = horizontal
        ? maxSiblings * (nodeHeight + 24) - 24 + padding * 2
        : layers.length * (nodeHeight + gap) - gap + padding * 2;

      layers.forEach((layerSteps, layerIdx) => {
        const span = layerSteps.length * (horizontal ? nodeHeight + 24 : nodeWidth + 16) - (horizontal ? 24 : 16);
        layerSteps.forEach((step, stepIdx) => {
          const x = horizontal ? padding + layerIdx * (nodeWidth + gap) : (totalWidth - span) / 2 + stepIdx * (nodeWidth + 16);
          const y = horizontal ? (totalHeight - span) / 2 + stepIdx * (nodeHeight + 24) : padding + layerIdx * (nodeHeight + gap);
          positions.set(step.id, { x, y, width: nodeWidth, height: nodeHeight, layer: layerIdx });
        });
      });
      return { positions, totalWidth, totalHeight, horizontal };
    }

    function renderEdges(steps, positions, horizontal) {
      const existingPaths = graphSvg.querySelectorAll('.edge-path');
      existingPaths.forEach(p => p.remove());

      const stepsById = new Map();
      steps.forEach(s => stepsById.set(s.id, s));

      steps.forEach(step => {
        const targetPos = positions.get(step.id);
        if (!targetPos) return;

        (step.dependsOn || []).forEach(depId => {
          const sourcePos = positions.get(depId);
          if (!sourcePos) return;

          const sourceStep = stepsById.get(depId);
          const isSourceComplete = sourceStep && sourceStep.status === 'complete';
          const isTargetActive = step.status === 'in-progress';

          const x1 = horizontal ? sourcePos.x + sourcePos.width : sourcePos.x + sourcePos.width / 2;
          const y1 = horizontal ? sourcePos.y + sourcePos.height / 2 : sourcePos.y + sourcePos.height;
          const x2 = horizontal ? targetPos.x : targetPos.x + targetPos.width / 2;
          const y2 = horizontal ? targetPos.y + targetPos.height / 2 : targetPos.y;
          const bend = Math.max(18, (horizontal ? x2 - x1 : y2 - y1) / 2);
          const pathData = horizontal
            ? `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`
            : `M ${x1} ${y1} C ${x1} ${y1 + bend}, ${x2} ${y2 - bend}, ${x2} ${y2}`;

          const pathEl = document.createElementNS('http://www.w3.org/2000/svg', 'path');
          pathEl.setAttribute('d', pathData);
          pathEl.classList.add('edge-path');

          if (isTargetActive) {
            pathEl.classList.add('edge-path--active');
            pathEl.setAttribute('marker-end', 'url(#arrow-active)');
          } else if (isSourceComplete) {
            pathEl.classList.add('edge-path--complete');
            pathEl.setAttribute('marker-end', 'url(#arrow-complete)');
          } else {
            pathEl.setAttribute('marker-end', 'url(#arrow-default)');
          }

          graphSvg.appendChild(pathEl);
        });
      });
    }

    function renderNodes(steps, positions, plan) {
      const currentStepIds = new Set(steps.map(s => s.id));

      for (const [id, el] of nodeElementsMap.entries()) {
        if (!currentStepIds.has(id)) {
          el.remove();
          nodeElementsMap.delete(id);
        }
      }

      steps.forEach(step => {
        const pos = positions.get(step.id);
        if (!pos) return;

        let nodeEl = nodeElementsMap.get(step.id);
        const isNew = !nodeEl;

        if (isNew) {
          nodeEl = document.createElement('div');
          nodeEl.className = 'step-node';
          nodeEl.setAttribute('role', 'button');
          nodeEl.setAttribute('tabindex', '0');
          nodeEl.setAttribute('aria-pressed', 'false');
          nodeEl.setAttribute('aria-label', `Step ${step.id}: ${step.title}`);

          const headerEl = document.createElement('div');
          headerEl.className = 'node-header';

          const idWrap = document.createElement('span');
          idWrap.className = 'node-id-group';

          const idEl = document.createElement('span');
          idEl.className = 'node-id';
          idWrap.appendChild(idEl);

          const nextBadge = document.createElement('span');
          nextBadge.className = 'node-next-badge';
          nextBadge.hidden = true;
          setText(nextBadge, 'Next');
          idWrap.appendChild(nextBadge);

          headerEl.appendChild(idWrap);

          const badgeEl = document.createElement('span');
          badgeEl.className = 'node-status-badge';

          const titleEl = document.createElement('div');
          titleEl.className = 'node-title';

          nodeEl.appendChild(headerEl);
          nodeEl.appendChild(badgeEl);
          nodeEl.appendChild(titleEl);

          nodeEl.addEventListener('click', () => selectStep(step.id));
          nodeEl.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              selectStep(step.id);
            }
          });

          graphNodes.appendChild(nodeEl);
          nodeElementsMap.set(step.id, nodeEl);
        }

        const sConf = STATUS_CONFIG[step.status] || STATUS_CONFIG.pending;
        nodeEl.style.transform = `translate(${pos.x}px, ${pos.y}px)`;
        nodeEl.style.width = `${pos.width}px`;
        nodeEl.style.height = `${pos.height}px`;
        nodeEl.setAttribute('aria-label', `Step ${step.id} (${sConf.label}): ${step.title}`);
        nodeEl.title = step.title;
        const statusChanged = !isNew && nodeEl.dataset.status !== step.status;
        nodeEl.dataset.status = step.status;

        nodeEl.className = `step-node step-node--${sConf.class}`;
        if (statusChanged) {
          nodeEl.classList.add('step-node--changed');
          setTimeout(() => nodeEl.classList.remove('step-node--changed'), 800);
        }
        if (step.id === selectedStepId) {
          nodeEl.classList.add('step-node--selected');
          nodeEl.setAttribute('aria-pressed', 'true');
        } else {
          nodeEl.setAttribute('aria-pressed', 'false');
        }

        const idEl = nodeEl.querySelector('.node-id');
        const nextBadge = nodeEl.querySelector('.node-next-badge');
        const badgeEl = nodeEl.querySelector('.node-status-badge');
        const titleEl = nodeEl.querySelector('.node-title');

        setText(idEl, step.id);
        setText(titleEl, step.title);
        titleEl.title = step.title;
        titleEl.setAttribute('aria-label', step.title);
        titleEl.setAttribute('data-caveman', cavemanUltra(step.title, 6));

        const isNext = plan && plan.nextStepId === step.id && step.status !== 'complete' && step.status !== 'retired';
        if (nextBadge) {
          nextBadge.hidden = !isNext;
        }
        if (isNext) {
          nodeEl.classList.add('step-node--next');
        }

        badgeEl.className = `node-status-badge badge badge--${sConf.class}`;
        renderStatusBadge(badgeEl, step.status);
      });
    }

    function renderRetired(retiredSteps) {
      if (!retiredSection || !retiredCount || !retiredList) return;
      const content = JSON.stringify((retiredSteps || []).map(step =>
        [step.id, step.title, step.retiredReason || '']));
      if (renderedRetiredContent === content) return;
      renderedRetiredContent = content;

      if (!retiredSteps || retiredSteps.length === 0) {
        retiredSection.hidden = true;
        retiredList.textContent = '';
        setText(retiredCount, '0');
        return;
      }

      retiredSection.hidden = false;
      setText(retiredCount, String(retiredSteps.length));
      retiredList.textContent = '';

      retiredSteps.forEach(step => {
        const itemEl = document.createElement('div');
        itemEl.className = 'retired-item';
        itemEl.setAttribute('role', 'button');
        itemEl.setAttribute('tabindex', '0');
        itemEl.setAttribute('aria-label', `Retired step ${step.id}: ${step.title}`);
        if (step.id === selectedStepId) {
          itemEl.classList.add('retired-item--selected');
        }

        const headerEl = document.createElement('div');
        headerEl.className = 'retired-item-header';

        const idEl = document.createElement('code');
        idEl.className = 'task-id-code';
        setText(idEl, step.id);

        const titleEl = document.createElement('span');
        titleEl.className = 'retired-item-title';
        setText(titleEl, step.title);

        headerEl.appendChild(idEl);
        headerEl.appendChild(titleEl);
        itemEl.appendChild(headerEl);

        if (step.retiredReason) {
          const reasonEl = document.createElement('div');
          reasonEl.className = 'retired-item-reason';
          setText(reasonEl, `Reason: ${step.retiredReason}`);
          itemEl.appendChild(reasonEl);
        }

        itemEl.addEventListener('click', () => selectStep(step.id));
        itemEl.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            selectStep(step.id);
          }
        });
        retiredList.appendChild(itemEl);
      });
    }

    function renderGraph(plan) {
      const allSteps = plan.steps || [];
      // Retired steps are excluded from map DAG layout
      const activeSteps = allSteps.filter(s => s.status !== 'retired');
      const retiredSteps = allSteps.filter(s => s.status === 'retired');

      if (activeSteps.length === 0 && retiredSteps.length === 0) {
        graphEmpty.hidden = false;
        graphViewport.hidden = true;
        nodeElementsMap.forEach(node => node.remove());
        nodeElementsMap.clear();
        graphSvg.querySelectorAll('.edge-path').forEach(edge => edge.remove());
        renderRetired([]);
        return;
      }

      graphEmpty.hidden = true;
      graphViewport.hidden = false;

      const { positions, totalWidth, totalHeight, horizontal } = computeDagLayout(activeSteps);

      graphViewport.style.width = `${totalWidth}px`;
      graphViewport.style.height = `${totalHeight}px`;
      graphSvg.setAttribute('width', String(totalWidth));
      graphSvg.setAttribute('height', String(totalHeight));
      graphSvg.setAttribute('viewBox', `0 0 ${totalWidth} ${totalHeight}`);

      renderEdges(activeSteps, positions, horizontal);
      renderNodes(activeSteps, positions, plan);
      renderRetired(retiredSteps);
    }

    function selectStep(stepId) {
      selectedStepId = stepId;

      for (const [id, el] of nodeElementsMap.entries()) {
        if (id === stepId) {
          el.classList.add('step-node--selected');
          el.setAttribute('aria-pressed', 'true');
        } else {
          el.classList.remove('step-node--selected');
          el.setAttribute('aria-pressed', 'false');
        }
      }

      if (retiredList) {
        const retiredItems = retiredList.querySelectorAll('.retired-item');
        retiredItems.forEach(el => {
          const idTag = el.querySelector('.task-id-code');
          if (idTag && idTag.textContent === stepId) {
            el.classList.add('retired-item--selected');
          } else {
            el.classList.remove('retired-item--selected');
          }
        });
      }

      renderDetails();
    }

    function renderDetails() {
      const steps = currentPlan && currentPlan.steps || [];
      const step = steps.find(item => item.id === selectedStepId);
      const content = JSON.stringify(step ? [
        step.id, step.title, step.status,
        step.status === 'retired' ? step.retiredReason || '' : '',
        step.summary || '', step.details || '', step.check || 'None specified.',
        step.evidence || [],
        (step.dependsOn || []).map(id => {
          const dependency = steps.find(item => item.id === id);
          return [id, dependency ? dependency.status : 'pending'];
        })
      ] : [steps.length ? 'unselected' : 'empty']);
      if (renderedDetailsContent === content) return;
      renderedDetailsContent = content;
      detailsContainer.textContent = '';

      if (!currentPlan || !currentPlan.steps || currentPlan.steps.length === 0) {
        const p = document.createElement('p');
        p.className = 'empty-message';
        setText(p, 'No step details available.');
        detailsContainer.appendChild(p);
        return;
      }

      if (!step) {
        const p = document.createElement('p');
        p.className = 'empty-message';
        setText(p, 'Select a node in the dependency map or retired list to view checks and evidence.');
        detailsContainer.appendChild(p);
        return;
      }

      const sConf = STATUS_CONFIG[step.status] || STATUS_CONFIG.pending;

      const titleSec = document.createElement('div');
      titleSec.className = 'detail-section';
      const titleHeader = document.createElement('div');
      titleHeader.className = 'detail-title-header';

      const idTag = document.createElement('code');
      idTag.className = 'task-id-code';
      setText(idTag, step.id);

      const statusBadge = document.createElement('span');
      statusBadge.className = `badge badge--${sConf.class}`;
      renderStatusBadge(statusBadge, step.status);

      titleHeader.appendChild(idTag);
      titleHeader.appendChild(statusBadge);

      const h3 = document.createElement('h3');
      h3.style.fontSize = '15px';
      h3.style.fontWeight = '600';
      h3.style.marginTop = '6px';
      setText(h3, step.title);

      titleSec.appendChild(titleHeader);
      titleSec.appendChild(h3);
      detailsContainer.appendChild(titleSec);

      // Retired Reason if retired
      if (step.status === 'retired' && step.retiredReason) {
        const retSec = document.createElement('div');
        retSec.className = 'detail-section';
        const retLabel = document.createElement('span');
        retLabel.className = 'detail-label';
        setText(retLabel, 'Retirement Reason');
        retSec.appendChild(retLabel);

        const retText = document.createElement('div');
        retText.className = 'detail-value-text';
        setText(retText, step.retiredReason);
        retSec.appendChild(retText);
        detailsContainer.appendChild(retSec);
      }

      // Summary
      if (step.summary) {
        const sumSec = document.createElement('div');
        sumSec.className = 'detail-section';
        const sumLabel = document.createElement('span');
        sumLabel.className = 'detail-label';
        setText(sumLabel, 'Summary');
        sumSec.appendChild(sumLabel);

        const sumText = document.createElement('div');
        sumText.className = 'detail-value-text';
        setText(sumText, step.summary);
        sumSec.appendChild(sumText);
        detailsContainer.appendChild(sumSec);
      }

      // Details
      if (step.details) {
        const detSec = document.createElement('div');
        detSec.className = 'detail-section';
        const detLabel = document.createElement('span');
        detLabel.className = 'detail-label';
        setText(detLabel, 'Details');
        detSec.appendChild(detLabel);

        const detText = document.createElement('div');
        detText.className = 'detail-value-text';
        setText(detText, step.details);
        detSec.appendChild(detText);
        detailsContainer.appendChild(detSec);
      }

      // Prerequisites
      const depSec = document.createElement('div');
      depSec.className = 'detail-section';
      const depLabel = document.createElement('span');
      depLabel.className = 'detail-label';
      setText(depLabel, 'Prerequisites');
      depSec.appendChild(depLabel);

      if (step.dependsOn && step.dependsOn.length > 0) {
        const depList = document.createElement('div');
        depList.style.display = 'flex';
        depList.style.flexWrap = 'wrap';
        depList.style.gap = '6px';

        step.dependsOn.forEach(depId => {
          const depStep = currentPlan.steps.find(s => s.id === depId);
          const depConf = depStep ? (STATUS_CONFIG[depStep.status] || STATUS_CONFIG.pending) : STATUS_CONFIG.pending;

          const depTag = document.createElement('button');
          depTag.type = 'button';
          depTag.className = 'dep-tag';
          depTag.setAttribute('aria-label', `Jump to prerequisite step ${depId} (${depConf.label})`);

          const depStatusBadge = document.createElement('span');
          depStatusBadge.className = `dep-status-badge badge badge--${depConf.class}`;
          renderStatusBadge(depStatusBadge, depStep ? depStep.status : 'pending');

          const depIdSpan = document.createElement('span');
          depIdSpan.className = 'dep-tag-id';
          setText(depIdSpan, depId);

          depTag.appendChild(depStatusBadge);
          depTag.appendChild(depIdSpan);

          depTag.addEventListener('click', () => {
            selectStep(depId);
            const targetNode = nodeElementsMap.get(depId);
            if (targetNode && typeof targetNode.focus === 'function') targetNode.focus();
          });
          depList.appendChild(depTag);
        });
        depSec.appendChild(depList);
      } else {
        const noDeps = document.createElement('span');
        noDeps.style.color = 'var(--ink-muted)';
        noDeps.style.fontSize = '12px';
        setText(noDeps, 'None (root step)');
        depSec.appendChild(noDeps);
      }
      detailsContainer.appendChild(depSec);

      // Check
      const checkSec = document.createElement('div');
      checkSec.className = 'detail-section';
      const checkLabel = document.createElement('span');
      checkLabel.className = 'detail-label';
      setText(checkLabel, 'Verification Check');
      checkSec.appendChild(checkLabel);

      const checkText = document.createElement('div');
      checkText.className = 'detail-value-text';
      setText(checkText, step.check || 'None specified.');
      checkSec.appendChild(checkText);
      detailsContainer.appendChild(checkSec);

      // Evidence
      const evSec = document.createElement('div');
      evSec.className = 'detail-section';
      const evLabel = document.createElement('span');
      evLabel.className = 'detail-label';
      setText(evLabel, 'Evidence');
      evSec.appendChild(evLabel);

      if (step.evidence && step.evidence.length > 0) {
        const evList = document.createElement('ul');
        evList.className = 'evidence-list';

        step.evidence.forEach(ev => {
          const li = document.createElement('li');
          li.className = 'evidence-item';

          const marker = document.createElement('span');
          marker.className = 'evidence-marker';
          setText(marker, '•');

          const text = document.createElement('span');
          setText(text, ev);

          li.appendChild(marker);
          li.appendChild(text);
          evList.appendChild(li);
        });
        evSec.appendChild(evList);
      } else {
        const noEv = document.createElement('span');
        noEv.style.color = 'var(--ink-muted)';
        noEv.style.fontSize = '12px';
        setText(noEv, step.status === 'complete' ? 'No evidence recorded.' : 'Awaiting step verification.');
        evSec.appendChild(noEv);
      }
      detailsContainer.appendChild(evSec);
    }

    function renderHistory(plan) {
      const history = plan.history || [];
      const content = JSON.stringify(history.map(entry => [
        entry.revision, entry.timestamp, entry.summary || 'Plan updated.',
        entry.authorizationNote || '', entry.recoveryNote || ''
      ]));
      if (renderedHistoryContent === content) return;
      renderedHistoryContent = content;
      historyList.textContent = '';

      if (history.length === 0) {
        historyEmpty.hidden = false;
        return;
      }

      historyEmpty.hidden = true;
      const reversedHistory = [...history].reverse();

      reversedHistory.forEach(entry => {
        const li = document.createElement('li');
        li.className = 'history-item';

        const meta = document.createElement('div');
        meta.className = 'history-meta';

        const rev = document.createElement('span');
        rev.className = 'history-rev';
        setText(rev, `Rev #${entry.revision}`);

        const time = document.createElement('time');
        time.className = 'history-time tabular';
        setText(time, formatTimeAgo(entry.timestamp));
        time.setAttribute('title', entry.timestamp || '');

        meta.appendChild(rev);
        meta.appendChild(time);

        const summary = document.createElement('div');
        summary.className = 'history-summary';
        setText(summary, entry.summary || 'Plan updated.');

        li.appendChild(meta);
        li.appendChild(summary);

        if (entry.authorizationNote) {
          const authNoteEl = document.createElement('div');
          authNoteEl.className = 'history-note history-note--auth';
          setText(authNoteEl, `Auth: ${entry.authorizationNote}`);
          li.appendChild(authNoteEl);
        }

        if (entry.recoveryNote) {
          const recNoteEl = document.createElement('div');
          recNoteEl.className = 'history-note history-note--rec';
          setText(recNoteEl, `Recovery: ${entry.recoveryNote}`);
          li.appendChild(recNoteEl);
        }

        historyList.appendChild(li);
      });
    }

    function onIncomingPlanEvent(rawText) {
      const decision = evaluatePlanUpdate(rawText, {
        currentPlan,
        lastRawText,
        consistencyWarning,
        availabilityWarning
      });

      if (decision.clearAvailability) {
        availabilityWarning = false;
      }
      if (decision.availabilityError) {
        availabilityWarning = true;
      }
      if (decision.clearConsistency) {
        consistencyWarning = false;
      }
      if (decision.consistencyWarning) {
        consistencyWarning = true;
      }

      updateBanner();

      if (!decision.accept) {
        return;
      }

      renderAcceptedPlan(decision.plan, rawText);
    }

    function renderAcceptedPlan(plan, rawText, publicationIdentity) {
      const identityChanged = currentPlan && (currentPlan.taskId !== plan.taskId ||
        (currentPlan.schemaVersion === 2 && plan.generation !== currentPlan.generation) ||
        (publicationIdentity !== undefined && publicationIdentity !== renderedPublicationIdentity));
      if (identityChanged) {
        selectedStepId = null;
        nodeElementsMap.forEach(node => node.remove());
        nodeElementsMap.clear();
        renderedGraphContent = renderedDetailsContent = renderedHistoryContent = renderedRetiredContent = null;
      }
      if (publicationIdentity !== undefined) renderedPublicationIdentity = publicationIdentity;
      currentPlan = plan;
      lastRawText = rawText;

      renderHeader(currentPlan);

      // Preserve selection only when meaningful for new state
      const steps = currentPlan.steps || [];
      const stepIds = new Set(steps.map(s => s.id));

      if (!selectedStepId || !stepIds.has(selectedStepId)) {
        const inProg = steps.find(s => s.status === 'in-progress');
        if (inProg) {
          selectedStepId = inProg.id;
        } else if (currentPlan.nextStepId && stepIds.has(currentPlan.nextStepId)) {
          selectedStepId = currentPlan.nextStepId;
        } else if (steps.length > 0) {
          const firstIncomplete = steps.find(s => s.status !== 'complete' && s.status !== 'retired');
          selectedStepId = firstIncomplete ? firstIncomplete.id : steps[0].id;
        } else {
          selectedStepId = null;
        }
      }

      const graphContent = JSON.stringify([
        currentPlan.nextStepId,
        steps.map(step => step.status === 'retired'
          ? [step.id, step.title, step.status, step.retiredReason || '']
          : [step.id, step.title, step.status, step.dependsOn || []])
      ]);
      if (renderedGraphContent !== graphContent) {
        renderGraph(currentPlan);
        renderedGraphContent = graphContent;
      }
      renderDetails();
      renderHistory(currentPlan);
    }

    function textElement(tag, className, text) {
      const node = document.createElement(tag);
      node.className = className;
      setText(node, text);
      return node;
    }

    function formatCardTime(isoString) {
      if (!isoString) return '';
      const d = new Date(isoString);
      if (isNaN(d.getTime())) return String(isoString);
      const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
      const day = String(d.getUTCDate()).padStart(2, '0');
      const mon = months[d.getUTCMonth()];
      const yr = d.getUTCFullYear();
      const hr = String(d.getUTCHours()).padStart(2, '0');
      const min = String(d.getUTCMinutes()).padStart(2, '0');
      const rel = formatTimeAgo(isoString);
      return rel && rel !== '—' ? `${day} ${mon} ${yr}, ${hr}:${min} UTC · ${rel}` : `${day} ${mon} ${yr}, ${hr}:${min} UTC`;
    }

    const CAVEMAN_STOP = new Set([
      'the', 'a', 'an', 'into', 'with', 'for', 'from', 'by', 'to', 'and', 'of', 'in',
      'on', 'at', 'is', 'are', 'be', 'been', 'this', 'that', 'its', 'their', 'current',
      'future', 'optional', 'authorized', 'remaining', 'work', 'system', 'initial'
    ]);

    function cavemanUltra(text, maxWords = 7) {
      if (!text || typeof text !== 'string') return '';
      let s = text.replace(/\s*\([^)]*\)/g, '').trim().replace(/\.+$/, '');
      s = s
        .replace(/\bwithout\s+(\w+)ing\b/gi, 'no-$1')
        .replace(/\bwithout\b/gi, 'no')
        .replace(/\barchitecture\b/gi, 'arch')
        .replace(/\bconfiguration\b/gi, 'config')
        .replace(/\bcomputer vision\b/gi, 'CV')
        .replace(/\bcalibration\b/gi, 'calib')
        .replace(/\bcontinuation\b/gi, 'cont')
        .replace(/\bpersistence\b/gi, 'persist')
        .replace(/\bacceptance\b/gi, 'accept')
        .replace(/\bsynchronization\b/gi, 'sync')
        .replace(/\binspection\b/gi, 'inspect')
        .replace(/\bcontainer-specific\b/gi, 'container')
        .replace(/\bzero-downtime\b/gi, '0-downtime');
      const rawWords = s.split(/\s+/).map(w => w.replace(/^[,;:]+|[,;:]+$/g, '')).filter(Boolean);
      const kept = [];
      const seen = new Set();
      for (const w of rawWords) {
        const lw = w.toLowerCase();
        if (CAVEMAN_STOP.has(lw)) continue;
        if (seen.has(lw) && lw.length > 3) continue;
        seen.add(lw);
        kept.push(w);
      }
      const list = kept.length ? kept : rawWords;
      const out = list.slice(0, maxWords).join(' ');
      return list.length > maxWords ? out + '…' : out;
    }

    function shortPlanId(planId) {
      if (!planId || typeof planId !== 'string') return '';
      return planId.length > 16 ? `${planId.slice(0, 8)}…${planId.slice(-4)}` : planId;
    }

    function renderServiceIndex(views) {
      const list = document.getElementById('plan-list');
      list.textContent = '';
      for (const view of views) {
        const card = textElement('article', 'plan-card', '');
        card.dataset.planId = view.plan_id;
        card.dataset.status = view.status;
        card.setAttribute('data-status', view.status);
        card.setAttribute('data-full-objective', view.draft.objective);
        const heading = textElement('h2', '', '');
        const link = textElement('a', '', view.draft.objective);
        link.setAttribute('href', '/plans/' + encodeURIComponent(view.plan_id));
        link.setAttribute('data-caveman', cavemanUltra(view.draft.objective));
        heading.appendChild(link);
        card.appendChild(heading);
        const meta = textElement('p', 'plan-card-meta', `${view.status} · Rev #${view.metadata.revision}`);
        meta.dataset.status = view.status;
        meta.setAttribute('data-status', view.status);
        card.appendChild(meta);
        const published = textElement('p', 'plan-card-published', 'Published: ');
        const time = textElement('time', '', view.published_at);
        time.setAttribute('datetime', view.published_at);
        time.setAttribute('data-pretty', formatCardTime(view.published_at));
        published.appendChild(time);
        card.appendChild(published);
        const code = textElement('code', 'task-id-code', view.plan_id);
        code.setAttribute('title', view.plan_id);
        code.setAttribute('data-short-id', shortPlanId(view.plan_id));
        card.appendChild(code);
        if (view.draft.workflow !== null && view.draft.workflow !== undefined) {
          card.appendChild(textElement('p', 'workflow-info', 'Workflow: ' +
            Object.entries(view.draft.workflow).map(([key, value]) => `${key}: ${value}`).join('; ')));
        }
        list.appendChild(card);
      }
      if (!views.length) list.appendChild(textElement('p', 'empty-message', 'No committed plans.'));
    }

    // Compact pages remain separate from the backward-compatible full-plan index.
    const compactIndexUrl = !bodyData.planId && typeof bodyData.plansUrl === 'string' &&
      bodyData.plansUrl.split(/[?#]/)[0] === '/api/plan-index' ? bodyData.plansUrl : null;
    const compactIndexQuery = compactIndexUrl === null ? null :
      new URLSearchParams((compactIndexUrl.split('?')[1] || '').split('#')[0]);
    const compactIndexLimit = compactIndexQuery && compactIndexQuery.has('limit') ? compactIndexQuery.get('limit') : '25';
    const compactIndexLimitQuery = 'limit=' + encodeURIComponent(compactIndexLimit);
    const compactIndexFirstHref = '/?' + compactIndexLimitQuery;
    const compactIndexContinuation = compactIndexQuery && (compactIndexQuery.has('after') || compactIndexQuery.has('before'));
    let compactIndexNav = null;
    let compactIndexNotice = null;
    const compactIndexAnchors = new Map();

    function compactIndexUnavailable() {
      const error = new Error('Invalid or unavailable public plan index');
      error.code = 'unavailable';
      return error;
    }

    function prepareCompactIndex(value) {
      const limit = Number(compactIndexLimit);
      const object = item => item !== null && typeof item === 'object' && !Array.isArray(item);
      const keys = (item, expected) => object(item) && Object.keys(item).length === expected.length &&
        expected.every(key => Object.prototype.hasOwnProperty.call(item, key));
      const nonempty = item => typeof item === 'string' && item.trim().length > 0;
      const cursor = item => item === null || (typeof item === 'string' && item.length > 0 && item.length <= 1024);
      if (!Number.isInteger(limit) || limit < 1 || limit > 100 || String(limit) !== compactIndexLimit ||
          !keys(value, ['plans', 'next_cursor', 'previous_cursor', 'epoch', 'collection_fence', 'store_sequence']) ||
          !Array.isArray(value.plans) || value.plans.length > limit || !nonempty(value.epoch) ||
          !cursor(value.next_cursor) || !cursor(value.previous_cursor) ||
          !keys(value.collection_fence, ['count', 'max_rowid']) ||
          !Number.isInteger(value.collection_fence.count) || value.collection_fence.count < 0 ||
          !Number.isInteger(value.collection_fence.max_rowid) || value.collection_fence.max_rowid < 0 ||
          !Number.isSafeInteger(value.store_sequence) || value.store_sequence < 0) throw compactIndexUnavailable();
      const seen = new Set();
      for (const plan of value.plans) {
        if (!keys(plan, ['plan_id', 'objective', 'status', 'revision', 'published_at', 'dashboard_url']) ||
            !nonempty(plan.plan_id) || typeof plan.objective !== 'string' ||
            !['planning', 'running', 'blocked', 'stopped', 'complete'].includes(plan.status) ||
            !Number.isSafeInteger(plan.revision) || plan.revision < 1 ||
            awareMicros(plan.published_at) === null || !nonempty(plan.dashboard_url) || seen.has(plan.plan_id)) {
          throw compactIndexUnavailable();
        }
        seen.add(plan.plan_id);
      }
      // Prepare every relative href before any card, navigation or accepted content changes.
      return {
        plans: value.plans.map(plan => ({summary: plan, href: '/plans/' + encodeURIComponent(plan.plan_id)})),
        navigation: [
          ['first', 'First', compactIndexContinuation || value.previous_cursor !== null ? compactIndexFirstHref : null],
          ['newer', 'Newer', value.previous_cursor === null ? null :
            '/?before=' + encodeURIComponent(value.previous_cursor) + '&' + compactIndexLimitQuery],
          ['older', 'Older', value.next_cursor === null ? null :
            '/?after=' + encodeURIComponent(value.next_cursor) + '&' + compactIndexLimitQuery]
        ],
        emptyText: value.collection_fence.count === 0 ? 'No committed plans.' : 'No plans on this page.'
      };
    }

    async function readCompactIndex() {
      const response = await fetch(compactIndexUrl, {cache: 'no-store', credentials: 'same-origin'});
      if (response.status === 409) {
        const error = new Error('The plan list changed');
        error.code = 'plan_index_conflict';
        throw error;
      }
      if (!response.ok) throw compactIndexUnavailable();
      return prepareCompactIndex(await response.json());
    }

    function compactIndexNavigation() {
      if (!compactIndexNav) {
        compactIndexNav = document.getElementById('plan-index-nav');
        if (!compactIndexNav) {
          compactIndexNav = textElement('nav', 'plan-index-nav', '');
          compactIndexNav.id = 'plan-index-nav';
          compactIndexNav.setAttribute('id', 'plan-index-nav');
          compactIndexNav.setAttribute('aria-label', 'Plan pages');
          const list = document.getElementById('plan-list');
          (list.parentNode || document.body).appendChild(compactIndexNav);
        }
      }
      return compactIndexNav;
    }

    function compactIndexAnchor(nav, key, label, href) {
      const id = 'plan-index-' + key;
      let anchor = compactIndexAnchors.get(key) || Array.from(nav.querySelectorAll('a')).find(node =>
        (node.id || node.getAttribute('id')) === id);
      if (!anchor) {
        anchor = textElement('a', '', label);
        anchor.id = id;
        anchor.setAttribute('id', id);
      }
      compactIndexAnchors.set(key, anchor);
      setText(anchor, label);
      if (anchor.getAttribute('href') !== href) anchor.setAttribute('href', href);
      return anchor;
    }

    function compactIndexCard(card, plan, href) {
      let heading = card && card.querySelector('h2');
      let link = heading && heading.querySelector('a');
      let meta = card && card.querySelector('.plan-card-meta');
      let time = card && card.querySelector('time');
      let code = card && card.querySelector('.task-id-code');
      let published = card && Array.from(card.children).find(node =>
        node.tagName.toLowerCase() === 'p' && node.querySelector('time') === time);
      if (!link || !meta || !time || !code || !published || published.textContent !== 'Published: ' + time.textContent) {
        card = textElement('article', 'plan-card', '');
        card.dataset.planId = plan.plan_id;
        card.setAttribute('data-plan-id', plan.plan_id);
        heading = textElement('h2', '', '');
        link = textElement('a', '', '');
        heading.appendChild(link);
        meta = textElement('p', 'plan-card-meta', '');
        published = textElement('p', 'plan-card-published', 'Published: ');
        time = textElement('time', '', '');
        published.appendChild(time);
        code = textElement('code', 'task-id-code', '');
        card.appendChild(heading);
        card.appendChild(meta);
        card.appendChild(published);
        card.appendChild(code);
      }
      // Equality checks preserve matching SSR descendants, including their text nodes.
      setText(link, plan.objective);
      if (link.getAttribute('href') !== href) link.setAttribute('href', href);
      const caveman = cavemanUltra(plan.objective);
      if (link.getAttribute('data-caveman') !== caveman) link.setAttribute('data-caveman', caveman);
      if (card.getAttribute('data-full-objective') !== plan.objective) card.setAttribute('data-full-objective', plan.objective);
      setText(meta, `${plan.status} · Rev #${plan.revision}`);
      setText(time, plan.published_at);
      if (time.getAttribute('datetime') !== plan.published_at) time.setAttribute('datetime', plan.published_at);
      setText(code, plan.plan_id);
      card.dataset.status = plan.status;
      if (card.getAttribute('data-status') !== plan.status) card.setAttribute('data-status', plan.status);
      meta.dataset.status = plan.status;
      if (meta.getAttribute('data-status') !== plan.status) meta.setAttribute('data-status', plan.status);
      published.className = 'plan-card-published';
      const pretty = formatCardTime(plan.published_at);
      if (time.getAttribute('data-pretty') !== pretty) time.setAttribute('data-pretty', pretty);
      const shortId = shortPlanId(plan.plan_id);
      if (code.getAttribute('data-short-id') !== shortId) code.setAttribute('data-short-id', shortId);
      if (code.getAttribute('title') !== plan.plan_id) code.setAttribute('title', plan.plan_id);
      return card;
    }

    function renderCompactIndex(page) {
      const list = document.getElementById('plan-list');
      const cards = new Map();
      for (const card of Array.from(list.children)) {
        if (!card.classList.contains('plan-card')) continue;
        const id = card.getAttribute('data-plan-id') || card.dataset.planId;
        if (!cards.has(id)) cards.set(id, card);
      }
      const nodes = page.plans.map(item => compactIndexCard(cards.get(item.summary.plan_id), item.summary, item.href));
      if (!nodes.length) {
        const empty = list.querySelector('.empty-message') || textElement('p', 'empty-message', '');
        setText(empty, page.emptyText);
        nodes.push(empty);
      }
      syncServiceChildren(list, nodes);
      const nav = compactIndexNavigation();
      const anchors = page.navigation.filter(item => item[2] !== null).map(item => compactIndexAnchor(nav, ...item));
      syncServiceChildren(nav, anchors);
    }

    function setCompactIndexNotice(message, restart) {
      if (compactIndexNotice) {
        compactIndexNotice.remove();
        compactIndexNotice = null;
      }
      if (message === null) return;
      compactIndexNotice = textElement('p', 'index-notice', '');
      compactIndexNotice.setAttribute('role', 'alert');
      compactIndexNotice.appendChild(textElement('span', '', message));
      if (restart) {
        const first = textElement('a', '', 'First');
        first.setAttribute('href', compactIndexFirstHref);
        compactIndexNotice.appendChild(textElement('span', '', ' '));
        compactIndexNotice.appendChild(first);
      }
      const list = document.getElementById('plan-list');
      (list.parentNode || document.body).appendChild(compactIndexNotice);
    }

    function coordinationLabel(item) {
      const value = item.value;
      if (item.kind === 'attempt') return `Step ${value.step_id} · ${value.state} · ${value.assignee} · ${value.attempt_id}`;
      if (item.kind === 'reservation') return `Reservation ${value.use_id} · ${value.writer_active ? 'writer active' : 'writer inactive'} · ${value.unresolved ? 'unresolved' : 'resolved'}`;
      if (item.kind === 'hold') return `Hold ${value.hold_id} · ${value.kind} · ${value.step_id || 'whole plan'}`;
      if (item.kind === 'review') return `Review ${value.step_id} · ${value.review.verdict} · ${value.review.reviewer}`;
      return `Invalidation ${value.invalidation_id} · producer ${value.producer_step_id}`;
    }

    function coordinationIdentity(item) {
      const value = item.value;
      if (item.kind === 'review') {
        return [value.plan_id, value.step_id, value.attempt_id, value.definition_version, value.recorded_sequence];
      }
      return value[{attempt: 'attempt_id', reservation: 'use_id', hold: 'hold_id', invalidation: 'invalidation_id'}[item.kind]];
    }

    function serviceDetailsByIdentity(container) {
      const records = new Map();
      for (const details of container.querySelectorAll('details')) {
        records.set(details.dataset.recordIdentity, details);
      }
      return records;
    }

    function serviceRecordDetails(records, identity, className, summary, content) {
      const key = JSON.stringify(identity);
      // Reuse only this durable record's node; its current open state belongs to the user.
      let details = records.get(key);
      if (!details) {
        details = textElement('details', className, '');
        details.dataset.recordIdentity = key;
        details.appendChild(textElement('summary', '', summary));
        details.appendChild(textElement('pre', '', content));
        records.set(key, details);
      } else {
        setText(details.querySelector('summary'), summary);
        setText(details.querySelector('pre'), content);
      }
      return details;
    }

    function syncServiceChildren(container, nodes) {
      const desired = [...new Set(nodes)];
      const retained = new Set(desired);
      for (const child of Array.from(container.childNodes)) {
        if (!retained.has(child)) container.removeChild(child);
      }
      // Keep the longest desired prefix already in DOM order; append only the remainder.
      let prefix = 0;
      for (const child of Array.from(container.children)) {
        if (child === desired[prefix]) prefix++;
      }
      for (let index = prefix; index < desired.length; index++) {
        container.appendChild(desired[index]);
      }
    }

    function renderServiceState(view, items, events, sequence) {
      document.getElementById('service-section').hidden = false;
      document.getElementById('plan-index-link').hidden = false;
      const published = document.getElementById('published-state');
      published.hidden = false;
      published.dataset.revision = String(view.metadata.revision);
      published.dataset.storeSequence = String(view.coordination.store_sequence);
      published.dataset.eventSequence = String(sequence);
      setText(published, `Last published state: Rev #${view.metadata.revision} · ${view.published_at}`);
      const coord = view.coordination;
      setText(document.getElementById('coordination-summary'),
        `Store sequence ${coord.store_sequence} · owner ${coord.ownership_actor} (version ${coord.ownership_version}). ` +
        Object.entries(coord.total_counts).map(([kind, count]) => `${kind}: ${count}`).join(' · ') +
        ` · Accepted result versions: ${JSON.stringify(coord.result_versions)}`);
      const identity = [view.metadata.epoch, view.plan_id, view.metadata.generation];
      const list = document.getElementById('coordination-list');
      const coordinationDetails = serviceDetailsByIdentity(list);
      const coordinationNodes = [];
      for (const item of items) {
        const details = serviceRecordDetails(coordinationDetails, [...identity, item.kind, coordinationIdentity(item)],
          'coordination-item coordination-item--' + item.kind, coordinationLabel(item), JSON.stringify(item.value, null, 2));
        coordinationNodes.push(details);
      }
      if (!items.length) {
        coordinationNodes.push(list.querySelector('.empty-message') ||
          textElement('p', 'empty-message', 'No coordination records.'));
      }
      syncServiceChildren(list, coordinationNodes);
      const recovery = [coord.store_hold ? 'Store hold active.' : 'No store hold.'];
      if (coord.restore_cutoff) recovery.push(`Restored backup cutoff: ${coord.restore_cutoff}.`);
      if (items.some(item => item.kind === 'attempt' && (item.value.state === 'stale' ||
          (['assigned', 'active', 'submitted'].includes(item.value.state) && item.value.recovery_required)))) {
        recovery.push('Stale attempts require recovery.');
      }
      if (coord.has_unresolved_uses) recovery.push('Outstanding reservations require host work reconciliation.');
      if (coord.has_plan_holds) recovery.push('Plan holds require explicit resolution.');
      setText(document.getElementById('recovery-state'), recovery.join(' '));
      setText(document.getElementById('events-summary'),
        `${events.length} committed events · last plan event sequence ${sequence}. All pages consumed for this published state.`);
      const eventList = document.getElementById('service-events');
      const eventDetails = serviceDetailsByIdentity(eventList);
      const eventRows = new Map();
      for (const row of eventList.children) {
        const details = row.querySelector('details');
        if (details) eventRows.set(details.dataset.recordIdentity, row);
      }
      const eventNodes = [];
      for (const event of events) {
        const details = serviceRecordDetails(eventDetails, [...identity, event.sequence], '',
          `#${event.sequence} ${event.kind} · ${event.published_at} · actor ${event.actor}` +
          (event.origin_worker === null ? '' : ` · origin ${event.origin_worker}`) +
          (event.relay === null ? '' : ` · relay ${event.relay}`),
          JSON.stringify(event.payload, null, 2));
        let li = eventRows.get(details.dataset.recordIdentity);
        if (!li) {
          li = textElement('li', 'event-item', '');
          li.appendChild(details);
          eventRows.set(details.dataset.recordIdentity, li);
        }
        eventNodes.push(li);
      }
      syncServiceChildren(eventList, eventNodes);
    }

    async function fetchPublic(url) {
      const response = await fetch(url, {cache: 'no-store', credentials: 'same-origin'});
      const value = await response.json();
      if (!response.ok) {
        const error = new Error('Public read unavailable');
        error.code = value && value.error && value.error.code;
        throw error;
      }
      return value;
    }

    function cursorConflict() {
      const error = new Error('Committed publication changed during paging');
      error.code = 'conflict';
      return error;
    }

    function awareMicros(value) {
      if (typeof value !== 'string') return null;
      const match = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$/.exec(value);
      if (!match) return null;
      const [year, month, day, hour, minute, second] = match.slice(1, 7).map(Number);
      const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
      const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
      const zone = match[8];
      if (year < 1 || month < 1 || month > 12 || day < 1 || day > days[month - 1] ||
          hour > 23 || minute > 59 || second > 59 ||
          (zone !== 'Z' && (Number(zone.slice(1, 3)) > 23 || Number(zone.slice(4, 6)) > 59))) return null;
      const millis = Date.parse(`${match[1]}-${match[2]}-${match[3]}T${match[4]}:${match[5]}:${match[6]}${zone}`);
      if (!Number.isSafeInteger(millis)) return null;
      return BigInt(millis) * 1000n + BigInt((match[7] || '').padEnd(6, '0'));
    }

    function renderServiceWarnings(value) {
      const region = document.getElementById('recovery-warnings');
      if (!region) return;
      region.dataset.status = value === null ? 'unavailable' : 'current';
      region.dataset.observedAt = value === null ? '' : value.observed_at;
      if (value === null) {
        setText(region, 'Recorded inactivity warnings unavailable.');
        return;
      }
      const overdue = value.overdue;
      const unable = value.unassessable_activity;
      const text = overdue.length ? `${overdue.length} overdue recorded inactivity observations: ` +
        overdue.map(item => `${item.attempt_id} (${item.state})`).join(', ') + '.' : 'No overdue recorded inactivity.';
      setText(region, `${text} Observed at ${value.observed_at}; policy ${value.threshold_seconds} seconds.` +
        (unable.length ? ` Unable to assess recorded activity: ${unable.map(item => item.attempt_id).join(', ')}.` : '') +
        ' These observations require an owner decision and do not establish that host work failed or stopped.');
    }

    function validateServiceWarnings(value, view, items) {
      const object = item => item !== null && typeof item === 'object' && !Array.isArray(item);
      const keys = (item, expected) => object(item) && Object.keys(item).length === expected.length &&
        expected.every(key => Object.prototype.hasOwnProperty.call(item, key));
      const metadataKeys = ['epoch', 'plan_id', 'generation', 'revision', 'ownership_version', 'dashboard_url'];
      if (!keys(value, ['metadata', 'store_sequence', 'observed_at', 'threshold_seconds', 'overdue', 'unassessable_activity']) ||
          !keys(value.metadata, metadataKeys) || !Number.isSafeInteger(value.store_sequence) || value.store_sequence < 0 ||
          !Number.isSafeInteger(value.threshold_seconds) || value.threshold_seconds < 1 ||
          !Number.isSafeInteger(value.metadata.revision) || value.metadata.revision < 1 ||
          !Number.isSafeInteger(value.metadata.ownership_version) || value.metadata.ownership_version < 1 ||
          !['epoch', 'plan_id', 'generation', 'dashboard_url'].every(key => typeof value.metadata[key] === 'string' && value.metadata[key]) ||
          !Array.isArray(value.overdue) || !Array.isArray(value.unassessable_activity)) throw new Error('Invalid recovery advisory');
      const observed = awareMicros(value.observed_at);
      if (observed === null) throw new Error('Invalid recovery observation time');
      if (metadataKeys.some(key => value.metadata[key] !== view.metadata[key]) ||
          value.store_sequence !== view.coordination.store_sequence) throw cursorConflict();
      const attempts = new Map(), expected = {overdue: new Set(), unassessable_activity: new Set()};
      for (const item of items.filter(item => item.kind === 'attempt')) {
        const current = item.value;
        if (typeof current.attempt_id !== 'string' || !current.attempt_id || attempts.has(current.attempt_id) ||
            !Number.isSafeInteger(current.version) || current.version < 1) throw new Error('Invalid current attempt');
        attempts.set(current.attempt_id, current);
        if (!['assigned', 'active', 'submitted'].includes(current.state)) continue;
        const activity = awareMicros(current.last_activity);
        if (activity === null || activity > observed) expected.unassessable_activity.add(current.attempt_id);
        else if (observed - activity >= BigInt(value.threshold_seconds) * 1000000n) expected.overdue.add(current.attempt_id);
      }
      const seen = new Set();
      // The canonical DTO can display started_at for missing activity. Exact service-observed
      // unassessable membership is authoritative; that display fallback never proves freshness.
      for (const category of ['unassessable_activity', 'overdue']) {
        for (const item of value[category]) {
          if (!keys(item, ['attempt_id', 'version', 'state', 'last_activity']) ||
              typeof item.attempt_id !== 'string' || !item.attempt_id ||
              !Number.isSafeInteger(item.version) || item.version < 1 ||
              !['assigned', 'active', 'submitted'].includes(item.state) ||
              !(item.last_activity === null || typeof item.last_activity === 'string')) throw new Error('Invalid recovery attempt');
          const current = attempts.get(item.attempt_id);
          if (seen.has(item.attempt_id) || !current || item.version !== current.version || item.state !== current.state ||
              item.last_activity !== current.last_activity) throw cursorConflict();
          if (category === 'unassessable_activity') expected.overdue.delete(item.attempt_id);
          else if (!expected.overdue.has(item.attempt_id)) throw cursorConflict();
          seen.add(item.attempt_id);
        }
        if (category === 'unassessable_activity') {
          if ([...expected.unassessable_activity].some(identity => !seen.has(identity))) throw cursorConflict();
        } else if (value.overdue.length !== expected.overdue.size) throw cursorConflict();
      }
      return value;
    }

    async function readServiceWarnings(view, items) {
      if (!bodyData.recoveryWarningsUrl) return null;
      try {
        return validateServiceWarnings(await fetchPublic(bodyData.recoveryWarningsUrl), view, items);
      } catch (error) {
        if (error.code === 'conflict') throw error;
        return null;
      }
    }

    let polling = false;
    let serviceNeedsFullRead = true;
    let serviceEvents = [];
    let serviceSequence = 0;
    let serviceIdentity = null;
    let serviceWarningPublication = null;

    async function readServicePublication() {
      const view = await fetchPublic(bodyData.planUrl);
      const plan = serviceSnapshot(view);
      if (view.plan_id !== bodyData.planId) throw new Error('Unexpected public plan identity');
      const identity = JSON.stringify([view.metadata.epoch, view.plan_id, view.metadata.generation]);
      const warningPublication = JSON.stringify([view.metadata, view.coordination.store_sequence]);
      if (warningPublication !== serviceWarningPublication) renderServiceWarnings(null);
      const full = serviceNeedsFullRead || identity !== serviceIdentity || view.coordination.store_sequence < serviceSequence;
      const items = [], cursors = new Set();
      let cursor = null;
      do {
        const url = bodyData.coordinationUrl + '?' + (cursor === null ? '' : 'after=' + encodeURIComponent(cursor) + '&') + 'limit=100';
        const page = await fetchPublic(url);
        if (page.plan_id !== view.plan_id || page.revision !== view.metadata.revision ||
            page.store_sequence !== view.coordination.store_sequence) throw cursorConflict();
        if (!Array.isArray(page.items) || page.items.length > 100 ||
            !page.items.every(item => item && ['attempt', 'reservation', 'hold', 'review', 'invalidation'].includes(item.kind) && item.value)) {
          throw new Error('Invalid coordination page');
        }
        items.push(...page.items);
        cursor = page.next_cursor;
        if (cursor !== null && (typeof cursor !== 'string' || !cursor || cursors.has(cursor))) {
          throw new Error('Coordination cursor made no progress');
        }
        if (cursor !== null) cursors.add(cursor);
      } while (cursor !== null);

      const events = full ? [] : serviceEvents.slice();
      let sequence = full ? 0 : serviceSequence;
      let page;
      do {
        page = await fetchPublic(bodyData.eventsUrl + '?after=' + sequence + '&limit=100');
        if (!Array.isArray(page.events) || page.events.length > 100) throw new Error('Invalid event page');
        for (const event of page.events) {
          if (!event || !Number.isSafeInteger(event.sequence) || event.sequence <= sequence || event.plan_id !== view.plan_id) {
            throw new Error('Event page made no progress');
          }
          if (event.sequence > view.coordination.store_sequence) throw cursorConflict();
          events.push(event);
          sequence = event.sequence;
        }
      } while (page.events.length === 100);

      const warnings = await readServiceWarnings(view, items);

      // A final public read fences the independently read pages. A write during
      // the drain causes a restart rather than a mixed view or fabricated freshness.
      const confirmed = await fetchPublic(bodyData.planUrl);
      serviceSnapshot(confirmed);
      if (confirmed.plan_id !== view.plan_id || confirmed.metadata.epoch !== view.metadata.epoch ||
          confirmed.metadata.generation !== view.metadata.generation || confirmed.metadata.revision !== view.metadata.revision ||
          confirmed.metadata.ownership_version !== view.metadata.ownership_version ||
          confirmed.coordination.store_sequence !== view.coordination.store_sequence) throw cursorConflict();
      return {view, plan, items, events, sequence, identity, warnings, warningPublication};
    }

    async function pollService() {
      if (polling) return;
      polling = true;
      try {
        if (compactIndexUrl !== null) {
          try {
            renderCompactIndex(await readCompactIndex());
            availabilityWarning = false;
            consistencyWarning = false;
            setTransportStatus('connected');
            setCompactIndexNotice(null);
          } catch (error) {
            const conflict = error && error.code === 'plan_index_conflict';
            availabilityWarning = !conflict;
            consistencyWarning = false;
            setTransportStatus(conflict ? 'connected' : 'offline');
            if (conflict) {
              const nav = compactIndexNavigation();
              const first = compactIndexAnchor(nav, 'first', 'First', compactIndexFirstHref);
              syncServiceChildren(nav, [first, ...Array.from(nav.children).filter(node => node !== first)]);
            }
            // Apply the index notice after the generic status banner has updated.
            setCompactIndexNotice(conflict
              ? 'The plan list changed since this page was opened. Restart from the first page to continue.'
              : 'Plan list unavailable. Retaining last confirmed page.', conflict);
          }
          return;
        }
        for (let attempt = 0; attempt < 2; attempt++) {
          try {
            if (bodyData.planId) {
              const state = await readServicePublication();
              renderAcceptedPlan(state.plan, JSON.stringify(state.plan), state.identity);
              renderServiceState(state.view, state.items, state.events, state.sequence);
              renderServiceWarnings(state.warnings);
              serviceWarningPublication = state.warningPublication;
              serviceEvents = state.events;
              serviceSequence = state.sequence;
              serviceIdentity = state.identity;
            } else {
              const value = await fetchPublic(bodyData.plansUrl);
              if (!value || !Array.isArray(value.plans)) throw new Error('Invalid public plan index');
              value.plans.forEach(serviceSnapshot);
              renderServiceIndex(value.plans);
            }
            serviceNeedsFullRead = false;
            availabilityWarning = false;
            consistencyWarning = false;
            setTransportStatus('connected');
            break;
          } catch (error) {
            renderServiceWarnings(null);
            serviceNeedsFullRead = true;
            if (error.code === 'conflict' && attempt === 0) continue;
            availabilityWarning = true;
            setTransportStatus('offline');
            break;
          }
        }
      } finally {
        polling = false;
      }
    }

    function initService() {
      setTransportStatus('connecting');
      pollService();
      setInterval(pollService, 1000);
    }

    function initEvents() {
      setTransportStatus('connecting');

      // SSE is the sole source: initialfetch removed
      const eventSource = new EventSource('/events');

      eventSource.onopen = function () {
        // onopen alone does not clear data warnings
        setTransportStatus('connected');
      };

      eventSource.addEventListener('plan', function (event) {
        try {
          onIncomingPlanEvent(event.data);
        } catch (err) {
          console.error('Failed to process incoming plan event:', err);
        }
      });

      eventSource.addEventListener('state-error', function () {
        availabilityWarning = true;
        updateBanner();
      });

      eventSource.onerror = function () {
        if (eventSource.readyState === 2 /* CLOSED */) {
          setTransportStatus('offline');
        } else {
          setTransportStatus('connecting');
        }
      };
    }

    const containerEl = document.getElementById('graph-container');
    if (containerEl && typeof ResizeObserver !== 'undefined') {
      new ResizeObserver(() => { if (currentPlan) renderGraph(currentPlan); }).observe(containerEl);
    }

    if (typeof document.addEventListener === 'function') {
      document.addEventListener('click', function (e) {
        const target = e && e.target;
        if (!target || typeof target.closest !== 'function') return;
        if (target.closest('a')) return;
        const card = target.closest('.plan-card');
        if (card && card.classList) {
          card.classList.toggle('is-expanded');
          return;
        }
        const goal = target.closest('#goal-heading');
        if (goal && goal.classList) {
          goal.classList.toggle('is-expanded');
        }
      });
    }

    const initialize = serviceMode ? initService : initEvents;
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', initialize);
    } else {
      initialize();
    }
  }

  return {
    evaluatePlanUpdate
  };
});
