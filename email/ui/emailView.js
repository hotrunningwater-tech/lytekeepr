import { markCaseRelated, archiveEmails, junkEmails, closeEmail } from '../state.js';
import { LABEL_DLYTE } from '../types.js';
import { escapeHtml, formatFullDate } from '../utils.js';

const cropLock = window.DLYTECropLock || {
  isActive() { return false; },
  guard() { return false; },
};

export function renderEmailView(container, email) {
  container.innerHTML = '';
  if (!email) return;

  const isDlyte = email.labels.includes(LABEL_DLYTE);
  const cropLocked = Boolean(cropLock.isActive());

  const wrap = document.createElement('div');
  wrap.className = 'email-detail';
  wrap.innerHTML = `
    <div class="email-detail-header">
      <button class="email-back-btn" id="backToListBtn" type="button">&larr; Back to list</button>
      <h3 class="email-detail-subject">
        <span>${escapeHtml(email.subject)}</span>
        ${isDlyte ? '<span class="email-tag-dlyte">SAVED</span>' : ''}
      </h3>
      <div class="email-detail-meta">
        <div><strong>${escapeHtml(email.senderName)}</strong> &lt;${escapeHtml(email.senderEmail)}&gt;</div>
        <div>To: ${email.recipients.map(escapeHtml).join(', ')}</div>
        <div>${formatFullDate(email.receivedAt)}</div>
      </div>
    </div>
    ${email.bodyLoading ? '<div class="email-detail-loading">Loading full message…</div>' : ''}
    <div class="email-detail-body">${escapeHtml(email.body).replace(/\n/g, '<br>')}</div>
    ${
      email.attachments.length
        ? `<div class="email-detail-attachments">
            <span class="block-label">Attachments</span>
            ${email.attachments
              .map(
                (a) =>
                  `<div class="file-row email-attachment-row">
                    <span class="file-icon">${a.isPhotoReviewable ? '🖼️' : '📎'}</span>
                    <span class="file-name">${escapeHtml(a.name)}</span>
                  </div>`
              )
              .join('')}
          </div>`
        : ''
    }
    <div class="email-detail-actions">
      ${isDlyte ? '' : `<button class="btn btn-dlyte" id="markDlyteBtn" type="button" ${cropLocked ? 'disabled aria-disabled="true"' : ''}>Save</button>`}
      <button class="btn btn-remove" id="archiveBtn" type="button" ${cropLocked ? 'disabled aria-disabled="true"' : ''}>Archive</button>
      <button class="btn btn-remove" id="junkBtn" type="button" ${cropLocked ? 'disabled aria-disabled="true"' : ''}>Not relevant</button>
    </div>
  `;

  wrap.querySelector('#backToListBtn').addEventListener('click', () => closeEmail());
  wrap.querySelector('#markDlyteBtn')?.addEventListener('click', () => markCaseRelated([email.id]));
  wrap.querySelector('#archiveBtn').addEventListener('click', () => archiveEmails([email.id]));
  wrap.querySelector('#junkBtn').addEventListener('click', () => junkEmails([email.id]));

  container.appendChild(wrap);
}
