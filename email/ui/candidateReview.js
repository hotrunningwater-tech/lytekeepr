/**
 * Candidate review panel: shows emails the classifier flagged as likely
 * case-relevant, awaiting user approval. Nothing here is a permanent DLYTE
 * record yet — approving via the parent's handlers is what saves them.
 * Purely presentational; drawer.js owns the actual save/dismiss calls.
 */

import { escapeHtml, formatRowTime } from '../utils.js';
import { fetchMessageDetail } from '../providers/gmailProvider.js';

const cropLock = window.DLYTECropLock || {
  isActive() { return false; },
  guard() { return false; },
};

export function renderCandidateReview(container, candidates, handlers = {}) {
  container.innerHTML = '';
  const cropLocked = Boolean(cropLock.isActive());

  const wrap = document.createElement('div');
  wrap.className = 'candidate-review';

  if (!candidates.length) {
    wrap.innerHTML = '<div class="email-empty">No candidates awaiting review.</div>';
    container.appendChild(wrap);
    return;
  }

  const sortedCandidates = [...candidates].sort((a, b) => {
    const aTime = Date.parse(a.receivedAt || '') || 0;
    const bTime = Date.parse(b.receivedAt || '') || 0;
    return bTime - aTime;
  });

  const selected = new Set();

  const heading = document.createElement('div');
  heading.className = 'candidate-heading';
  heading.innerHTML = `
    <span>${sortedCandidates.length} email${sortedCandidates.length === 1 ? '' : 's'} look related to your case</span>
    <label class="candidate-select-all-label">
      <input type="checkbox" class="candidate-select-all" ${cropLocked ? 'disabled aria-disabled="true"' : ''}>
      <span>Select all</span>
    </label>
  `;
  wrap.appendChild(heading);

  const selectAllCheckbox = heading.querySelector('.candidate-select-all');
  const syncSelectAllState = () => {
    selectAllCheckbox.checked = selected.size === sortedCandidates.length;
    selectAllCheckbox.indeterminate = selected.size > 0 && selected.size < sortedCandidates.length;
  };
  selectAllCheckbox.addEventListener('change', () => {
    if (cropLock.guard('candidate-review-select-all')) {
      syncSelectAllState();
      return;
    }
    const checkAll = selectAllCheckbox.checked;
    selected.clear();
    for (const c of sortedCandidates) {
      if (checkAll) selected.add(c.id);
    }
    for (const checkbox of list.querySelectorAll('.candidate-checkbox')) {
      checkbox.checked = checkAll;
    }
    selectAllCheckbox.indeterminate = false;
  });

  const list = document.createElement('div');
  list.className = 'candidate-list';
  for (const c of sortedCandidates) {
    const row = document.createElement('div');
    row.className = 'candidate-row';
    row.innerHTML = `
      <div class="candidate-row-main">
        <div class="candidate-row-top">
          <span class="candidate-sender">${escapeHtml(c.senderName)}</span>
          <span class="candidate-time">${formatRowTime(c.receivedAt)}</span>
        </div>
        <div class="candidate-subject">${escapeHtml(c.subject)}</div>
        <div class="candidate-preview">
          ${escapeHtml(c.preview)}
          ${c.hasAttachments ? '<span class="email-attach-icon" title="Has attachment">📎</span>' : ''}
        </div>
        <div class="candidate-reason">${escapeHtml(c.reason || 'Flagged as likely related to your case.')}</div>
        <div class="candidate-body" hidden></div>
      </div>
      <label class="candidate-checkbox-label">
        <input type="checkbox" class="candidate-checkbox" data-id="${escapeHtml(c.id)}" ${cropLocked ? 'disabled aria-disabled="true"' : ''}>
      </label>
    `;
    const checkbox = row.querySelector('.candidate-checkbox');
    checkbox.addEventListener('click', (e) => e.stopPropagation());
    checkbox.addEventListener('change', () => {
      if (cropLock.guard('candidate-review-toggle')) {
        checkbox.checked = selected.has(c.id);
        return;
      }
      if (checkbox.checked) selected.add(c.id);
      else selected.delete(c.id);
      syncSelectAllState();
    });

    const main = row.querySelector('.candidate-row-main');
    const bodyEl = row.querySelector('.candidate-body');
    let loaded = false;
    main.addEventListener('click', async () => {
      if (cropLock.guard('candidate-review-open-body')) return;
      const isHidden = bodyEl.hidden;
      bodyEl.hidden = !isHidden;
      if (isHidden && !loaded) {
        bodyEl.textContent = 'Loading…';
        try {
          const detail = await fetchMessageDetail(c.id);
          const attachments = detail.attachments || [];
          bodyEl.innerHTML = `
            <div>${escapeHtml(detail.body || '(no content)').replace(/\n/g, '<br>')}</div>
            ${
              attachments.length
                ? `<div class="candidate-attachments">
                    <span class="block-label">Attachments</span>
                    ${attachments
                      .map(
                        (attachment) =>
                          `<div class="file-row candidate-attachment-row">
                            <span class="file-icon">${attachment.isPhotoReviewable ? '🖼️' : '📎'}</span>
                            <span class="file-name">${escapeHtml(attachment.name || '(unnamed attachment)')}</span>
                          </div>`
                      )
                      .join('')}
                  </div>`
                : ''
            }
          `;
          loaded = true;
        } catch (err) {
          bodyEl.textContent = 'Could not load this email.';
        }
      }
    });

    list.appendChild(row);
  }
  wrap.appendChild(list);

  const actions = document.createElement('div');
  actions.className = 'candidate-actions';
  actions.innerHTML = `
    <button class="btn btn-remove" id="removeSelectedBtn" type="button" title="Permanently exclude the checked emails from future review" ${cropLocked ? 'disabled aria-disabled="true"' : ''}>Remove Selected</button>
    <button class="btn btn-dlyte" id="saveSelectedBtn" type="button" ${cropLocked ? 'disabled aria-disabled="true"' : ''}>Save Selected</button>
  `;
  actions.querySelector('#saveSelectedBtn').addEventListener('click', () => {
    if (cropLock.guard('candidate-review-save-selected')) return;
    handlers.onSave?.([...selected]);
  });
  actions.querySelector('#removeSelectedBtn').addEventListener('click', () => {
    if (cropLock.guard('candidate-review-remove-selected')) return;
    if (!selected.size) return;
    handlers.onDismiss?.([...selected]);
  });
  wrap.appendChild(actions);

  container.appendChild(wrap);
}
