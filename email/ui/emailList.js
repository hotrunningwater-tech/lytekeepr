import { toggleSelect, openEmail, getState } from '../state.js';
import { LABEL_DLYTE } from '../types.js';
import { escapeHtml, formatRowTime } from '../utils.js';

const cropLock = window.DLYTECropLock || {
  isActive() { return false; },
  guard() { return false; },
};

export function renderEmailList(container, emails) {
  const { selectedIds } = getState();
  const cropLocked = Boolean(cropLock.isActive());
  container.innerHTML = '';

  if (!emails.length) {
    const empty = document.createElement('div');
    empty.className = 'email-empty';
    empty.textContent = container.dataset.emptyMessage || 'No emails here yet.';
    container.appendChild(empty);
    return;
  }

  for (const email of emails) {
    const row = document.createElement('div');
    row.className =
      'email-row' +
      (email.unread ? ' unread' : '') +
      (selectedIds.has(email.id) ? ' selected' : '');

    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'email-row-checkbox';
    checkbox.checked = selectedIds.has(email.id);
    checkbox.disabled = cropLocked;
    checkbox.setAttribute('aria-label', `Select email: ${email.subject}`);
    checkbox.addEventListener('click', (e) => {
      e.stopPropagation();
      if (cropLock.guard('email-list-toggle-checkbox')) return;
      toggleSelect(email.id);
    });

    const main = document.createElement('div');
    main.className = 'email-row-main';
    main.innerHTML = `
      <div class="email-row-top">
        <span class="email-row-sender">${escapeHtml(email.senderName)}</span>
        <span class="email-row-time">${formatRowTime(email.receivedAt)}</span>
      </div>
      <div class="email-row-subject">
        ${email.labels.includes(LABEL_DLYTE) ? '<span class="email-tag-dlyte">SAVED</span>' : ''}
        <span>${escapeHtml(email.subject)}</span>
      </div>
      <div class="email-row-preview">
        ${escapeHtml(email.preview)}
        ${email.attachments.length ? '<span class="email-attach-icon" title="Has attachment">📎</span>' : ''}
      </div>
    `;
    if (cropLocked) row.classList.add('email-row-disabled');
    main.addEventListener('click', () => {
      if (cropLock.guard('email-list-open-message')) return;
      openEmail(email.id);
    });

    row.appendChild(checkbox);
    row.appendChild(main);
    container.appendChild(row);
  }
}
