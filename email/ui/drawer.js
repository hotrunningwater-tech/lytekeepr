import * as state from '../state.js';
import { renderEmailList } from './emailList.js';
import { renderEmailView } from './emailView.js';
import { renderGmailBar } from './gmailBar.js';
import { renderCandidateReview } from './candidateReview.js';

const cropLock = window.DLYTECropLock || {
  isActive() { return false; },
  getMessage() { return 'Finish or cancel crop editing first.'; },
  guard() { return false; },
};

let panelEl,
  backdropEl,
  tabInboxEl,
  tabDlyteEl,
  listContainerEl,
  detailContainerEl,
  candidateContainerEl,
  actionBarEl,
  toastEl,
  statusEl,
  gmailBarEl;
let isOpen = false;
let showingCandidates = false;

function isCropLockActive() {
  return Boolean(cropLock.isActive());
}

function getCropLockMessage() {
  return cropLock.getMessage();
}

function guardCropLockedAction(action) {
  return cropLock.guard(action);
}

// Gmail connection/scan status is separate from the mock-inbox state in
// state.js — it reflects the server-side OAuth token and local scan-state,
// not the mock/gmail email records themselves.
let gmail = {
  status: 'idle', // 'idle' | 'connecting' | 'connected' | 'error'
  errorMessage: '',
  synced: false,
  syncing: false,
  messageCount: 0,
  lastSyncAt: null,
  accountEmail: null,
  pendingCount: 0,
  activeAccountId: null,
  knownAccounts: [],
  syncSinceDate: null,
  scanProgress: null,
};

function getShellProductName() {
  return document.documentElement.dataset.shellProductName || 'LyteKeepr';
}

function updateBrandTabLabel() {
  const labelEl = panelEl?.querySelector('[data-email-brand-tab-label]');
  if (labelEl) labelEl.textContent = getShellProductName();
}

export async function initEmailDrawer() {
  buildDom();
  wireStaticEvents();
  syncCropLockUi();
  renderGmailStatus();
  state.subscribe(render);

  await setupGmailStatus();
  state.init();
  render(state.getState());
}

// Entry points for other modules (case-overview's attention rows) — the
// email drawer owns its own DOM/state, so this is the only supported way
// in from outside.
export function openReviewFlow() {
  if (guardCropLockedAction('email-open-review-flow')) return;
  open();
  openCandidateReview();
}

export async function openSpecificEmail(emailId) {
  if (guardCropLockedAction('email-open-specific-email')) return;
  open();
  showingCandidates = false;
  const opened = await state.ensureEmailOpen(emailId);
  if (opened) return;

  await setupGmailStatus();
  await state.ensureEmailOpen(emailId);
}

async function setupGmailStatus() {
  const params = new URLSearchParams(window.location.search);
  const gmailParam = params.get('gmail');

  if (gmailParam === 'error') {
    gmail = { ...gmail, status: 'error', errorMessage: params.get('reason') || 'Something went wrong.' };
    state.useEmptyProvider();
    renderGmailStatus();
    cleanUpGmailQueryParam();
    return;
  }

  // gmailParam === 'connected' just means OAuth succeeded — fall through to
  // the real status check below so we also learn scan state/message count.
  try {
    const res = await fetch('/api/gmail/status');
    const data = await res.json();
    applyGmailStatus(data);
  } catch (err) {
    state.useEmptyProvider();
  }

  renderGmailStatus();
  if (gmailParam) cleanUpGmailQueryParam();
}

function renderGmailStatus() {
  renderGmailBar(gmailBarEl, gmail, {
    onScanClick: handleScanClick,
    onReviewClick: openCandidateReview,
    onConnectClick: handleConnectGmailClick,
    onActivateAccount: handleActivateAccount,
    onDemoModeChange: renderGmailStatus,
    onSinceDateChange: handleSinceDateChange,
  });
}

function handleSinceDateChange(value) {
  gmail = { ...gmail, syncSinceDate: value || null };
  // No re-render here: the date input already shows the user's edit, and
  // re-rendering would blow away its focus/cursor mid-typing.
}

function cleanUpGmailQueryParam() {
  const url = new URL(window.location.href);
  url.searchParams.delete('gmail');
  url.searchParams.delete('reason');
  window.history.replaceState({}, '', url);
}

let scanProgressTimer = null;

function startScanProgressPolling() {
  stopScanProgressPolling();
  scanProgressTimer = setInterval(async () => {
    try {
      const res = await fetch('/api/gmail/scan/progress');
      const progress = await res.json();
      gmail = { ...gmail, scanProgress: progress };
      renderGmailStatus();
    } catch (err) {
      // Transient poll failure — the next tick (or the scan request's own
      // completion) will resolve the UI either way; nothing to surface here.
    }
  }, 1200);
}

function stopScanProgressPolling() {
  if (scanProgressTimer) {
    clearInterval(scanProgressTimer);
    scanProgressTimer = null;
  }
}

async function handleScanClick() {
  if (guardCropLockedAction('email-scan-gmail')) return;
  gmail = { ...gmail, syncing: true, scanProgress: null };
  renderGmailStatus();
  startScanProgressPolling();

  try {
    const res = await fetch('/api/gmail/scan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ since: gmail.syncSinceDate || null }),
    });
    const data = await res.json();
    if (!res.ok) {
      gmail = { ...gmail, syncing: false, status: 'error', errorMessage: data.error || 'Scan failed.' };
      renderGmailStatus();
      return;
    }
    await setupGmailStatus();
    state.refresh();
    if (data.total_pending > 0) openCandidateReview();
  } catch (err) {
    gmail = { ...gmail, syncing: false, status: 'error', errorMessage: 'Could not reach the server.' };
    renderGmailStatus();
  } finally {
    stopScanProgressPolling();
  }
}

function handleConnectGmailClick() {
  window.location.href = (window.LK_PREFIX || '') + '/auth/google/start';
}

async function handleActivateAccount(accountId) {
  if (guardCropLockedAction('email-activate-gmail-account')) return;
  if (!accountId || accountId === gmail.activeAccountId) return;

  try {
    const res = await fetch('/api/gmail/accounts/activate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ account_id: accountId }),
    });
    const data = await res.json();
    if (!res.ok) {
      gmail = { ...gmail, status: 'error', errorMessage: data.error || 'Could not switch Gmail account.' };
      renderGmailStatus();
      return;
    }
    showingCandidates = false;
    applyGmailStatus(data);
    state.setView('inbox');
    renderGmailStatus();
    await state.refresh();
  } catch (err) {
    gmail = { ...gmail, status: 'error', errorMessage: 'Could not switch Gmail account.' };
    renderGmailStatus();
  }
}

function applyGmailStatus(data) {
  // Keep whatever the user already picked in the date field across status
  // refreshes (e.g. after a scan) — only fall back to the server's .env
  // default the first time, before the user has touched the picker.
  const syncSinceDate = gmail.syncSinceDate ?? data.default_sync_since_date ?? null;
  gmail = {
    status: data.connected ? 'connected' : data.active_account_id ? 'profile' : 'idle',
    errorMessage: '',
    synced: Boolean(data.synced),
    syncing: false,
    messageCount: data.message_count || 0,
    lastSyncAt: data.last_sync_at || null,
    accountEmail: data.account_email || null,
    pendingCount: data.pending_count || 0,
    activeAccountId: data.active_account_id || null,
    knownAccounts: data.known_accounts || [],
    syncSinceDate,
  };
  if (data.connected || data.synced || data.active_account_id) {
    state.useGmailProvider();
  } else {
    state.useEmptyProvider();
  }
}

async function handleRefreshClick() {
  if (guardCropLockedAction('email-refresh-drawer')) return;
  if (gmail.status === 'connected') {
    await handleScanClick();
    return;
  }
  state.refresh();
}

async function openCandidateReview() {
  if (guardCropLockedAction('email-open-candidate-review')) return;
  showingCandidates = true;
  renderDrawerBody(state.getState());
  await refreshCandidateReview();
}

async function refreshCandidateReview() {
  try {
    const res = await fetch('/api/gmail/candidates');
    const data = await res.json();
    const candidates = data.candidates || [];
    if (!candidates.length) {
      // Nothing left to review this round — close instead of showing an
      // empty panel, so finishing a review pass doesn't need an extra click.
      closeCandidateReview();
      return;
    }
    renderCandidateReview(candidateContainerEl, candidates, {
      onSave: handleSaveCandidates,
      onDismiss: handleDismissCandidates,
    });
  } catch (err) {
    candidateContainerEl.innerHTML = '<div class="email-status error">Could not load candidates.</div>';
  }
}

function closeCandidateReview() {
  showingCandidates = false;
  renderDrawerBody(state.getState());
}

async function handleSaveCandidates(ids) {
  if (guardCropLockedAction('email-save-candidates')) return;
  try {
    const res = await fetch('/api/gmail/save-candidates', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids }),
    });
    const data = await res.json();
    if (res.ok) {
      gmail = {
        ...gmail,
        messageCount: data.state.messageCount,
        pendingCount: Math.max(0, gmail.pendingCount - (data.saved + data.dismissed)),
      };
      renderGmailStatus();
      state.useGmailProvider();
      state.refresh();
      // Case Overview (documents/chronology counts) only refreshes itself
      // when it hears this event — without it, newly-saved docs never show
      // up there until an unrelated full page reload happens to occur.
      if (data.saved > 0) {
        document.dispatchEvent(new CustomEvent('dlyte:case-emails-saved', { detail: { ids } }));
      }
    }
  } finally {
    // Stay open on the remaining pending items instead of closing — lets a
    // review pass save some, then keep going without reopening from scratch.
    await refreshCandidateReview();
  }
}

async function handleDismissCandidates(ids) {
  if (guardCropLockedAction('email-dismiss-candidates')) return;
  try {
    const res = await fetch('/api/gmail/save-candidates', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: [], dismissed_ids: ids }),
    });
    const data = await res.json();
    if (res.ok) {
      gmail = {
        ...gmail,
        pendingCount: Math.max(0, gmail.pendingCount - (data.saved + data.dismissed)),
      };
      renderGmailStatus();
      // Dismissing changes the pending-review count Case Overview shows
      // too, even though nothing new was saved.
      document.dispatchEvent(new CustomEvent('dlyte:case-emails-saved', { detail: { ids: [] } }));
    }
  } finally {
    await refreshCandidateReview();
  }
}

function buildDom() {
  backdropEl = document.createElement('div');
  backdropEl.className = 'email-backdrop';
  backdropEl.hidden = true;

  panelEl = document.createElement('aside');
  panelEl.className = 'email-panel';
  panelEl.innerHTML = `
    <button class="email-collapse-btn" type="button" aria-label="Collapse">&lsaquo;</button>
    <div class="email-panel-header">
      <h2>Email</h2>
      <div class="email-panel-header-actions">
        <button class="email-refresh-btn" type="button" title="Refresh">&#8635;</button>
      </div>
    </div>
    <div class="email-gmail-bar-container"></div>
    <div class="email-body">
      <div class="email-tabs-vertical">
        <button class="email-tab active" data-view="inbox" type="button"><span class="email-tab-label">INBOX</span></button>
        <button class="email-tab" data-view="dlyte" type="button"><span class="email-tab-label" data-email-brand-tab-label>${getShellProductName()}</span></button>
      </div>
      <div class="email-main">
        <div class="email-status" hidden></div>
        <div class="email-action-bar" hidden></div>
        <div class="candidate-container" hidden></div>
        <div class="email-list-container"></div>
        <div class="email-detail-container" hidden></div>
      </div>
    </div>
    <div class="email-toast" hidden></div>
  `;

  document.body.appendChild(backdropEl);
  document.body.appendChild(panelEl);

  tabInboxEl = panelEl.querySelector('[data-view="inbox"]');
  tabDlyteEl = panelEl.querySelector('[data-view="dlyte"]');
  gmailBarEl = panelEl.querySelector('.email-gmail-bar-container');
  statusEl = panelEl.querySelector('.email-status');
  actionBarEl = panelEl.querySelector('.email-action-bar');
  candidateContainerEl = panelEl.querySelector('.candidate-container');
  listContainerEl = panelEl.querySelector('.email-list-container');
  detailContainerEl = panelEl.querySelector('.email-detail-container');
  toastEl = panelEl.querySelector('.email-toast');
  updateBrandTabLabel();
}

function wireStaticEvents() {
  document.addEventListener('dlyte:shell-changed', updateBrandTabLabel);
  const emailTab = document.getElementById('emailTab');
  if (emailTab) {
    emailTab.addEventListener('click', () => {
      if (guardCropLockedAction('email-open-drawer')) return;
      open();
    });
  }

  const blockIfCropLocked = (event, action) => {
    if (!isCropLockActive()) return;
    guardCropLockedAction(action);
    event.preventDefault();
    event.stopPropagation();
    if (typeof event.stopImmediatePropagation === 'function') {
      event.stopImmediatePropagation();
    }
  };

  if (emailTab) {
    emailTab.addEventListener('click', (event) => blockIfCropLocked(event, 'email-tab-capture'), true);
    emailTab.addEventListener('pointerdown', (event) => blockIfCropLocked(event, 'email-tab-pointerdown'), true);
  }
  backdropEl.addEventListener('click', (event) => blockIfCropLocked(event, 'email-backdrop-capture'), true);
  panelEl.addEventListener('click', (event) => blockIfCropLocked(event, 'email-panel-capture'), true);
  panelEl.addEventListener('pointerdown', (event) => blockIfCropLocked(event, 'email-panel-pointerdown'), true);

  backdropEl.addEventListener('click', () => {
    if (guardCropLockedAction('email-close-backdrop')) return;
    close();
  });
  panelEl.querySelector('.email-collapse-btn').addEventListener('click', () => {
    if (guardCropLockedAction('email-close-button')) return;
    close();
  });
  panelEl.querySelector('.email-refresh-btn').addEventListener('click', handleRefreshClick);

  tabInboxEl.addEventListener('click', () => {
    showingCandidates = false;
    state.setView('inbox');
  });
  tabDlyteEl.addEventListener('click', () => {
    showingCandidates = false;
    state.setView('dlyte');
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && isOpen) {
      if (guardCropLockedAction('email-close-escape')) return;
      close();
    }
  });
}

function open() {
  if (guardCropLockedAction('email-open-drawer-internal')) return;
  isOpen = true;
  backdropEl.hidden = false;
  panelEl.classList.add('open');
}

function close() {
  if (guardCropLockedAction('email-close-drawer-internal')) return;
  isOpen = false;
  panelEl.classList.remove('open');
  backdropEl.hidden = true;
}

function forceCloseForCropLock() {
  isOpen = false;
  panelEl.classList.remove('open');
  backdropEl.hidden = true;
}

function syncCropLockUi() {
  const emailTab = document.getElementById('emailTab');
  const locked = isCropLockActive();
  if (emailTab) {
    emailTab.setAttribute('aria-disabled', locked ? 'true' : 'false');
    emailTab.classList.toggle('email-drawer-disabled', locked);
  }
  if (panelEl) {
    panelEl.classList.toggle('email-panel-locked', locked);
  }
  if (backdropEl) {
    backdropEl.classList.toggle('email-backdrop-locked', locked);
  }
}

function render(s) {
  tabInboxEl.classList.toggle('active', !showingCandidates && s.view === 'inbox');
  tabDlyteEl.classList.toggle('active', !showingCandidates && s.view === 'dlyte');
  renderDrawerBody(s);
}

function renderDrawerBody(s) {
  const locked = isCropLockActive();
  const refreshBtn = panelEl?.querySelector('.email-refresh-btn');
  if (refreshBtn) refreshBtn.disabled = locked;
  if (tabInboxEl) tabInboxEl.disabled = locked;
  if (tabDlyteEl) tabDlyteEl.disabled = locked;
  if (s.status === 'loading') {
    statusEl.hidden = false;
    statusEl.className = 'email-status loading';
    statusEl.textContent = 'Loading inbox…';
  } else if (s.status === 'error') {
    statusEl.hidden = false;
    statusEl.className = 'email-status error';
    statusEl.textContent = s.errorMessage;
  } else {
    statusEl.hidden = true;
  }

  if (locked) {
    statusEl.hidden = false;
    statusEl.className = 'email-status loading';
    statusEl.textContent = getCropLockMessage();
  }

  if (showingCandidates) {
    listContainerEl.hidden = true;
    detailContainerEl.hidden = true;
    actionBarEl.hidden = true;
    candidateContainerEl.hidden = false;
    return;
  }
  candidateContainerEl.hidden = true;

  const openedEmail = s.openedId ? s.emails.find((e) => e.id === s.openedId) : null;

  if (openedEmail) {
    listContainerEl.hidden = true;
    actionBarEl.hidden = true;
    detailContainerEl.hidden = false;
    renderEmailView(detailContainerEl, openedEmail);
  } else {
    detailContainerEl.hidden = true;
    listContainerEl.hidden = false;

    const visible = s.view === 'dlyte' ? state.getDlyteEmails() : state.getInboxEmails();
    listContainerEl.dataset.emptyMessage =
      s.view === 'dlyte'
        ? 'No emails saved yet — open an email and save it.'
        : 'No emails in your inbox.';
    renderEmailList(listContainerEl, visible);

    if (s.selectedIds.size > 0) {
      const selectedEmails = [...s.selectedIds]
        .map((id) => s.emails.find((email) => email.id === id))
        .filter(Boolean);
      const canMarkSelected = selectedEmails.some((email) => !email.labels.includes('dlyte'));
      actionBarEl.hidden = false;
      actionBarEl.innerHTML = `
        <span class="email-selected-count">${s.selectedIds.size} selected</span>
        <div class="email-action-buttons">
          ${canMarkSelected ? `<button class="btn btn-dlyte" id="bulkMarkBtn" type="button" ${locked ? 'disabled aria-disabled="true"' : ''}>Save</button>` : ''}
          <button class="btn btn-remove" id="bulkArchiveBtn" type="button" ${locked ? 'disabled aria-disabled="true"' : ''}>Archive</button>
          <button class="btn btn-remove" id="bulkJunkBtn" type="button" ${locked ? 'disabled aria-disabled="true"' : ''}>Not relevant</button>
        </div>
      `;
      const ids = [...s.selectedIds];
      actionBarEl.querySelector('#bulkMarkBtn')?.addEventListener('click', () => state.markCaseRelated(ids));
      actionBarEl.querySelector('#bulkArchiveBtn').addEventListener('click', () => state.archiveEmails(ids));
      actionBarEl.querySelector('#bulkJunkBtn').addEventListener('click', () => state.junkEmails(ids));
    } else {
      actionBarEl.hidden = true;
    }
  }

  if (s.toast) {
    toastEl.hidden = false;
    toastEl.textContent = s.toast;
  } else {
    toastEl.hidden = true;
  }
}

document.addEventListener('dlyte:photo-crop-lock-changed', () => {
  if (isCropLockActive()) forceCloseForCropLock();
  syncCropLockUi();
  renderGmailStatus();
  renderDrawerBody(state.getState());
});
