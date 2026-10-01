/**
 * Gmail connection + scan banner. Purely presentational — all OAuth and
 * Gmail scan/classify logic lives server-side; this only navigates the
 * browser to /auth/google/start for connecting, and calls back into
 * drawer.js (via the handlers param) for Scan Gmail / Review candidates,
 * which need to coordinate with state.js's provider switch.
 */

import { escapeHtml, formatRelative } from '../utils.js';
import { getMaskedAccountLabel, isDemoModeEnabled, toggleDemoMode } from './demoMode.js';

function formatScanProgress(progress) {
  if (!progress || progress.phase === 'idle') return 'Scanning…';
  if (progress.phase === 'fetching') {
    const { current = 0, total = 0, receivedAt } = progress;
    const dateBit = receivedAt
      ? ` — checking ${new Date(receivedAt).toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' })}`
      : '';
    return total ? `Scanning… ${current}/${total}${dateBit}` : 'Scanning…';
  }
  if (progress.phase === 'classifying') {
    const total = progress.total || 0;
    return total ? `Checking ${total} email${total === 1 ? '' : 's'} for case relevance…` : 'Checking for case relevance…';
  }
  return 'Scanning…';
}

function buildAccountSwitcher(knownAccounts, activeAccountId, cropLocked, demoModeEnabled) {
  if (!Array.isArray(knownAccounts) || knownAccounts.length < 2) return '';

  return `
    <div class="email-gmail-switcher">
      <select class="email-gmail-select" id="gmailAccountSelect" ${cropLocked ? 'disabled aria-disabled="true"' : ''}>
        ${knownAccounts
          .map((account, index) => `
            <option value="${escapeHtml(account.account_id)}" ${account.account_id === activeAccountId ? 'selected' : ''}>
              ${escapeHtml(demoModeEnabled ? getMaskedAccountLabel(account.account_id, index) : account.account_id)}
            </option>
          `)
          .join('')}
      </select>
      <button class="btn btn-remove" id="switchGmailAccountBtn" type="button" ${cropLocked ? 'disabled aria-disabled="true"' : ''}>Switch</button>
    </div>
  `;
}

export function renderGmailBar(container, gmailState, handlers = {}) {
  const { status, errorMessage, synced, syncing, messageCount, lastSyncAt, accountEmail, pendingCount, activeAccountId, knownAccounts, syncSinceDate, scanProgress } = gmailState;
  const cropLock = window.DLYTECropLock || {
    isActive() { return false; },
    guard() { return false; },
  };
  const cropLocked = Boolean(cropLock.isActive());
  const demoModeEnabled = isDemoModeEnabled();
  container.innerHTML = '';

  const bar = document.createElement('div');
  bar.className = 'email-gmail-bar';

  if (status === 'connected') {
    const metaLine = syncing
      ? formatScanProgress(scanProgress)
      : synced
        ? `${messageCount} saved · scanned ${formatRelative(lastSyncAt)}`
        : 'Not scanned yet';
    const switcherMarkup = buildAccountSwitcher(knownAccounts, activeAccountId, cropLocked, demoModeEnabled);

    bar.innerHTML = `
      <div class="email-gmail-info">
        <span class="email-gmail-text email-gmail-connected">Gmail connected${accountEmail ? demoModeEnabled ? ' — <span class="email-gmail-mask" aria-hidden="true"></span>' : ` — ${escapeHtml(accountEmail)}` : ''}</span>
        <span class="email-gmail-meta">${escapeHtml(metaLine)}</span>
      </div>
      <div class="email-gmail-actions">
        ${switcherMarkup}
        <label class="email-gmail-since" for="gmailSinceDate" title="Only scan mail received on or after this date">
          Since
          <input
            type="date"
            id="gmailSinceDate"
            class="email-gmail-since-input"
            value="${escapeHtml(syncSinceDate || '')}"
            ${(syncing || cropLocked) ? 'disabled aria-disabled="true"' : ''}
          >
        </label>
        <button
          class="email-demo-toggle-btn${demoModeEnabled ? ' active' : ''}"
          id="toggleDemoModeBtn"
          type="button"
          aria-label="Toggle account masking"
          title="Toggle account masking"
        ><span class="email-demo-toggle-knob" aria-hidden="true"></span></button>
        <button class="btn btn-remove" id="connectGmailBtn" type="button" ${cropLocked ? 'disabled aria-disabled="true"' : ''}>Connect another</button>
        <button class="btn btn-dlyte" id="scanGmailBtn" type="button" ${(syncing || cropLocked) ? 'disabled aria-disabled="true"' : ''}>
          ${syncing ? 'Scanning…' : synced ? 'Scan for new mail' : 'Scan Gmail'}
        </button>
      </div>
    `;
    const select = bar.querySelector('#gmailAccountSelect');
    const switchBtn = bar.querySelector('#switchGmailAccountBtn');
    if (select && switchBtn && handlers.onActivateAccount) {
      switchBtn.addEventListener('click', () => handlers.onActivateAccount(select.value));
    }
    const sinceInput = bar.querySelector('#gmailSinceDate');
    if (sinceInput && handlers.onSinceDateChange) {
      sinceInput.addEventListener('change', () => handlers.onSinceDateChange(sinceInput.value));
    }
    const scanBtn = bar.querySelector('#scanGmailBtn');
    if (scanBtn && handlers.onScanClick) {
      scanBtn.addEventListener('click', handlers.onScanClick);
    }
    const demoToggleBtn = bar.querySelector('#toggleDemoModeBtn');
    if (demoToggleBtn) {
      demoToggleBtn.addEventListener('click', () => {
        toggleDemoMode();
        handlers.onDemoModeChange?.();
      });
    }
    const connectBtn = bar.querySelector('#connectGmailBtn');
    if (connectBtn) {
      connectBtn.addEventListener('click', () => {
        if (cropLock.guard('gmail-connect-another')) return;
        connectBtn.disabled = true;
        connectBtn.textContent = 'Connecting…';
        handlers.onConnectClick?.();
      });
    }
    container.appendChild(bar);

    if (pendingCount > 0) {
      const review = document.createElement('div');
      review.className = 'email-gmail-review-banner';
      review.innerHTML = `
        <span>${pendingCount} email${pendingCount === 1 ? '' : 's'} flagged as likely related to your case</span>
        <button class="btn btn-dlyte" id="reviewCandidatesBtn" type="button" ${cropLocked ? 'disabled aria-disabled="true"' : ''}>Review</button>
      `;
      review.querySelector('#reviewCandidatesBtn').addEventListener('click', () => handlers.onReviewClick?.());
      container.appendChild(review);
    }
    return;
  }

  if (status === 'profile') {
    const metaLine = synced
      ? `${messageCount} saved · scanned ${formatRelative(lastSyncAt)}`
      : 'Saved profile ready · connect Gmail to scan live mail';
    const switcherMarkup = buildAccountSwitcher(knownAccounts, activeAccountId, cropLocked, demoModeEnabled);

    bar.innerHTML = `
      <div class="email-gmail-info">
        <span class="email-gmail-text email-gmail-connected">Saved Gmail profile${accountEmail ? demoModeEnabled ? ' — <span class="email-gmail-mask" aria-hidden="true"></span>' : ` — ${escapeHtml(accountEmail)}` : ''}</span>
        <span class="email-gmail-meta">${escapeHtml(metaLine)}</span>
      </div>
      <div class="email-gmail-actions">
        ${switcherMarkup}
        <button
          class="email-demo-toggle-btn${demoModeEnabled ? ' active' : ''}"
          id="toggleDemoModeBtn"
          type="button"
          aria-label="Toggle account masking"
          title="Toggle account masking"
        ><span class="email-demo-toggle-knob" aria-hidden="true"></span></button>
        <button class="btn btn-dlyte" id="connectGmailBtn" type="button" ${cropLocked ? 'disabled aria-disabled="true"' : ''}>Connect Gmail</button>
      </div>
    `;
    const select = bar.querySelector('#gmailAccountSelect');
    const switchBtn = bar.querySelector('#switchGmailAccountBtn');
    if (select && switchBtn && handlers.onActivateAccount) {
      switchBtn.addEventListener('click', () => handlers.onActivateAccount(select.value));
    }
    const demoToggleBtn = bar.querySelector('#toggleDemoModeBtn');
    if (demoToggleBtn) {
      demoToggleBtn.addEventListener('click', () => {
        toggleDemoMode();
        handlers.onDemoModeChange?.();
      });
    }
    const connectBtn = bar.querySelector('#connectGmailBtn');
    if (connectBtn) {
      connectBtn.addEventListener('click', () => {
        if (cropLock.guard('gmail-connect')) return;
        connectBtn.disabled = true;
        connectBtn.textContent = 'Connecting…';
        handlers.onConnectClick?.();
      });
    }
    container.appendChild(bar);
  } else if (status === 'connecting') {
    bar.innerHTML = `
      <span class="email-gmail-text">Connecting…</span>
      <button class="btn btn-dlyte" type="button" disabled>Connecting…</button>
    `;
  } else if (status === 'error') {
    bar.innerHTML = `
      <span class="email-gmail-text email-gmail-error">Connection failed${errorMessage ? `: ${escapeHtml(errorMessage)}` : ''}</span>
      <button class="btn btn-dlyte" id="connectGmailBtn" type="button" ${cropLocked ? 'disabled aria-disabled="true"' : ''}>Try again</button>
    `;
  } else {
    bar.innerHTML = `
      <span class="email-gmail-text">Using mock inbox</span>
      <button class="btn btn-dlyte" id="connectGmailBtn" type="button" ${cropLocked ? 'disabled aria-disabled="true"' : ''}>Connect Gmail</button>
    `;
  }

  container.appendChild(bar);

  const connectBtn = bar.querySelector('#connectGmailBtn');
  if (connectBtn) {
    connectBtn.addEventListener('click', () => {
      if (cropLock.guard('gmail-connect')) return;
      connectBtn.disabled = true;
      connectBtn.textContent = 'Connecting…';
      handlers.onConnectClick?.();
    });
  }
}
