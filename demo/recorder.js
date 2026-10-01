const SETTINGS_KEY = 'dlyte_demo_recorder_settings_v1';

const PRESETS = {
  standard: {
    label: 'Standard',
    capture: {
      preferCurrentTab: true,
      selfBrowserSurface: 'include',
      surfaceSwitching: 'include',
      monitorTypeSurfaces: 'include',
      systemAudio: 'include',
      video: {
        frameRate: { ideal: 30, max: 30 },
        width: { ideal: 1920 },
        height: { ideal: 1080 },
      },
      audio: {
        suppressLocalAudioPlayback: false,
      },
    },
    videoBitsPerSecond: 5_000_000,
  },
  high: {
    label: 'High',
    capture: {
      preferCurrentTab: true,
      selfBrowserSurface: 'include',
      surfaceSwitching: 'include',
      monitorTypeSurfaces: 'include',
      systemAudio: 'include',
      video: {
        frameRate: { ideal: 60, max: 60 },
        width: { ideal: 2560 },
        height: { ideal: 1440 },
      },
      audio: {
        suppressLocalAudioPlayback: false,
      },
    },
    videoBitsPerSecond: 10_000_000,
  },
};

const MIME_CANDIDATES = [
  'video/webm;codecs=vp9',
  'video/webm;codecs=vp8',
  'video/webm',
];

export function initDemoRecorder({
  quickButtonEl,
  presetSelectEl,
  filenameInputEl,
  previewEl,
  statusEl,
  lastSavedEl,
  libraryListEl,
  playerWrapEl,
  playerEl,
  editorEl,
  toggleEditBtnEl,
  addBoxBtnEl,
  setStartBtnEl,
  setEndBtnEl,
  deleteBoxBtnEl,
  exportBtnEl,
  editStatusEl,
  overlayEl,
  selectionBoxEl,
  redactionListEl,
}) {
  if (
    !quickButtonEl || !presetSelectEl || !filenameInputEl || !previewEl || !statusEl || !lastSavedEl ||
    !libraryListEl || !playerWrapEl || !playerEl || !editorEl || !toggleEditBtnEl || !addBoxBtnEl ||
    !setStartBtnEl || !setEndBtnEl || !deleteBoxBtnEl || !exportBtnEl || !editStatusEl ||
    !overlayEl || !selectionBoxEl || !redactionListEl
  ) {
    return null;
  }

  const supportsCapture = Boolean(navigator.mediaDevices?.getDisplayMedia);
  const supportsRecorder = typeof window.MediaRecorder === 'function';
  const supportedMimeType = resolveMimeType();

  const state = {
    status: supportsCapture && supportsRecorder ? 'ready' : 'unsupported',
    preset: 'standard',
    filenameBase: 'dlyte-demo',
    lastSavedFilename: '',
    lastSavedAt: '',
    errorMessage: '',
    stream: null,
    displayStream: null,
    micStream: null,
    audioContext: null,
    audioDestination: null,
    recorder: null,
    chunks: [],
    mimeType: supportedMimeType,
    stopRequested: false,
    recordings: [],
    activeRecordingPath: '',
    activeRecordingFilename: '',
    editMode: false,
    drawMode: false,
    exportStatus: '',
    redactions: [],
    selectedRedactionId: '',
    isDragging: false,
    dragStart: null,
    currentDraftRect: null,
  };

  restoreSettings();
  wireEvents();
  render();
  loadRecordings();

  return {
    isRecording: () => state.status === 'recording' || state.status === 'saving',
  };

  function wireEvents() {
    quickButtonEl.hidden = false;
    quickButtonEl.addEventListener('click', () => {
      if (state.status === 'recording') {
        stopRecording();
        return;
      }
      if (state.status === 'ready' || state.status === 'saved' || state.status === 'error') {
        startRecording();
      }
    });

    presetSelectEl.addEventListener('change', () => {
      state.preset = PRESETS[presetSelectEl.value] ? presetSelectEl.value : 'standard';
      saveSettings();
      render();
    });

    filenameInputEl.addEventListener('input', () => {
      state.filenameBase = filenameInputEl.value;
      saveSettings();
      render();
    });

    toggleEditBtnEl.addEventListener('click', () => {
      if (!state.activeRecordingFilename) return;
      state.editMode = !state.editMode;
      state.drawMode = false;
      state.currentDraftRect = null;
      state.exportStatus = state.editMode ? 'Paused video recommended before drawing a box.' : '';
      render();
    });

    addBoxBtnEl.addEventListener('click', () => {
      if (!state.editMode || !state.activeRecordingFilename) return;
      if (!playerEl.paused) {
        state.exportStatus = 'Pause the video before drawing a redaction box.';
        render();
        return;
      }
      const current = getSelectedRedaction();
      if (current && current.status === 'open') {
        closeRedaction(current, playerEl.currentTime || 0);
      }
      state.drawMode = true;
      state.currentDraftRect = null;
      state.exportStatus = 'Drag on the preview to draw a box.';
      render();
    });

    setStartBtnEl.addEventListener('click', () => {
      const item = getSelectedRedaction();
      if (!item || item.status !== 'open') return;
      closeRedaction(item, playerEl.currentTime || 0);
      state.exportStatus = `Stopped ${item.idLabel}.`;
      render();
    });

    deleteBoxBtnEl.addEventListener('click', () => {
      if (!state.selectedRedactionId) return;
      state.redactions = state.redactions.filter((item) => item.id !== state.selectedRedactionId);
      state.selectedRedactionId = state.redactions[0]?.id || '';
      state.exportStatus = 'Removed the selected redaction.';
      render();
    });

    exportBtnEl.addEventListener('click', () => {
      exportRedactions();
    });

    playerEl.addEventListener('timeupdate', () => {
      renderOverlay();
      renderRedactionList();
    });
    playerEl.addEventListener('loadedmetadata', () => {
      renderOverlay();
      renderRedactionList();
    });

    overlayEl.addEventListener('mousedown', beginDraw);
    window.addEventListener('mousemove', updateDraw);
    window.addEventListener('mouseup', finishDraw);
  }

  function render() {
    presetSelectEl.value = state.preset;
    if (document.activeElement !== filenameInputEl) {
      filenameInputEl.value = state.filenameBase;
    }

    const previewName = buildDownloadFilename(state.filenameBase);
    previewEl.textContent = `Will save as ${previewName} into demo_state/recordings`;

    if (state.status === 'unsupported') {
      statusEl.textContent = 'This browser cannot record screen capture here.';
    } else if (state.status === 'requesting') {
      statusEl.textContent = 'Choose the screen, window, or tab to record.';
    } else if (state.status === 'recording') {
      statusEl.textContent = 'Recording now. Use the subtle header circle to stop.';
    } else if (state.status === 'saving') {
      statusEl.textContent = 'Stopping recording and saving the file into the DLYTE project...';
    } else if (state.status === 'error') {
      statusEl.textContent = state.errorMessage || 'Recorder hit an error.';
    } else if (state.status === 'saved') {
      statusEl.textContent = 'Ready for the next take. Use the subtle header circle to start again.';
    } else {
      statusEl.textContent = 'Use the subtle circle in the header to start recording.';
    }

    if (state.lastSavedFilename) {
      lastSavedEl.textContent = `Last saved: ${state.lastSavedFilename}${state.lastSavedAt ? ` · ${formatStamp(state.lastSavedAt)}` : ''}`;
    } else {
      lastSavedEl.textContent = 'No recording saved yet.';
    }

    renderRecordings();
    renderEditor();

    quickButtonEl.disabled = state.status === 'unsupported' || state.status === 'requesting' || state.status === 'saving';
    quickButtonEl.classList.toggle('is-ready', state.status === 'ready' || state.status === 'saved' || state.status === 'error');
    quickButtonEl.classList.toggle('is-recording', state.status === 'recording');
    quickButtonEl.classList.toggle('is-busy', state.status === 'requesting' || state.status === 'saving');
    quickButtonEl.setAttribute('aria-label', quickButtonLabel());
    quickButtonEl.title = quickButtonLabel();
  }

  async function startRecording() {
    if (state.status === 'unsupported' || state.status === 'requesting' || state.status === 'recording' || state.status === 'saving') {
      return;
    }

    state.errorMessage = '';
    state.status = 'requesting';
    render();

    try {
      const displayStream = await navigator.mediaDevices.getDisplayMedia(PRESETS[state.preset].capture);
      const micStream = await getMicrophoneStream();
      const stream = await buildComposedStream(displayStream, micStream);
      const recorder = new MediaRecorder(stream, buildRecorderOptions());

      state.stream = stream;
      state.displayStream = displayStream;
      state.micStream = micStream;
      state.recorder = recorder;
      state.chunks = [];
      state.stopRequested = false;

      recorder.addEventListener('dataavailable', (event) => {
        if (event.data && event.data.size > 0) {
          state.chunks.push(event.data);
        }
      });

      recorder.addEventListener('stop', handleRecorderStop);
      recorder.addEventListener('error', () => {
        setError('Recording failed before the file could be saved.');
      });

      const [videoTrack] = displayStream.getVideoTracks();
      if (videoTrack) {
        videoTrack.addEventListener('ended', () => {
          if (state.status === 'recording') stopRecording();
        });
      }

      recorder.start(250);
      state.status = 'recording';
      render();
    } catch (error) {
      if (error?.name === 'NotAllowedError' || error?.name === 'AbortError') {
        state.status = 'ready';
        state.errorMessage = '';
        render();
        return;
      }
      setError('Could not start screen recording.');
    }
  }

  function stopRecording() {
    if (!state.recorder || state.status !== 'recording') return;
    state.stopRequested = true;
    state.status = 'saving';
    render();
    state.recorder.stop();
  }

  function handleRecorderStop() {
    const blob = new Blob(state.chunks, { type: state.mimeType || 'video/webm' });
    if (!blob.size) {
      cleanupRecordingResources();
      setError('Recording ended, but no video was captured.');
      return;
    }

    const filename = buildDownloadFilename(state.filenameBase);
    saveRecordingToProject(blob, filename)
      .then((saved) => {
        cleanupRecordingResources();
        state.lastSavedFilename = saved.relative_path || saved.filename || filename;
        state.activeRecordingPath = saved.relative_path || '';
        state.lastSavedAt = new Date().toISOString();
        state.status = 'saved';
        state.errorMessage = '';
        loadRecordings();
        render();
      })
      .catch(() => {
        downloadBlob(blob, filename);
        cleanupRecordingResources();
        state.lastSavedFilename = `Downloads/${filename}`;
        state.activeRecordingPath = '';
        state.lastSavedAt = new Date().toISOString();
        state.status = 'saved';
        state.errorMessage = '';
        statusEl.textContent = 'Saved fallback copy to Downloads because project save failed.';
        render();
      });
  }

  function cleanupRecordingResources() {
    if (state.stream) {
      for (const track of state.stream.getTracks()) track.stop();
    }
    if (state.displayStream) {
      for (const track of state.displayStream.getTracks()) track.stop();
    }
    if (state.micStream) {
      for (const track of state.micStream.getTracks()) track.stop();
    }
    if (state.audioContext) {
      state.audioContext.close().catch(() => {});
    }
    state.stream = null;
    state.displayStream = null;
    state.micStream = null;
    state.audioContext = null;
    state.audioDestination = null;
    state.recorder = null;
    state.chunks = [];
    state.stopRequested = false;
  }

  function setError(message) {
    cleanupRecordingResources();
    state.status = 'error';
    state.errorMessage = message;
    render();
  }

  function buildRecorderOptions() {
    const options = {
      videoBitsPerSecond: PRESETS[state.preset].videoBitsPerSecond,
    };
    if (state.mimeType) options.mimeType = state.mimeType;
    return options;
  }

  function quickButtonLabel() {
    if (state.status === 'recording') return 'Stop screen recording';
    if (state.status === 'saving') return 'Saving screen recording';
    if (state.status === 'requesting') return 'Choose what to record';
    if (state.status === 'unsupported') return 'Screen recorder unavailable';
    return 'Start screen recording';
  }

  function saveSettings() {
    try {
      window.localStorage.setItem(
        SETTINGS_KEY,
        JSON.stringify({
          preset: state.preset,
          filenameBase: state.filenameBase,
        }),
      );
    } catch (_) {
      // Best effort only.
    }
  }

  function restoreSettings() {
    try {
      const raw = window.localStorage.getItem(SETTINGS_KEY);
      if (!raw) return;
      const parsed = JSON.parse(raw);
      if (parsed?.preset && PRESETS[parsed.preset]) state.preset = parsed.preset;
      if (typeof parsed?.filenameBase === 'string' && parsed.filenameBase.trim()) {
        state.filenameBase = parsed.filenameBase.trim();
      }
    } catch (_) {
      // Keep defaults.
    }
  }

  async function loadRecordings() {
    try {
      const res = await fetch('/api/demo/recordings', {
        headers: { 'X-DLYTE-Demo-Key': 'local-reset' },
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Could not load recordings.');
      state.recordings = data.recordings || [];
      renderRecordings();
    } catch (_) {
      state.recordings = [];
      renderRecordings();
    }
  }

  function renderRecordings() {
    if (!state.recordings.length) {
      libraryListEl.innerHTML = '<div class="demo-recorder-library-empty">No saved videos yet.</div>';
      if (!state.activeRecordingPath) {
        playerWrapEl.hidden = true;
        playerEl.removeAttribute('src');
        state.activeRecordingFilename = '';
      }
      renderEditor();
      return;
    }

    libraryListEl.innerHTML = state.recordings.map((item) => `
      <div class="demo-recorder-library-row${item.relative_path === state.activeRecordingPath ? ' is-active' : ''}" data-path="${escapeHtml(item.relative_path)}" data-filename="${escapeHtml(item.filename)}">
        <div class="demo-recorder-library-meta">
          <div class="demo-recorder-library-name">${escapeHtml(item.filename)}</div>
          <div class="demo-recorder-library-sub">${formatBytes(item.size_bytes)} · ${formatStamp(item.modified_at)}</div>
        </div>
      </div>
    `).join('');

    for (const row of libraryListEl.querySelectorAll('.demo-recorder-library-row')) {
      row.addEventListener('dblclick', () => {
        openRecording(row.dataset.filename || '', row.dataset.path || '');
      });
    }
  }

  function openRecording(filename, relativePath) {
    if (!filename) return;
    state.activeRecordingPath = relativePath;
    state.activeRecordingFilename = filename;
    state.editMode = false;
    state.drawMode = false;
    state.exportStatus = '';
    state.currentDraftRect = null;
    state.redactions = [];
    state.selectedRedactionId = '';
    playerEl.src = `/api/demo/recordings/${encodeURIComponent(filename)}`;
    playerWrapEl.hidden = false;
    playerEl.play().catch(() => {});
    renderRecordings();
    renderEditor();
  }

  function renderEditor() {
    const hasActiveRecording = Boolean(state.activeRecordingFilename);
    editorEl.hidden = !hasActiveRecording;
    redactionListEl.hidden = !hasActiveRecording;
    toggleEditBtnEl.textContent = state.editMode ? 'Close editor' : 'Edit redactions';
    const selected = getSelectedRedaction();
    const selectedIsOpen = Boolean(selected) && selected.status === 'open';
    addBoxBtnEl.hidden = !state.editMode;
    addBoxBtnEl.textContent = selectedIsOpen ? 'Move blur here' : 'Add blur';
    setStartBtnEl.hidden = !state.editMode || !selectedIsOpen;
    setStartBtnEl.textContent = 'Stop blur';
    setEndBtnEl.hidden = true;
    deleteBoxBtnEl.hidden = !state.editMode || !state.selectedRedactionId;
    exportBtnEl.hidden = !state.editMode || !state.redactions.length;
    editStatusEl.hidden = !state.exportStatus;
    editStatusEl.textContent = state.exportStatus;
    overlayEl.hidden = !state.editMode;
    renderOverlay();
    renderRedactionList();
  }

  function renderOverlay() {
    overlayEl.innerHTML = '';
    overlayEl.appendChild(selectionBoxEl);
    selectionBoxEl.hidden = !state.currentDraftRect;
    const viewport = getVideoViewportRect(playerEl, overlayEl);
    if (state.currentDraftRect) {
      applyRectStyles(selectionBoxEl, state.currentDraftRect, viewport);
    }
    if (!state.editMode) return;

    const currentTime = playerEl.currentTime || 0;
    for (const item of state.redactions) {
      const visible = item.status === 'open'
        ? currentTime >= item.start_time
        : currentTime >= item.start_time && currentTime <= item.end_time;
      if (!visible) continue;
      const box = document.createElement('div');
      box.className = [
        'demo-recorder-redaction-box',
        item.id === state.selectedRedactionId ? 'is-selected' : '',
        item.status === 'open' ? 'is-open' : '',
      ].filter(Boolean).join(' ');
      applyRectStyles(box, item, viewport);
      overlayEl.appendChild(box);
    }
  }

  function renderRedactionList() {
    if (!state.activeRecordingFilename) {
      redactionListEl.hidden = true;
      redactionListEl.innerHTML = '';
      return;
    }
    redactionListEl.hidden = false;
    if (!state.redactions.length) {
      redactionListEl.innerHTML = '<div class="demo-recorder-library-empty">No redactions yet.</div>';
      return;
    }
    redactionListEl.innerHTML = state.redactions.map((item) => `
      <button class="demo-recorder-redaction-row${item.id === state.selectedRedactionId ? ' is-selected' : ''}${item.status === 'open' ? ' is-open' : ''}" type="button" data-redaction-id="${escapeHtml(item.id)}">
        <span class="demo-recorder-redaction-name">${escapeHtml(item.idLabel)}${item.status === 'open' ? ' · live' : ''}</span>
        <span class="demo-recorder-redaction-sub">${item.status === 'open' ? `${formatSeconds(item.start_time)} → …` : `${formatSeconds(item.start_time)} → ${formatSeconds(item.end_time)}`}</span>
      </button>
    `).join('');
    for (const row of redactionListEl.querySelectorAll('.demo-recorder-redaction-row')) {
      row.addEventListener('click', () => {
        state.selectedRedactionId = row.dataset.redactionId || '';
        state.exportStatus = '';
        render();
      });
    }
  }

  function beginDraw(event) {
    if (!state.editMode || !state.drawMode) return;
    const rect = overlayEl.getBoundingClientRect();
    const viewport = getVideoViewportRect(playerEl, overlayEl);
    state.isDragging = true;
    state.dragStart = pointFromMouseEvent(event, rect, viewport);
    state.currentDraftRect = {
      x: state.dragStart.x,
      y: state.dragStart.y,
      width: 0,
      height: 0,
    };
    renderOverlay();
  }

  function updateDraw(event) {
    if (!state.isDragging || !state.dragStart) return;
    const rect = overlayEl.getBoundingClientRect();
    const viewport = getVideoViewportRect(playerEl, overlayEl);
    const current = pointFromMouseEvent(event, rect, viewport);
    state.currentDraftRect = normalizeDraftRect(state.dragStart, current);
    renderOverlay();
  }

  function finishDraw() {
    if (!state.isDragging || !state.currentDraftRect) return;
    state.isDragging = false;
    state.dragStart = null;
    const rect = state.currentDraftRect;
    state.currentDraftRect = null;
    state.drawMode = false;
    if (rect.width < 0.01 || rect.height < 0.01) {
      state.exportStatus = 'Box was too small. Draw again.';
      render();
      return;
    }
    const item = {
      id: `r${Date.now()}`,
      idLabel: `Redaction ${state.redactions.length + 1}`,
      x: rect.x,
      y: rect.y,
      width: rect.width,
      height: rect.height,
      start_time: clampTime(playerEl.currentTime || 0),
      end_time: null,
      status: 'open',
    };
    state.redactions.push(item);
    state.selectedRedactionId = item.id;
    state.exportStatus = `${item.idLabel} is live. Press play to keep it tracking, then Stop blur when done.`;
    render();
  }

  function closeRedaction(item, atTime) {
    item.end_time = Math.max(clampTime(atTime), item.start_time + 0.1);
    item.status = 'closed';
  }

  async function exportRedactions() {
    if (!state.activeRecordingFilename || !state.redactions.length) return;
    const openItem = state.redactions.find((item) => item.status === 'open');
    if (openItem) {
      state.exportStatus = `Stop ${openItem.idLabel} before exporting.`;
      render();
      return;
    }
    exportBtnEl.disabled = true;
    state.exportStatus = 'Rendering edited MP4...';
    render();
    try {
      const res = await fetch('/api/demo/recordings/redact', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-DLYTE-Demo-Key': 'local-reset',
        },
        body: JSON.stringify({
          source_filename: state.activeRecordingFilename,
          redactions: state.redactions.map(({ x, y, width, height, start_time, end_time }) => ({
            x, y, width, height, start_time, end_time,
          })),
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Could not export redacted video.');
      await loadRecordings();
      openRecording(data.filename, data.relative_path || '');
      state.editMode = false;
      state.redactions = [];
      state.selectedRedactionId = '';
      state.exportStatus = `Saved edited file: ${data.filename}`;
      render();
    } catch (error) {
      state.exportStatus = error?.message || 'Could not export redacted video.';
      render();
    } finally {
      exportBtnEl.disabled = false;
    }
  }

  function getSelectedRedaction() {
    return state.redactions.find((item) => item.id === state.selectedRedactionId) || null;
  }

  async function getMicrophoneStream() {
    try {
      return await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
        video: false,
      });
    } catch (_) {
      return null;
    }
  }

  async function buildComposedStream(displayStream, micStream) {
    const composed = new MediaStream();
    for (const track of displayStream.getVideoTracks()) composed.addTrack(track);

    const displayAudioTracks = displayStream.getAudioTracks();
    const micAudioTracks = micStream ? micStream.getAudioTracks() : [];
    if (!displayAudioTracks.length && !micAudioTracks.length) {
      return composed;
    }

    const audioContext = new AudioContext();
    const destination = audioContext.createMediaStreamDestination();
    state.audioContext = audioContext;
    state.audioDestination = destination;

    if (displayAudioTracks.length) {
      const displayAudioStream = new MediaStream(displayAudioTracks);
      const displaySource = audioContext.createMediaStreamSource(displayAudioStream);
      displaySource.connect(destination);
    }

    if (micAudioTracks.length) {
      const micAudioOnlyStream = new MediaStream(micAudioTracks);
      const micSource = audioContext.createMediaStreamSource(micAudioOnlyStream);
      micSource.connect(destination);
    }

    for (const track of destination.stream.getAudioTracks()) composed.addTrack(track);
    return composed;
  }
}

function resolveMimeType() {
  if (typeof window.MediaRecorder !== 'function' || typeof window.MediaRecorder.isTypeSupported !== 'function') {
    return '';
  }
  return MIME_CANDIDATES.find((candidate) => window.MediaRecorder.isTypeSupported(candidate)) || '';
}

function slugifyBaseName(value) {
  const cleaned = String(value || 'dlyte-demo')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
  return cleaned || 'dlyte-demo';
}

function buildTimestamp(date = new Date()) {
  const parts = [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, '0'),
    String(date.getDate()).padStart(2, '0'),
    String(date.getHours()).padStart(2, '0'),
    String(date.getMinutes()).padStart(2, '0'),
    String(date.getSeconds()).padStart(2, '0'),
  ];
  return `${parts[0]}-${parts[1]}-${parts[2]}-${parts[3]}${parts[4]}${parts[5]}`;
}

function buildDownloadFilename(baseName) {
  return `${slugifyBaseName(baseName)}-${buildTimestamp()}.webm`;
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

function formatBytes(bytes) {
  if (!bytes) return '0 B';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

async function saveRecordingToProject(blob, filename) {
  const form = new FormData();
  form.append('filename', filename);
  form.append('recording', blob, filename);

  const res = await fetch('/api/demo/recordings', {
    method: 'POST',
    headers: {
      'X-DLYTE-Demo-Key': 'local-reset',
    },
    body: form,
  });
  const data = await res.json();
  if (!res.ok) {
    throw new Error(data.error || 'Could not save recording into the project.');
  }
  return data;
}

function formatStamp(value) {
  if (!value) return 'No timestamp';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function pointFromMouseEvent(event, rect, viewport) {
  const x = clamp01((event.clientX - rect.left - viewport.left) / viewport.width);
  const y = clamp01((event.clientY - rect.top - viewport.top) / viewport.height);
  return { x, y };
}

function normalizeDraftRect(start, end) {
  const x = Math.min(start.x, end.x);
  const y = Math.min(start.y, end.y);
  return {
    x,
    y,
    width: Math.abs(end.x - start.x),
    height: Math.abs(end.y - start.y),
  };
}

function applyRectStyles(element, rect, viewport) {
  element.style.left = `${viewport.left + rect.x * viewport.width}px`;
  element.style.top = `${viewport.top + rect.y * viewport.height}px`;
  element.style.width = `${rect.width * viewport.width}px`;
  element.style.height = `${rect.height * viewport.height}px`;
}

function clamp01(value) {
  return Math.min(1, Math.max(0, value));
}

function clampTime(value) {
  return Math.max(0, Number(value) || 0);
}

function formatSeconds(value) {
  const total = Math.max(0, Number(value) || 0);
  const minutes = Math.floor(total / 60);
  const seconds = total - minutes * 60;
  return `${minutes}:${seconds.toFixed(1).padStart(4, '0')}`;
}

function getVideoViewportRect(playerEl, overlayEl) {
  const containerWidth = overlayEl.clientWidth || 1;
  const containerHeight = overlayEl.clientHeight || 1;
  const videoWidth = playerEl.videoWidth || containerWidth;
  const videoHeight = playerEl.videoHeight || containerHeight;
  const scale = Math.min(containerWidth / videoWidth, containerHeight / videoHeight);
  const width = videoWidth * scale;
  const height = videoHeight * scale;
  return {
    left: (containerWidth - width) / 2,
    top: (containerHeight - height) / 2,
    width,
    height,
  };
}
