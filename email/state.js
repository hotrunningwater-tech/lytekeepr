/**
 * Email feature state — a small pub/sub store. UI modules subscribe() and
 * re-render on change; they never mutate this state directly, only through
 * the exported actions below.
 */

import { fetchInbox as fetchMockInbox } from './providers/mockProvider.js';
import { fetchInbox as fetchEmptyInbox } from './providers/emptyProvider.js';
import { fetchInbox as fetchGmailInbox, fetchMessageDetail } from './providers/gmailProvider.js';
import { FOLDER, LABEL_DLYTE } from './types.js';

const cropLock = window.DLYTECropLock || {
  guard() { return false; },
  isActive() { return false; },
};

let activeFetch = fetchMockInbox;
let activeProvider = 'mock'; // 'mock' | 'gmail'

function guardCropLockedAction(action) {
  return cropLock.guard(action);
}

export function useMockProvider() {
  activeFetch = fetchMockInbox;
  activeProvider = 'mock';
}

export function useGmailProvider() {
  activeFetch = fetchGmailInbox;
  activeProvider = 'gmail';
}

export function useEmptyProvider() {
  activeFetch = fetchEmptyInbox;
  activeProvider = 'empty';
}

const state = {
  emails: [],
  selectedIds: new Set(),
  openedId: null,
  view: 'inbox', // 'inbox' | 'dlyte'
  status: 'idle', // 'idle' | 'loading' | 'error'
  errorMessage: '',
  toast: null,
};

const listeners = new Set();

function notify() {
  for (const fn of listeners) fn(state);
}

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function getState() {
  return state;
}

async function load() {
  state.status = 'loading';
  notify();
  try {
    state.emails = await activeFetch();
    state.status = 'idle';
    state.errorMessage = '';
  } catch (err) {
    state.status = 'error';
    state.errorMessage = 'Could not load your inbox. Please try again.';
  }
  notify();
}

export function init() {
  return load();
}

export function refresh() {
  if (guardCropLockedAction('email-refresh-state')) return Promise.resolve();
  return load();
}

export function setView(view) {
  if (guardCropLockedAction('email-set-view')) return;
  state.view = view;
  state.openedId = null;
  state.selectedIds.clear();
  notify();
}

export function getInboxEmails() {
  if (activeProvider === 'gmail') {
    return state.emails.filter((e) => e.folder !== FOLDER.JUNK);
  }
  if (activeProvider === 'empty') {
    return [];
  }
  return state.emails.filter((e) => e.folder === FOLDER.INBOX);
}

export function getDlyteEmails() {
  if (activeProvider === 'empty') {
    return [];
  }
  return state.emails.filter((e) => e.labels.includes(LABEL_DLYTE));
}

export function openEmail(id) {
  if (guardCropLockedAction('email-open-message')) return;
  const email = state.emails.find((e) => e.id === id);
  if (!email) return;
  email.unread = false;
  state.openedId = id;
  notify(); // show immediately with whatever we already have (snippet)

  if (activeProvider === 'gmail' && email.bodyLoaded === false) {
    email.bodyLoading = true;
    notify();
    fetchMessageDetail(id)
      .then((detail) => {
        Object.assign(email, detail, { bodyLoaded: true, bodyLoading: false });
        notify();
      })
      .catch(() => {
        email.bodyLoading = false;
        notify();
      });
  }
}

function upsertEmail(detail) {
  if (!detail?.id) return null;
  const existing = state.emails.find((email) => email.id === detail.id);
  if (existing) {
    Object.assign(existing, detail);
    return existing;
  }
  state.emails = [detail, ...state.emails];
  return detail;
}

function setViewForEmail(email) {
  if (!email) return;
  state.view = email.labels?.includes(LABEL_DLYTE) ? 'dlyte' : FOLDER.INBOX;
  state.selectedIds.clear();
}

export async function ensureEmailOpen(id) {
  if (guardCropLockedAction('email-ensure-open-message')) return false;

  let email = state.emails.find((entry) => entry.id === id);
  if (email) {
    setViewForEmail(email);
    openEmail(id);
    return state.openedId === id;
  }

  if (activeProvider !== 'gmail') {
    useGmailProvider();
  }

  try {
    email = upsertEmail(await fetchMessageDetail(id));
    notify();
  } catch (err) {
    await load();
    email = state.emails.find((entry) => entry.id === id) || null;
    if (!email) return false;
  }

  setViewForEmail(email);
  openEmail(id);
  return state.openedId === id;
}

export function closeEmail() {
  state.openedId = null;
  notify();
}

export function toggleSelect(id) {
  if (guardCropLockedAction('email-toggle-select')) return;
  if (state.selectedIds.has(id)) state.selectedIds.delete(id);
  else state.selectedIds.add(id);
  notify();
}

function showToast(message) {
  state.toast = message;
  notify();
  setTimeout(() => {
    state.toast = null;
    notify();
  }, 2200);
}

async function parseApiResponse(res) {
  const text = await res.text();
  try {
    return text ? JSON.parse(text) : {};
  } catch (err) {
    if (!res.ok) {
      throw new Error(`${res.status} ${res.statusText}`);
    }
    throw new Error('Server returned an invalid response.');
  }
}

export function markCaseRelated(ids) {
  if (guardCropLockedAction('email-mark-case-related')) return;
  if (!ids || !ids.length) return;
  const markableIds = ids.filter((id) => {
    const email = state.emails.find((e) => e.id === id);
    return email && !email.labels.includes(LABEL_DLYTE);
  });
  if (!markableIds.length) return;

  if (activeProvider !== 'gmail') {
    for (const id of markableIds) {
      const email = state.emails.find((e) => e.id === id);
      if (email && !email.labels.includes(LABEL_DLYTE)) email.labels.push(LABEL_DLYTE);
    }
    state.selectedIds.clear();
    showToast(markableIds.length > 1 ? `${markableIds.length} emails saved` : 'Saved');
    return;
  }

  for (const id of markableIds) {
    const email = state.emails.find((e) => e.id === id);
    if (email && !email.labels.includes(LABEL_DLYTE)) email.labels.push(LABEL_DLYTE);
  }
  state.selectedIds.clear();
  notify();

  fetch('/api/gmail/save-message', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids: markableIds }),
  })
    .then(async (res) => {
      const data = await parseApiResponse(res);
      if (!res.ok) throw new Error(data.error || 'Could not save email to case.');
      showToast(markableIds.length > 1 ? `${markableIds.length} emails saved to case` : 'Email saved to case');
      document.dispatchEvent(new CustomEvent('dlyte:case-emails-saved', { detail: { ids: markableIds } }));
      return load();
    })
    .catch((err) => {
      showToast(err?.message || 'Could not save email to case');
    });
}

export function archiveEmails(ids) {
  if (guardCropLockedAction('email-archive')) return;
  if (!ids || !ids.length) return;
  for (const id of ids) {
    const email = state.emails.find((e) => e.id === id);
    if (email) email.folder = FOLDER.ARCHIVED;
  }
  if (ids.includes(state.openedId)) state.openedId = null;
  state.selectedIds.clear();
  showToast(ids.length > 1 ? `${ids.length} emails archived` : 'Email archived');
}

export function junkEmails(ids) {
  if (guardCropLockedAction('email-junk')) return;
  if (!ids || !ids.length) return;
  if (ids.includes(state.openedId)) state.openedId = null;
  state.selectedIds.clear();
  notify();

  // Marking "not relevant" has to persist server-side and actually
  // remove the message from view — it used to just flip a local `folder`
  // flag that never reached the server, so the next refresh (or reload)
  // silently brought the message right back.
  fetch('/api/gmail/save-candidates', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids: [], dismissed_ids: ids }),
  })
    .then(async (res) => {
      const data = await parseApiResponse(res);
      if (!res.ok) throw new Error(data.error || 'Could not mark as not relevant.');
      showToast(ids.length > 1 ? `${ids.length} emails marked not relevant` : 'Marked not relevant');
      return load();
    })
    .catch((err) => {
      showToast(err?.message || 'Could not mark as not relevant');
    });
}
