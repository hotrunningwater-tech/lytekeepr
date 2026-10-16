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
  const emailConfirmTo = document.getElementById('emailConfirmTo');
  const emailConfirmSubject = document.getElementById('emailConfirmSubject');
  const emailConfirmBody = document.getElementById('emailConfirmBody');
  const emailSendConfirmBtn = document.getElementById('emailSendConfirmBtn');
  const emailSendCancelBtn = document.getElementById('emailSendCancelBtn');

  let messages = loadMessages();
  let isLoading = false;

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
    await saveEmailDraftEdits();         // anything typed into the draft card is on the server before the AI is asked about it

    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages }),
      });
      const data = await res.json();

      if (!res.ok) {
        showError(data.error || 'Something went wrong talking to the assistant.');
      } else {
        addAssistantMessage(data.reply);
        showLinkedinSendDraft(data.pendingLinkedinSend || null);
        showEmailSendDraft(data.pendingEmailSend || null);
        bulkDismissedId = null;
        checkBulkEmail();
      }
    } catch (err) {
      showError('Could not reach the server. Check that it is running and try again.');
    } finally {
      setBusy(false);
      input.focus();
    }
  }

  sendBtn.addEventListener('click', sendMessage);
  // Used by the contact page chat: hand a request over to this chat as if the user had typed it.
  window.lkHomeChat = {
    send: (text) => { input.value = text; resizeInput(); sendMessage(); },
    // A draft made elsewhere (e.g. a call's follow-up): say so in the chat and bring the draft card up now.
    showDraft: (note) => { messages.push({ role: 'assistant', content: note }); saveMessages(); render(); pollEmailSendPending(); },
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
  input.addEventListener('input', resizeInput);
  clearBtn.addEventListener('click', () => {
    messages = [{ role: 'assistant', content: GREETING }];
    saveMessages();
    clearError();
    render();
    showLinkedinSendDraft(null);
    fetch('/api/linkedin/send-pending/cancel', { method: 'POST' }).catch(() => {});
    showEmailSendDraft(null);
    fetch('/api/email/send-pending/cancel', { method: 'POST' }).catch(() => {});
    fetch('/api/funnel-email/reset', { method: 'POST' }).catch(() => {});      // forget the funnel and stage picked so far
    bulkTemplateRow.hidden = true;
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

  let emailDraftTo = '';
  let emailDraftSubjectEl = null;
  let emailDraftBodyEl = null;
  function growEmailBody() {
    if (!emailDraftBodyEl) return;
    emailDraftBodyEl.style.height = 'auto';
    emailDraftBodyEl.style.height = Math.min(emailDraftBodyEl.scrollHeight + 4, 360) + 'px';
  }
  function showEmailSendDraft(pending) {
    if (!pending) {
      emailSendDraftRow.hidden = true;
      return;
    }
    // Don't rebuild the card while the user is typing in it (the periodic check would otherwise wipe their edits).
    if (!emailSendDraftRow.hidden && emailDraftBodyEl && document.activeElement === emailDraftBodyEl) return;
    emailDraftTo = `${pending.person_name} (${pending.to_email})`;
    emailSendDraftText.replaceChildren();
    const meta = document.createElement('div');
    meta.className = 'email-draft-meta';
    meta.innerHTML = '<span>To</span> ';
    meta.appendChild(document.createTextNode(emailDraftTo));
    const subjRow = document.createElement('div');
    subjRow.className = 'email-draft-meta email-draft-subjrow';
    subjRow.innerHTML = '<span>Subject</span> ';
    emailDraftSubjectEl = document.createElement('input');
    emailDraftSubjectEl.type = 'text';
    emailDraftSubjectEl.className = 'email-draft-subject';
    emailDraftSubjectEl.value = pending.subject || '';
    emailDraftSubjectEl.placeholder = '(no subject)';
    subjRow.appendChild(emailDraftSubjectEl);
    emailDraftBodyEl = document.createElement('textarea');
    emailDraftBodyEl.className = 'email-draft-body';
    emailDraftBodyEl.value = pending.body || '';
    emailDraftBodyEl.spellcheck = true;
    emailDraftBodyEl.addEventListener('input', growEmailBody);
    emailSendDraftText.append(meta, subjRow, emailDraftBodyEl);
    const hint = document.createElement('div');
    hint.className = 'email-draft-hint';
    hint.textContent = 'You can edit the subject and the text. Send now sends exactly what you see. Ask me to fix or restyle it and I will start from your version.';
    emailSendDraftText.appendChild(hint);
    emailSendDraftRow.hidden = false;
    requestAnimationFrame(growEmailBody);
  }
  // Keep the server's copy equal to what is on screen, so Send now and the AI both use the user's edits.
  function saveEmailDraftEdits() {
    if (!emailDraftBodyEl || emailSendDraftRow.hidden) return Promise.resolve();
    return fetch('/api/email/send-pending/update', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ subject: emailDraftSubjectEl.value, body: emailDraftBodyEl.value }),
    }).catch(() => {});
  }
  document.addEventListener('focusout', (e) => {
    if (e.target === emailDraftBodyEl || e.target === emailDraftSubjectEl) saveEmailDraftEdits();
  });

  emailSendNowBtn.addEventListener('click', () => {
    saveEmailDraftEdits().then(() => {
      emailConfirmTo.textContent = emailDraftTo;
      emailConfirmSubject.textContent = emailDraftSubjectEl.value.trim() || '(no subject)';
      emailConfirmBody.textContent = emailDraftBodyEl.value;
      emailSendConfirmBackdrop.hidden = false;
    });
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

  // An email draft waiting on the server comes back after a page reload.
  function pollEmailSendPending() {
    if (!emailSendConfirmBackdrop.hidden || emailSendConfirmBtn.disabled) return;
    fetch('/api/email/send-pending')
      .then((res) => res.json())
      .then((data) => {
        const pending = data.pending || null;
        // A draft that was on screen and is suddenly gone (expired or cleared on the server) is announced, never dropped silently.
        // Sending and cancelling hide the card first, so they never trigger this.
        if (!pending && !emailSendDraftRow.hidden) {
          showEmailSendDraft(null);
          messages.push({ role: 'assistant', content: 'The email draft above is no longer available (it expired or was cleared). Ask me to write it again.' });
          saveMessages();
          render();
          return;
        }
        showEmailSendDraft(pending);
      })
      .catch(() => {});
  }
  setInterval(pollEmailSendPending, 30000);
  pollEmailSendPending();

  // ---- Funnel email, in three steps: (1) the one email, shown in the chat and edited here; (2) every person's copy in a popup,
  // each editable on its own; (3) the send, one email at a time with a pause. The user approves between the steps. ----
  const bulkBackdrop = document.getElementById('bulkEmailBackdrop');
  const bulkTitle = document.getElementById('bulkEmailTitle');
  const bulkSummary = document.getElementById('bulkEmailSummary');
  const bulkList = document.getElementById('bulkEmailList');
  const bulkProgress = document.getElementById('bulkEmailProgress');
  const bulkSendBtn = document.getElementById('bulkEmailSendBtn');
  const bulkBackBtn = document.getElementById('bulkEmailBackBtn');
  const bulkCancelBtn = document.getElementById('bulkEmailCancelBtn');
  const bulkPause = document.getElementById('bulkPause');
  const bulkTemplateRow = document.getElementById('bulkTemplateRow');
  const bulkTemplateText = document.getElementById('bulkTemplateText');
  const bulkApproveBtn = document.getElementById('bulkApproveBtn');
  const bulkTemplateCancelBtn = document.getElementById('bulkTemplateCancelBtn');
  let bulkBatch = null;            // the batch on screen (the server's preview)
  let bulkOff = new Set();         // person ids unticked
  let bulkOverrides = {};          // person id -> {subject, body}: copies the user edited by hand
  let bulkTpl = { subject: null, body: null };   // the template inputs in the chat card
  let bulkDismissedId = null;      // popup closed with Esc: do not pop it open again by itself
  let bulkSending = false;
  let bulkTimer = null;
  let bulkSaveTimer = null;

  function bulkIncluded() { return bulkBatch ? bulkBatch.recipients.filter((r) => !bulkOff.has(r.person_id)) : []; }
  function bulkPost(path, payload) {
    return fetch('/api/email/bulk-pending/' + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload || {}) })
      .then((res) => res.json().then((data) => ({ ok: res.ok, data }))).catch(() => ({ ok: false, data: {} }));
  }
  function bulkSaveNow() {
    clearTimeout(bulkSaveTimer);
    if (!bulkBatch || bulkSending) return Promise.resolve();
    const payload = { excluded: Array.from(bulkOff), overrides: bulkOverrides };
    if (bulkTpl.subject) { payload.subject = bulkTpl.subject.value; payload.body = bulkTpl.body.value; }
    return bulkPost('update', payload);
  }
  function bulkQueueSave() { clearTimeout(bulkSaveTimer); bulkSaveTimer = setTimeout(bulkSaveNow, 700); }

  // ----- step 1: the one email, in the chat
  function growBulkBody() {
    if (!bulkTpl.body) return;
    bulkTpl.body.style.height = 'auto';
    bulkTpl.body.style.height = Math.min(bulkTpl.body.scrollHeight + 4, 360) + 'px';
  }
  function showBulkTemplate(batch) {
    if (!bulkTemplateRow.hidden && bulkTpl.body && (document.activeElement === bulkTpl.body || document.activeElement === bulkTpl.subject)) return;
    bulkBatch = batch;
    bulkOff = new Set(batch.recipients.filter((r) => r.included === false).map((r) => r.person_id));
    bulkTemplateText.replaceChildren();
    const meta = document.createElement('div');
    meta.className = 'email-draft-meta';
    meta.innerHTML = '<span>To</span> ';
    meta.appendChild(document.createTextNode(batch.recipients.length + ' people · ' + batch.stage_name + ' · ' + batch.funnel_name));
    if (batch.phase === 'review') {
      bulkTpl = { subject: null, body: null };
      const note = document.createElement('div');
      note.className = 'email-draft-hint';
      note.textContent = 'Your email is approved. The copies for each person are ready to check.';
      bulkTemplateText.append(meta, note);
      bulkApproveBtn.textContent = 'Open review';
      bulkApproveBtn.onclick = () => { bulkDismissedId = null; openBulk(batch); };
    } else {
      const subjRow = document.createElement('div');
      subjRow.className = 'email-draft-meta email-draft-subjrow';
      subjRow.innerHTML = '<span>Subject</span> ';
      const subj = document.createElement('input');
      subj.type = 'text';
      subj.className = 'email-draft-subject';
      subj.value = batch.subject || '';
      subj.placeholder = '(no subject)';
      subjRow.appendChild(subj);
      const body = document.createElement('textarea');
      body.className = 'email-draft-body';
      body.value = batch.body || '';
      body.spellcheck = true;
      bulkTpl = { subject: subj, body };
      body.addEventListener('input', () => { growBulkBody(); bulkQueueSave(); });
      subj.addEventListener('input', bulkQueueSave);
      const hint = document.createElement('div');
      hint.className = 'email-draft-hint';
      hint.textContent = '{{first_name}} becomes each person\'s first name; someone with no name just gets "Hi,". Edit it here, then approve to see every copy.';
      bulkTemplateText.append(meta, subjRow, body, hint);
      bulkApproveBtn.textContent = 'Approve';
      bulkApproveBtn.onclick = async () => {
        await bulkSaveNow();
        const r = await bulkPost('phase', { phase: 'review' });
        if (r.ok && r.data.pending) { bulkDismissedId = null; openBulk(r.data.pending); }
      };
      requestAnimationFrame(growBulkBody);
    }
    bulkTemplateRow.hidden = false;
  }
  bulkTemplateCancelBtn.addEventListener('click', () => {
    bulkTemplateRow.hidden = true;
    bulkBatch = null;
    bulkTpl = { subject: null, body: null };
    bulkPost('cancel');
  });

  // ----- step 2: every person's copy, each editable
  function refreshBulkButton() {
    const n = bulkIncluded().length;
    bulkSendBtn.textContent = bulkSending ? 'Sending…' : 'Send ' + n + (n === 1 ? ' email' : ' emails');
    bulkSendBtn.disabled = bulkSending || n === 0;
  }
  function renderBulkList(statusById) {
    bulkList.replaceChildren();
    bulkBatch.recipients.forEach((r) => {
      const item = document.createElement('div');
      item.className = 'bulk-item' + (bulkOff.has(r.person_id) ? ' is-off' : '');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = !bulkOff.has(r.person_id);
      cb.disabled = bulkSending;
      cb.addEventListener('change', () => {
        if (cb.checked) bulkOff.delete(r.person_id); else bulkOff.add(r.person_id);
        item.classList.toggle('is-off', !cb.checked);
        refreshBulkButton();
        bulkQueueSave();
      });
      const who = document.createElement('div');
      who.className = 'bulk-who';
      who.textContent = r.name;
      const mail = document.createElement('small');
      mail.textContent = r.email;
      who.appendChild(mail);
      const flag = document.createElement('span');
      flag.className = 'bulk-flag';
      const st = statusById && statusById[r.person_id];
      const setEditedFlag = () => {
        if (st) return;
        const edited = bulkOverrides[r.person_id] || r.edited;
        flag.textContent = edited ? 'Edited' : (r.last_emailed ? 'Emailed before ' + new Date(r.last_emailed).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : '');
        flag.classList.toggle('edited', !!edited);
      };
      if (st) {
        flag.textContent = st.status === 'sent' ? 'Sent' : st.status === 'failed' ? 'Failed' : 'Waiting';
        if (st.status === 'sent') flag.classList.add('ok');
        if (st.status === 'failed') { flag.classList.add('bad'); flag.title = st.message || ''; }
      } else {
        setEditedFlag();
      }
      const edit = document.createElement('div');
      edit.className = 'bulk-mail-edit';
      const subj = document.createElement('input');
      subj.type = 'text';
      subj.value = (bulkOverrides[r.person_id] || {}).subject ?? r.subject_out;
      const body = document.createElement('textarea');
      body.value = (bulkOverrides[r.person_id] || {}).body ?? r.body_out;
      body.spellcheck = true;
      subj.disabled = body.disabled = bulkSending;
      const changed = () => {
        bulkOverrides[r.person_id] = { subject: subj.value, body: body.value };
        setEditedFlag();
        bulkQueueSave();
      };
      subj.addEventListener('input', changed);
      body.addEventListener('input', changed);
      edit.append(subj, body);
      item.append(cb, who, flag, edit);
      bulkList.appendChild(item);
    });
  }
  function openBulk(batch) {
    bulkSending = false;
    bulkBatch = batch;
    bulkOff = new Set(batch.recipients.filter((r) => r.included === false).map((r) => r.person_id));
    bulkOverrides = {};
    bulkTpl = { subject: null, body: null };
    bulkTemplateRow.hidden = true;
    bulkProgress.hidden = true;
    bulkBackBtn.hidden = false;
    bulkPause.disabled = false;
    bulkCancelBtn.textContent = 'Cancel';
    bulkTitle.textContent = 'Check every email · ' + batch.stage_name + ' · ' + batch.funnel_name;
    const skipped = batch.skipped || [];
    bulkSummary.textContent = batch.recipients.length + ' emails. Each one is exactly what that person will get.'
      + (skipped.length ? ' Skipped, no email address: ' + skipped.map((x) => x.name).join(', ') + '.' : '');
    renderBulkList(null);
    refreshBulkButton();
    bulkBackdrop.hidden = false;
  }
  bulkBackBtn.addEventListener('click', async () => {
    if (bulkSending) return;
    await bulkSaveNow();
    const r = await bulkPost('phase', { phase: 'template' });
    bulkBackdrop.hidden = true;
    if (r.ok && r.data.pending) showBulkTemplate(r.data.pending);
  });

  // ----- step 3: sending, one at a time
  function showBulkProgress(job) {
    const done = job.results.filter((r) => r.status === 'sent' || r.status === 'failed').length;
    const failed = job.results.filter((r) => r.status === 'failed').length;
    const byId = {};
    job.results.forEach((r) => { byId[r.person_id] = r; });
    bulkProgress.hidden = false;
    if (job.status === 'running') {
      bulkProgress.textContent = 'Sent ' + done + ' of ' + job.total + ', one every ' + (job.pause || 10) + ' seconds. You can close this window; sending carries on.';
    } else {
      const ok = done - failed;
      bulkProgress.textContent = (job.status === 'interrupted' ? 'Stopped early (the server restarted). ' : 'Done. ') + ok + ' sent' + (failed ? ', ' + failed + ' failed (hover "Failed" for the reason)' : '') + '.';
    }
    if (bulkBatch) renderBulkList(byId);
    return { done, failed };
  }
  function pollBulkJob() {
    clearTimeout(bulkTimer);
    fetch('/api/email/bulk-pending').then((res) => res.json()).then((data) => {
      const job = data.job;
      if (!job || !bulkBatch || job.id !== bulkBatch.id) return;
      const { done, failed } = showBulkProgress(job);
      if (job.status === 'running') { bulkTimer = setTimeout(pollBulkJob, 1500); return; }
      bulkSending = false;
      bulkSendBtn.disabled = true;
      bulkSendBtn.textContent = 'Done';
      bulkCancelBtn.textContent = 'Close';
      const ok = done - failed;
      messages.push({ role: 'assistant', content: 'Funnel email finished: ' + ok + ' sent' + (failed ? ', ' + failed + ' failed' : '') + '. Each is saved on the contact and marked "email sent".' });
      saveMessages();
      render();
      document.dispatchEvent(new Event('lk-contacts-changed'));
    }).catch(() => { bulkTimer = setTimeout(pollBulkJob, 3000); });
  }
  bulkSendBtn.addEventListener('click', async () => {
    if (!bulkBatch || bulkSending) return;
    await bulkSaveNow();
    if (!bulkIncluded().length) return;
    bulkSending = true;
    bulkPause.disabled = true;
    bulkBackBtn.hidden = true;
    refreshBulkButton();
    renderBulkList(null);
    const r = await bulkPost('send', { id: bulkBatch.id, pause: parseInt(bulkPause.value, 10) });
    if (!r.ok) {
      bulkSending = false;
      bulkPause.disabled = false;
      bulkBackBtn.hidden = false;
      bulkProgress.hidden = false;
      bulkProgress.textContent = r.data.message || 'Could not start sending.';
      renderBulkList(null);
      refreshBulkButton();
      return;
    }
    pollBulkJob();
  });
  bulkCancelBtn.addEventListener('click', () => {
    bulkBackdrop.hidden = true;
    if (bulkSending) return;                                   // sending carries on in the background
    if (bulkCancelBtn.textContent !== 'Close') bulkPost('cancel');
    bulkBatch = null;
    bulkTemplateRow.hidden = true;
  });

  // A staged batch (or one being sent) is looked for after every chat reply, after a reload, and every 30 seconds.
  function checkBulkEmail() {
    if (!bulkBackdrop.hidden && bulkBatch) return;
    fetch('/api/email/bulk-pending').then((res) => res.json()).then((data) => {
      const pending = data.pending;
      if (!pending) {
        if (!bulkSending && bulkBackdrop.hidden) { bulkTemplateRow.hidden = true; if (!(data.job && data.job.status === 'running')) bulkBatch = null; }
        return;
      }
      if (pending.phase === 'review' && pending.id !== bulkDismissedId) { openBulk(pending); return; }
      showBulkTemplate(pending);
    }).catch(() => {});
  }
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !bulkBackdrop.hidden && bulkBatch) {
      bulkDismissedId = bulkBatch.id;
      if (!bulkSending) setTimeout(checkBulkEmail, 50);          // the card comes back so the review can be reopened
    }
  });
  setInterval(checkBulkEmail, 30000);
  checkBulkEmail();

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
  resizeInput();
})();
