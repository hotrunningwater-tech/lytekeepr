(() => {
  const STORAGE_KEY = 'dlyte_chat_messages';
  const GREETING = "Hi — what's up?";

  const conversation = document.getElementById('conversation');
  const errorEl = document.getElementById('chatError');
  const input = document.getElementById('chatInput');
  const sendBtn = document.getElementById('sendBtn');
  const clearBtn = document.getElementById('clearChatBtn');

  const linkedinSendDraftRow = document.getElementById('linkedinSendDraftRow');
  const linkedinSendDraftText = document.getElementById('linkedinSendDraftText');
  const linkedinSendNowBtn = document.getElementById('linkedinSendNowBtn');
  const linkedinSendConfirmBackdrop = document.getElementById('linkedinSendConfirmBackdrop');
  const linkedinSendConfirmText = document.getElementById('linkedinSendConfirmText');
  const linkedinSendConfirmBtn = document.getElementById('linkedinSendConfirmBtn');
  const linkedinSendCancelBtn = document.getElementById('linkedinSendCancelBtn');

  const emailSendDraftRow = document.getElementById('emailSendDraftRow');
  const emailSendDraftText = document.getElementById('emailSendDraftText');
  const emailSendNowBtn = document.getElementById('emailSendNowBtn');
  const emailSendConfirmBackdrop = document.getElementById('emailSendConfirmBackdrop');
  const emailSendConfirmText = document.getElementById('emailSendConfirmText');
  const emailSendConfirmBtn = document.getElementById('emailSendConfirmBtn');
  const emailSendCancelBtn = document.getElementById('emailSendCancelBtn');

  const uploadArea = document.getElementById('uploadArea');
  const uploadContent = document.getElementById('uploadContent');
  const pdfInput = document.getElementById('pdfInput');
  const filesList = document.getElementById('filesList');
  const SUPPORTED_EXTENSIONS = new Set(['.pdf', '.png', '.jpg', '.jpeg', '.webp']);
  const IMAGE_EXTENSIONS = new Set(['.png', '.jpg', '.jpeg', '.webp']);
  const cropLock = window.DLYTECropLock || {
    setActive() {},
    isActive() { return false; },
    getMessage() { return 'Finish or cancel crop editing first.'; },
    guard() { return false; },
  };

  let messages = loadMessages();
  let isLoading = false;

  // Upload state: idle | pending | processing | reviewing | naming | succeeded | failed
  let ocrState = 'idle';
  // Overrides the generic "processing" label below — set for flows (like
  // reviewing an email's photo attachments) where pendingFiles is empty so
  // the default label logic can't tell what's actually happening, and
  // where no OCR has actually started yet (that only happens later, when
  // the user explicitly clicks "Create and scan PDF").
  let processingLabel = null;
  let pendingFiles = [];
  let currentDocument = null; // { filename, page_count, full_text, pages } once scanned
  let activePreviewDocument = null; // { job_id, filename } for the currently open saved preview doc
  let currentPhotoReview = null; // { job_id, pages, outputFilename }
  let pendingSavedDocument = null; // { job_id, filename, suggested_filename, naming_source, naming_reason, naming_details, naming_debug, page_count, full_text, pages, warnings, status }
  let isPhotoCropEditorOpen = Boolean(cropLock.isActive());
  let uploadFailureMessage = '';
  let photoReviewScrollTop = 0;
  let namingDebugPanelOpen = false;

  function syncCropLockState(active) {
    isPhotoCropEditorOpen = Boolean(active);
  }

  function setCropLockActive(active, source = 'photo-review') {
    cropLock.setActive(active, { source });
    syncCropLockState(cropLock.isActive());
  }

  function getCropLockMessage() {
    return cropLock.getMessage();
  }

  function guardCropLockedAction(action, { showInlineError = true } = {}) {
    if (!cropLock.guard(action)) return false;
    if (showInlineError) showError(getCropLockMessage());
    return true;
  }

  function hasPendingImageBatch() {
    return pendingFiles.length > 0 && pendingFiles.every(isImageFile);
  }

  function loadMessages() {
    try {
      const raw = sessionStorage.getItem(STORAGE_KEY);
      if (raw) return JSON.parse(raw);
    } catch (e) { /* fall through to default */ }
    return [{ role: 'assistant', content: GREETING }];
  }

  function saveMessages() {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(messages));
  }

  function addAssistantMessage(text) {
    messages.push({ role: 'assistant', content: text });
    saveMessages();
  }

  const COPY_ICON = '<svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="7" y="7" width="10" height="10" rx="1.5"/><path d="M4.5 12.5H4a1.5 1.5 0 0 1-1.5-1.5V4A1.5 1.5 0 0 1 4 2.5h7A1.5 1.5 0 0 1 12.5 4v.5"/></svg>';
  const CHECK_ICON = '<svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 10.5 8 14.5 16 5.5"/></svg>';

  async function handleCopyClick(button, text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      // Clipboard permission denied or unavailable — silently leave the
      // button as-is rather than surfacing a scary error for a copy icon.
      return;
    }
    if (button.dataset.copyTimer) clearTimeout(Number(button.dataset.copyTimer));
    button.innerHTML = CHECK_ICON;
    button.classList.add('copied');
    button.title = 'Copied!';
    const timer = setTimeout(() => {
      button.innerHTML = COPY_ICON;
      button.classList.remove('copied');
      button.title = 'Copy';
    }, 1500);
    button.dataset.copyTimer = String(timer);
  }

  function render() {
    conversation.innerHTML = '';
    for (const m of messages) {
      const group = document.createElement('div');
      group.className = 'chat-message' + (m.role === 'user' ? ' user' : '');

      const bubble = document.createElement('div');
      bubble.className = 'chat-bubble' + (m.role === 'user' ? ' user' : '');
      bubble.textContent = m.content;
      group.appendChild(bubble);

      if (m.role === 'assistant') {
        const actions = document.createElement('div');
        actions.className = 'chat-message-actions';
        const copyBtn = document.createElement('button');
        copyBtn.type = 'button';
        copyBtn.className = 'chat-copy-btn';
        copyBtn.title = 'Copy';
        copyBtn.setAttribute('aria-label', 'Copy reply');
        copyBtn.innerHTML = COPY_ICON;
        copyBtn.addEventListener('click', () => handleCopyClick(copyBtn, m.content));
        actions.appendChild(copyBtn);

        if (m.isFollowupAnnouncement) {
          const dismissBtn = document.createElement('button');
          dismissBtn.type = 'button';
          dismissBtn.className = 'chat-copy-btn';
          dismissBtn.title = 'Dismiss';
          dismissBtn.setAttribute('aria-label', 'Dismiss this follow-up announcement');
          dismissBtn.textContent = 'Dismiss';
          dismissBtn.addEventListener('click', () => dismissFollowupAnnouncement());
          actions.appendChild(dismissBtn);
        }

        group.appendChild(actions);
      }

      conversation.appendChild(group);
    }
    if (isLoading) {
      const loading = document.createElement('div');
      loading.className = 'chat-bubble chat-bubble-loading';
      loading.textContent = 'Thinking…';
      conversation.appendChild(loading);
    }
    conversation.scrollTop = conversation.scrollHeight;
  }

  function setBusy(busy) {
    isLoading = busy;
    input.disabled = busy;
    sendBtn.disabled = busy;
    render();
  }

  function showError(text) {
    errorEl.textContent = text;
    errorEl.hidden = false;
  }

  function clearError() {
    errorEl.hidden = true;
    errorEl.textContent = '';
  }

  function resizeInput() {
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
  }

  function isImageFile(file) {
    const lowerName = file.name.toLowerCase();
    return [...IMAGE_EXTENSIONS].some((ext) => lowerName.endsWith(ext));
  }

  function isPdfFile(file) {
    return file.name.toLowerCase().endsWith('.pdf');
  }

  async function sendMessage() {
    if (isLoading) return;
    const text = input.value.trim();
    if (!text) return;

    clearError();
    messages.push({ role: 'user', content: text });
    input.value = '';
    resizeInput();
    saveMessages();
    setBusy(true);

    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages,
          document: currentDocument,
          active_preview_job_id: activePreviewDocument?.job_id || null,
        }),
      });
      const data = await res.json();

      if (!res.ok) {
        showError(data.error || 'Something went wrong talking to the assistant.');
      } else {
        addAssistantMessage(data.reply);
        showLinkedinSendDraft(data.pendingLinkedinSend || null);
        showEmailSendDraft(data.pendingEmailSend || null);
      }
    } catch (err) {
      showError('Could not reach the server. Check that it is running and try again.');
    } finally {
      setBusy(false);
      input.focus();
    }
  }

  sendBtn.addEventListener('click', sendMessage);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
  input.addEventListener('input', resizeInput);
  clearBtn.addEventListener('click', () => {
    if (guardCropLockedAction('clear-chat')) return;
    messages = [{ role: 'assistant', content: GREETING }];
    saveMessages();
    clearError();
    render();
    showLinkedinSendDraft(null);
    fetch('/api/linkedin/send-pending/cancel', { method: 'POST' }).catch(() => {});
    showEmailSendDraft(null);
    fetch('/api/email/send-pending/cancel', { method: 'POST' }).catch(() => {});
  });

  function showLinkedinSendDraft(pending) {
    if (!pending) {
      linkedinSendDraftRow.hidden = true;
      return;
    }
    linkedinSendDraftText.textContent = `Message to ${pending.person_name}: "${pending.message_text}"`;
    linkedinSendDraftRow.hidden = false;
  }

  linkedinSendNowBtn.addEventListener('click', () => {
    linkedinSendConfirmText.textContent = linkedinSendDraftText.textContent;
    linkedinSendConfirmBackdrop.hidden = false;
  });

  linkedinSendCancelBtn.addEventListener('click', () => {
    linkedinSendConfirmBackdrop.hidden = true;
    showLinkedinSendDraft(null);
    fetch('/api/linkedin/send-pending/cancel', { method: 'POST' }).catch(() => {});
  });

  linkedinSendConfirmBtn.addEventListener('click', () => {
    linkedinSendConfirmBtn.disabled = true;
    // Nothing is actually known until the backend's re-check (step 3)
    // finishes, so show one single waiting state the whole time — not a
    // "Sending…" label that implies we already know something we don't.
    linkedinSendConfirmBtn.textContent = 'Waiting for confirmation…';
    fetch('/api/linkedin/send-pending/confirm', { method: 'POST' })
      .then((res) => res.json())
      .then((data) => {
        linkedinSendConfirmBackdrop.hidden = true;
        linkedinSendConfirmBtn.disabled = false;
        linkedinSendConfirmBtn.textContent = 'Confirm & send';
        showLinkedinSendDraft(null);
        addAssistantMessage(data.message || 'Something went wrong sending the message.');
        render();
      })
      .catch((err) => {
        linkedinSendConfirmBackdrop.hidden = true;
        linkedinSendConfirmBtn.disabled = false;
        linkedinSendConfirmBtn.textContent = 'Confirm & send';
        showError('Could not reach the server to send the message: ' + err.message);
      });
  });

  function showEmailSendDraft(pending) {
    if (!pending) {
      emailSendDraftRow.hidden = true;
      return;
    }
    const subjectBit = pending.subject ? ` — "${pending.subject}"` : '';
    emailSendDraftText.textContent = `Email to ${pending.person_name} (${pending.to_email})${subjectBit}: "${pending.body}"`;
    emailSendDraftRow.hidden = false;
  }

  emailSendNowBtn.addEventListener('click', () => {
    emailSendConfirmText.textContent = emailSendDraftText.textContent;
    emailSendConfirmBackdrop.hidden = false;
  });

  emailSendCancelBtn.addEventListener('click', () => {
    emailSendConfirmBackdrop.hidden = true;
    showEmailSendDraft(null);
    fetch('/api/email/send-pending/cancel', { method: 'POST' }).catch(() => {});
  });

  emailSendConfirmBtn.addEventListener('click', () => {
    emailSendConfirmBtn.disabled = true;
    // Gmail is a real API, not browser automation — its response is
    // trustworthy on its own, so this is a normal single request, not the
    // wait-then-re-check pattern LinkedIn needs.
    emailSendConfirmBtn.textContent = 'Sending…';
    fetch('/api/email/send-pending/confirm', { method: 'POST' })
      .then((res) => res.json())
      .then((data) => {
        emailSendConfirmBackdrop.hidden = true;
        emailSendConfirmBtn.disabled = false;
        emailSendConfirmBtn.textContent = 'Confirm & send';
        showEmailSendDraft(null);
        addAssistantMessage(data.message || 'Something went wrong sending the email.');
        render();
      })
      .catch((err) => {
        emailSendConfirmBackdrop.hidden = true;
        emailSendConfirmBtn.disabled = false;
        emailSendConfirmBtn.textContent = 'Confirm & send';
        showError('Could not reach the server to send the email: ' + err.message);
      });
  });

  document.addEventListener('dlyte:preview-document-context', (event) => {
    const detail = event.detail || {};
    if (!detail.job_id) return;
    activePreviewDocument = {
      job_id: detail.job_id,
      filename: detail.filename || 'Scanned document',
    };
  });

  document.addEventListener('dlyte:close-document', () => {
    activePreviewDocument = null;
  });

  function timeAgo(unixSeconds) {
    const mins = Math.round((Date.now() - unixSeconds * 1000) / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.round(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    return `${Math.round(hours / 24)}d ago`;
  }

  async function refreshFilesList() {
    if (!filesList) return;
    try {
      const res = await fetch('/api/files');
      const data = await res.json();
      const files = data.files || [];
      document.dispatchEvent(new CustomEvent('dlyte:recent-scans-updated', {
        detail: { files },
      }));
      if (!files.length) {
        filesList.innerHTML = '<span class="files-empty">No documents scanned yet</span>';
        return;
      }
      const cropLockedAttr = isPhotoCropEditorOpen ? 'disabled aria-disabled="true"' : '';
      filesList.innerHTML = files.map((f) => `
        <div class="file-row file-row-with-actions">
          <button class="file-row-button recent-scan-open-btn" type="button" data-job-id="${escapeHtml(f.job_id)}" data-filename="${escapeHtml(f.filename)}" ${cropLockedAttr}>
            <span class="file-icon">📄</span>
            <span class="file-name">${escapeHtml(f.filename)}</span>
            <span class="file-meta">${f.page_count}p · ${timeAgo(f.scanned_at)}</span>
          </button>
          <button class="btn btn-remove file-row-rename-btn recent-scan-rename-btn" type="button" data-job-id="${escapeHtml(f.job_id)}" data-filename="${escapeHtml(f.filename)}" ${cropLockedAttr}>Rename</button>
          <button class="btn btn-remove file-row-delete-btn recent-scan-delete-btn" type="button" data-job-id="${escapeHtml(f.job_id)}" data-filename="${escapeHtml(f.filename)}" ${cropLockedAttr}>Delete</button>
        </div>
      `).join('');
      for (const row of filesList.querySelectorAll('.recent-scan-open-btn')) {
        row.addEventListener('click', () => {
          if (guardCropLockedAction('open-recent-scan')) return;
          document.dispatchEvent(new CustomEvent('dlyte:open-document', {
            detail: {
              job_id: row.dataset.jobId,
              filename: row.dataset.filename,
            },
          }));
        });
      }
      for (const button of filesList.querySelectorAll('.recent-scan-rename-btn')) {
        button.addEventListener('click', async (event) => {
          event.stopPropagation();
          if (guardCropLockedAction('rename-recent-scan')) return;
          const currentBase = (button.dataset.filename || '').replace(/\.[^./]+$/, '');
          const input = window.prompt('Rename document to:', currentBase);
          if (input === null) return;
          const trimmed = input.trim();
          if (!trimmed) return;
          try {
            const renameRes = await fetch(`/api/files/${encodeURIComponent(button.dataset.jobId)}/rename`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ filename: trimmed }),
            });
            const renameData = await renameRes.json();
            if (!renameRes.ok) {
              throw new Error(renameData.error || 'Could not rename the scanned document.');
            }
            await refreshFilesList();
          } catch (err) {
            showError(err.message || 'Could not rename the scanned document.');
          }
        });
      }
      for (const button of filesList.querySelectorAll('.recent-scan-delete-btn')) {
        button.addEventListener('click', async (event) => {
          event.stopPropagation();
          if (guardCropLockedAction('delete-recent-scan')) return;
          const confirmed = window.confirm(`Delete local scanned document "${button.dataset.filename || button.dataset.jobId}"? This removes the original file and OCR data from DLYTE only.`);
          if (!confirmed) return;
          try {
            const deleteRes = await fetch(`/api/files/${encodeURIComponent(button.dataset.jobId)}/delete`, { method: 'POST' });
            const deleteData = await deleteRes.json();
            if (!deleteRes.ok) {
              throw new Error(deleteData.message || deleteData.error || 'Could not delete the local scanned document.');
            }
            await refreshFilesList();
          } catch (err) {
            showError(err.message || 'Could not delete the local scanned document.');
          }
        });
      }
    } catch (e) {
      // Not critical.
    }
  }

  function humanSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1024 / 1024).toFixed(2) + ' MB';
  }

  function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  function resetUpload() {
    if (guardCropLockedAction('reset-upload')) return;
    ocrState = 'idle';
    pendingFiles = [];
    currentDocument = null;
    currentPhotoReview = null;
    pendingSavedDocument = null;
    setCropLockActive(false);
    uploadFailureMessage = '';
    pdfInput.value = '';
    clearError();
    document.dispatchEvent(new CustomEvent('dlyte:close-photo-crop-editor'));
    renderUpload();
  }

  function renderUpload() {
    uploadArea.classList.toggle('drag', false);
    uploadArea.classList.toggle('upload-area-active-review', ocrState === 'reviewing');
    uploadArea.classList.toggle('upload-area-active-naming', ocrState === 'naming');

    if (ocrState === 'idle') {
      uploadFailureMessage = '';
      uploadContent.innerHTML = `
        <div class="upload-title">Drop a document here</div>
        <div class="upload-sub">PDF or image file · photographed pages can be cleaned before OCR</div>
      `;
      return;
    }

    if (ocrState === 'pending') {
      uploadFailureMessage = '';
      const fileSummary = pendingFiles.length === 1
        ? escapeHtml(pendingFiles[0].name)
        : `${pendingFiles.length} files selected`;
      const totalBytes = pendingFiles.reduce((sum, file) => sum + file.size, 0);
      const isPhotoBatch = hasPendingImageBatch();
      uploadContent.innerHTML = `
        <div class="upload-title">${fileSummary}</div>
        <div class="upload-sub">${humanSize(totalBytes)} · ready to ${isPhotoBatch ? 'prepare' : 'scan'}</div>
        <div class="upload-actions">
          <button id="scanBtn" class="btn btn-primary" type="button">${isPhotoBatch ? 'Review pages' : 'Scan document'}</button>
          ${isPhotoBatch ? '<button id="addMoreBtn" class="btn btn-remove" type="button">Add more photos</button>' : ''}
          <button id="removeBtn" class="btn btn-remove" type="button">${isPhotoBatch ? 'Remove all' : 'Remove'}</button>
        </div>
      `;
      document.getElementById('scanBtn').addEventListener('click', beginPendingUpload);
      const addMoreBtn = document.getElementById('addMoreBtn');
      if (addMoreBtn) addMoreBtn.addEventListener('click', () => pdfInput.click());
      document.getElementById('removeBtn').addEventListener('click', resetUpload);
      return;
    }

    if (ocrState === 'processing') {
      uploadFailureMessage = '';
      let label = processingLabel
        ? processingLabel
        : hasPendingImageBatch()
          ? 'Preparing photographed pages…'
          : `Scanning ${escapeHtml(pendingFiles[0]?.name || 'document')}…`;
      if (pendingSavedDocument) {
        label = `Saving ${escapeHtml((pendingSavedDocument.suggested_filename || pendingSavedDocument.filename || 'document.pdf').replace(/\.pdf$/i, ''))}…`;
      }
      uploadContent.innerHTML = `
        <div class="upload-title">${label}</div>
        <div class="upload-sub">This can take a minute for multi-page documents.</div>
      `;
      return;
    }

    if (ocrState === 'reviewing') {
      renderPhotoReview();
      return;
    }

    if (ocrState === 'naming') {
      renderSavedDocumentNaming();
      return;
    }

    if (ocrState === 'succeeded') {
      uploadFailureMessage = '';
      uploadContent.innerHTML = `
        <div class="upload-title">${escapeHtml(currentDocument.filename)} ready</div>
        <div class="upload-sub">${currentDocument.page_count} page${currentDocument.page_count === 1 ? '' : 's'} scanned and OCR-ready · ask me about it below</div>
        <div class="upload-actions">
          <button id="removeDocBtn" class="btn btn-remove" type="button">Scan another document</button>
        </div>
      `;
      document.getElementById('removeDocBtn').addEventListener('click', resetUpload);
      return;
    }

    if (ocrState === 'failed') {
      uploadContent.innerHTML = `
        <div class="upload-title">Scan failed</div>
        <div class="upload-sub">${escapeHtml(pendingFiles[0] ? pendingFiles[0].name : '')}</div>
        ${uploadFailureMessage ? `<div class="upload-sub">${escapeHtml(uploadFailureMessage)}</div>` : ''}
        <div class="upload-actions">
          <button id="retryBtn" class="btn btn-primary" type="button">Try again</button>
          <button id="removeBtn" class="btn btn-remove" type="button">Remove</button>
        </div>
      `;
      document.getElementById('retryBtn').addEventListener('click', beginPendingUpload);
      document.getElementById('removeBtn').addEventListener('click', resetUpload);
    }
  }

  function handleFilesSelected(fileList) {
    if (guardCropLockedAction('select-upload-files')) return;
    const files = [...fileList];
    if (!files.length) return;
    const invalid = files.find((file) => ![...SUPPORTED_EXTENSIONS].some((ext) => file.name.toLowerCase().endsWith(ext)));
    if (invalid) {
      showError('Supported files: PDF, PNG, JPG, JPEG, WEBP.');
      return;
    }

    const pdfs = files.filter(isPdfFile);
    const images = files.filter(isImageFile);
    if (pdfs.length && images.length) {
      showError('Use either one PDF or one or more image files, not a mixed batch.');
      return;
    }
    if (pdfs.length > 1) {
      showError('Upload one PDF at a time.');
      return;
    }

    if (ocrState === 'pending' && hasPendingImageBatch()) {
      if (pdfs.length) {
        showError('Finish or clear the current photo batch before adding a PDF.');
        return;
      }
      clearError();
      pendingFiles = [...pendingFiles, ...images];
      renderUpload();
      return;
    }

    if (ocrState !== 'idle') {
      showError('Finish or clear the current document before starting another one.');
      return;
    }

    clearError();
    pendingFiles = files;
    ocrState = 'pending';
    renderUpload();
  }

  async function beginPendingUpload() {
    if (guardCropLockedAction('begin-upload')) return;
    if (!pendingFiles.length) return;
    if (hasPendingImageBatch()) {
      await preparePhotoReview();
    } else {
      await scanDocument();
    }
  }

  async function scanDocument() {
    if (!pendingFiles.length) return;
    ocrState = 'processing';
    processingLabel = null;
    clearError();
    renderUpload();

    const formData = new FormData();
    formData.append('file', pendingFiles[0]);

    try {
      const res = await fetch('/api/ocr', { method: 'POST', body: formData });
      const data = await res.json();

      if (!res.ok) {
        ocrState = 'failed';
        uploadFailureMessage = data.error || 'OCR failed for an unknown reason.';
        showError(uploadFailureMessage);
        renderUpload();
        return;
      }

      currentDocument = {
        filename: data.filename,
        page_count: data.page_count,
        full_text: data.full_text,
        pages: data.pages,
      };
      ocrState = 'succeeded';
      renderUpload();

      const note = data.status === 'partial'
        ? ` (some pages had OCR issues — ${data.warnings.length} warning(s))`
        : '';
      addAssistantMessage(
        `Document ready: "${data.filename}" (${data.page_count} page${data.page_count === 1 ? '' : 's'})${note}. Ask me anything about it.`
      );
      render();
      refreshFilesList();
    } catch (err) {
      ocrState = 'failed';
      uploadFailureMessage = 'Could not reach the server for OCR. Check that it is running and try again.';
      showError(uploadFailureMessage);
      renderUpload();
    }
  }

  async function preparePhotoReview() {
    ocrState = 'processing';
    processingLabel = null;
    clearError();
    renderUpload();

    const formData = new FormData();
    for (const file of pendingFiles) formData.append('files', file);

    try {
      const res = await fetch('/api/photo-review/start', { method: 'POST', body: formData });
      const data = await res.json();
      if (!res.ok) {
        ocrState = 'failed';
        uploadFailureMessage = data.error || 'Could not prepare photographed pages.';
        showError(uploadFailureMessage);
        renderUpload();
        return;
      }
      currentPhotoReview = data.review;
      pendingFiles = [];
      ocrState = 'reviewing';
      renderUpload();
    } catch (err) {
      ocrState = 'failed';
      uploadFailureMessage = 'Could not reach the server for photographed-page review.';
      showError(uploadFailureMessage);
      renderUpload();
    }
  }

  function getReviewPage(pageId) {
    return currentPhotoReview?.pages.find((page) => page.id === pageId) || null;
  }

  function moveReviewPage(pageId, direction) {
    if (!currentPhotoReview) return;
    const pages = currentPhotoReview.pages;
    const index = pages.findIndex((page) => page.id === pageId);
    const nextIndex = index + direction;
    if (index < 0 || nextIndex < 0 || nextIndex >= pages.length) return;
    [pages[index], pages[nextIndex]] = [pages[nextIndex], pages[index]];
    renderUpload();
  }

  function toggleReviewPageDeleted(pageId) {
    if (guardCropLockedAction('toggle-review-page-deleted')) return;
    const page = getReviewPage(pageId);
    if (!page) return;
    page.deleted = !page.deleted;
    rerenderPhotoReviewPreservingScroll();
  }

  function getCornerPercentStyle(point) {
    return `left:${point.x * 100}%;top:${point.y * 100}%;`;
  }

  function getCornerPolygonPoints(corners) {
    return corners.map((point) => `${point.x * 100},${point.y * 100}`).join(' ');
  }

  function rerenderPhotoReviewPreservingScroll() {
    const list = uploadContent.querySelector('.photo-review-list');
    if (list) photoReviewScrollTop = list.scrollTop;
    renderUpload();
  }

  function renderPhotoReview() {
    if (!currentPhotoReview) return;
    const previousList = uploadContent.querySelector('.photo-review-list');
    if (previousList) photoReviewScrollTop = previousList.scrollTop;
    const activeCount = currentPhotoReview.pages.filter((page) => !page.deleted).length;
    const outputBaseName = (currentPhotoReview.outputFilename || 'scanned-pages.pdf').replace(/\.pdf$/i, '');
    const reviewLockedAttr = isPhotoCropEditorOpen ? 'disabled' : '';
    const cropLockMessage = isPhotoCropEditorOpen
      ? `<div class="photo-review-lock-note">${escapeHtml(getCropLockMessage())} All controls in this review area are locked while the crop editor is open.</div>`
      : '';
    uploadContent.innerHTML = `
      <div class="photo-review-shell${isPhotoCropEditorOpen ? ' is-locked' : ''}">
        <div class="photo-review-head">
          <div>
            <div class="upload-title">Review photographed pages</div>
            <div class="upload-sub">${activeCount} page${activeCount === 1 ? '' : 's'} selected · adjust, then create and scan PDF</div>
          </div>
          <div class="upload-actions photo-review-top-actions">
            <button id="finalizePhotoReviewBtn" class="btn btn-primary" type="button" ${reviewLockedAttr}>Create and scan PDF</button>
            <button id="cancelPhotoReviewBtn" class="btn btn-remove" type="button" ${reviewLockedAttr}>Cancel</button>
          </div>
        </div>
        ${cropLockMessage}
        <div class="photo-review-name-row">
          <label class="photo-review-name-label" for="photoReviewNameInput">Document name</label>
          <div class="photo-review-name-field">
            <input id="photoReviewNameInput" class="photo-review-name-input" type="text" value="${escapeHtml(outputBaseName)}" autocomplete="off" spellcheck="false" ${reviewLockedAttr}>
            <span class="photo-review-name-ext">.pdf</span>
          </div>
        </div>
        <div class="photo-review-list">
          ${currentPhotoReview.pages.map((page, index) => `
            <div class="photo-page-card${page.deleted ? ' is-deleted' : ''}">
              <div class="photo-page-meta">
                <div class="photo-page-name">${index + 1}. ${escapeHtml(page.filename)}</div>
                <div class="photo-page-sub">${page.detected ? 'Auto-detected page boundary · use Crop to fine-tune if needed' : 'Fallback crop shown · use Crop to frame the page before finalizing'}</div>
              </div>
              <div class="photo-page-previews">
                <div class="photo-preview-pane">
                  <div class="photo-preview-label">Original</div>
                  <div class="photo-original-stage photo-original-stage-static">
                    <img class="photo-original-image" src="${escapeHtml(page.originalUrl)}" alt="">
                    <div class="photo-crop-overlay" aria-hidden="true">
                      <svg class="photo-crop-svg" viewBox="0 0 100 100" preserveAspectRatio="none">
                        <polygon class="photo-crop-polygon" points="${getCornerPolygonPoints(page.corners)}"></polygon>
                      </svg>
                    </div>
                  </div>
                </div>
                <div class="photo-preview-pane">
                  <div class="photo-preview-label">Processed preview</div>
                  <img class="photo-processed-image" src="${escapeHtml(page.previewUrl)}?v=${page.rotationTurns}-${page.corners.map((point) => `${point.x.toFixed(3)}-${point.y.toFixed(3)}`).join('_')}" alt="">
                </div>
              </div>
              <div class="photo-page-actions">
                <button class="btn btn-primary photo-action-btn" type="button" data-action="crop" data-page-id="${escapeHtml(page.id)}" ${page.deleted || isPhotoCropEditorOpen ? 'disabled' : ''}>Crop</button>
                <button class="btn btn-remove photo-action-btn" type="button" data-action="rotate-left" data-page-id="${escapeHtml(page.id)}" ${reviewLockedAttr}>Rotate left</button>
                <button class="btn btn-remove photo-action-btn" type="button" data-action="rotate-right" data-page-id="${escapeHtml(page.id)}" ${reviewLockedAttr}>Rotate right</button>
                <button class="btn btn-remove photo-action-btn" type="button" data-action="refresh" data-page-id="${escapeHtml(page.id)}" ${reviewLockedAttr}>Refresh preview</button>
                <button class="btn btn-remove photo-action-btn" type="button" data-action="toggle-delete" data-page-id="${escapeHtml(page.id)}" ${reviewLockedAttr}>${page.deleted ? 'Restore' : 'Remove'}</button>
              </div>
            </div>
          `).join('')}
        </div>
      </div>
    `;

    document.getElementById('finalizePhotoReviewBtn').addEventListener('click', finalizePhotoReview);
    document.getElementById('cancelPhotoReviewBtn').addEventListener('click', resetUpload);
    const nameInput = document.getElementById('photoReviewNameInput');
    if (nameInput) {
      nameInput.addEventListener('input', () => {
        if (guardCropLockedAction('edit-photo-review-name')) return;
        if (!currentPhotoReview) return;
        currentPhotoReview.outputFilename = `${nameInput.value.trim() || 'scanned-pages'}.pdf`;
      });
    }

    for (const button of uploadContent.querySelectorAll('.photo-action-btn')) {
      button.addEventListener('click', async () => {
        const pageId = button.dataset.pageId;
        const action = button.dataset.action;
        if (guardCropLockedAction(`photo-review-${action}`)) return;
        if (action === 'crop') openPhotoCropEditor(pageId);
        if (action === 'rotate-left') await rotateReviewPage(pageId, -1);
        if (action === 'rotate-right') await rotateReviewPage(pageId, 1);
        if (action === 'refresh') await refreshPhotoPreview(pageId);
        if (action === 'toggle-delete') toggleReviewPageDeleted(pageId);
      });
    }
    const nextList = uploadContent.querySelector('.photo-review-list');
    if (nextList) {
      nextList.scrollTop = photoReviewScrollTop;
      nextList.addEventListener('scroll', () => {
        photoReviewScrollTop = nextList.scrollTop;
      });
    }
  }

  function renderSavedDocumentNaming() {
    if (!pendingSavedDocument) return;
    const suggested = pendingSavedDocument.suggested_filename || pendingSavedDocument.filename || 'scanned-document.pdf';
    const outputBaseName = suggested.replace(/\.pdf$/i, '');
    const originalBaseName = (pendingSavedDocument.filename || suggested).replace(/\.pdf$/i, '');
    const changed = outputBaseName !== originalBaseName;
    const namingSource = pendingSavedDocument.naming_source || 'original';
    const namingReason = pendingSavedDocument.naming_reason || '';
    const suggestionCopy = getNamingSuggestionCopy({
      suggested,
      changed,
      namingSource,
      namingReason,
    });
    uploadContent.innerHTML = `
      <div class="photo-review-shell">
        <div class="photo-review-head">
          <div>
            <div class="upload-title">OCR complete</div>
            <div class="upload-sub">${pendingSavedDocument.page_count} page${pendingSavedDocument.page_count === 1 ? '' : 's'} processed · confirm the final saved document name</div>
          </div>
          <div class="upload-actions photo-review-top-actions">
            <button id="saveNamedDocumentBtn" class="btn btn-primary" type="button">Save document</button>
            <button id="keepCurrentNameBtn" class="btn btn-remove" type="button">Keep current name</button>
          </div>
        </div>
        <div class="photo-review-name-row">
          <label class="photo-review-name-label" for="finalDocumentNameInput">Final document name</label>
          <div class="photo-review-name-field">
            <input id="finalDocumentNameInput" class="photo-review-name-input" type="text" value="${escapeHtml(outputBaseName)}" autocomplete="off" spellcheck="false">
            <span class="photo-review-name-ext">.pdf</span>
          </div>
          <div class="upload-sub">${suggestionCopy}</div>
        </div>
      </div>
    `;

    document.getElementById('saveNamedDocumentBtn').addEventListener('click', saveNamedDocument);
    document.getElementById('keepCurrentNameBtn').addEventListener('click', keepCurrentDocumentName);
    const input = document.getElementById('finalDocumentNameInput');
    if (input) {
      input.addEventListener('input', () => {
        if (!pendingSavedDocument) return;
        pendingSavedDocument.suggested_filename = `${input.value.trim() || 'scanned-document'}.pdf`;
      });
    }
  }

  function getNamingSuggestionCopy({ suggested, changed, namingSource, namingReason }) {
    if (namingSource === 'ai') {
      return `AI suggested this document name: "${escapeHtml(suggested)}"`;
    }
    if (namingSource === 'ocr') {
      return `OCR title guess: "${escapeHtml(suggested)}"`;
    }
    if (namingReason === 'heuristic_failed_kept_original') {
      return 'No strong title was found from OCR, so the current filename is being kept unless you change it.';
    }
    if (changed) {
      return `Suggested filename: "${escapeHtml(suggested)}"`;
    }
    return 'Current name is ready to save. You can still edit it before continuing.';
  }

  function openPhotoCropEditor(pageId) {
    if (guardCropLockedAction('open-photo-crop-editor', { showInlineError: false })) return;
    const page = getReviewPage(pageId);
    if (!page || page.deleted) return;
    setCropLockActive(true);
    rerenderPhotoReviewPreservingScroll();
    document.dispatchEvent(
      new CustomEvent('dlyte:open-photo-crop-editor', {
        detail: {
          pageId: page.id,
          filename: page.filename,
          detected: page.detected,
          originalUrl: page.originalUrl,
          corners: page.corners.map((point) => ({ x: point.x, y: point.y })),
        },
      })
    );
  }

  async function refreshPhotoPreview(pageId) {
    if (guardCropLockedAction('refresh-photo-preview', { showInlineError: false })) return;
    const page = getReviewPage(pageId);
    if (!page || !currentPhotoReview) return;
    try {
      const res = await fetch(`/api/photo-review/${encodeURIComponent(currentPhotoReview.job_id)}/preview`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          pageId,
          corners: page.corners,
          rotationTurns: page.rotationTurns,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        showError(data.error || 'Could not refresh page preview.');
        return;
      }
      const index = currentPhotoReview.pages.findIndex((entry) => entry.id === pageId);
      if (index >= 0) currentPhotoReview.pages[index] = data.page;
      clearError();
      rerenderPhotoReviewPreservingScroll();
    } catch (err) {
      showError('Could not refresh photographed-page preview.');
    }
  }

  async function rotateReviewPage(pageId, delta) {
    if (guardCropLockedAction('rotate-photo-page', { showInlineError: false })) return;
    const page = getReviewPage(pageId);
    if (!page) return;
    page.rotationTurns = (((page.rotationTurns || 0) + delta) % 4 + 4) % 4;
    await refreshPhotoPreview(pageId);
  }

  async function finalizePhotoReview() {
    if (!currentPhotoReview) return;
    if (guardCropLockedAction('finalize-photo-review')) return;
    clearError();
    ocrState = 'processing';
    processingLabel = null;
    renderUpload();

    try {
      const res = await fetch(`/api/photo-review/${encodeURIComponent(currentPhotoReview.job_id)}/finalize`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          pages: currentPhotoReview.pages.map((page) => ({
            id: page.id,
            corners: page.corners,
            rotationTurns: page.rotationTurns,
            deleted: page.deleted,
          })),
          outputFilename: currentPhotoReview.outputFilename,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        ocrState = 'reviewing';
        showError(data.error || 'Could not create PDF from photographed pages.');
        renderUpload();
        return;
      }

      pendingSavedDocument = {
        job_id: data.job_id,
        filename: data.filename,
        suggested_filename: data.suggested_filename || data.filename,
        naming_source: data.naming_source || 'original',
        naming_reason: data.naming_reason || '',
        naming_details: data.naming_details || null,
        naming_debug: data.naming_debug || null,
        page_count: data.page_count,
        full_text: data.full_text,
        pages: data.pages,
        warnings: data.warnings || [],
        status: data.status,
      };
      currentPhotoReview = null;
      ocrState = 'naming';
      renderUpload();
      document.dispatchEvent(new CustomEvent('dlyte:close-photo-crop-editor'));
    } catch (err) {
      ocrState = 'reviewing';
      showError('Could not finalize photographed pages.');
      renderUpload();
    }
  }

  async function saveNamedDocument() {
    if (!pendingSavedDocument) return;
    clearError();
    ocrState = 'processing';
    processingLabel = null;
    renderUpload();
    const desiredFilename = pendingSavedDocument.suggested_filename || pendingSavedDocument.filename;

    try {
      const res = await fetch(`/api/files/${encodeURIComponent(pendingSavedDocument.job_id)}/rename`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: desiredFilename }),
      });
      const data = await res.json();
      if (!res.ok) {
        ocrState = 'naming';
        showError(data.error || 'Could not save the final document name.');
        renderUpload();
        return;
      }

      currentDocument = {
        job_id: pendingSavedDocument.job_id,
        filename: data.filename,
        page_count: data.page_count,
        full_text: data.full_text,
        pages: data.pages,
      };
      pendingSavedDocument = null;
      ocrState = 'succeeded';
      renderUpload();

      const note = data.status === 'partial'
        ? ` OCR completed with ${data.warnings.length} warning(s).`
        : ' OCR is complete.';
      addAssistantMessage(
        `Document saved and ready: "${data.filename}" (${data.page_count} page${data.page_count === 1 ? '' : 's'}) from photographed pages.${note} You can ask me about it now.`
      );
      render();
      refreshFilesList();
    } catch (err) {
      ocrState = 'naming';
      showError('Could not save the final document name.');
      renderUpload();
    }
  }

  function keepCurrentDocumentName() {
    if (!pendingSavedDocument) return;
    pendingSavedDocument.suggested_filename = pendingSavedDocument.filename;
    saveNamedDocument();
  }

  uploadArea.addEventListener('click', () => {
    if (guardCropLockedAction('open-upload-picker')) return;
    if (ocrState === 'idle' || (ocrState === 'pending' && hasPendingImageBatch())) pdfInput.click();
  });
  uploadArea.addEventListener('dragover', (e) => {
    e.preventDefault();
    if (guardCropLockedAction('drag-upload-files', { showInlineError: false })) return;
    if (ocrState === 'idle' || (ocrState === 'pending' && hasPendingImageBatch())) uploadArea.classList.add('drag');
  });
  uploadArea.addEventListener('dragleave', () => uploadArea.classList.remove('drag'));
  uploadArea.addEventListener('drop', (e) => {
    e.preventDefault();
    uploadArea.classList.remove('drag');
    if (guardCropLockedAction('drop-upload-files')) return;
    if (ocrState !== 'idle' && !(ocrState === 'pending' && hasPendingImageBatch())) return;
    if (e.dataTransfer.files.length) handleFilesSelected(e.dataTransfer.files);
  });
  pdfInput.addEventListener('change', () => {
    if (guardCropLockedAction('change-upload-input')) {
      pdfInput.value = '';
      return;
    }
    if (pdfInput.files.length) handleFilesSelected(pdfInput.files);
  });
  document.addEventListener('dlyte:confirm-photo-crop-editor', async (event) => {
    const detail = event.detail || {};
    if (!detail.pageId || !Array.isArray(detail.corners)) return;
    const page = getReviewPage(detail.pageId);
    if (!page) return;
    page.corners = detail.corners.map((point) => ({ x: point.x, y: point.y }));
    try {
      const res = await fetch(`/api/photo-review/${encodeURIComponent(currentPhotoReview.job_id)}/preview`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          pageId: detail.pageId,
          corners: page.corners,
          rotationTurns: page.rotationTurns,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        showError(data.error || 'Could not refresh page preview.');
        return;
      }
      const index = currentPhotoReview.pages.findIndex((entry) => entry.id === detail.pageId);
      if (index >= 0) currentPhotoReview.pages[index] = data.page;
      clearError();
    } catch (err) {
      showError('Could not refresh photographed-page preview.');
      return;
    }
    document.dispatchEvent(new CustomEvent('dlyte:close-photo-crop-editor'));
  });
  document.addEventListener('dlyte:close-photo-crop-editor', () => {
    setCropLockActive(false);
    if (ocrState === 'reviewing') {
      rerenderPhotoReviewPreservingScroll();
    }
  });
  document.addEventListener('dlyte:photo-crop-lock-changed', (event) => {
    syncCropLockState(Boolean(event.detail?.active));
    if (ocrState === 'reviewing') {
      rerenderPhotoReviewPreservingScroll();
    }
    refreshFilesList();
  });
  document.addEventListener('dlyte:documents-scanned', refreshFilesList);
  document.addEventListener('dlyte:start-photo-review-from-email', async (event) => {
    if (guardCropLockedAction('start-photo-review-from-email')) return;
    const detail = event.detail || {};
    if (!detail.messageId) return;
    clearError();
    ocrState = 'processing';
    processingLabel = 'Loading photo pages for review…';
    pendingFiles = [];
    currentDocument = null;
    pendingSavedDocument = null;
    setCropLockActive(false);
    renderUpload();
    try {
      const res = await fetch('/api/photo-review/from-email', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messageId: detail.messageId,
          attachmentIds: detail.attachmentIds || null,
        }),
      });
      const data = await res.json();
      processingLabel = null;
      if (!res.ok) {
        ocrState = 'failed';
        showError(data.error || 'Could not prepare photographed attachment review.');
        renderUpload();
        return;
      }
      currentPhotoReview = data.review;
      ocrState = 'reviewing';
      renderUpload();
    } catch (err) {
      processingLabel = null;
      ocrState = 'failed';
      showError('Could not reach the server for photographed attachment review.');
      renderUpload();
    }
  });

  // Catches a follow-up draft the scheduler generated on its own — nobody
  // has to be actively chatting for the Send now button to show up.
  // isFollowupAnnouncement/followupKey are persisted on the message object
  // itself (not just held in a JS variable) so both "Later" and the manual
  // Dismiss button below still find and remove the right message even
  // after a page reload, when any in-memory reference would already be gone.
  // Seeded from any already-persisted announcement so a page reload
  // doesn't treat the same still-pending draft as new and push a
  // duplicate message — the in-memory dedup key would otherwise reset
  // to null on every reload while the draft itself is unchanged.
  let lastAnnouncedLinkedinDraft = (messages.find((m) => m.isFollowupAnnouncement) || {}).followupKey || null;
  function pollLinkedinSendPending() {
    if (!linkedinSendConfirmBackdrop.hidden || linkedinSendConfirmBtn.disabled) return;
    fetch('/api/linkedin/send-pending')
      .then((res) => res.json())
      .then((data) => {
        const pending = data.pending || null;
        showLinkedinSendDraft(pending);
        const key = pending ? pending.person_name + '|' + pending.message_text : null;
        if (key && key !== lastAnnouncedLinkedinDraft) {
          const msg = {
            role: 'assistant',
            content: `Follow-up drafted for ${pending.person_name}: "${pending.message_text}" — Send now button is above, waiting on your review.`,
            isFollowupAnnouncement: true,
            followupKey: key,
          };
          messages.push(msg);
          saveMessages();
          render();
        }
        lastAnnouncedLinkedinDraft = key;
      })
      .catch(() => {});
  }
  setInterval(pollLinkedinSendPending, 30000);
  pollLinkedinSendPending();

  function dismissFollowupAnnouncement() {
    // Removes every flagged announcement, not just one — self-heals any
    // duplicates left over from before the reload-dedup fix above.
    const before = messages.length;
    messages = messages.filter((m) => !m.isFollowupAnnouncement);
    if (messages.length !== before) {
      saveMessages();
      render();
    }
  }

  // Fired when the user dismisses the Home follow-up popup with "Later" —
  // pulls the chat announcement back out too, since it's just clutter at
  // that point (the person may be in the middle of something unrelated).
  // This only removes the chat message — the Home badge/notification stays,
  // since that's driven by the real follow_up DB state, not by chat content.
  document.addEventListener('dlyte:dismiss-followup-announcement', dismissFollowupAnnouncement);

  render();
  renderUpload();
  resizeInput();
  refreshFilesList();
})();
