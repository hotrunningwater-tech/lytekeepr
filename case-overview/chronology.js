/**
 * Chronology UI: the case-overview "Upcoming"/"Recent chronology" blocks,
 * plus the full chronology modal (timeline + review queue in one filtered
 * list). Talks only to /api/chronology/* — extraction logic, dedup, and
 * storage all live server-side in chronology/ and assistant/
 * chronology_extractor.py; this file only renders and dispatches actions.
 */

import { openSpecificEmail } from '../email/index.js';
import { escapeHtml, formatFullDate } from '../email/utils.js';

const CATEGORY_LABELS = {
  court_procedural: 'Court & procedural',
  confirmed_action: 'Confirmed action',
  deadline_request: 'Deadline / request',
  communication: 'Communication',
  case_fact: 'Case fact',
};

const NATURE_LABELS = {
  confirmed_fact: 'Confirmed',
  proposed: 'Proposed',
  alleged: 'Alleged',
  disputed: 'Disputed',
};

let backdropEl, modalEl, bodyEl, extractBtnEl, statusEl;
let filterCategoryEl, filterImportanceEl, filterSourceEl, filterStatusEl;
let initialized = false;

let allEvents = []; // confirmed + review, merged for unified filtering
let reviewCount = 0;

function ensureDom() {
  if (initialized) return;
  initialized = true;

  backdropEl = document.createElement('div');
  backdropEl.className = 'docs-modal-backdrop';
  backdropEl.hidden = true;
  backdropEl.addEventListener('click', closeChronologyModal);

  modalEl = document.createElement('div');
  modalEl.className = 'docs-modal chrono-modal';
  modalEl.hidden = true;
  modalEl.innerHTML = `
    <div class="docs-modal-header">
      <h2>Chronology</h2>
      <div class="docs-modal-header-actions">
        <button class="docs-recheck-btn" id="chronoExtractBtn" type="button">Extract new events</button>
        <button class="docs-modal-close" type="button" aria-label="Close">&times;</button>
      </div>
    </div>
    <div class="chrono-filters">
      <select id="chronoFilterCategory">
        <option value="">All categories</option>
        ${Object.entries(CATEGORY_LABELS).map(([v, l]) => `<option value="${v}">${l}</option>`).join('')}
      </select>
      <select id="chronoFilterImportance">
        <option value="">All importance</option>
        <option value="high">High</option>
        <option value="medium">Medium</option>
        <option value="low">Low</option>
      </select>
      <select id="chronoFilterSource">
        <option value="">All sources</option>
        <option value="email">Email</option>
        <option value="document">Document</option>
      </select>
      <select id="chronoFilterStatus">
        <option value="">All statuses</option>
        <option value="confirmed">Confirmed</option>
        <option value="pending_review">Awaiting review</option>
      </select>
    </div>
    <div class="chrono-status" id="chronoStatus" hidden></div>
    <div class="docs-modal-body chrono-modal-body" id="chronoModalBody"></div>
  `;
  bodyEl = modalEl.querySelector('#chronoModalBody');
  extractBtnEl = modalEl.querySelector('#chronoExtractBtn');
  statusEl = modalEl.querySelector('#chronoStatus');
  filterCategoryEl = modalEl.querySelector('#chronoFilterCategory');
  filterImportanceEl = modalEl.querySelector('#chronoFilterImportance');
  filterSourceEl = modalEl.querySelector('#chronoFilterSource');
  filterStatusEl = modalEl.querySelector('#chronoFilterStatus');

  modalEl.querySelector('.docs-modal-close').addEventListener('click', closeChronologyModal);
  extractBtnEl.addEventListener('click', handleExtract);
  for (const el of [filterCategoryEl, filterImportanceEl, filterSourceEl, filterStatusEl]) {
    el.addEventListener('change', renderList);
  }

  document.body.appendChild(backdropEl);
  document.body.appendChild(modalEl);

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !modalEl.hidden) closeChronologyModal();
  });
}

async function loadAllEvents() {
  try {
    const [eventsRes, queueRes] = await Promise.all([
      fetch('/api/chronology/events'),
      fetch('/api/chronology/review-queue'),
    ]);
    const eventsData = await eventsRes.json();
    const queueData = await queueRes.json();
    allEvents = [...(eventsData.events || []), ...(queueData.queue || [])];
    reviewCount = (queueData.queue || []).length;
  } catch (err) {
    allEvents = [];
    reviewCount = 0;
  }
  return { allEvents, reviewCount };
}

export async function refreshChronologyOverview() {
  await loadAllEvents();
  renderOverviewBlocks();
  return reviewCount;
}

export async function openChronologyView(defaultStatusFilter = '') {
  ensureDom();
  await loadAllEvents();
  filterStatusEl.value = defaultStatusFilter;
  backdropEl.hidden = false;
  modalEl.hidden = false;
  renderList();
}

export function openChronologyReview() {
  return openChronologyView('pending_review');
}

export function closeChronologyModal() {
  if (!initialized) return;
  backdropEl.hidden = true;
  modalEl.hidden = true;
}

async function handleExtract() {
  extractBtnEl.disabled = true;
  extractBtnEl.textContent = 'Extracting…';
  statusEl.hidden = false;
  statusEl.textContent = 'Reading approved emails and OCR documents…';
  try {
    const res = await fetch('/api/chronology/extract', { method: 'POST' });
    const data = await res.json();
    if (res.ok) {
      statusEl.textContent = `Processed ${data.sourcesProcessed} new source(s) — ${data.eventsCreatedConfirmed} confirmed, ${data.eventsCreatedReview} sent to review.`;
      await loadAllEvents();
      renderList();
      renderOverviewBlocks();
    } else {
      statusEl.textContent = data.error || 'Extraction failed.';
    }
  } catch (err) {
    statusEl.textContent = 'Could not reach the server.';
  } finally {
    extractBtnEl.disabled = false;
    extractBtnEl.textContent = 'Extract new events';
  }
}

function sortByDate(events) {
  return [...events].sort((a, b) => {
    const da = a.eventDate || a.dateRangeFrom || '9999';
    const db = b.eventDate || b.dateRangeFrom || '9999';
    return da < db ? -1 : da > db ? 1 : 0;
  });
}

function sortByMostRecentChronology(events) {
  return [...events].sort((a, b) => {
    const aDate = a.eventDate || a.dateRangeTo || a.dateRangeFrom || '';
    const bDate = b.eventDate || b.dateRangeTo || b.dateRangeFrom || '';
    if (aDate !== bDate) return bDate.localeCompare(aDate);

    const aUpdated = a.updatedAt || a.createdAt || '';
    const bUpdated = b.updatedAt || b.createdAt || '';
    return bUpdated.localeCompare(aUpdated);
  });
}

function renderList() {
  if (!bodyEl) return;
  const category = filterCategoryEl.value;
  const importance = filterImportanceEl.value;
  const source = filterSourceEl.value;
  const status = filterStatusEl.value;

  let filtered = allEvents.filter((e) => {
    if (category && e.category !== category) return false;
    if (importance && e.importance !== importance) return false;
    if (status && e.status !== status) return false;
    if (source && !(e.sources || []).some((s) => s.sourceType === source)) return false;
    return true;
  });
  filtered = sortByDate(filtered);

  bodyEl.innerHTML = '';
  if (!filtered.length) {
    bodyEl.innerHTML = '<div class="email-empty">No events match these filters yet.</div>';
    return;
  }

  for (const event of filtered) {
    bodyEl.appendChild(buildEventRow(event));
  }
}

function buildEventRow(event) {
  const row = document.createElement('div');
  row.className = 'chrono-event-row';

  const isPending = event.status === 'pending_review';
  const primarySource = getPrimarySource(event);
  const primaryEmailSource = event.primaryEmailSource || null;
  const dateLabel = event.eventDate
    ? formatFullDate(event.eventDate)
    : event.dateRangeFrom
      ? `${formatFullDate(event.dateRangeFrom)} – ${event.dateRangeTo ? formatFullDate(event.dateRangeTo) : '?'}`
      : 'Date unknown';
  const sourceSummary = primaryEmailSource
    ? `${primaryEmailSource.senderName || primaryEmailSource.senderEmail || 'Unknown sender'}${primaryEmailSource.senderEmail ? ` <${primaryEmailSource.senderEmail}>` : ''}`
    : '';

  if (primarySource) {
    row.classList.add('chrono-event-row-clickable');
    row.tabIndex = 0;
    row.setAttribute('role', 'button');
    row.setAttribute('aria-label', `Open source for chronology event ${event.title || ''}`.trim());
  }

  row.innerHTML = `
    <div class="chrono-event-top">
      <span class="chrono-cat-badge">${escapeHtml(CATEGORY_LABELS[event.category] || event.category)}</span>
      ${event.natureOfClaim && event.natureOfClaim !== 'confirmed_fact' ? `<span class="chrono-nature-badge chrono-nature-${escapeHtml(event.natureOfClaim)}">${escapeHtml(NATURE_LABELS[event.natureOfClaim] || event.natureOfClaim)}</span>` : ''}
      <span class="chrono-status-badge ${isPending ? 'pending' : 'confirmed'}">${isPending ? 'Awaiting review' : 'Confirmed'}</span>
      ${!isPending ? `
        <div class="chrono-hover-actions" aria-label="Chronology item actions">
          <button class="chrono-subtle-btn chrono-edit-inline-btn" type="button">Edit</button>
          <button class="chrono-subtle-btn chrono-delete-inline-btn" type="button">Delete</button>
        </div>
      ` : ''}
    </div>
    <div class="chrono-event-title">${escapeHtml(event.title)}</div>
    <div class="chrono-event-date">${escapeHtml(dateLabel)}${event.involvedParties?.length ? ' · ' + escapeHtml(event.involvedParties.join(', ')) : ''}</div>
    ${sourceSummary ? `<div class="chrono-event-source-summary">Email: ${escapeHtml(sourceSummary)}</div>` : ''}
    <div class="chrono-event-desc">${escapeHtml(event.description)}</div>
    ${isPending && event.reviewReason ? `<div class="chrono-review-reason">${escapeHtml(event.reviewReason)}</div>` : ''}
    <div class="chrono-sources"></div>
    <div class="chrono-actions"></div>
  `;

  if (primarySource) {
    row.addEventListener('click', (eventObj) => {
      if (eventObj.target.closest('button, input, textarea, select, a, label')) return;
      openSource(primarySource);
    });
    row.addEventListener('keydown', (eventObj) => {
      if (eventObj.key !== 'Enter' && eventObj.key !== ' ') return;
      if (eventObj.target.closest('button, input, textarea, select, a, label')) return;
      eventObj.preventDefault();
      openSource(primarySource);
    });
  }

  const sourcesEl = row.querySelector('.chrono-sources');
  for (const source of event.sources || []) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'chrono-source-chip';
    chip.textContent = `${source.sourceType === 'email' ? '✉' : '📄'} ${source.sourceLabel}${source.pageNumber ? ` (p.${source.pageNumber})` : ''}`;
    chip.addEventListener('click', () => openSource(source));
    sourcesEl.appendChild(chip);
  }

  const actionsEl = row.querySelector('.chrono-actions');
  if (isPending) {
    actionsEl.innerHTML = `
      <button class="btn btn-dlyte chrono-approve-btn" type="button">Approve</button>
      <button class="btn btn-remove chrono-edit-btn" type="button">Edit</button>
      <button class="btn btn-remove chrono-reject-btn" type="button">Reject</button>
      ${event.conflictsWith?.length ? '<button class="btn btn-remove chrono-merge-btn" type="button">Merge into conflicting event</button>' : ''}
    `;
    actionsEl.querySelector('.chrono-approve-btn').addEventListener('click', () => handleApprove(event.id));
    actionsEl.querySelector('.chrono-reject-btn').addEventListener('click', () => handleReject(event.id));
    actionsEl.querySelector('.chrono-edit-btn').addEventListener('click', () => handleEdit(event, row));
    const mergeBtn = actionsEl.querySelector('.chrono-merge-btn');
    if (mergeBtn) mergeBtn.addEventListener('click', () => handleMerge(event.id, event.conflictsWith[0]));
  } else {
    row.querySelector('.chrono-edit-inline-btn')?.addEventListener('click', () => handleEdit(event, row));
    row.querySelector('.chrono-delete-inline-btn')?.addEventListener('click', () => handleDelete(event));
  }

  return row;
}

function getPrimarySource(event) {
  const sources = event.sources || [];
  return sources.find((source) => source.sourceType === 'email') || sources[0] || null;
}

async function openSource(source) {
  if (source.sourceType === 'email') {
    closeChronologyModal();
    openSpecificEmail(source.sourceId);
    return;
  }
  closeChronologyModal();
  document.dispatchEvent(new CustomEvent('dlyte:open-document', {
    detail: {
      job_id: source.sourceId,
      filename: source.sourceLabel,
    },
  }));
}

async function handleApprove(eventId) {
  await fetch(`/api/chronology/review/${encodeURIComponent(eventId)}/approve`, { method: 'POST' });
  await loadAllEvents();
  renderList();
  renderOverviewBlocks();
}

async function handleReject(eventId) {
  await fetch(`/api/chronology/review/${encodeURIComponent(eventId)}/reject`, { method: 'POST' });
  await loadAllEvents();
  renderList();
  renderOverviewBlocks();
}

async function handleMerge(eventId, targetId) {
  await fetch(`/api/chronology/review/${encodeURIComponent(eventId)}/merge`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mergeIntoId: targetId }),
  });
  await loadAllEvents();
  renderList();
  renderOverviewBlocks();
}

async function handleDelete(event) {
  const label = event?.title || 'this chronology item';
  if (!window.confirm(`Delete "${label}" from chronology?`)) return;
  await fetch(`/api/chronology/${encodeURIComponent(event.id)}/delete`, { method: 'POST' });
  await loadAllEvents();
  renderList();
  renderOverviewBlocks();
}

function handleEdit(event, row) {
  const desc = row.querySelector('.chrono-event-desc');
  const form = document.createElement('div');
  form.className = 'chrono-edit-form';
  form.innerHTML = `
    <input type="text" class="chrono-edit-title" value="${escapeHtml(event.title)}" placeholder="Title">
    <input type="date" class="chrono-edit-date" value="${event.eventDate || ''}">
    <textarea class="chrono-edit-desc" rows="2">${escapeHtml(event.description)}</textarea>
    <div class="chrono-edit-actions">
      <button class="btn btn-dlyte chrono-save-btn" type="button">Save</button>
      <button class="btn btn-remove chrono-cancel-btn" type="button">Cancel</button>
    </div>
  `;
  desc.replaceWith(form);

  form.querySelector('.chrono-cancel-btn').addEventListener('click', () => renderList());
  form.querySelector('.chrono-save-btn').addEventListener('click', async () => {
    const title = form.querySelector('.chrono-edit-title').value.trim();
    const eventDate = form.querySelector('.chrono-edit-date').value || null;
    const description = form.querySelector('.chrono-edit-desc').value.trim();
    await fetch(`/api/chronology/${encodeURIComponent(event.id)}/edit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, eventDate, description }),
    });
    await loadAllEvents();
    renderList();
  });
}

function renderOverviewBlocks() {
  const upcomingList = document.getElementById('upcomingList');
  const chronoList = document.getElementById('chronoList');
  if (!upcomingList || !chronoList) return;

  const confirmed = allEvents.filter((e) => e.status === 'confirmed');
  const today = new Date().toISOString().slice(0, 10);
  const upcoming = sortByDate(
    confirmed.filter((e) => e.category === 'court_procedural' && e.eventDate && e.eventDate >= today)
  ).slice(0, 3);
  const recent = sortByMostRecentChronology(confirmed).slice(0, 5);

  upcomingList.innerHTML = upcoming.length
    ? upcoming
        .map(
          (e) => `
        <div class="upcoming-row">
          <span class="upcoming-date">${escapeHtml(formatFullDate(e.eventDate))}</span>
          <span class="upcoming-action">${escapeHtml(e.title)}</span>
        </div>`
        )
        .join('')
    : '<span class="files-empty">Nothing confirmed yet</span>';

  chronoList.innerHTML = recent.length
    ? recent
        .map(
          (e) => `
        <li class="chrono-entry">
          <span class="chrono-marker"></span>
          <span class="chrono-text"><strong>${e.eventDate ? escapeHtml(formatFullDate(e.eventDate)) : 'Undated'}</strong> · ${escapeHtml(e.title)}</span>
        </li>`
        )
        .join('')
    : '<li class="files-empty">No events yet — run chronology extraction to get started.</li>';
}

function wireOverviewClicks() {
  const chronoBlock = document.getElementById('chronoBlock');
  if (chronoBlock) chronoBlock.addEventListener('click', () => openChronologyView());
}

async function init() {
  wireOverviewClicks();
  await refreshChronologyOverview();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
