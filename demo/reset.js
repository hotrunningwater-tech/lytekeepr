import { initDemoRecorder } from './recorder.js';

const DEMO_FLAG = new URLSearchParams(window.location.search).get('demo') === '1';
const IS_LOCAL_HOST = ['127.0.0.1', 'localhost'].includes(window.location.hostname);
const DEMO_HEADERS = {
  'Content-Type': 'application/json',
  'X-DLYTE-Demo-Key': 'local-reset',
};

if (DEMO_FLAG || IS_LOCAL_HOST) {
  setupDemoReset();
}

function setupDemoReset() {
  const triggerBtn = document.getElementById('demoStateTriggerBtn');
  const quickRecorderBtn = document.getElementById('demoRecorderQuickBtn');
  const shell = document.getElementById('demoResetShell');
  const panel = document.getElementById('demoResetPanel');
  const closeBtn = document.getElementById('demoResetCloseBtn');
  const profilesTabBtn = document.getElementById('demoProfilesTabBtn');
  const stateTabBtn = document.getElementById('demoStateTabBtn');
  const recorderTabBtn = document.getElementById('demoRecorderTabBtn');
  const profilesView = document.getElementById('demoProfilesView');
  const stateView = document.getElementById('demoStateView');
  const recorderView = document.getElementById('demoRecorderView');
  const captureBtn = document.getElementById('demoCaptureSnapshotBtn');
  const saveBaselineBtn = document.getElementById('demoSaveBaselineBtn');
  const resetBaselineBtn = document.getElementById('demoResetToBaselineBtn');
  const nameInput = document.getElementById('demoSnapshotNameInput');
  const statusEl = document.getElementById('demoResetStatus');
  const listEl = document.getElementById('demoSnapshotList');
  const listLabelEl = document.getElementById('demoSnapshotListLabel');
  const profileNameInput = document.getElementById('demoProfileNameInput');
  const createProfileBtn = document.getElementById('demoCreateProfileBtn');
  const seedProfilesBtn = document.getElementById('demoSeedProfilesBtn');
  const profileListEl = document.getElementById('demoProfileList');

  const recorderPresetSelectEl = document.getElementById('demoRecorderPresetSelect');
  const recorderFilenameInputEl = document.getElementById('demoRecorderFilenameInput');
  const recorderFilenamePreviewEl = document.getElementById('demoRecorderFilenamePreview');
  const recorderStatusEl = document.getElementById('demoRecorderStatus');
  const recorderLastSavedEl = document.getElementById('demoRecorderLastSaved');
  const recorderLibraryListEl = document.getElementById('demoRecorderLibraryList');
  const recorderPlayerWrapEl = document.getElementById('demoRecorderPlayerWrap');
  const recorderPlayerEl = document.getElementById('demoRecorderPlayer');
  const recorderEditorEl = document.getElementById('demoRecorderEditor');
  const recorderToggleEditBtnEl = document.getElementById('demoRecorderToggleEditBtn');
  const recorderAddBoxBtnEl = document.getElementById('demoRecorderAddBoxBtn');
  const recorderSetStartBtnEl = document.getElementById('demoRecorderSetStartBtn');
  const recorderSetEndBtnEl = document.getElementById('demoRecorderSetEndBtn');
  const recorderDeleteBoxBtnEl = document.getElementById('demoRecorderDeleteBoxBtn');
  const recorderExportBtnEl = document.getElementById('demoRecorderExportBtn');
  const recorderEditStatusEl = document.getElementById('demoRecorderEditStatus');
  const recorderOverlayEl = document.getElementById('demoRecorderOverlay');
  const recorderSelectionBoxEl = document.getElementById('demoRecorderSelectionBox');
  const recorderRedactionListEl = document.getElementById('demoRecorderRedactionList');

  if (
    !triggerBtn || !quickRecorderBtn || !shell || !panel || !closeBtn ||
    !profilesTabBtn || !stateTabBtn || !recorderTabBtn ||
    !profilesView || !stateView || !recorderView ||
    !captureBtn || !saveBaselineBtn || !resetBaselineBtn ||
    !nameInput || !statusEl || !listEl || !listLabelEl ||
    !profileNameInput || !createProfileBtn || !seedProfilesBtn || !profileListEl ||
    !recorderPresetSelectEl || !recorderFilenameInputEl || !recorderFilenamePreviewEl ||
    !recorderStatusEl || !recorderLastSavedEl || !recorderLibraryListEl ||
    !recorderPlayerWrapEl || !recorderPlayerEl || !recorderEditorEl ||
    !recorderToggleEditBtnEl || !recorderAddBoxBtnEl || !recorderSetStartBtnEl ||
    !recorderSetEndBtnEl || !recorderDeleteBoxBtnEl || !recorderExportBtnEl ||
    !recorderEditStatusEl || !recorderOverlayEl || !recorderSelectionBoxEl || !recorderRedactionListEl
  ) {
    return;
  }

  let open = false;
  let activeTab = 'profiles';
  let snapshots = [];
  let manager = { activeSnapshot: null, baselineSnapshot: null, hasBaseline: false };
  let suggestedName = '';
  let profiles = [];

  initDemoRecorder({
    quickButtonEl: quickRecorderBtn,
    presetSelectEl: recorderPresetSelectEl,
    filenameInputEl: recorderFilenameInputEl,
    previewEl: recorderFilenamePreviewEl,
    statusEl: recorderStatusEl,
    lastSavedEl: recorderLastSavedEl,
    libraryListEl: recorderLibraryListEl,
    playerWrapEl: recorderPlayerWrapEl,
    playerEl: recorderPlayerEl,
    editorEl: recorderEditorEl,
    toggleEditBtnEl: recorderToggleEditBtnEl,
    addBoxBtnEl: recorderAddBoxBtnEl,
    setStartBtnEl: recorderSetStartBtnEl,
    setEndBtnEl: recorderSetEndBtnEl,
    deleteBoxBtnEl: recorderDeleteBoxBtnEl,
    exportBtnEl: recorderExportBtnEl,
    editStatusEl: recorderEditStatusEl,
    overlayEl: recorderOverlayEl,
    selectionBoxEl: recorderSelectionBoxEl,
    redactionListEl: recorderRedactionListEl,
  });

  function showStatus(text, isError = false) {
    statusEl.hidden = false;
    statusEl.textContent = text;
    statusEl.classList.toggle('error', isError);
  }

  function clearStatus() {
    statusEl.hidden = true;
    statusEl.textContent = '';
    statusEl.classList.remove('error');
  }

  function setOpen(nextOpen) {
    open = nextOpen;
    shell.hidden = !open;
    panel.hidden = !open;
    if (open) {
      clearStatus();
      loadSnapshots();
      loadProfiles();
      if (activeTab === 'profiles') profileNameInput.focus();
      if (activeTab === 'state') nameInput.focus();
    }
  }

  function setActiveTab(nextTab) {
    activeTab = nextTab === 'state' || nextTab === 'recorder' ? nextTab : 'profiles';
    const profilesSelected = activeTab === 'profiles';
    const stateSelected = activeTab === 'state';
    const recorderSelected = activeTab === 'recorder';
    profilesTabBtn.classList.toggle('is-active', profilesSelected);
    stateTabBtn.classList.toggle('is-active', stateSelected);
    recorderTabBtn.classList.toggle('is-active', recorderSelected);
    profilesTabBtn.setAttribute('aria-selected', String(profilesSelected));
    stateTabBtn.setAttribute('aria-selected', String(stateSelected));
    recorderTabBtn.setAttribute('aria-selected', String(recorderSelected));
    profilesView.hidden = !profilesSelected;
    stateView.hidden = !stateSelected;
    recorderView.hidden = !recorderSelected;
  }

  function buildSuggestedSnapshotName() {
    const usedNames = new Set(snapshots.map((snapshot) => snapshot.name));
    let index = 1;
    while (usedNames.has(`checkpoint-${index}`)) {
      index += 1;
    }
    return `checkpoint-${index}`;
  }

  function syncSuggestedName() {
    const nextSuggestedName = buildSuggestedSnapshotName();
    if (!nameInput.value.trim() || nameInput.value === suggestedName) {
      nameInput.value = nextSuggestedName;
    }
    suggestedName = nextSuggestedName;
  }

  async function loadSnapshots() {
    listEl.innerHTML = '<div class="demo-reset-empty">Loading snapshots...</div>';
    try {
      const res = await fetch('/api/demo/snapshots', { headers: DEMO_HEADERS });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Could not load snapshots.');
      snapshots = data.snapshots || [];
      manager = data.manager || manager;
      syncSuggestedName();
      renderSnapshots();
    } catch (err) {
      listEl.innerHTML = '<div class="demo-reset-empty">Could not load snapshots.</div>';
      showStatus(err.message || 'Could not load snapshots.', true);
    }
  }

  async function loadProfiles() {
    profileListEl.innerHTML = '<div class="demo-reset-empty">Loading profiles...</div>';
    try {
      const res = await fetch('/api/demo/profiles', { headers: DEMO_HEADERS });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Could not load profiles.');
      profiles = data.profiles || [];
      renderProfiles();
    } catch (err) {
      profileListEl.innerHTML = '<div class="demo-reset-empty">Could not load profiles.</div>';
      showStatus(err.message || 'Could not load profiles.', true);
    }
  }

  function renderSnapshots() {
    saveBaselineBtn.hidden = Boolean(manager.hasBaseline);
    resetBaselineBtn.hidden = !manager.hasBaseline;
    listLabelEl.textContent = manager.hasBaseline
      ? `Saved checkpoints${manager.baselineSnapshot ? ` · clean start: ${manager.baselineSnapshot}` : ''}`
      : 'Saved checkpoints';

    if (!snapshots.length) {
      listEl.innerHTML = '<div class="demo-reset-empty">No saved snapshots yet.</div>';
      return;
    }

    listEl.innerHTML = snapshots.map((snapshot) => `
      <div class="demo-reset-row${snapshot.name === manager.activeSnapshot ? ' demo-reset-row-active' : ''}">
        <div class="demo-reset-meta">
          <div class="demo-reset-name">${escapeHtml(snapshot.name)}${snapshot.isBaseline ? '<span class="demo-reset-chip">Clean start</span>' : ''}${snapshot.name === manager.activeSnapshot ? '<span class="demo-reset-chip demo-reset-chip-active">Current</span>' : ''}</div>
          <div class="demo-reset-sub">${snapshot.description ? `${escapeHtml(snapshot.description)} · ` : ''}${formatStamp(snapshot.capturedAt)}</div>
        </div>
        <button class="btn btn-primary demo-reset-restore-btn" type="button" data-name="${escapeHtml(snapshot.name)}">Restore</button>
      </div>
    `).join('');

    for (const btn of listEl.querySelectorAll('.demo-reset-restore-btn')) {
      btn.addEventListener('click', () => restoreSnapshot(btn.dataset.name || ''));
    }
  }

  function renderProfiles() {
    if (!profiles.length) {
      profileListEl.innerHTML = '<div class="demo-reset-empty">No local profiles yet.</div>';
      return;
    }

    profileListEl.innerHTML = profiles.map((profile) => `
      <div class="demo-reset-row${profile.is_active ? ' demo-reset-row-active' : ''}">
        <div class="demo-reset-meta">
          <div class="demo-reset-name">${escapeHtml(profile.display_name || profile.profile_id)}${profile.is_active ? '<span class="demo-reset-chip demo-reset-chip-active">Current</span>' : ''}</div>
          <div class="demo-reset-sub">
            ID ${escapeHtml(profile.storage_id || profile.profile_id)} ·
            key ${escapeHtml(profile.profile_id)} ·
            shell ${escapeHtml(profile.shell_variant || 'default')} ·
            ${profile.counts?.saved_emails || 0} saved emails ·
            ${profile.counts?.pending_email_candidates || 0} pending ·
            ${profile.counts?.scanned_documents || 0} docs ·
            ${profile.counts?.chronology_events || 0} chronology
          </div>
        </div>
        <div class="demo-reset-actions-row">
          <button class="btn btn-primary demo-profile-switch-btn" type="button" data-profile-id="${escapeHtml(profile.profile_id)}" ${profile.is_active ? 'disabled' : ''}>Switch</button>
          <button class="btn btn-remove demo-profile-rename-btn" type="button" data-profile-id="${escapeHtml(profile.profile_id)}" data-display-name="${escapeHtml(profile.display_name || profile.profile_id)}">Rename</button>
          <button class="btn btn-remove demo-profile-reset-btn" type="button" data-profile-id="${escapeHtml(profile.profile_id)}">Reset</button>
        </div>
      </div>
    `).join('');

    for (const btn of profileListEl.querySelectorAll('.demo-profile-switch-btn')) {
      btn.addEventListener('click', () => activateProfile(btn.dataset.profileId || ''));
    }
    for (const btn of profileListEl.querySelectorAll('.demo-profile-rename-btn')) {
      btn.addEventListener('click', () => renameProfile(btn.dataset.profileId || '', btn.dataset.displayName || ''));
    }
    for (const btn of profileListEl.querySelectorAll('.demo-profile-reset-btn')) {
      btn.addEventListener('click', () => resetProfile(btn.dataset.profileId || ''));
    }
  }

  async function captureSnapshot() {
    const name = nameInput.value.trim();
    if (!name) {
      showStatus('Enter a snapshot name first.', true);
      return;
    }
    clearStatus();
    captureBtn.disabled = true;
    try {
      const res = await fetch('/api/demo/snapshots', {
        method: 'POST',
        headers: DEMO_HEADERS,
        body: JSON.stringify({ name }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Could not save snapshot.');
      showStatus(`Saved snapshot "${data.snapshot.name}".`);
      await loadSnapshots();
    } catch (err) {
      showStatus(err.message || 'Could not save snapshot.', true);
    } finally {
      captureBtn.disabled = false;
    }
  }

  async function captureBaseline() {
    const name = nameInput.value.trim() || 'baseline';
    clearStatus();
    saveBaselineBtn.disabled = true;
    try {
      const res = await fetch('/api/demo/snapshots', {
        method: 'POST',
        headers: DEMO_HEADERS,
        body: JSON.stringify({ name, isBaseline: true, description: 'Clean start for demo restore' }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Could not save clean start.');
      showStatus(`Saved clean start "${data.snapshot.name}".`);
      await loadSnapshots();
    } catch (err) {
      showStatus(err.message || 'Could not save clean start.', true);
    } finally {
      saveBaselineBtn.disabled = false;
    }
  }

  async function restoreSnapshot(name) {
    if (!name) return;
    if (!window.confirm(`Restore snapshot "${name}"? Current local demo state will be replaced.`)) return;

    clearStatus();
    try {
      const res = await fetch(`/api/demo/snapshots/${encodeURIComponent(name)}/restore`, {
        method: 'POST',
        headers: DEMO_HEADERS,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Could not restore snapshot.');
      sessionStorage.removeItem('dlyte_chat_messages');
      window.location.reload();
    } catch (err) {
      showStatus(err.message || 'Could not restore snapshot.', true);
    }
  }

  async function resetToBaseline() {
    if (!window.confirm('Reset to clean start? Current local demo state will be replaced.')) return;

    clearStatus();
    try {
      const res = await fetch('/api/demo/reset', {
        method: 'POST',
        headers: DEMO_HEADERS,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Could not reset to clean start.');
      sessionStorage.removeItem('dlyte_chat_messages');
      window.location.reload();
    } catch (err) {
      showStatus(err.message || 'Could not reset to clean start.', true);
    }
  }

  async function createProfile() {
    const displayName = profileNameInput.value.trim();
    if (!displayName) {
      showStatus('Enter a profile name first.', true);
      return;
    }
    clearStatus();
    createProfileBtn.disabled = true;
    try {
      const res = await fetch('/api/demo/profiles', {
        method: 'POST',
        headers: DEMO_HEADERS,
        body: JSON.stringify({ display_name: displayName }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Could not create profile.');
      profileNameInput.value = '';
      showStatus(`Created profile "${data.profile.display_name}".`);
      await loadProfiles();
    } catch (err) {
      showStatus(err.message || 'Could not create profile.', true);
    } finally {
      createProfileBtn.disabled = false;
    }
  }

  async function seedProfiles() {
    clearStatus();
    seedProfilesBtn.disabled = true;
    try {
      const res = await fetch('/api/demo/profiles/seed-defaults', {
        method: 'POST',
        headers: DEMO_HEADERS,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Could not create test profiles.');
      showStatus(`Ready: ${data.profiles.length} local test profiles.`);
      await loadProfiles();
    } catch (err) {
      showStatus(err.message || 'Could not create test profiles.', true);
    } finally {
      seedProfilesBtn.disabled = false;
    }
  }

  async function activateProfile(profileId) {
    if (!profileId) return;
    clearStatus();
    try {
      const res = await fetch(`/api/demo/profiles/${encodeURIComponent(profileId)}/activate`, {
        method: 'POST',
        headers: DEMO_HEADERS,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Could not switch profile.');
      sessionStorage.removeItem('dlyte_chat_messages');
      window.location.reload();
    } catch (err) {
      showStatus(err.message || 'Could not switch profile.', true);
    }
  }

  async function resetProfile(profileId) {
    if (!profileId) return;
    if (!window.confirm(`Reset local state for "${profileId}"?`)) return;
    clearStatus();
    try {
      const res = await fetch(`/api/demo/profiles/${encodeURIComponent(profileId)}/reset`, {
        method: 'POST',
        headers: DEMO_HEADERS,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Could not reset profile.');
      showStatus(`Reset local state for "${data.profile.display_name || profileId}".`);
      await loadProfiles();
      await loadSnapshots();
    } catch (err) {
      showStatus(err.message || 'Could not reset profile.', true);
    }
  }

  async function renameProfile(profileId, currentDisplayName) {
    if (!profileId) return;
    const nextName = window.prompt('Rename profile', currentDisplayName || profileId);
    if (nextName == null) return;
    const cleaned = nextName.trim();
    if (!cleaned) {
      showStatus('Profile name cannot be empty.', true);
      return;
    }
    clearStatus();
    try {
      const res = await fetch(`/api/demo/profiles/${encodeURIComponent(profileId)}/rename`, {
        method: 'POST',
        headers: DEMO_HEADERS,
        body: JSON.stringify({ display_name: cleaned }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Could not rename profile.');
      showStatus(`Renamed profile to "${data.profile.display_name}".`);
      await loadProfiles();
    } catch (err) {
      showStatus(err.message || 'Could not rename profile.', true);
    }
  }

  document.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === 'd') {
      event.preventDefault();
      setOpen(!open);
      return;
    }
    if (event.key === 'Escape' && open) {
      setOpen(false);
    }
  });

  shell.addEventListener('click', (event) => {
    if (event.target === shell) setOpen(false);
  });
  triggerBtn.addEventListener('click', () => setOpen(true));
  closeBtn.addEventListener('click', () => setOpen(false));
  profilesTabBtn.addEventListener('click', () => setActiveTab('profiles'));
  stateTabBtn.addEventListener('click', () => setActiveTab('state'));
  recorderTabBtn.addEventListener('click', () => setActiveTab('recorder'));
  captureBtn.addEventListener('click', captureSnapshot);
  saveBaselineBtn.addEventListener('click', captureBaseline);
  resetBaselineBtn.addEventListener('click', resetToBaseline);
  createProfileBtn.addEventListener('click', createProfile);
  seedProfilesBtn.addEventListener('click', seedProfiles);
  nameInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      captureSnapshot();
    }
  });
  profileNameInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      createProfile();
    }
  });

  setActiveTab('profiles');
}

function formatStamp(value) {
  if (!value) return 'No timestamp';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}
