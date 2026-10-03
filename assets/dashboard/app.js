/**
 * Live Plan Dashboard Application Script
 * Local-only, zero dependencies. Connects exclusively to SSE endpoint /events
 * and renders reactive dependency DAG, verification evidence, retired steps,
 * and revision history.
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

  // If running in browser environment, initialize UI logic
  if (typeof document !== 'undefined') {
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
      container.textContent = '';
      const icon = createSvgIcon(conf.iconType);
      container.appendChild(icon);
      const labelSpan = document.createElement('span');
      setText(labelSpan, customLabel || conf.label);
      container.appendChild(labelSpan);
      return container;
    }

    function setText(el, text) {
      if (el) el.textContent = text != null ? String(text) : '';
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
      setText(goalHeading, plan.objective || 'Untitled Plan');

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
      detailsContainer.textContent = '';

      if (!currentPlan || !currentPlan.steps || currentPlan.steps.length === 0) {
        const p = document.createElement('p');
        p.className = 'empty-message';
        setText(p, 'No step details available.');
        detailsContainer.appendChild(p);
        return;
      }

      const step = currentPlan.steps.find(s => s.id === selectedStepId);
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
      historyList.textContent = '';
      const history = plan.history || [];

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

      // Accepted update
      const identityChanged = currentPlan && (currentPlan.taskId !== decision.plan.taskId ||
        (currentPlan.schemaVersion === 2 && decision.plan.generation !== currentPlan.generation));
      if (identityChanged) selectedStepId = null;
      currentPlan = decision.plan;
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

      renderGraph(currentPlan);
      renderDetails();
      renderHistory(currentPlan);
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

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', initEvents);
    } else {
      initEvents();
    }
  }

  return {
    evaluatePlanUpdate
  };
});
