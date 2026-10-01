/**
 * Owns the case-overview panel's dynamic "attention" rows and the scan
 * queue block — the only place that decides what needs the user's
 * attention right now. Reads from the same records the email drawer and
 * document registry already expose via their APIs; doesn't duplicate any
 * of that logic itself.
 */

import { openReviewFlow, openSpecificEmail } from '../email/index.js';
import { escapeHtml } from '../email/utils.js';
import { openDocsModal, closeDocsModal } from './pendingDocuments.js';
import { openChronologyReview, refreshChronologyOverview } from './chronology.js';

const cropLock = window.DLYTECropLock || {
  isActive() { return false; },
  getMessage() { return 'Finish or cancel crop editing first.'; },
  guard() { return false; },
};

let emailPendingCount = 0;
let docCounts = { matched: 0, pendingScan: 0, pendingPhotoReview: 0, unresolved: 0 };
let pendingDocs = [];
let scanQueue = [];
let chronoReviewCount = 0;

let runScanBtnEl;
let updateCaseBtnEl, updateCaseStatusEl;
let caseOverviewContentEl, documentViewerEl, documentViewerTitleEl;
let documentViewerFrameEl, documentViewerErrorEl, documentViewerFrameWrapEl, closeDocumentViewerBtnEl;
let documentViewerNavEl, documentViewerNavCountEl, documentViewerPrevBtnEl, documentViewerNextBtnEl;
let documentsBrowserEl, documentsBrowserListEl, openAllDocsBtnEl, closeAllDocsBtnEl;
let photoCropEditorEl, photoCropEditorTitleEl, photoCropEditorCopyEl, photoCropEditorStageEl;
let photoCropEditorImageEl, photoCropEditorPolygonEl, photoCropConfirmBtnEl, photoCropCancelBtnEl;
let photoCropHandleEls = [];
let recentScans = [];
let activePhotoCrop = null;
let activeCropHandle = null;
let isPhotoCropEditorActive = Boolean(cropLock.isActive());
let activeDocumentJobId = null;
let documentViewerReturnToPendingDocs = false;
// Set only for a multi-attachment photo-review row: lets Prev/Next step
// through each raw page one at a time without touching OCR/scanning.
let documentViewerAttachmentSet = null; // { messageId, filenames: [...], attachmentIds: [...], index }

function getCropLockMessage() {
  return cropLock.getMessage();
}

function guardCropLockedAction(action) {
  if (!cropLock.guard(action)) return false;
  return true;
}

function flagLikelyDuplicates(entries) {
  const groups = new Map();
  for (const entry of entries) {
    if (!entry.filename || !entry.size) continue;
    const key = `${entry.filename}|${entry.size}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(entry);
  }
  for (const group of groups.values()) {
    if (group.length < 2) continue;
    for (const entry of group) {
      entry.isDuplicate = true;
      entry.duplicateCount = group.length;
    }
  }
  return entries;
}

function getPendingDocumentEntries(entries) {
  return flagLikelyDuplicates(
    entries.filter(
      (entry) =>
        entry.status === 'pending_scan' ||
        entry.status === 'pending_photo_review' ||
        entry.status === 'unresolved_local'
    )
  ).sort((a, b) => {
    const aTime = Date.parse(a.emailDate || '') || 0;
    const bTime = Date.parse(b.emailDate || '') || 0;
    return bTime - aTime;
  });
}

function summarizeDocumentCounts(entries, fallback = {}) {
  const counts = {
    matched: 0,
    pendingScan: 0,
    pendingPhotoReview: 0,
    unresolved: 0,
  };

  for (const entry of entries) {
    if (entry.status === 'matched_scanned') counts.matched += 1;
    if (entry.status === 'pending_scan') counts.pendingScan += 1;
    if (entry.status === 'pending_photo_review') counts.pendingPhotoReview += 1;
    if (entry.status === 'unresolved_local') counts.unresolved += 1;
  }

  return {
    matched: counts.matched || fallback.matched || 0,
    pendingScan: counts.pendingScan || fallback.pendingScan || 0,
    pendingPhotoReview: counts.pendingPhotoReview || fallback.pendingPhotoReview || 0,
    unresolved: counts.unresolved || fallback.unresolved || 0,
  };
}

function buildDocumentViewerUrl(jobId, filename) {
  const lower = (filename || '').toLowerCase();
  const base = `/api/files/${encodeURIComponent(jobId)}/original`;
  if (lower.endsWith('.pdf')) {
    return `${base}#toolbar=1&navpanes=0&zoom=page-width`;
  }
  return base;
}

function buildAttachmentViewerUrl(messageId, attachmentId, filename) {
  const lower = (filename || '').toLowerCase();
  const base = `/api/gmail/messages/${encodeURIComponent(messageId)}/attachments/${encodeURIComponent(attachmentId)}/raw`;
  if (lower.endsWith('.pdf')) {
    return `${base}#toolbar=1&navpanes=0&zoom=page-width`;
  }
  return base;
}

const IMAGE_EXTENSIONS = ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.heic'];

function isImageFilename(filename) {
  const lower = (filename || '').toLowerCase();
  return IMAGE_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

function isWordDocumentFilename(filename) {
  const lower = (filename || '').toLowerCase();
  return lower.endsWith('.doc') || lower.endsWith('.docx');
}

// Navigating an iframe straight to an image URL shows it at native pixel
// size (tiny phone photos, or huge scans that need scrolling both ways).
// srcdoc wraps it in a page that scales the image down to fit the frame
// while keeping its aspect ratio — same-origin img fetch, no CSP issue.
function setDocumentViewerContent(url, filename) {
  if (isImageFilename(filename)) {
    documentViewerFrameEl.removeAttribute('src');
    documentViewerFrameEl.srcdoc = `<!doctype html><html><head><style>
      html,body{margin:0;height:100%;background:#15171b;display:flex;align-items:center;justify-content:center;overflow:auto}
      img{max-width:100%;max-height:100%;object-fit:contain;display:block}
    </style></head><body><img src="${url}" alt=""></body></html>`;
  } else {
    documentViewerFrameEl.removeAttribute('srcdoc');
    documentViewerFrameEl.src = url;
  }
}

function setupDocumentViewer() {
  caseOverviewContentEl = document.getElementById('caseOverviewContent');
  documentViewerEl = document.getElementById('documentViewer');
  documentViewerTitleEl = document.getElementById('documentViewerTitle');
  documentViewerFrameEl = document.getElementById('documentViewerFrame');
  documentViewerErrorEl = document.getElementById('documentViewerError');
  documentViewerFrameWrapEl = document.getElementById('documentViewerFrameWrap');
  closeDocumentViewerBtnEl = document.getElementById('closeDocumentViewerBtn');
  documentViewerNavEl = document.getElementById('documentViewerNav');
  documentViewerNavCountEl = document.getElementById('documentViewerNavCount');
  documentViewerPrevBtnEl = document.getElementById('documentViewerPrevBtn');
  documentViewerNextBtnEl = document.getElementById('documentViewerNextBtn');
  documentsBrowserEl = document.getElementById('documentsBrowser');
  documentsBrowserListEl = document.getElementById('documentsBrowserList');
  openAllDocsBtnEl = document.getElementById('openAllDocsBtn');
  closeAllDocsBtnEl = document.getElementById('closeAllDocsBtn');
  photoCropEditorEl = document.getElementById('photoCropEditor');
  photoCropEditorTitleEl = document.getElementById('photoCropEditorTitle');
  photoCropEditorCopyEl = document.getElementById('photoCropEditorCopy');
  photoCropEditorStageEl = document.getElementById('photoCropEditorStage');
  photoCropEditorImageEl = document.getElementById('photoCropEditorImage');
  photoCropEditorPolygonEl = document.getElementById('photoCropEditorPolygon');
  photoCropConfirmBtnEl = document.getElementById('photoCropConfirmBtn');
  photoCropCancelBtnEl = document.getElementById('photoCropCancelBtn');
  photoCropHandleEls = [0, 1, 2, 3].map((index) => document.getElementById(`photoCropHandle${index}`)).filter(Boolean);

  if (closeDocumentViewerBtnEl) {
    closeDocumentViewerBtnEl.addEventListener('click', closeDocumentViewer);
  }
  if (documentViewerPrevBtnEl) {
    documentViewerPrevBtnEl.addEventListener('click', () => stepDocumentViewerAttachment(-1));
  }
  if (documentViewerNextBtnEl) {
    documentViewerNextBtnEl.addEventListener('click', () => stepDocumentViewerAttachment(1));
  }
  if (openAllDocsBtnEl) {
    openAllDocsBtnEl.addEventListener('click', openAllDocuments);
  }
  if (closeAllDocsBtnEl) {
    closeAllDocsBtnEl.addEventListener('click', closeAllDocuments);
  }

  document.addEventListener('dlyte:open-document', (event) => {
    if (guardCropLockedAction('open-document-viewer')) return;
    openDocumentViewer(event.detail || {});
  });
  document.addEventListener('dlyte:open-photo-crop-editor', (event) => {
    isPhotoCropEditorActive = true;
    syncGlobalPhotoReviewLocks();
    openPhotoCropEditor(event.detail || {});
  });
  document.addEventListener('dlyte:close-photo-crop-editor', () => {
    isPhotoCropEditorActive = false;
    syncGlobalPhotoReviewLocks();
    closePhotoCropEditor();
  });
  document.addEventListener('dlyte:photo-crop-lock-changed', (event) => {
    isPhotoCropEditorActive = Boolean(event.detail?.active);
    syncGlobalPhotoReviewLocks();
    render();
    if (!documentsBrowserEl?.hidden) renderAllDocuments();
  });
  document.addEventListener('dlyte:recent-scans-updated', (event) => {
    recentScans = event.detail?.files || [];
    if (!documentsBrowserEl.hidden) renderAllDocuments();
  });
  document.addEventListener('dlyte:case-emails-saved', async () => {
    // Force an actual rebuild (not the plain GET, which is throttled to
    // once per 24h) — this event means "something just changed," so the
    // registry has to be recomputed now, not whenever the throttle allows.
    await Promise.all([refreshEmailStatus(), recheckDocuments(), refreshScanQueue()]);
    render();
  });
  if (photoCropConfirmBtnEl) {
    photoCropConfirmBtnEl.addEventListener('click', confirmPhotoCrop);
  }
  if (photoCropCancelBtnEl) {
    photoCropCancelBtnEl.addEventListener('click', () => {
      document.dispatchEvent(new CustomEvent('dlyte:close-photo-crop-editor'));
    });
  }
  for (const handleEl of photoCropHandleEls) {
    handleEl.addEventListener('pointerdown', beginPhotoCropDrag);
  }
  if (photoCropEditorStageEl) {
    photoCropEditorStageEl.addEventListener('pointermove', updatePhotoCropDrag);
    photoCropEditorStageEl.addEventListener('pointerup', endPhotoCropDrag);
    photoCropEditorStageEl.addEventListener('pointercancel', endPhotoCropDrag);
  }
}

async function init() {
  setupDocumentViewer();
  runScanBtnEl = document.getElementById('runScanBtn');
  if (runScanBtnEl) runScanBtnEl.addEventListener('click', handleRunScan);

  updateCaseBtnEl = document.getElementById('updateCaseBtn');
  updateCaseStatusEl = document.getElementById('updateCaseStatus');
  if (updateCaseBtnEl) updateCaseBtnEl.addEventListener('click', handleUpdateCase);
  syncGlobalPhotoReviewLocks();

  await Promise.all([refreshEmailStatus(), refreshDocumentRegistry(), refreshScanQueue(), refreshChronoCount()]);
  render();
}

function openDocumentViewer(doc) {
  if (guardCropLockedAction('open-document-viewer')) return;
  if (!caseOverviewContentEl || !documentViewerEl || !documentViewerTitleEl || !documentViewerFrameEl) return;

  const title = doc.filename || 'Scanned document';
  const jobId = doc.job_id;
  activeDocumentJobId = jobId || null;
  document.dispatchEvent(
    new CustomEvent('dlyte:preview-document-context', {
      detail: {
        job_id: activeDocumentJobId,
        filename: title,
      },
    })
  );
  documentViewerErrorEl.hidden = true;
  documentViewerErrorEl.textContent = '';
  documentViewerFrameWrapEl.hidden = false;
  documentViewerFrameEl.removeAttribute('srcdoc');
  documentViewerFrameEl.src = 'about:blank';

  const attachmentIds = doc.attachmentIds?.length ? doc.attachmentIds : doc.attachmentId ? [doc.attachmentId] : [];

  if (jobId) {
    documentViewerAttachmentSet = null;
    setDocumentViewerContent(buildDocumentViewerUrl(jobId, title), title);
    documentViewerTitleEl.textContent = title;
  } else if (doc.messageId && attachmentIds.length && !isWordDocumentFilename(title)) {
    // Raw, not-yet-scanned attachment(s) — same viewer, read-only stream
    // from Gmail, no OCR/scan triggered by opening this. attachmentIds.length
    // > 1 (a photo-review batch) gets Prev/Next to step through every page.
    documentViewerAttachmentSet = {
      messageId: doc.messageId,
      attachmentIds,
      filenames: doc.filenames?.length ? doc.filenames : [doc.filename || ''],
      baseTitle: title,
      index: 0,
    };
    showDocumentViewerAttachmentIndex(0);
  } else if (doc.messageId && attachmentIds.length && isWordDocumentFilename(title)) {
    // Browsers have no inline renderer for .doc/.docx at all — navigating
    // an iframe to one doesn't show a blank page, it triggers the
    // browser's own file-download fallback instead, which looked like
    // this system was "saving" the file. Word docs can only be previewed
    // after they've been scanned (converted to PDF) — see doc_convert.py.
    documentViewerAttachmentSet = null;
    documentViewerFrameWrapEl.hidden = true;
    documentViewerErrorEl.hidden = false;
    documentViewerErrorEl.textContent = 'Cannot preview a Word document (.doc/.docx) before it’s scanned — browsers can’t display this format directly. Scan it first to preview the converted PDF.';
    documentViewerTitleEl.textContent = title;
  } else {
    documentViewerAttachmentSet = null;
    documentViewerFrameWrapEl.hidden = true;
    documentViewerErrorEl.hidden = false;
    documentViewerErrorEl.textContent = 'This item is missing its file reference, so it cannot be previewed.';
    documentViewerTitleEl.textContent = title;
  }

  showCaseOverviewView('document');
}

function showDocumentViewerAttachmentIndex(index) {
  const set = documentViewerAttachmentSet;
  if (!set) return;
  const total = set.attachmentIds.length;
  set.index = Math.max(0, Math.min(index, total - 1));

  const filename = set.filenames[set.index] || set.baseTitle || 'Attachment';
  const attachmentId = set.attachmentIds[set.index];
  setDocumentViewerContent(buildAttachmentViewerUrl(set.messageId, attachmentId, filename), filename);
  documentViewerTitleEl.textContent = total > 1 ? `${set.baseTitle || filename} — page ${set.index + 1} of ${total}` : filename;

  if (documentViewerNavEl) documentViewerNavEl.hidden = total <= 1;
  if (documentViewerNavCountEl) documentViewerNavCountEl.textContent = `${set.index + 1} / ${total}`;
  if (documentViewerPrevBtnEl) documentViewerPrevBtnEl.disabled = set.index <= 0;
  if (documentViewerNextBtnEl) documentViewerNextBtnEl.disabled = set.index >= total - 1;
}

function stepDocumentViewerAttachment(delta) {
  if (guardCropLockedAction('document-viewer-step-attachment')) return;
  if (!documentViewerAttachmentSet) return;
  showDocumentViewerAttachmentIndex(documentViewerAttachmentSet.index + delta);
}

function syncGlobalPhotoReviewLocks() {
  if (updateCaseBtnEl) {
    if (isPhotoCropEditorActive) {
      updateCaseBtnEl.disabled = true;
    } else if (updateCaseBtnEl.textContent === 'Update case') {
      updateCaseBtnEl.disabled = false;
    }
  }
  if (runScanBtnEl) {
    runScanBtnEl.disabled = isPhotoCropEditorActive || !scanQueue.length;
  }
  if (openAllDocsBtnEl) {
    openAllDocsBtnEl.disabled = isPhotoCropEditorActive;
  }
  if (closeAllDocsBtnEl) {
    closeAllDocsBtnEl.disabled = isPhotoCropEditorActive;
  }
  if (closeDocumentViewerBtnEl) {
    closeDocumentViewerBtnEl.disabled = isPhotoCropEditorActive;
  }
  if (updateCaseStatusEl) {
    if (isPhotoCropEditorActive) {
      updateCaseStatusEl.hidden = false;
      updateCaseStatusEl.textContent = getCropLockMessage();
    } else if (updateCaseStatusEl.textContent === getCropLockMessage()) {
      updateCaseStatusEl.textContent = '';
      updateCaseStatusEl.hidden = true;
    }
  }
}

function closeDocumentViewer() {
  if (guardCropLockedAction('close-document-viewer')) return;
  if (!caseOverviewContentEl || !documentViewerEl || !documentViewerFrameEl || !documentViewerErrorEl || !documentViewerFrameWrapEl) return;
  documentViewerFrameEl.removeAttribute('srcdoc');
  documentViewerFrameEl.src = 'about:blank';
  activeDocumentJobId = null;
  documentViewerAttachmentSet = null;
  if (documentViewerNavEl) documentViewerNavEl.hidden = true;
  document.dispatchEvent(new CustomEvent('dlyte:close-document'));
  documentViewerErrorEl.hidden = true;
  documentViewerErrorEl.textContent = '';
  documentViewerFrameWrapEl.hidden = false;
  showCaseOverviewView('overview');
  if (documentViewerReturnToPendingDocs) {
    documentViewerReturnToPendingDocs = false;
    openPendingDocumentsModal();
  }
}

async function openAllDocuments() {
  if (guardCropLockedAction('open-all-documents')) return;
  if (!caseOverviewContentEl || !documentsBrowserEl) return;
  await refreshAllDocuments();
  renderAllDocuments();
  showCaseOverviewView('documents');
}

function closeAllDocuments() {
  if (guardCropLockedAction('close-all-documents')) return;
  if (!caseOverviewContentEl || !documentsBrowserEl) return;
  showCaseOverviewView('overview');
}

function showCaseOverviewView(view) {
  document.documentElement.dataset.caseOverviewView = view;
  document.body.classList.toggle('photo-crop-editor-open', view === 'photo-crop');
  if (caseOverviewContentEl) caseOverviewContentEl.hidden = view !== 'overview';
  if (documentViewerEl) documentViewerEl.hidden = view !== 'document';
  if (documentsBrowserEl) documentsBrowserEl.hidden = view !== 'documents';
  if (photoCropEditorEl) photoCropEditorEl.hidden = view !== 'photo-crop';
}

function getCornerPercentStyle(point) {
  return `left:${point.x * 100}%;top:${point.y * 100}%;`;
}

function getCornerPolygonPoints(corners) {
  return corners.map((point) => `${point.x * 100},${point.y * 100}`).join(' ');
}

function cloneCorners(corners) {
  return (corners || []).map((point) => ({ x: Number(point.x), y: Number(point.y) }));
}

function openPhotoCropEditor(detail) {
  if (!photoCropEditorEl || !photoCropEditorTitleEl || !photoCropEditorImageEl || !photoCropEditorPolygonEl) return;
  if (!detail.pageId || !detail.originalUrl || !Array.isArray(detail.corners) || detail.corners.length !== 4) return;

  activePhotoCrop = {
    pageId: detail.pageId,
    filename: detail.filename || 'Photo page',
    detected: Boolean(detail.detected),
    originalUrl: detail.originalUrl,
    corners: cloneCorners(detail.corners),
  };

  photoCropEditorTitleEl.textContent = activePhotoCrop.filename;
  if (photoCropEditorCopyEl) {
    photoCropEditorCopyEl.textContent = activePhotoCrop.detected
      ? 'Auto-detected corners are loaded below. Drag them until the page is framed correctly, then confirm.'
      : 'Detection fell back to a broad crop. Drag the corners around the page, then confirm the crop.';
  }
  photoCropEditorImageEl.src = activePhotoCrop.originalUrl;
  renderPhotoCropEditor();
  showCaseOverviewView('photo-crop');
}

function renderPhotoCropEditor() {
  if (!activePhotoCrop || !photoCropEditorPolygonEl) return;
  photoCropEditorPolygonEl.setAttribute('points', getCornerPolygonPoints(activePhotoCrop.corners));
  for (const handleEl of photoCropHandleEls) {
    const index = Number(handleEl.dataset.cornerIndex);
    handleEl.style.cssText = getCornerPercentStyle(activePhotoCrop.corners[index]);
  }
}

function cancelPhotoCrop() {
  activePhotoCrop = null;
  activeCropHandle = null;
  if (photoCropEditorImageEl) photoCropEditorImageEl.src = '';
  showCaseOverviewView('overview');
}

function closePhotoCropEditor() {
  if (!photoCropEditorEl?.hidden) {
    cancelPhotoCrop();
  }
}

function confirmPhotoCrop() {
  if (!activePhotoCrop) return;
  if (!activePhotoCrop) return;
  document.dispatchEvent(
    new CustomEvent('dlyte:confirm-photo-crop-editor', {
      detail: {
        pageId: activePhotoCrop.pageId,
        corners: cloneCorners(activePhotoCrop.corners),
      },
    })
  );
}

function beginPhotoCropDrag(event) {
  if (!activePhotoCrop) return;
  event.preventDefault();
  if (event.currentTarget?.setPointerCapture) {
    event.currentTarget.setPointerCapture(event.pointerId);
  }
  activeCropHandle = {
    cornerIndex: Number(event.currentTarget.dataset.cornerIndex),
    pointerId: event.pointerId,
    handleEl: event.currentTarget,
  };
}

function updatePhotoCropDrag(event) {
  if (!activePhotoCrop || !activeCropHandle || !photoCropEditorStageEl) return;
  if (event.pointerId !== activeCropHandle.pointerId) return;
  const rect = photoCropEditorStageEl.getBoundingClientRect();
  const x = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
  const y = Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height));
  activePhotoCrop.corners[activeCropHandle.cornerIndex] = { x, y };
  renderPhotoCropEditor();
}

function endPhotoCropDrag(event) {
  if (!activeCropHandle) return;
  if (event?.pointerId != null && event.pointerId !== activeCropHandle.pointerId) return;
  const handle = activeCropHandle.handleEl;
  const pointerId = activeCropHandle.pointerId;
  activeCropHandle = null;
  if (handle?.releasePointerCapture) {
    try {
      handle.releasePointerCapture(pointerId);
    } catch {
      // Ignore if the browser already released pointer capture.
    }
  }
}

function renderAllDocuments() {
  if (!documentsBrowserListEl) return;
  if (!recentScans.length) {
    documentsBrowserListEl.innerHTML = '<div class="files-empty">No scanned documents yet</div>';
    return;
  }

  const cropLockedAttr = isPhotoCropEditorActive ? 'disabled aria-disabled="true"' : '';
  documentsBrowserListEl.innerHTML = recentScans.map((doc) => `
    <div class="file-row file-row-with-actions documents-browser-row">
      <button class="file-row-button documents-browser-open-btn" type="button" data-job-id="${escapeHtml(doc.job_id)}" data-filename="${escapeHtml(doc.filename)}" ${cropLockedAttr}>
        <span class="file-icon">📄</span>
        <span class="file-name">${escapeHtml(doc.filename)}</span>
        <span class="file-meta">${doc.page_count}p · ${formatScanDate(doc.scanned_at)}</span>
      </button>
      <button class="btn btn-remove file-row-rename-btn documents-browser-rename-btn" type="button" data-job-id="${escapeHtml(doc.job_id)}" data-filename="${escapeHtml(doc.filename)}" ${cropLockedAttr}>Rename</button>
      <button class="btn btn-remove file-row-delete-btn documents-browser-delete-btn" type="button" data-job-id="${escapeHtml(doc.job_id)}" data-filename="${escapeHtml(doc.filename)}" ${cropLockedAttr}>Delete</button>
    </div>
  `).join('');

  for (const row of documentsBrowserListEl.querySelectorAll('.documents-browser-open-btn')) {
    row.addEventListener('click', () => {
      if (guardCropLockedAction('open-documents-browser-row')) return;
      openDocumentViewer({
        job_id: row.dataset.jobId,
        filename: row.dataset.filename,
      });
    });
  }
  for (const button of documentsBrowserListEl.querySelectorAll('.documents-browser-rename-btn')) {
    button.addEventListener('click', async (event) => {
      event.stopPropagation();
      if (guardCropLockedAction('rename-documents-browser-row')) return;
      try {
        await renameLocalScannedDocument(button.dataset.jobId, button.dataset.filename);
      } catch (err) {
        if (updateCaseStatusEl) {
          updateCaseStatusEl.hidden = false;
          updateCaseStatusEl.textContent = err.message || 'Could not rename the scanned document.';
        }
      }
    });
  }
  for (const button of documentsBrowserListEl.querySelectorAll('.documents-browser-delete-btn')) {
    button.addEventListener('click', async (event) => {
      event.stopPropagation();
      if (guardCropLockedAction('delete-documents-browser-row')) return;
      try {
        await deleteLocalScannedDocument(button.dataset.jobId, button.dataset.filename);
      } catch (err) {
        if (updateCaseStatusEl) {
          updateCaseStatusEl.hidden = false;
          updateCaseStatusEl.textContent = err.message || 'Could not delete the local scanned document.';
        }
      }
    });
  }
}

function formatScanDate(unixSeconds) {
  try {
    return new Date(unixSeconds * 1000).toLocaleDateString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    });
  } catch {
    return '';
  }
}

async function refreshAllDocuments() {
  try {
    const res = await fetch('/api/files?limit=all');
    const data = await res.json();
    recentScans = data.files || [];
  } catch (err) {
    // Fall back to whatever recent scans were last rendered.
  }
}

async function deleteLocalScannedDocument(jobId, filename) {
  if (!jobId) return false;
  const confirmed = window.confirm(`Delete local scanned document "${filename || jobId}"? This removes the original file and OCR data from DLYTE only.`);
  if (!confirmed) return false;

  const res = await fetch(`/api/files/${encodeURIComponent(jobId)}/delete`, { method: 'POST' });
  const data = await res.json();
  if (!res.ok) {
    throw new Error(data.message || data.error || 'Could not delete the local scanned document.');
  }

  if (activeDocumentJobId === jobId) {
    closeDocumentViewer();
  }
  await Promise.all([refreshAllDocuments(), refreshDocumentRegistry(), refreshScanQueue()]);
  document.dispatchEvent(new CustomEvent('dlyte:recent-scans-updated', { detail: { files: recentScans } }));
  render();
  if (!documentsBrowserEl?.hidden) renderAllDocuments();
  if (updateCaseStatusEl) {
    updateCaseStatusEl.hidden = false;
    updateCaseStatusEl.textContent = data.message || 'Local scanned document deleted.';
  }
  return true;
}

async function renameLocalScannedDocument(jobId, currentFilename) {
  if (!jobId) return false;
  const currentBase = (currentFilename || '').replace(/\.[^./]+$/, '');
  const input = window.prompt('Rename document to:', currentBase);
  if (input === null) return false;
  const trimmed = input.trim();
  if (!trimmed) return false;

  const res = await fetch(`/api/files/${encodeURIComponent(jobId)}/rename`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename: trimmed }),
  });
  const data = await res.json();
  if (!res.ok) {
    throw new Error(data.error || 'Could not rename the scanned document.');
  }

  await Promise.all([refreshAllDocuments(), refreshDocumentRegistry()]);
  document.dispatchEvent(new CustomEvent('dlyte:recent-scans-updated', { detail: { files: recentScans } }));
  render();
  if (!documentsBrowserEl?.hidden) renderAllDocuments();
  if (updateCaseStatusEl) {
    updateCaseStatusEl.hidden = false;
    updateCaseStatusEl.textContent = `Renamed to "${data.filename || trimmed}".`;
  }
  return true;
}

function openPendingDocumentsModal() {
  openDocsModal(pendingDocs, {
    onOpenEmail: openSpecificEmail,
    onRecheck: recheckDocuments,
    onConfirm: confirmForScan,
    onDismiss: dismissDocuments,
    onDeleteLocal: async (doc) => {
      try {
        const deleted = await deleteLocalScannedDocument(doc.ocrJobId, doc.filename);
        if (!deleted) return;
        pendingDocs = pendingDocs.filter((entry) => entry.id !== doc.id);
        openPendingDocumentsModal();
      } catch (err) {
        if (updateCaseStatusEl) {
          updateCaseStatusEl.hidden = false;
          updateCaseStatusEl.textContent = err.message || 'Could not delete the local scanned document.';
        }
      }
    },
    onPreview: (doc) => {
      if (guardCropLockedAction('preview-pending-doc')) return;
      closeDocsModal();
      documentViewerReturnToPendingDocs = true;
      if (doc.ocrJobId) {
        openDocumentViewer({ job_id: doc.ocrJobId, filename: doc.filename });
      } else if (doc.emailId && doc.attachmentId) {
        openDocumentViewer({
          messageId: doc.emailId,
          attachmentId: doc.attachmentId,
          attachmentIds: doc.attachmentIds,
          filename: doc.filename,
          filenames: doc.filenames,
        });
      } else {
        documentViewerReturnToPendingDocs = false;
      }
    },
  });
}

async function handleUpdateCase() {
  if (!updateCaseBtnEl) return;
  if (guardCropLockedAction('update-case')) return;
  updateCaseBtnEl.disabled = true;
  updateCaseStatusEl.hidden = false;

  const before = { matched: docCounts.matched, confirmed: 0 };
  try {
    const eventsBeforeRes = await fetch('/api/chronology/events');
    before.confirmed = ((await eventsBeforeRes.json()).events || []).length;
  } catch (err) {
    // Non-fatal — the summary just won't show a chronology delta.
  }

  updateCaseBtnEl.textContent = 'Scanning Gmail…';
  let newCandidates = 0;
  try {
    const res = await fetch('/api/gmail/scan', { method: 'POST' });
    const data = await res.json();
    if (res.ok) newCandidates = data.new_candidates || 0;
  } catch (err) {
    // Continue to the next step even if this one fails.
  }

  updateCaseBtnEl.textContent = 'Checking documents…';
  try {
    await fetch('/api/documents/recheck', { method: 'POST' });
  } catch (err) {
    // Continue regardless.
  }

  updateCaseBtnEl.textContent = 'Extracting chronology…';
  let chronoResult = null;
  try {
    const res = await fetch('/api/chronology/extract', { method: 'POST' });
    chronoResult = await res.json();
  } catch (err) {
    // Continue — the summary just won't include chronology numbers.
  }

  await Promise.all([refreshEmailStatus(), refreshDocumentRegistry(), refreshScanQueue(), refreshChronoCount()]);
  render();

  const matchedDelta = Math.max(0, docCounts.matched - before.matched);
  const parts = [];
  parts.push(newCandidates === 1 ? '1 new email to review' : `${newCandidates} new emails to review`);
  parts.push(matchedDelta === 1 ? '1 document matched' : `${matchedDelta} documents matched`);
  if (docCounts.pendingPhotoReview) {
    parts.push(
      docCounts.pendingPhotoReview === 1
        ? '1 photo document needs review'
        : `${docCounts.pendingPhotoReview} photo documents need review`
    );
  }
  if (chronoResult) {
    parts.push(
      chronoResult.eventsCreatedConfirmed === 1
        ? '1 new chronology event'
        : `${chronoResult.eventsCreatedConfirmed} new chronology events`
    );
    if (chronoResult.eventsCreatedReview) {
      parts.push(`${chronoResult.eventsCreatedReview} need review`);
    }
  }

  updateCaseStatusEl.textContent = parts.join(' · ');
  updateCaseBtnEl.disabled = isPhotoCropEditorActive;
  updateCaseBtnEl.textContent = 'Update case';
  if (!isPhotoCropEditorActive) syncGlobalPhotoReviewLocks();
}

async function refreshChronoCount() {
  try {
    chronoReviewCount = await refreshChronologyOverview();
  } catch (err) {
    chronoReviewCount = 0;
  }
}

async function refreshEmailStatus() {
  try {
    const res = await fetch('/api/gmail/status');
    const data = await res.json();
    emailPendingCount = data.pending_count || 0;
  } catch (err) {
    emailPendingCount = 0;
  }
}

async function refreshDocumentRegistry() {
  try {
    const res = await fetch('/api/documents/registry');
    const data = await res.json();
    const entries = Array.isArray(data.entries) ? data.entries : [];
    pendingDocs = getPendingDocumentEntries(entries);
    docCounts = summarizeDocumentCounts(entries, data.counts || {});
  } catch (err) {
    docCounts = { matched: 0, pendingScan: 0, pendingPhotoReview: 0, unresolved: 0 };
    pendingDocs = [];
  }
}

async function refreshScanQueue() {
  try {
    const res = await fetch('/api/documents/queue');
    const data = await res.json();
    scanQueue = data.queue || [];
  } catch (err) {
    scanQueue = [];
  }
  syncGlobalPhotoReviewLocks();
}

async function recheckDocuments() {
  try {
    const res = await fetch('/api/documents/recheck', { method: 'POST' });
    const data = await res.json();
    const entries = Array.isArray(data.entries) ? data.entries : [];
    pendingDocs = getPendingDocumentEntries(entries);
    docCounts = summarizeDocumentCounts(entries, data.counts || docCounts);
    render();
  } catch (err) {
    // Leave the existing list as-is if the recheck fails.
  }
  return pendingDocs;
}

async function dismissDocuments(ids) {
  try {
    const res = await fetch('/api/documents/dismiss', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids }),
    });
    const data = await res.json();
    const entries = Array.isArray(data.entries) ? data.entries : [];
    pendingDocs = getPendingDocumentEntries(entries);
    docCounts = summarizeDocumentCounts(entries, data.counts || docCounts);
    render();
  } catch (err) {
    // Leave state as-is if this fails; the modal will just still show the item.
  }
  return pendingDocs;
}

async function confirmForScan(ids) {
  try {
    const res = await fetch('/api/documents/queue', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids }),
    });
    const data = await res.json();
    scanQueue = data.queue || scanQueue;
    // Confirmed items are no longer undecided — drop them from the pending list too.
    const queuedIds = new Set(ids);
    pendingDocs = pendingDocs.filter((d) => !queuedIds.has(d.id));
    render();
  } catch (err) {
    // Leave state as-is if this fails; the modal will just show the item as still pending.
  }
}

async function handleRunScan() {
  if (guardCropLockedAction('run-scan')) return;
  if (!scanQueue.length || !runScanBtnEl) return;
  runScanBtnEl.disabled = true;
  runScanBtnEl.textContent = 'Scanning…';
  if (updateCaseStatusEl) {
    updateCaseStatusEl.hidden = false;
    updateCaseStatusEl.textContent = 'Running scan queue…';
  }

  try {
    const res = await fetch('/api/documents/run-scan', { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Could not run the scan queue.');
    docCounts = data.counts || docCounts;
    await Promise.all([refreshScanQueue(), refreshDocumentRegistry()]);
    if ((data.succeeded || []).length > 0) {
      // The "Recent scans" mini-preview (#filesList) is owned and rendered
      // by chat.js's refreshFilesList — this scan flow has no other way to
      // tell it a new document just landed, so without this it silently
      // goes stale until something else in chat.js happens to refresh it.
      document.dispatchEvent(new CustomEvent('dlyte:documents-scanned'));
    }
    if (updateCaseStatusEl) {
      const succeeded = (data.succeeded || []).length;
      const failed = (data.failed || []).length;
      updateCaseStatusEl.hidden = false;
      updateCaseStatusEl.textContent = failed ? `${succeeded} scanned · ${failed} failed` : `${succeeded} scanned`;
    }
  } catch (err) {
    if (updateCaseStatusEl) {
      updateCaseStatusEl.hidden = false;
      updateCaseStatusEl.textContent = err.message || 'Could not run the scan queue.';
    }
  } finally {
    runScanBtnEl.textContent = 'Run scan';
    syncGlobalPhotoReviewLocks();
    render();
  }
}

function render() {
  syncGlobalPhotoReviewLocks();
  renderScanQueueBlock();
  renderAttentionList();
}

function renderScanQueueBlock() {
  const block = document.getElementById('scanQueueBlock');
  const countEl = document.getElementById('scanQueueCount');
  const listEl = document.getElementById('scanQueueList');
  if (!block || !countEl || !listEl) return;

  if (scanQueue.length > 0) {
    block.hidden = false;
    countEl.textContent =
      scanQueue.length === 1 ? '1 document to be scanned' : `${scanQueue.length} documents to be scanned`;
    listEl.innerHTML = scanQueue
      .map(
        (item) => `
          <div class="file-row">
            <span class="file-icon">📄</span>
            <span class="file-name">${escapeHtml(item.filename || '(unnamed file)')}</span>
          </div>
        `
      )
      .join('');
  } else {
    block.hidden = true;
    listEl.innerHTML = '';
  }
}

function renderAttentionList() {
  const list = document.getElementById('attentionList');
  if (!list) return;
  list.innerHTML = '';

  if (emailPendingCount > 0) {
    list.appendChild(
      buildRow(
        emailPendingCount === 1 ? 'One email needs your review' : `${emailPendingCount} emails need your review`,
        'Flagged as likely related to your case — confirm which ones to save.',
        () => {
          if (guardCropLockedAction('open-email-review')) return;
          openReviewFlow();
        }
      )
    );
  }

  const docsNeeding =
    pendingDocs.length ||
    docCounts.pendingScan + docCounts.pendingPhotoReview + docCounts.unresolved;
  if (docsNeeding > 0) {
    list.appendChild(
      buildRow(
        docsNeeding === 1 ? 'One document needs attention' : `${docsNeeding} documents need attention`,
        'Referenced by case emails but not yet scanned, still need photographed-page review, or found locally with no matching email.',
        () => {
          if (guardCropLockedAction('open-documents-modal')) return;
          openPendingDocumentsModal();
        }
      )
    );
  }

  if (chronoReviewCount > 0) {
    list.appendChild(
      buildRow(
        chronoReviewCount === 1 ? 'One chronology event needs review' : `${chronoReviewCount} chronology events need review`,
        'Uncertain, incomplete, or conflicting events found in your case emails and documents.',
        () => {
          if (guardCropLockedAction('open-chronology-review')) return;
          openChronologyReview();
        }
      )
    );
  }
}

function buildRow(flag, copy, onClick) {
  const row = document.createElement('div');
  row.className = 'attention' + (isPhotoCropEditorActive ? ' attention-disabled' : '');
  row.setAttribute('aria-disabled', isPhotoCropEditorActive ? 'true' : 'false');
  row.innerHTML = `
    <span class="attention-flag">${flag}</span>
    <p class="attention-copy">${copy}</p>
  `;
  row.addEventListener('click', () => {
    if (guardCropLockedAction('click-attention-row')) return;
    onClick();
  });
  return row;
}

document.addEventListener('DOMContentLoaded', init);
