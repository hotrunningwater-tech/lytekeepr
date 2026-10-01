/**
 * Pending-documents modal: lists case-email attachments that don't yet
 * have a matching OCR'd document, plus locally scanned documents that
 * couldn't be confidently matched to any case email. Purely presentational
 * — index.js supplies the data and the onOpenEmail/onRecheck/onConfirm
 * handlers.
 *
 * Only "pending_scan" rows get a selection checkbox for the scan queue.
 * Any row with a previewable file (already-OCR'd local docs, or a raw
 * email attachment) gets a "Preview" button — opens the same in-app
 * document viewer used elsewhere, read-only, no scan/OCR triggered, and
 * closing it returns here to this same list. Deliberately has zero
 * interaction with the right-side chat/OCR panel.
 * "unresolved_local" rows are already-local files with nothing to queue,
 * but they can be deleted locally from here.
 */

import { escapeHtml, formatFullDate } from '../email/utils.js';

const cropLock = window.DLYTECropLock || {
  isActive() { return false; },
  getMessage() { return 'Finish or cancel crop editing first.'; },
  guard() { return false; },
};

let backdropEl, modalEl, bodyEl, recheckBtnEl, footerEl, selectedCountEl, confirmBtnEl, removeBtnEl;
let initialized = false;
let currentHandlers = {};
let currentDocs = [];
let selected = new Set();

function isCropLockActive() {
  return Boolean(cropLock.isActive());
}

function getCropLockMessage() {
  return cropLock.getMessage();
}

function guardCropLockedAction(action) {
  return cropLock.guard(action);
}

function ensureDom() {
  if (initialized) return;
  initialized = true;

  backdropEl = document.createElement('div');
  backdropEl.className = 'docs-modal-backdrop';
  backdropEl.hidden = true;
  backdropEl.addEventListener('click', closeDocsModal);

  modalEl = document.createElement('div');
  modalEl.className = 'docs-modal';
  modalEl.hidden = true;
  modalEl.innerHTML = `
    <div class="docs-modal-header">
      <h2>Documents needing attention</h2>
      <div class="docs-modal-header-actions">
        <button class="docs-recheck-btn" type="button">Recheck now</button>
        <button class="docs-modal-close" type="button" aria-label="Close">&times;</button>
      </div>
    </div>
    <div class="docs-modal-body"></div>
    <div class="docs-modal-footer">
      <span class="docs-selected-count">0 selected</span>
      <button class="btn btn-remove docs-remove-btn" type="button" disabled>Remove Selected</button>
      <button class="btn btn-dlyte docs-confirm-btn" type="button" disabled>Confirm — add to scan queue</button>
    </div>
  `;
  bodyEl = modalEl.querySelector('.docs-modal-body');
  footerEl = modalEl.querySelector('.docs-modal-footer');
  selectedCountEl = modalEl.querySelector('.docs-selected-count');
  confirmBtnEl = modalEl.querySelector('.docs-confirm-btn');
  removeBtnEl = modalEl.querySelector('.docs-remove-btn');
  recheckBtnEl = modalEl.querySelector('.docs-recheck-btn');
  modalEl.querySelector('.docs-modal-close').addEventListener('click', closeDocsModal);
  confirmBtnEl.addEventListener('click', handleConfirm);
  removeBtnEl.addEventListener('click', handleRemove);

  document.body.appendChild(backdropEl);
  document.body.appendChild(modalEl);

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !modalEl.hidden) closeDocsModal();
  });
}

export function openDocsModal(docs, handlers = {}) {
  ensureDom();
  currentHandlers = handlers;
  // Preserve selections across reopens (e.g. Preview a row, close it, come
  // back) — only drop ids that no longer exist in this list (queued,
  // removed, or a recheck resolved them). Losing the whole selection just
  // for looking at a preview wastes a full re-check pass.
  const validIds = new Set(docs.map((d) => d.id));
  for (const id of selected) {
    if (!validIds.has(id)) selected.delete(id);
  }

  recheckBtnEl.onclick = async () => {
    if (guardCropLockedAction('docs-modal-recheck')) return;
    recheckBtnEl.disabled = true;
    recheckBtnEl.textContent = 'Rechecking…';
    const fresh = await handlers.onRecheck?.();
    recheckBtnEl.disabled = false;
    recheckBtnEl.textContent = 'Recheck now';
    renderList(fresh || docs);
  };

  backdropEl.hidden = false;
  modalEl.hidden = false;
  renderList(docs);
}

export function closeDocsModal() {
  if (!initialized) return;
  backdropEl.hidden = true;
  modalEl.hidden = true;
}

async function handleConfirm() {
  if (guardCropLockedAction('docs-modal-confirm')) return;
  const ids = currentDocs.filter((d) => selected.has(d.id) && d.status === 'pending_scan').map((d) => d.id);
  if (!ids.length) return;
  confirmBtnEl.disabled = true;
  confirmBtnEl.textContent = 'Adding…';
  await currentHandlers.onConfirm?.(ids);
  confirmBtnEl.disabled = false;
  confirmBtnEl.textContent = 'Confirm — add to scan queue';
  // Queued items no longer belong in this "needs a decision" list.
  currentDocs = currentDocs.filter((d) => !selected.has(d.id));
  selected = new Set();
  renderList(currentDocs);
}

async function handleRemove() {
  if (guardCropLockedAction('docs-modal-remove')) return;
  if (!selected.size) return;
  const ids = [...selected];
  removeBtnEl.disabled = true;
  removeBtnEl.textContent = 'Removing…';
  const fresh = await currentHandlers.onDismiss?.(ids);
  removeBtnEl.disabled = false;
  removeBtnEl.textContent = 'Remove Selected';
  selected = new Set();
  renderList(fresh || currentDocs.filter((d) => !ids.includes(d.id)));
}

function updateFooter() {
  selectedCountEl.textContent = `${selected.size} selected`;
  const selectedHasPendingScan = currentDocs.some((d) => selected.has(d.id) && d.status === 'pending_scan');
  confirmBtnEl.disabled = !selectedHasPendingScan || isCropLockActive();
  removeBtnEl.disabled = selected.size === 0 || isCropLockActive();
}

function renderList(docs) {
  currentDocs = docs;
  bodyEl.innerHTML = '';

  if (!docs.length) {
    bodyEl.innerHTML = '<div class="email-empty">Nothing pending — every referenced attachment is matched.</div>';
    footerEl.hidden = true;
    return;
  }

  if (isCropLockActive()) {
    const note = document.createElement('div');
    note.className = 'photo-review-lock-note docs-modal-lock-note';
    note.textContent = getCropLockMessage();
    bodyEl.appendChild(note);
  }

  footerEl.hidden = false;
  recheckBtnEl.disabled = isCropLockActive();
  updateFooter();

  for (const doc of docs) {
    const row = document.createElement('div');
    row.className = 'docs-row';
    const isUnresolvedLocal = doc.status === 'unresolved_local';
    const isPhotoReview = doc.status === 'pending_photo_review';
    // Every row can be selected for Remove; only pending_scan rows are
    // meaningful for "Confirm — add to scan queue" (handleConfirm filters).
    const isSelectable = true;
    const photoCountLabel =
      isPhotoReview && (doc.attachmentCount || 0) > 1
        ? `${doc.attachmentCount} attached pages`
        : null;
    const isPreviewable = Boolean(doc.ocrJobId) || Boolean(doc.emailId && doc.attachmentId);

    row.innerHTML = `
      <div class="docs-row-main">
        <div class="docs-row-filename">
          ${escapeHtml(doc.filename || '(unnamed file)')}
          ${doc.isDuplicate ? `<span class="docs-duplicate-badge" title="Same filename and size as ${doc.duplicateCount - 1} other listed item(s) — likely the same file sent more than once">Possible duplicate ×${doc.duplicateCount}</span>` : ''}
        </div>
        ${
          isUnresolvedLocal
            ? '<div class="docs-row-meta">Locally scanned document — no confident match to a case email.</div>'
            : `<div class="docs-row-meta">From ${escapeHtml(doc.emailSender?.name || doc.emailSender?.email || 'unknown sender')} — "${escapeHtml(doc.emailSubject || '')}" · Sent ${escapeHtml(formatFullDate(doc.emailDate))}${photoCountLabel ? ` · ${escapeHtml(photoCountLabel)}` : ''}</div>`
        }
        <div class="docs-row-reason">${escapeHtml(doc.reason || '')}</div>
        <div class="docs-row-actions">
          ${isPreviewable ? `<button class="btn btn-dlyte docs-preview-btn" type="button" ${isCropLockActive() ? 'disabled aria-disabled="true"' : ''}>Preview${photoCountLabel ? ` (${escapeHtml(String(doc.attachmentCount))} pages)` : ''}</button>` : ''}
          ${doc.emailId ? `<button class="btn btn-remove docs-open-email-btn" type="button" ${isCropLockActive() ? 'disabled aria-disabled="true"' : ''}>Open source email</button>` : ''}
          ${isUnresolvedLocal && doc.ocrJobId ? `<button class="btn btn-remove docs-delete-local-btn" type="button" ${isCropLockActive() ? 'disabled aria-disabled="true"' : ''}>Delete local copy</button>` : ''}
        </div>
      </div>
      ${isSelectable ? `<input type="checkbox" class="docs-row-checkbox" ${selected.has(doc.id) ? 'checked' : ''} ${isCropLockActive() ? 'disabled aria-disabled="true"' : ''}>` : ''}
    `;

    const checkbox = row.querySelector('.docs-row-checkbox');
    if (checkbox) {
      checkbox.addEventListener('change', () => {
        if (guardCropLockedAction('docs-modal-toggle-selection')) {
          checkbox.checked = selected.has(doc.id);
          return;
        }
        if (checkbox.checked) selected.add(doc.id);
        else selected.delete(doc.id);
        updateFooter();
      });
    }

    const openBtn = row.querySelector('.docs-open-email-btn');
    if (openBtn) {
      openBtn.addEventListener('click', () => {
        if (guardCropLockedAction('docs-modal-open-email')) return;
        closeDocsModal();
        currentHandlers.onOpenEmail?.(doc.emailId);
      });
    }

    const previewBtn = row.querySelector('.docs-preview-btn');
    if (previewBtn) {
      previewBtn.addEventListener('click', () => {
        if (guardCropLockedAction('docs-modal-preview')) return;
        currentHandlers.onPreview?.(doc);
      });
    }

    const deleteBtn = row.querySelector('.docs-delete-local-btn');
    if (deleteBtn) {
      deleteBtn.addEventListener('click', async () => {
        if (guardCropLockedAction('docs-modal-delete-local')) return;
        await currentHandlers.onDeleteLocal?.(doc);
      });
    }

    bodyEl.appendChild(row);
  }
}
