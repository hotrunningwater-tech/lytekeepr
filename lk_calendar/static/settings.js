/* Contacts: import wizard (scan + match fields), add-contact form, saved mappings. Used by the CRM tab and the Settings panel. */
(function () {
  'use strict';
  var CH = { linkedin: 'LinkedIn', instagram: 'Instagram', email: 'Email' };
  var S = { imp: null };
  var imp = { root: null, onDone: null };

  function h(tag, attrs, kids) {
    var el = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === 'class') el.className = attrs[k];
      else if (k === 'style') el.style.cssText = attrs[k];
      else if (k.slice(0, 2) === 'on') el.addEventListener(k.slice(2), attrs[k]);
      else if (attrs[k] === true) el.setAttribute(k, '');
      else if (attrs[k] !== false && attrs[k] != null) el.setAttribute(k, attrs[k]);
    });
    [].concat(kids == null ? [] : kids).flat(Infinity).forEach(function (c) {
      if (c == null || c === false) return;
      el.appendChild(typeof c === 'string' || typeof c === 'number' ? document.createTextNode(String(c)) : c);
    });
    return el;
  }
  function api(method, url, body, isForm) {
    return fetch(url, { method: method, headers: body && !isForm ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? (isForm ? body : JSON.stringify(body)) : undefined }).then(function (res) {
      if (res.status === 401) { location.href = (window.LK_PREFIX || '') + '/login'; throw new Error('login required'); }
      return res.text().then(function (text) {
        var data = null; try { data = JSON.parse(text); } catch (e) { /* a web server page */ }
        if (!res.ok || data === null) {
          if (data && data.error) throw new Error(data.error);
          throw new Error(res.status === 502 || res.status === 504 ? 'The server took too long to answer. Please check before repeating it.' : 'Unexpected answer from the server (HTTP ' + res.status + ').');
        }
        return data;
      });
    });
  }
  function fieldLabel(scan, key) { var f = scan.fields.filter(function (x) { return x.key === key; })[0]; return f ? f.label : key; }
  function slice(arr, n) { return arr.slice(0, n); }

  function changed() { document.dispatchEvent(new Event('lk-contacts-changed')); }
  function importRender() { if (!imp.root) return; imp.root.replaceChildren(); importView(); }
  window.lkContacts = {
    mountImport: function (root, onDone) { imp.root = root; imp.onDone = function () { changed(); if (onDone) onDone(); }; S.imp = null; importRender(); },
    addContact: function (preset, onSaved) { modal(contactForm(preset || {}, onSaved)); },
    anotherContact: function (companyId) {
      api('GET', '/api/calendar/companies/' + companyId).then(function (co) {
        var f = co.contacts[0] || {};
        window.lkContacts.addContact({ company_id: co.id, company: co.name, channel: f.channel || '', location: f.location || '', industry: f.industry || '',
          website: f.website || co.website || '', batch: f.batch || '' });
      }).catch(function (e) { alert(e.message); });
    }
  };

  /* Outreach card on a person: status per channel + a pop-out of pre-coded choices */
  window.lkContacts.renderOutreach = function (box, personId, funnelId) {
    box.textContent = 'Loading…';
    api('GET', '/api/calendar/people/' + personId + '/outreach').then(function (r) {
      box.replaceChildren();
      var go = funnelId
        ? h('button', { class: 'btn btn-secondary btn-small', type: 'button', onclick: function () { window.lkGoToFunnel(funnelId); } }, 'Go to funnel')
        : h('button', { class: 'btn btn-primary btn-small', type: 'button', onclick: function () { window.lkOpenMove([personId]); } }, 'Move to funnel');
      if (funnelId) r.channels.forEach(function (c) {
        box.appendChild(h('div', { class: 'lks-outreach' }, [h('div', {}, [h('span', { class: 'crm-channel-tag crm-channel-tag-' + c.channel }, c.label),
          h('b', {}, (!c.lead_id || c.status === 'new') ? 'Not started' : c.status),
          c.status_date && c.lead_id ? h('span', { class: 'lks-note' }, 'since ' + c.status_date + (c.follow_up_date ? ' · follow-up ' + c.follow_up_date : '')) : null])]));
      });
      box.appendChild(h('div', { class: 'lks-row', style: 'margin-top:8px' }, [go]));
    }).catch(function (e) { box.textContent = e.message; });
  };
  window.lkContacts.openStatus = function (personId, channel) {
    api('GET', '/api/calendar/people/' + personId + '/outreach').then(function (r) {
      var c = r.channels.filter(function (x) { return x.channel === channel; })[0];
      if (c) outreachPopout(personId, c);
    }).catch(function (e) { alert(e.message); });
  };
  function outreachPopout(personId, c, box) {
    var choice = c.status || null, err = h('p', { class: 'lks-err', hidden: true });
    var pills = h('div', { class: 'lks-choices' });
    function paint() {
      pills.replaceChildren.apply(pills, c.options.map(function (o) {
        return h('button', { type: 'button', class: 'lks-choice' + (choice === o ? ' on' : ''), onclick: function () { choice = o; err.hidden = true; paint(); } }, o);
      }));
    }
    paint();
    modal(h('div', { class: 'lks-card' }, [
      h('h2', {}, c.label + ': what happened?'),
      h('p', {}, 'Pick the status. The calendar task and the funnel stage follow from it.'),
      pills, err,
      h('div', { class: 'lks-row' }, [
        h('button', { class: 'btn btn-primary', type: 'button', onclick: function () {
          if (!choice) { err.textContent = 'Pick a status.'; err.hidden = false; return; }
          api('POST', '/api/calendar/people/' + personId + '/outreach', { channel: c.channel, status: choice }).then(function () {
            closeModal(true); changed(); document.dispatchEvent(new Event('lk-outreach-saved'));
          }).catch(function (e) { err.textContent = e.message; err.hidden = false; });
        } }, 'Save'),
        h('button', { class: 'btn lks-soft', type: 'button', onclick: closeModal }, 'Cancel')])
    ]));
  }



  /* Chips: words the AI/code marked inside the structured notes, as [[type:text]]. Drag one into a field, or click it and then click a field. */
  var MARK = /\[\[(name|company|software|role|other|email|phone|link):([^\]]+?)\]\]/g;
  var armed = null;
  function plain(text) { return String(text || '').replace(MARK, '$2'); }
  function renderMarked(text) {
    var box = h('div', { class: 'lks-marked' }), last = 0, m;
    MARK.lastIndex = 0;
    while ((m = MARK.exec(text)) !== null) {
      if (m.index > last) box.appendChild(document.createTextNode(text.slice(last, m.index)));
      box.appendChild(chip(m[1], m[2]));
      last = m.index + m[0].length;
    }
    if (last < text.length) box.appendChild(document.createTextNode(text.slice(last)));
    return box;
  }
  function disarm() { armed = null; document.querySelectorAll('.lks-chip2.armed').forEach(function (x) { x.classList.remove('armed'); }); }
  function chip(kind, word) {
    var el = h('span', { role: 'button', tabindex: 0, class: 'lks-chip2 lks-chip2-' + kind, title: 'Hold and drag into a field (or click it, then click a field)' }, word);
    el.addEventListener('pointerdown', function (e) {
      if (e.button !== 0) return;
      e.preventDefault();
      var sx = e.clientX, sy = e.clientY, moved = false, ghost = null, target = null;
      try { el.setPointerCapture(e.pointerId); } catch (x) { /* older browsers */ }
      function move(ev) {
        if (!moved && Math.hypot(ev.clientX - sx, ev.clientY - sy) > 5) {
          moved = true; ghost = h('div', { class: 'lks-ghost lks-chip2-' + kind }, word); document.body.appendChild(ghost); el.classList.add('lifted');
        }
        if (!moved) return;
        ghost.style.left = (ev.clientX + 10) + 'px'; ghost.style.top = (ev.clientY + 10) + 'px';
        var under = document.elementFromPoint(ev.clientX, ev.clientY), t = under && under.closest ? under.closest('.lks-target') : null;
        if (t !== target) { if (target) target.classList.remove('lks-drop-on'); target = t; if (target) target.classList.add('lks-drop-on'); }
      }
      function up() {
        el.removeEventListener('pointermove', move); el.removeEventListener('pointerup', up); el.removeEventListener('pointercancel', up);
        el.classList.remove('lifted'); if (ghost) ghost.remove();
        if (moved) { if (target) { target.classList.remove('lks-drop-on'); target.lksDrop(word); disarm(); } return; }
        var was = el.classList.contains('armed'); disarm(); if (!was) { armed = word; el.classList.add('armed'); }      // a plain click arms it
      }
      el.addEventListener('pointermove', move); el.addEventListener('pointerup', up); el.addEventListener('pointercancel', up);
    });
    return el;
  }

  /* A field that holds several values as removable chips (used for Job title / vocation). Its .value is the values joined with " · ". */
  var SEP = ' · ';
  var CHECKS = {
    email: function (w) { return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(w) ? '' : 'That doesn\'t look like an email address.'; },
    url: function (w) { return /^(https?:\/\/|www\.|(linkedin|instagram)\.com\/|@[\w.]+$)/i.test(w) ? '' : (w.indexOf('@') > 0 ? 'That is an email address, not a profile link.' : 'A profile link should be a web address (LinkedIn or Instagram).'); },
    phone: function (w) { var n = w.replace(/\D/g, '').length; return n >= 7 && n <= 15 ? '' : 'That doesn\'t look like a phone number.'; }
  };
  // opts: { multi: true|false, placeholder }. Single-value fields show their value as one chip (click it to edit, × to delete); a dropped chip replaces it.
  function chipField(initial, opts) {
    opts = opts || {};
    var check = opts.check || null;
    var multi = opts.multi !== false, items = [], box = h('div', { class: 'lks-chipfield' }), input = h('input', { type: 'text', placeholder: opts.placeholder || 'Add…' });
    function split(v) { return multi ? String(v || '').split(/\s*[·;|]\s*/).map(function (x) { return x.trim(); }).filter(Boolean) : (String(v || '').trim() ? [String(v).trim()] : []); }
    function changed() { box.dispatchEvent(new Event('input')); }
    function editTag(i, label) {
      var ed = h('input', { type: 'text', class: 'lks-tag-edit', value: items[i] });
      label.replaceWith(ed); ed.focus(); ed.select();
      function done(save) {
        var v = ed.value.trim();
        if (save) { var bad = v && check ? check(v) : ''; if (bad) { notice(bad); } else { if (v) items[i] = v; else items.splice(i, 1); changed(); } }
        render();
      }
      ed.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); done(true); } else if (e.key === 'Escape') { e.stopPropagation(); done(false); } });
      ed.addEventListener('blur', function () { done(true); });
    }
    function render() {
      var kids = items.map(function (x, i) {
        var label = h('span', { class: 'lks-tag-text', title: 'Click to edit' }, x);
        label.addEventListener('click', function (e) { e.stopPropagation(); if (!box.classList.contains('lks-ro')) editTag(i, label); });
        return h('span', { class: 'lks-tag' }, [label, h('button', { type: 'button', title: 'Remove', onclick: function (e) { e.stopPropagation(); items.splice(i, 1); render(); changed(); } }, '×')]);
      });
      if (multi || !items.length) kids.push(input);
      box.replaceChildren.apply(box, kids);
    }
    function add(word) {
      word = String(word || '').trim(); if (!word || box.classList.contains('lks-ro')) return;
      if (check) { var bad = check(word); if (bad) { notice(bad); return; } }
      if (multi) { if (items.indexOf(word) < 0) items.push(word); } else items = [word];
      render(); changed();
    }
    input.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); add(input.value); input.value = ''; input.focus(); } });
    input.addEventListener('blur', function () { if (input.value.trim()) { add(input.value); input.value = ''; } });
    Object.defineProperty(box, 'value', { get: function () { return items.join(SEP); }, set: function (v) { items = split(v); render(); } });
    box.lksChips = { add: add };
    box.classList.add('lks-target'); box.lksDrop = add;
    items = split(initial); render();
    return box;
  }
  function acceptChips(el) {
    el.classList.add('lks-target');
    if (el.lksChips) {
      el.addEventListener('click', function () { if (!armed) return; el.lksChips.add(armed); disarm(); });
      return;
    }
    function put(word) { el.value = el.tagName === 'TEXTAREA' && el.value.trim() ? el.value.trim() + '\n' + word : word; el.dispatchEvent(new Event('input')); }
    el.lksDrop = put;
    el.addEventListener('click', function () { if (!armed) return; put(armed); disarm(); });
  }

  /* =========================== CONTACT ASSISTANT (chat beside a contact) =========================== */
  var AI_LABELS = { name: 'Name', company: 'Company', job_title: 'Job title', email: 'Email', phone: 'Phone', profile_url: 'Profile link' };
  // opts: getContact() -> promise of the current field values; apply(fields, notesText) fills or saves them
  function assistantPanel(opts) {
    var messages = [], log = h('div', { class: 'lks-ai-log' }), busy = false, pending = null, offer = null;
    var CONFIRM = /^(yes|yep|yeah|ok|okay|good|great|perfect|confirmed?|approved?|apply( it| that| this)?|save( it| that| this)?|looks good|that'?s? (good|right|correct)|all good|do it|go ahead|sure|да|ок|хорошо)[\s.!]*$/i;
    var asking = opts.mode === 'ask';
    var input = h('textarea', { rows: 3, placeholder: asking ? 'Ask about this person, their notes, messages or funnel…' : 'Paste notes or an email about this person…' });
    var send = h('button', { class: 'btn btn-primary btn-small', type: 'button' }, 'Send');
    function say(who, text) { log.appendChild(h('div', { class: 'lks-ai-msg ' + who }, text)); log.scrollTop = log.scrollHeight; }
    function proposalCard(res, raw, current) {
      var p = res.proposal, picked = {}, rows = [];
      Object.keys(p.fields).forEach(function (k) {
        var cur = (current[k] || '').trim(), already = !!cur && cur.toLowerCase() !== p.fields[k].toLowerCase();
        picked[k] = !already;
        var cb = h('input', { type: 'checkbox', checked: picked[k], onchange: function () { picked[k] = this.checked; } });
        rows.push(h('label', { class: 'lks-ai-row' }, [cb, h('span', {}, [h('b', {}, AI_LABELS[k] || k), ' ', cur ? h('s', {}, cur + ' ') : null, '→ ' + p.fields[k],
          already ? h('em', {}, ' (already has a value: tick to replace)') : null, p.evidence && p.evidence[k] ? h('small', {}, '“' + p.evidence[k] + '”') : null])]));
      });
      var useNotes = !!p.notes, noteBox = null;
      if (p.notes) {
        var nb = h('input', { type: 'checkbox', checked: true, onchange: function () { useNotes = this.checked; } });
        noteBox = h('div', {}, [h('label', { class: 'lks-ai-row' }, [nb, h('b', {}, 'Notes (structured summary; your pasted text is saved untouched under Raw notes)')]), h('div', { class: 'lks-ai-notes' }, renderMarked(p.notes))]);
      }
      var extra = (p.entities || []).filter(function (e) { return (p.notes || '').indexOf('[[' + e.type + ':' + e.text + ']]') === -1; });
      var extraBox = extra.length ? h('div', { class: 'lks-marked' }, [h('div', { class: 'lks-note' }, 'Also found in your text (drag into a field):'), h('div', {}, extra.map(function (e) { return chip(e.type, e.text); }))]) : null;
      var card = h('div', { class: 'lks-ai-proposal' }, [h('div', { class: 'lks-note' }, 'Proposed update'), rows, noteBox, extraBox, res.duplicate ? h('p', { class: 'lks-err' }, res.duplicate) : null,
        h('div', { class: 'lks-row' }, [
          h('button', { class: 'btn btn-primary btn-small', type: 'button', onclick: function () { pending = { apply: applyNow, card: card }; confirmAndSave(); } }, opts.save ? 'Apply & save' : 'Apply'),
          h('button', { class: 'btn lks-soft btn-small', type: 'button', onclick: function () { pending = null; card.remove(); say('assistant', 'Okay, nothing changed.'); } }, 'Dismiss')])]);
      // What each field held when this proposal was made (an empty field is pre-filled with the proposal at once).
      // When the proposal is applied, a field you changed since then (even by clearing it) keeps YOUR value.
      var base = {}; Object.keys(p.fields).forEach(function (k) { base[k] = (current[k] || '').trim() || p.fields[k].trim(); });
      var notesBase = (current.notes || '').trim() || plain(p.notes || '').trim();
      function applyNow() {
        card.classList.add('done'); card.querySelectorAll('input,button').forEach(function (el) { el.disabled = true; });
        pending = null;
        return Promise.resolve(opts.getContact()).then(function (now) {
          var fields = {}, kept = [];
          Object.keys(picked).forEach(function (k) {
            if (!picked[k]) return;
            var have = ((now[k === 'profile_url' ? 'url' : k] || '') + '').trim();
            if (have !== base[k]) { kept.push(AI_LABELS[k] || k); return; }
            fields[k] = p.fields[k];
          });
          var notesChanged = ((now.notes || '') + '').trim() !== notesBase;
          var notes = useNotes && !notesChanged ? plain(p.notes) : '';
          return Promise.resolve(opts.apply(fields, notes, current.notes || '')).then(function () { return kept; });
        }).then(function (kept) { say('assistant', (opts.save ? 'Applied.' : 'Done. Check it over and keep going if there is more.') + (kept.length ? ' I left ' + kept.join(', ') + ' as you changed it.' : '')); })
          .catch(function (e) { say('assistant', 'Could not apply: ' + e.message); throw e; });
      }
      pending = { apply: applyNow, card: card };
      log.appendChild(card); log.scrollTop = log.scrollHeight;
    }
    function confirmAndSave() {          // "confirmed": apply the proposal, then save the contact
      var p = pending; if (!p) return Promise.resolve();
      return p.apply().then(function () {
        if (!opts.save) return;
        return Promise.resolve(opts.save()).then(function () { say('assistant', 'Saved.'); }).catch(function (e) { say('assistant', e.message); notice(e.message); });
      }).catch(function () { /* the error is already shown */ });
    }
    function go() {
      var text = input.value.trim(); if (!text || busy) return;
      busy = true; send.disabled = true; input.value = ''; say('user', text.length > 280 ? text.slice(0, 280) + '…' : text);
      if (offer && text.length <= 30 && CONFIRM.test(text)) { var picked = offer; offer = null; if (picked.kind === 'email') opts.openHome(picked.text); else opts.openEditor(picked.text); busy = false; send.disabled = false; return; }
      if (pending && text.length <= 30 && CONFIRM.test(text)) { confirmAndSave().then(function () { busy = false; send.disabled = false; input.focus(); }); return; }
      messages.push({ role: 'user', content: text });
      var wait = h('div', { class: 'lks-ai-msg assistant lks-note' }, 'Reading…'); log.appendChild(wait);
      Promise.resolve(text.length >= 40 && opts.saveRaw ? opts.saveRaw(text) : null).catch(function () { /* the AI step still runs */ }).then(function () { return opts.getContact(); }).then(function (current) {
        return api('POST', '/api/calendar/contact-ai', { messages: messages, contact: current, pending: !!pending, mode: asking ? 'ask' : '', person_id: asking ? opts.personId : null }).then(function (res) {
          wait.remove(); messages.push({ role: 'assistant', content: res.reply }); say('assistant', res.reply);
          if (res.handover && opts.openHome) {
            var askText = res.handover.note; offer = { kind: 'email', text: askText };
            var rowE = h('div', { class: 'lks-row lks-offer' }, [
              h('button', { class: 'btn btn-primary btn-small', type: 'button', onclick: function () { if (!offer) return; offer = null; rowE.remove(); opts.openHome(askText); } }, 'Open Home chat'),
              h('button', { class: 'btn lks-soft btn-small', type: 'button', onclick: function () { offer = null; rowE.remove(); say('assistant', 'Okay, nothing sent.'); } }, 'Not now')]);
            log.appendChild(rowE); log.scrollTop = log.scrollHeight;
          } else if (res.edit_offer && opts.openEditor) {
            var seedText = text; offer = { kind: 'edit', text: seedText };
            var row = h('div', { class: 'lks-row lks-offer' }, [
              h('button', { class: 'btn btn-primary btn-small', type: 'button', onclick: function () { if (!offer) return; offer = null; row.remove(); opts.openEditor(seedText); } }, 'Open editor'),
              h('button', { class: 'btn lks-soft btn-small', type: 'button', onclick: function () { offer = null; row.remove(); say('assistant', 'Okay, nothing changed.'); } }, 'Not now')]);
            log.appendChild(row); log.scrollTop = log.scrollHeight;
          } else offer = null;
          if (res.confirm && pending) return confirmAndSave();
          if (res.proposal) { if (opts.noCard) say('assistant', 'I opened the editor with these changes. Review them there and press Save.'); else proposalCard(res, text, current); if (opts.onProposal) opts.onProposal(res.proposal); }
        });
      }).catch(function (e) { wait.remove(); messages.pop(); say('assistant', e.message); })
        .then(function () { busy = false; send.disabled = false; input.focus(); });
    }
    send.addEventListener('click', go);
    input.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); go(); } });
    var panelEl = h('div', { class: 'lks-ai' }, [h('div', { class: 'lks-ai-head' }, [h('b', {}, asking ? 'Ask about this contact' : 'Add notes with AI'), h('span', { class: 'lks-note' }, asking ? 'Questions, search, next steps. To change the contact, press Edit.' : 'Paste notes. It proposes, you apply.')]), log, input,
      h('div', { class: 'lks-row' }, [send, h('span', { class: 'lks-note' }, 'Enter sends · Shift+Enter for a new line')])]);
    panelEl.lksSend = function (t) { input.value = t; go(); };
    return panelEl;
  }
  function copyText(text, btn) {
    function done() { if (!btn) return; var old = btn.textContent; btn.textContent = 'Copied ✓'; setTimeout(function () { btn.textContent = old; }, 1500); }
    if (navigator.clipboard && navigator.clipboard.writeText) { navigator.clipboard.writeText(text).then(done, function () { fallback(); }); } else fallback();
    function fallback() { var ta = h('textarea', { style: 'position:fixed;opacity:0' }, text); document.body.appendChild(ta); ta.select(); try { document.execCommand('copy'); done(); } catch (e) { /* nothing to do */ } ta.remove(); }
  }
  window.lkContacts.copyText = copyText;
  var COPY_ICON = '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/></svg>';
  function copyPill(getText) {
    var b = h('button', { type: 'button', class: 'lks-copy', title: 'Copy to clipboard', onclick: function (e) { e.preventDefault(); e.stopPropagation(); copyText(getText(), b.lastChild); } });
    b.innerHTML = COPY_ICON + '<span>Copy</span>';
    return b;
  }
  window.lkContacts.copyPill = copyPill;

  function notice(message) {                          // a small popup that must be dismissed, so a warning can't be missed
    var ok = h('button', { class: 'btn btn-primary', type: 'button' }, 'OK');
    var box = h('div', { class: 'lks-notice-back' }, [h('div', { class: 'lks-notice' }, [h('p', {}, message), ok])]);
    function close() { box.remove(); document.removeEventListener('keydown', onKey, true); }
    function onKey(e) { if (e.key === 'Escape' || e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); close(); } }
    ok.addEventListener('click', close); document.addEventListener('keydown', onKey, true);
    document.body.appendChild(box); ok.focus();
  }
  window.lkContacts.notice = notice;
  function askConfirm(message, okLabel) {              // a popup with two choices; resolves true only on the confirm button
    return new Promise(function (resolve) {
      var ok = h('button', { class: 'btn btn-primary', type: 'button' }, okLabel || 'Confirm'), no = h('button', { class: 'btn lks-soft', type: 'button' }, 'Cancel');
      var box = h('div', { class: 'lks-notice-back' }, [h('div', { class: 'lks-notice' }, [h('p', {}, message), h('div', { class: 'lks-row' }, [ok, no])])]);
      function done(v) { box.remove(); document.removeEventListener('keydown', onKey, true); resolve(v); }
      function onKey(e) { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); done(false); } }
      ok.addEventListener('click', function () { done(true); }); no.addEventListener('click', function () { done(false); });
      document.addEventListener('keydown', onKey, true); document.body.appendChild(box); no.focus();
    });
  }
  window.lkContacts.editContact = function (id, onSaved, proposal, seedText) {
    api('GET', '/api/calendar/contacts/' + id).then(function (c) { var w = contactForm(c, onSaved); modal(w); if (proposal) w.lksPrefill(proposal); if (seedText) w.lksSend(seedText); }).catch(function (e) { notice(e.message); });
  };

  var modalEl = null;
  function modal(content) {
    closeModal(true);
    modalEl = h('div', { class: 'lks-modal', onclick: function (e) { if (e.target === modalEl) closeModal(); } }, [content]);
    document.body.appendChild(modalEl);
  }
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && modalEl) closeModal(); });
  function closeModal(force) {
    if (!modalEl) return;
    var guard = modalEl.firstChild && modalEl.firstChild.lksGuard;
    if (force !== true && guard && !guard() && !confirm('Close without saving? What you entered or pasted here will be lost.')) return;
    modalEl.remove(); modalEl = null;
  }

  /* =========================== IMPORT CONTACTS =========================== */
  function newImport() { return { step: 1, file: null, scan: null, mapping: {}, busy: false, error: '', result: null,
    opts: { channel: '', batch: '', dup: 'skip', remember: true, mappingName: '' } }; }

  function steps(n) {
    return h('div', { class: 'lks-steps' }, [['1', 'Choose file'], ['2', 'Match fields'], ['3', 'Done']].map(function (s, i) {
      return h('div', { class: 'lks-step' + (n === i + 1 ? ' on' : '') + (n > i + 1 ? ' done' : '') }, [h('b', {}, s[0]), s[1]]);
    }));
  }

  function importView() {
    if (!S.imp) S.imp = newImport();
    var im = S.imp;
    imp.root.appendChild(steps(im.step));
    if (im.step === 1) imp.root.appendChild(importStep1(im));
    else if (im.step === 2) imp.root.appendChild(importStep2(im));
    else imp.root.appendChild(importStep3(im));
  }

  function importStep1(im) {
    var input = h('input', { type: 'file', accept: '.csv,.xlsx,text/csv', hidden: true, onchange: function () { im.file = this.files[0] || null; im.error = ''; importRender(); } });
    var drop = h('div', { class: 'lks-drop' + (im.file ? ' has' : ''),
      ondragover: function (e) { e.preventDefault(); this.classList.add('over'); }, ondragleave: function () { this.classList.remove('over'); },
      ondrop: function (e) { e.preventDefault(); im.file = e.dataTransfer.files[0] || null; im.error = ''; importRender(); }, onclick: function () { input.click(); } },
      [h('b', {}, im.file ? im.file.name : 'Drop a file here'), h('span', {}, im.file ? 'Click to choose a different file' : 'or click to choose · CSV or Excel (.xlsx)')]);
    return h('div', { class: 'lks-card' }, [
      h('h2', {}, 'Import contacts'),
      h('p', {}, 'Import people from a spreadsheet. On the next step you match its columns to LyteKeepr\'s fields. They are saved as contacts and companies.'),
      drop, input,
      im.error ? h('p', { class: 'lks-err' }, im.error) : null,
      h('div', { class: 'lks-row' }, [h('button', { class: 'btn btn-primary', type: 'button', disabled: !im.file || im.busy, onclick: function () {
        var fd = new FormData(); fd.append('file', im.file); im.busy = true; im.error = ''; importRender();
        api('POST', '/api/calendar/import/scan', fd, true).then(function (scan) {
          im.scan = scan; im.mapping = {}; scan.columns.forEach(function (c) { im.mapping[c.name] = c.field; });
          im.opts.batch = scan.default_batch; im.opts.channel = ''; im.step = 2; im.busy = false; importRender();
        }).catch(function (e) { im.busy = false; im.error = e.message; importRender(); });
      } }, im.busy ? 'Scanning…' : 'Scan file and match fields')])
    ]);
  }

  function sel(options, value, onchange) {
    return h('select', { onchange: function () { onchange(this.value); } }, options.map(function (o) { return h('option', { value: o[0], selected: o[0] === value }, o[1]); }));
  }

  function importStep2(im) {
    var scan = im.scan, o = im.opts, effective = o.channel || scan.channel;
    var rows = scan.columns.map(function (c) {
      return h('tr', {}, [
        h('td', { class: 'col' }, [h('span', { class: 'dot ' + (c.matched ? 'ok' : 'ask') }), c.name]),
        h('td', { class: 'sample' }, c.sample.length ? slice(c.sample, 2).join(' · ') : '(empty)'),
        h('td', {}, sel(scan.fields.map(function (f) { return [f.key, f.label]; }), im.mapping[c.name], function (v) { im.mapping[c.name] = v; }))
      ]);
    });
    return h('div', { class: 'lks-card' }, [
      h('h2', {}, 'Match the columns'),
      h('p', {}, (scan.saved_mapping ? 'Matched from your saved mapping "' + scan.saved_mapping + '". ' : '') + scan.rows + ' rows found in ' + scan.filename +
        '. Every file is matched to the same universal fields. Check the columns marked in orange.'),
      h('table', { class: 'lks-map' }, [h('thead', {}, h('tr', {}, [h('th', {}, 'Column in your file'), h('th', {}, 'Example'), h('th', {}, 'Goes into')])), h('tbody', {}, rows)]),
      h('div', { class: 'lks-opts' }, [
        h('div', { class: 'lks-opt' }, [h('label', {}, 'Batch name: start with what you will remember, e.g. Aspan CRM · Instagram · Week of Oct 12'), h('input', { type: 'text', value: o.batch, oninput: function () { o.batch = this.value; } })]),
        h('div', { class: 'lks-opt' }, [h('label', {}, 'Channel' + (scan.channel ? ' (detected: ' + CH[scan.channel] + ')' : '')),
          sel([['', scan.channel ? 'Auto (' + CH[scan.channel] + ')' : 'Auto-detect from links'], ['linkedin', 'LinkedIn'], ['instagram', 'Instagram']], o.channel, function (v) { o.channel = v; importRender(); })]),
        h('div', { class: 'lks-opt' }, [h('label', {}, 'If the person already exists'),
          sel([['skip', 'Skip them (keep their history)'], ['fill', 'Fill in their blank fields only']], o.dup, function (v) { o.dup = v; })])
      ]),
      h('label', { class: 'lks-check' }, [h('input', { type: 'checkbox', checked: o.remember, onchange: function () { o.remember = this.checked; } }), 'Remember this mapping for files with the same columns']),
      im.error ? h('p', { class: 'lks-err' }, im.error) : null,
      h('div', { class: 'lks-row' }, [
        h('button', { class: 'btn btn-primary', type: 'button', disabled: im.busy, onclick: function () {
          var mapped = Object.keys(im.mapping).map(function (k) { return im.mapping[k]; });
          if (!['url', 'name', 'business_name'].some(function (k) { return mapped.indexOf(k) >= 0; })) { im.error = 'Match at least one column to URL, Name or Business name, so each person can be identified.'; importRender(); return; }
          im.busy = true; im.error = ''; importRender();
          api('POST', '/api/calendar/import/run', { token: scan.token, mapping: im.mapping, options: { channel: o.channel, batch: o.batch, duplicates: o.dup, remember: o.remember } }).then(function (r) { im.result = r; im.step = 3; im.busy = false; changed(); importRender(); })
            .catch(function (e) { im.busy = false; im.error = e.message; importRender(); });
        } }, im.busy ? 'Importing…' : 'Import ' + scan.rows + ' rows'),
        h('button', { class: 'btn lks-soft', type: 'button', onclick: function () { im.step = 1; im.error = ''; importRender(); } }, 'Back')
      ])
    ]);
  }

  function importStep3(im) {
    var r = im.result;
    function stat(n, label) { return h('div', { class: 'lks-stat' }, [h('b', {}, String(n)), h('span', {}, label)]); }
    var stats = [stat(r.added, 'new contacts added'), stat(r.skipped, 'already in LyteKeepr, skipped')];
    if (r.filled) stats.push(stat(r.filled, 'existing contacts, blanks filled in'));
    if (r.merged_in_file) stats.push(stat(r.merged_in_file, 'repeats inside the file, merged'));
    if (r.companies_created) stats.push(stat(r.companies_created, 'new companies'));
    if (r.excluded) stats.push(stat(r.excluded, 'on your removed list, skipped'));
    return h('div', { class: 'lks-card' }, [
      h('h2', {}, 'Import finished'), h('div', { class: 'lks-stats' }, stats),
      h('p', {}, 'Batch "' + (r.batch || '(none)') + '"' + (im.opts.remember ? ' · mapping saved' : '') + '.'),
      r.errors.length ? h('div', { class: 'lks-errs' }, [h('b', {}, r.errors.length + ' row(s) skipped:')].concat(slice(r.errors, 8).map(function (m) { return h('span', {}, '• ' + m); }),
        r.errors.length > 8 ? [h('span', {}, '…and ' + (r.errors.length - 8) + ' more')] : [])) : null,
      h('div', { class: 'lks-row' }, [
        h('button', { class: 'btn btn-primary', type: 'button', onclick: function () { S.imp = newImport(); importRender(); } }, 'Import another file'),
        h('button', { class: 'btn lks-soft', type: 'button', onclick: function () { if (imp.onDone) imp.onDone(); } }, 'View contacts')
      ])
    ]);
  }


  var FORM_FIELDS = [['name', 'Name'], ['job_title', 'Job title / vocation'], ['url', 'URL (profile link)'], ['headline', 'Headline / bio'], ['phone', 'Phone / WhatsApp'],
    ['telegram', 'Telegram'], ['email', 'Email'], ['location', 'Location'], ['industry', 'Industry / type'], ['website', 'Website'], ['notes', 'Notes']];

  function expander(textarea, baseRows) {            // a small link that makes a text box taller and back
    var open = false;
    return h('button', { type: 'button', class: 'lks-expand', onclick: function (e) { e.preventDefault(); open = !open; textarea.rows = open ? 16 : baseRows; this.textContent = open ? 'Collapse' : 'Expand'; } }, 'Expand');
  }

  function contactForm(c, onSaved, mode) {
    var page = mode === 'page';       // the contact page shows the form read-only; New / Edit open it in a window
    var vals = {}; var isNew = !c.id;
    var inputs = FORM_FIELDS.map(function (f) {
      var el = f[0] === 'job_title' ? chipField(c.job_title) : ['name', 'url', 'phone', 'email'].indexOf(f[0]) >= 0 ? chipField(c[f[0]], { multi: false, check: CHECKS[f[0]] }) : f[0] === 'notes' || f[0] === 'headline' ? h('textarea', { rows: f[0] === 'notes' ? 5 : 2 }, c[f[0]] || '') : h('input', { type: 'text', value: c[f[0]] || '' });
      vals[f[0]] = el; return h('div', { class: 'lks-opt' + (f[0] === 'notes' || f[0] === 'headline' ? ' wide' : '') }, [h('label', {}, f[0] === 'notes' ? [f[1] + ' ', copyPill(function () { return el.value; }), expander(el, 5)] : f[1]), el]);
    });
    var company = chipField(c.company || '', { multi: false, placeholder: 'Company (leave empty for none)' });
    var rawField = h('textarea', { rows: 3, class: 'lks-rawfield', placeholder: 'Whatever you paste into the AI is copied here automatically.' }, c.raw_notes || '');
    acceptChips(company); Object.keys(vals).forEach(function (k) { acceptChips(vals[k]); });
    var err = h('p', { class: 'lks-err', hidden: true });
    var formCard = h('div', { class: 'lks-card lks-form' }, [
      page ? null : h('h2', {}, isNew ? 'New contact' : 'Edit contact'),
      h('div', { class: 'lks-opt wide' }, [h('label', {}, 'Company'), company]),
      h('div', { class: 'lks-opts' }, inputs),
      h('div', { class: 'lks-opt wide' }, [h('label', {}, ['Raw notes (saved by code, exactly what you paste) ',
        copyPill(function () { return rawField.value; }), expander(rawField, 3)]), rawField]),
      err,
      page ? null : h('div', { class: 'lks-row' }, [
        h('button', { class: 'btn btn-primary', type: 'button', onclick: function () { save().catch(function (e) { err.hidden = false; err.textContent = e.message; notice(e.message); }); } }, 'Save'),
        h('button', { class: 'btn lks-soft', type: 'button', onclick: function () { closeModal(); } }, 'Cancel')
      ])
    ]);
    function setEditable(b) {
      formCard.classList.toggle('lks-ro', !b);
      [company].concat(Object.keys(vals).map(function (k) { return vals[k]; })).forEach(function (el) { if (el.lksChips) el.classList.toggle('lks-ro', !b); else el.readOnly = !b; });
      rawField.readOnly = !b;
    }
    function save() {
      var body = { company: company.value.trim(), channel: c.channel || '', batch: c.batch || '' };
      Object.keys(vals).forEach(function (k) { body[k] = vals[k].value; });
      body.raw_notes = rawField.value;
      if (isNew && body.company === '' && c.company_id && !c.company) body.company_id = c.company_id;      // only a company that never had a name; a name you cleared stays cleared
      if (!isNew) delete body.channel;
      err.hidden = true;
      var q = '?email=' + encodeURIComponent(body.email || '') + '&url=' + encodeURIComponent(body.url || '') + '&phone=' + encodeURIComponent(body.phone || '') + (c.id ? '&exclude=' + c.id : '');
      function finish(req) { return req.then(function () { closeModal(true); changed(); if (onSaved) onSaved(); }); }
      return api('GET', '/api/calendar/contacts/duplicate' + q).then(function (r) {
        var dup = r.duplicate;
        if (!dup) return finish(isNew ? api('POST', '/api/calendar/contacts', body) : api('PUT', '/api/calendar/contacts/' + c.id, body));
        var who = (dup.name || 'another contact') + (dup.company && dup.company !== dup.name ? ' · ' + dup.company : '');
        if (!isNew) throw new Error('That ' + (dup.reason || 'email or link') + ' already belongs to ' + who + '. Open that contact instead, or use a different one.');
        return askConfirm('A contact with this ' + (dup.reason || 'email or link') + ' already exists: ' + who + '.\n\nSaving will OVERWRITE that contact with what you entered here. Its old raw notes are kept, and yours are added below them.', 'Overwrite and save').then(function (yes) {
          if (!yes) return;
          body.raw_notes = (dup.raw_notes ? dup.raw_notes.trim() + '\n\n' : '') + rawField.value;
          delete body.channel;
          return finish(api('PUT', '/api/calendar/contacts/' + dup.id, body));
        });
      });
    }
    var snapshot = function () { return JSON.stringify([company.value, rawField.value].concat(Object.keys(vals).map(function (k) { return vals[k].value; }))); };
    var startState = snapshot();
    function prefill(p) {
      Object.keys(p.fields).forEach(function (k) {
        var el = k === 'company' ? company : k === 'profile_url' ? vals.url : vals[k];
        if (el && !el.value.trim()) el.value = p.fields[k];
      });
      if (p.notes && !vals.notes.value.trim()) vals.notes.value = plain(p.notes);
    }
    var ai = page ? assistantPanel({ mode: 'ask', personId: c.id, getContact: function () { return Promise.resolve({ id: c.id }); },
      openHome: function (note) {            // the note already names the contact (and the email address when it is this contact)
        var home = document.getElementById('homeTab'); if (home) home.click();
        setTimeout(function () { if (window.lkHomeChat) window.lkHomeChat.send(note); }, 200);
      },
      openEditor: function (seedText) { window.lkContacts.editContact(c.id, function () { document.dispatchEvent(new Event('lk-outreach-saved')); }, null, seedText); } }) : assistantPanel({
      saveRaw: function (text) {
        rawField.value = (rawField.value.trim() ? rawField.value.trim() + '\n\n' : '') + '— ' + new Date().toISOString().slice(0, 16).replace('T', ' ') + ' —\n' + text;
        rawField.scrollTop = rawField.scrollHeight;
        return c.id ? api('POST', '/api/calendar/contacts/' + c.id + '/raw-notes', { text: text }) : null;      // an existing contact is backed up at once
      },
      onProposal: function (p) {                                       // prefill: empty fields only; nothing is saved until Save
        prefill(p);
      },
      getContact: function () {
        var cur = { id: c.id };
        Object.keys(vals).forEach(function (k) { cur[k] = vals[k].value; });
        cur.company = company.value;
        return Promise.resolve(cur);
      },
      save: function () { return save(); },
      apply: function (fields, notes, existingNotes) {
        Object.keys(fields).forEach(function (k) { if (k === 'company') company.value = fields[k]; else if (k === 'profile_url') vals.url.value = fields[k]; else if (vals[k] && vals[k].lksChips) vals[k].lksChips.add(fields[k]); else if (vals[k]) vals[k].value = fields[k]; });
        if (notes && vals.notes.value.indexOf(notes) === -1) vals.notes.value = vals.notes.value.trim() ? vals.notes.value.trim() + '\n\n' + notes : notes;
        return Promise.resolve();
      }
    });
    if (page) {
      setEditable(false);
      return {
        card: formCard, ai: ai,
        reload: function (c2) {            // the page shows what is saved: refresh the values
          c = c2;
          Object.keys(vals).forEach(function (k) { vals[k].value = c2[k] || ''; });
          company.value = c2.company || ''; rawField.value = c2.raw_notes || '';
        }
      };
    }
    var wrap = h('div', { class: 'lks-split2' }, [formCard, ai]);
    wrap.lksPrefill = function (p) { prefill(p); startState = snapshot(); };
    wrap.lksSend = function (t) { ai.lksSend(t); };
    wrap.lksGuard = function () { return snapshot() === startState; };
    return wrap;
  }

  /* The contact page itself: the details read-only with the "ask" chat beside them. Edit opens the same window as New contact. */
  var pageState = { id: null, editor: null };
  window.lkContacts.mountContactPage = function (personId) {
    return api('GET', '/api/calendar/contacts/' + personId).then(function (c) {
      if (pageState.id === personId && pageState.editor) { pageState.editor.reload(c); return; }
      var ed = contactForm(c, null, 'page');
      pageState.id = personId; pageState.editor = ed;
      document.getElementById('crmEditPersonBtn').onclick = function () { window.lkContacts.editContact(personId, function () { document.dispatchEvent(new Event('lk-outreach-saved')); }); };
      document.getElementById('crmDeletePersonBtn').onclick = function () {
        var nm = (c.name || c.company || 'this contact');
        askConfirm('Delete ' + nm + '?\n\nThis removes the contact for good: their details, notes, raw notes, messages, statuses, calendar tasks and saved email drafts, and their company if they are its only contact. It cannot be undone.', 'Delete').then(function (yes) {
          if (!yes) return;
          // gone from the screen at once; the server finishes the cleanup in the background and the lists are then re-read to match
          pageState.id = null; pageState.editor = null;
          document.dispatchEvent(new CustomEvent('lk-contact-removed', { detail: { id: personId } }));
          var back = document.getElementById('closeCrmPersonDetailBtn'); if (back) back.click();
          api('DELETE', '/api/calendar/contacts/' + personId).then(changed).catch(function (e) { notice(e.message); changed(); });
        });
      };
      document.getElementById('crmPersonDetailEditor').replaceChildren(ed.card);
      document.getElementById('crmPersonDetailAI').replaceChildren(ed.ai);
    }).catch(function (e) { notice(e.message); });
  };

  /* Possible duplicates: found by the app, merged only when the user says so */
  function personCard(p, keepBtn) {
    var facts = [p.company, p.email, p.added, p.messages ? p.messages + (p.messages === 1 ? ' message' : ' messages') : 'no messages', p.has_notes ? 'has notes' : '', p.in_funnel ? 'in a funnel' : ''].filter(Boolean);
    return h('div', { class: 'lks-dup-person' }, [h('b', {}, p.name || 'No name'), h('span', { class: 'lks-note' }, facts.join(' · ')), keepBtn]);
  }
  function reviewDuplicates() {
    api('GET', '/api/calendar/duplicates').then(function (r) {
      var groups = r.groups || [];
      if (!groups.length) { closeModal(true); checkDuplicates(); return; }
      var card = h('div', { class: 'lks-card lks-dups' }, [h('h2', {}, 'Possible duplicate contacts'),
        h('p', {}, 'These look like the same person. Nothing is merged unless you choose. Merging keeps the contact you pick, adds the other one\'s messages, notes and details to it, and removes the other.')]);
      groups.forEach(function (g) {
        var row = h('div', { class: 'lks-dup-group' }, [h('div', { class: 'lks-note' }, 'Why: ' + g.reasons.join(' · '))]);
        var people = h('div', { class: 'lks-dup-people' });
        g.people.forEach(function (p) {
          people.appendChild(personCard(p, h('button', { class: 'btn btn-primary btn-small', type: 'button', onclick: function () {
            var others = g.ids.filter(function (i) { return i !== p.id; });
            askConfirm('Keep ' + (p.name || 'this contact') + ' and merge the other ' + (others.length === 1 ? 'contact' : others.length + ' contacts') + ' into it?\n\nThe other contact is removed, and its messages, notes and details are added to the one you keep.', 'Merge').then(function (yes) {
              if (!yes) return;
              others.reduce(function (chain, o) { return chain.then(function () { return api('POST', '/api/crm/people/merge', { keep_person_id: p.id, merge_person_id: o }); }); }, Promise.resolve())
                .then(function () { changed(); reviewDuplicates(); }).catch(function (e) { notice(e.message); });
            });
          } }, 'Keep this one')));
        });
        row.appendChild(people);
        row.appendChild(h('button', { class: 'btn lks-soft btn-small', type: 'button', onclick: function () {
          api('POST', '/api/calendar/duplicates/dismiss', { ids: g.ids }).then(function () { reviewDuplicates(); checkDuplicates(); });
        } }, 'Not the same person'));
        card.appendChild(row);
      });
      card.appendChild(h('div', { class: 'lks-row' }, [h('button', { class: 'btn lks-soft', type: 'button', onclick: function () { closeModal(true); } }, 'Close')]));
      modal(card);
    }).catch(function (e) { notice(e.message); });
  }
  function checkDuplicates() {
    var bar = document.getElementById('crmDupBar'); if (!bar) return;
    api('GET', '/api/calendar/duplicates').then(function (r) {
      var n = (r.groups || []).length;
      bar.hidden = !n; bar.replaceChildren();
      if (!n) return;
      bar.appendChild(h('span', {}, n + (n === 1 ? ' possible duplicate contact' : ' possible duplicate contacts') + ': the same email, link, phone or name.'));
      bar.appendChild(h('button', { class: 'btn btn-secondary btn-small', type: 'button', onclick: reviewDuplicates }, 'Review'));
    }).catch(function () { bar.hidden = true; });
  }
  window.lkContacts.checkDuplicates = checkDuplicates;

  /* =========================== SETTINGS PANEL (gear) =========================== */
  var panel = document.getElementById('settingsPanel'), btn = document.getElementById('settingsBtn'), main = document.getElementById('lksMain'), nav = document.getElementById('lksNav');
  if (!panel || !btn || !main) return;
  var me = null;
  function showChip(u) {                               // the signed-in person, top right next to the gear
    var chip = document.getElementById('userChip'); if (!chip || !u) return;
    var label = (u.name || '').trim() || (u.email || '').split('@')[0] || 'Admin';
    document.getElementById('userAvatar').textContent = (label[0] || '?').toUpperCase();
    document.getElementById('userName').textContent = label;
    document.getElementById('userRole').hidden = u.role !== 'admin';
    chip.classList.toggle('is-admin', u.role === 'admin');
    chip.title = u.email || '';
    chip.hidden = false;
    document.body.classList.toggle('is-team-member', u.role !== 'admin');      // team members do not see Gmail, LinkedIn or the assistant yet
    document.body.classList.toggle('no-linkedin', !(u.features && u.features.linkedin));   // LinkedIn is the first workspace's only
    if (u.account_status === 'waiting') showWaiting(u);
    if (u.gmail_connect_now) location.href = (window.LK_PREFIX || '') + '/auth/google/start';     // once: connect the mailbox you signed in with
  }
  function showWaiting(u) {                            // a new sign-up: signed in, but nothing works until the account is approved
    if (document.getElementById('lkWaiting')) return;
    document.body.appendChild(h('div', { class: 'lk-waiting', id: 'lkWaiting' }, [h('div', { class: 'lk-waiting-card' }, [
      h('div', { class: 'lk-waiting-dot' }), h('h1', {}, 'Thanks for signing up'),
      h('p', {}, ['Your LyteKeepr account (', h('b', {}, u.email || ''), ') is waiting for approval. You can use it as soon as it is approved.']),
      h('p', { class: 'lk-waiting-small' }, 'You do not need to do anything else. Come back later and sign in again.'),
      h('a', { class: 'btn btn-secondary', href: (window.LK_PREFIX || '') + '/logout' }, 'Sign out')])]));
  }
  function loadMe() {                                  // who is signed in; the Team section is only for the admin
    return api('GET', '/api/me').then(function (r) { me = r; showChip(r); var tb = nav.querySelector('[data-view=team]'); if (tb) tb.hidden = r.role !== 'admin'; return r; }).catch(function () { return null; });
  }
  setTimeout(loadMe, 300);
  function openPanel() {
    loadMe().then(function () { if (!panel.hidden) showSettings(currentSettingsView); });
    ['caseOverviewPanel', 'caseOverviewContent', 'aiPanel', 'calendarPanel', 'crmPanel', 'linkedinPanel', 'crmPersonDetailView'].forEach(function (id) { var el = document.getElementById(id); if (el) el.hidden = true; });
    panel.hidden = false; btn.classList.add('on'); showSettings(currentSettingsView);
  }
  function closePanel() {
    if (panel.hidden) return; panel.hidden = true; btn.classList.remove('on');
    var overview = document.getElementById('caseOverviewPanel'), cal = document.getElementById('calendarPanel');
    if (overview && !(cal && !cal.hidden)) overview.hidden = false;       // the left card comes back (the Calendar hides it again itself)
  }
  btn.addEventListener('click', function () {
    if (panel.hidden) openPanel(); else { var home = document.getElementById('homeTab'); closePanel(); if (home) home.click(); }
  });
  ['homeTab', 'linkedinTab', 'crmTab', 'calendarTab'].forEach(function (id) { var el = document.getElementById(id); if (el) el.addEventListener('click', closePanel); });
  document.getElementById('lksClose').addEventListener('click', function () { btn.click(); });

  /* Name for signing emails: suggested from the Google account, used only once confirmed or edited */
  function askName(suggested, onDone) {
    var input = h('input', { type: 'text', value: suggested, maxlength: 80, class: 'lks-name-input' });
    var editRow = h('div', { class: 'lks-opt', hidden: true }, [h('label', {}, 'Your name'), input]);
    var ok = h('button', { class: 'btn btn-primary', type: 'button' }, 'Confirm'), edit = h('button', { class: 'btn lks-soft', type: 'button' }, 'Edit'), later = h('button', { class: 'btn lks-soft', type: 'button' }, 'Later');
    var text = h('p', {}, ['I\'ll save your name in your profile as ', h('b', {}, '“' + suggested + '”'), ', taken from your email account. I\'ll use it to sign your emails. Please confirm or edit.']);
    var box = h('div', { class: 'lks-notice-back' }, [h('div', { class: 'lks-notice' }, [text, editRow, h('div', { class: 'lks-row' }, [ok, edit, later])])]);
    function close() { box.remove(); }
    ok.addEventListener('click', function () {
      var name = input.value.trim(); if (!name) return;
      api('PUT', '/api/calendar/profile', { name: name }).then(function () { close(); if (onDone) onDone(name); if (panel && !panel.hidden && currentSettingsView === 'details') detailsView(); }).catch(function (e) { notice(e.message); });
    });
    edit.addEventListener('click', function () { editRow.hidden = false; text.hidden = true; edit.hidden = true; ok.textContent = 'Save'; input.focus(); input.select(); });
    later.addEventListener('click', close);
    document.body.appendChild(box); ok.focus();
  }
  function checkName() {
    api('GET', '/api/calendar/profile').then(function (r) { if (!r.name && r.suggested) askName(r.suggested); }).catch(function () { /* not signed in or offline */ });
  }
  setTimeout(checkName, 2500);

  var currentSettingsView = 'details';
  function showSettings(view) {
    currentSettingsView = view;
    nav.querySelectorAll('button').forEach(function (b) { b.classList.toggle('on', b.dataset.view === view); });
    if (view === 'mappings') mappingsView(); else if (view === 'team') teamView(); else detailsView();
  }
  nav.querySelectorAll('button').forEach(function (b) { b.addEventListener('click', function () { showSettings(b.dataset.view); }); });

  function memberProfileView() {                      // team members: who they are; the signing name belongs to the admin's mailbox for now
    main.replaceChildren(h('div', { class: 'lks-card' }, [h('h2', {}, 'Your account'),
      h('p', {}, 'You are signed in with Google as ' + (me.email || '') + '.'),
      h('div', { class: 'lks-row' }, [h('a', { class: 'btn lks-soft btn-small', href: (window.LK_PREFIX || '') + '/logout' }, 'Sign out')])]));
  }
  function detailsView() {
    if (me && me.role !== 'admin') return memberProfileView();
    main.replaceChildren();
    var input = h('input', { type: 'text', placeholder: 'e.g. Peter', maxlength: 80 }), msg = h('span', { class: 'lks-note' });
    main.appendChild(h('div', { class: 'lks-card' }, [h('h2', {}, 'Profile'),
      h('p', {}, 'The name the assistant signs your emails and messages with, so a draft never ends with "[Your name]".'),
      h('div', { class: 'lks-opt' }, [h('label', {}, 'Your name'), input]),
      h('div', { class: 'lks-row' }, [h('button', { class: 'btn btn-primary', type: 'button', onclick: function () {
        api('PUT', '/api/calendar/profile', { name: input.value }).then(function (r) { input.value = r.name || ''; msg.textContent = r.name ? 'Saved. Your emails are signed "' + r.name + '".' : 'Cleared.'; }).catch(function (e) { msg.textContent = e.message; });
      } }, 'Save'), msg])]));
    var who = h('div', { class: 'lks-row' });
    main.firstChild.appendChild(who);
    loadMe().then(function (u) {
      if (!u) return;
      who.replaceChildren(h('span', { class: 'lks-note' }, 'Signed in as ' + (u.email || 'the admin') + (u.role === 'admin' ? ' · Admin' : '')),
        h('a', { class: 'btn lks-soft btn-small', href: (window.LK_PREFIX || '') + '/logout' }, 'Sign out'),
        u.is_super_admin ? h('a', { class: 'btn btn-primary btn-small', href: (window.LK_PREFIX || '') + '/owner' }, 'Owner console') : null);
    });
    api('GET', '/api/calendar/profile').then(function (r) {
      input.value = r.name || '';
      if (r.name) msg.textContent = 'Your emails are signed "' + r.name + '".';
      else if (r.suggested) msg.textContent = 'From your Google account: “' + r.suggested + '”. Type it above and Save to confirm.';
    });
  }

  /* Team: the admin adds people by Google email; they sign in with Google. Nobody else gets in. */
  function initial(u) { return ((u.name || u.email || '?').trim()[0] || '?').toUpperCase(); }
  function shortDate(iso) { var d = new Date(iso); return isNaN(d) ? '' : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + ', ' + d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }); }
  function teamView(freshEmail) {
    main.replaceChildren();
    var list = h('div', { class: 'lks-card' }, [h('h2', {}, 'Team'), h('p', {}, 'Loading…')]);
    main.appendChild(list);
    Promise.all([api('GET', '/api/team'), api('GET', '/api/team/funnels')]).then(function (res) {
      var r = res[0], funnels = res[1].funnels;
      var kids = [h('div', { class: 'lks-teamhead' }, [h('div', {}, [h('h2', {}, 'Team'),
        h('p', { style: 'margin-top:4px' }, 'People you add can sign in with their Google account. Everyone works in the same LyteKeepr data.')]),
        h('button', { class: 'btn btn-primary', type: 'button', onclick: addUserDialog }, '+ Add user')])];
      if (freshEmail) kids.push(h('div', { class: 'lks-banner' }, [h('b', {}, freshEmail + ' was added. '), 'Tell them to open LyteKeepr and press “Sign in with Google” with that email.']));
      r.users.forEach(function (u) {
        var isAdmin = u.role === 'admin', isMe = me && u.id === me.id;
        var status = isAdmin ? 'Admin' : !u.active ? 'Off' : u.last_login_at ? 'Active' : 'Waiting for first sign-in';
        var line = u.email + ' · ' + (!u.active && !isAdmin ? 'turned off' : u.last_login_at ? 'last signed in ' + shortDate(u.last_login_at) : isAdmin ? 'admin' : 'has not signed in yet');
        var actions = [];
        if (!isAdmin) {
          actions.push(h('button', { class: 'lks-toggle' + (u.share_emails ? ' on' : ''), type: 'button', title: 'They can read, but not change, the emails you exchanged with shared contacts',
            onclick: function () { api('POST', '/api/team/' + u.id + '/share', { share: !u.share_emails }).then(function () { teamView(); }).catch(function (e) { notice(e.message); }); } },
            [h('span', { class: 'lks-sw' }), 'Sees my emails']));
          actions.push(h('button', { class: 'lks-link', type: 'button', onclick: function () { api('POST', '/api/team/' + u.id + '/active', { active: !u.active }).then(function () { teamView(); }).catch(function (e) { notice(e.message); }); } }, u.active ? 'Turn off' : 'Turn on'));
          actions.push(h('button', { class: 'lks-link danger', type: 'button', onclick: function () {
            askConfirm('Remove ' + u.email + '? They will no longer be able to sign in. Nothing they added is deleted.', 'Remove').then(function (yes) {
              if (yes) api('DELETE', '/api/team/' + u.id).then(function () { teamView(); }).catch(function (e) { notice(e.message); });
            });
          } }, 'Remove'));
        } else actions.push(h('span', { class: 'lks-note' }, 'Cannot be turned off'));
        var memberRow = h('div', { class: 'lks-member' + (freshEmail && u.email === freshEmail ? ' fresh' : '') }, [
          h('div', { class: 'lks-avatar' + (isAdmin ? ' admin' : !u.active ? ' off' : '') }, initial(u)),
          h('div', { class: 'lks-who' }, [h('b', {}, isMe ? 'You' : (u.name || u.email)), h('span', {}, line)]),
          h('span', { class: 'lks-badge ' + (isAdmin ? 'admin' : !u.active ? 'off' : u.last_login_at ? 'on' : 'wait') }, status),
          h('div', { class: 'lks-member-actions' }, actions)]);
        kids.push(memberRow);
        if (!isAdmin) {
          var sel = h('select', { class: 'lks-select', onchange: function () {
            api('POST', '/api/team/' + u.id + '/access', { access: sel.value }).then(function () { teamView(); }).catch(function (e) { notice(e.message); }); } },
            [h('option', { value: 'funnels' }, 'Selected funnels'), h('option', { value: 'all' }, 'Everything (the whole database)')]);
          sel.value = u.access;
          var chips = funnels.length ? funnels.map(function (f) {
            var on = u.funnel_ids.indexOf(f.funnel_id) !== -1;
            return h('button', { class: 'lks-chipbtn' + (on ? ' on' : ''), type: 'button', title: f.people_count + ' contacts', onclick: function () {
              var ids = on ? u.funnel_ids.filter(function (x) { return x !== f.funnel_id; }) : u.funnel_ids.concat([f.funnel_id]);
              api('POST', '/api/team/' + u.id + '/funnels', { funnel_ids: ids }).then(function () { teamView(); }).catch(function (e) { notice(e.message); }); } }, [(on ? '✓ ' : '') + f.name]);
          }) : [h('span', { class: 'lks-note' }, 'No funnels yet.')];
          kids.push(h('div', { class: 'lks-access' }, [h('span', { class: 'lks-note' }, 'Sees'), sel,
            u.access === 'all' ? h('span', { class: 'lks-note' }, 'Every contact, funnel and task.') : h('div', { class: 'lks-chips' }, chips),
            u.access === 'funnels' ? h('span', { class: 'lks-note' }, '+ the contacts they add themselves') : null]));
        }
      });
      list.replaceChildren.apply(list, kids);
    }).catch(function (e) { list.replaceChildren(h('h2', {}, 'Team'), h('p', {}, e.message)); });
  }
  function addUserDialog() {
    var email = h('input', { type: 'email', placeholder: 'their Google email', autocomplete: 'off' }), name = h('input', { type: 'text', placeholder: 'optional', maxlength: 100 });
    var share = false, sw = h('button', { class: 'lks-toggle', type: 'button', onclick: function () { share = !share; sw.classList.toggle('on', share); } }, [h('span', { class: 'lks-sw' }), 'Let them read the emails I exchanged with shared contacts']);
    var err = h('p', { class: 'lks-err', hidden: true });
    var add = h('button', { class: 'btn btn-primary', type: 'button' }, 'Add user');
    add.addEventListener('click', function () {
      err.hidden = true;
      api('POST', '/api/team', { email: email.value, name: name.value, share_emails: share }).then(function (r) { closeModal(true); teamView(r.user.email); })
        .catch(function (e) { err.textContent = e.message; err.hidden = false; });
    });
    modal(h('div', { class: 'lks-card' }, [h('h2', {}, 'Add a user'),
      h('div', { class: 'lks-opt' }, [h('label', {}, 'Their Google email'), email]),
      h('div', { class: 'lks-opt' }, [h('label', {}, 'Name (optional)'), name]), sw,
      h('div', { class: 'lks-hint' }, ['No password. They open LyteKeepr, press ', h('b', {}, 'Sign in with Google'), ' and choose this email. Anyone whose email is not on this list is refused.']), err,
      h('div', { class: 'lks-row' }, [add, h('button', { class: 'btn lks-soft', type: 'button', onclick: function () { closeModal(true); } }, 'Cancel')])]));
    email.focus();
  }

  function mappingsView() {
    main.replaceChildren();
    var box = h('div', { class: 'lks-card' }, [h('h2', {}, 'Saved mappings'), h('p', {}, 'Loading…')]); main.appendChild(box);
    api('GET', '/api/calendar/import/mappings').then(function (r) {
      var kids = [h('h2', {}, 'Saved mappings'), h('p', {}, 'A saved mapping is applied automatically when an imported file has the same columns, so weekly files need no clicks. Importing itself is in the CRM tab.')];
      if (!r.mappings.length) kids.push(h('p', { class: 'lks-note' }, 'None yet. Tick "Remember this mapping" when you import a file.'));
      r.mappings.forEach(function (m) {
        kids.push(h('div', { class: 'lks-tpl' }, [h('span', {}, [h('b', {}, m.name), ' · ' + m.matched + ' of ' + m.columns + ' columns matched · used ' + m.used + (m.used === 1 ? ' time' : ' times')]),
          h('button', { class: 'btn lks-soft', type: 'button', onclick: function () {
            if (!confirm('Delete this saved mapping?')) return; api('DELETE', '/api/calendar/import/mappings/' + m.id).then(mappingsView); } }, 'Delete')]));
      });
      box.replaceChildren.apply(box, kids);
    }).catch(function (e) { box.textContent = e.message; });
  }
})();
