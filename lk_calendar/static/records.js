/* Contact notes and logged conversations, and an opportunity's combined communication history.
   - Notes card on the contact page (general notes, dated).
   - "Log a call or conversation" next to the contact's messages; logged ones show in that timeline.
   - The opportunity window's "Communication history": everyone on it, oldest to newest, filterable by person and channel. */
(function () {
  'use strict';

  function h(tag, attrs, kids) {
    var el = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (k === 'class') el.className = v; else if (k === 'style') el.style.cssText = v;
      else if (k.slice(0, 2) === 'on') el.addEventListener(k.slice(2), v);
      else if (v === true) el.setAttribute(k, ''); else if (v !== false && v != null) el.setAttribute(k, v);
    });
    [].concat(kids == null ? [] : kids).forEach(function (c) { if (c == null || c === false) return; el.appendChild(typeof c === 'string' || typeof c === 'number' ? document.createTextNode(String(c)) : c); });
    return el;
  }
  function api(method, url, body) {
    return fetch(url, { method: method, headers: body ? { 'Content-Type': 'application/json' } : undefined, body: body ? JSON.stringify(body) : undefined })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { if (!r.ok) throw new Error(d.error || 'Something went wrong'); return d; }); });
  }
  function C() { return window.lkContacts || {}; }
  function confirmBox(msg, ok) { return C().askConfirm ? C().askConfirm(msg, ok) : Promise.resolve(window.confirm(msg)); }
  function fail(e) { if (C().notice) C().notice(e.message); else alert(e.message); }
  function stamp(iso) {
    if (!iso) return '';
    var d = new Date(iso); if (isNaN(d)) return iso;
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }) + (String(iso).length > 10 ? ', ' + d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }) : '');
  }
  function tagClass(ch) { return 'crm-channel-tag crm-channel-tag-' + String(ch || '').toLowerCase().replace(/[^a-z0-9]+/g, '-'); }
  function localNow() { var d = new Date(); d.setMinutes(d.getMinutes() - d.getTimezoneOffset()); return d.toISOString().slice(0, 16); }
  var TYPES = [['phone call', 'Phone call'], ['meeting', 'Meeting'], ['in person', 'In person'], ['video call', 'Video call'], ['other', 'Other']];

  /* ---------- notes card on the contact page ---------- */
  function renderNotes(el, personId) {
    if (!el) return;
    api('GET', '/api/crm/people/' + personId + '/notes').then(function (r) {
      var input = h('textarea', { rows: '2', class: 'lko-note-input', placeholder: 'Add a note about this contact…' });
      function add() {
        if (!input.value.trim()) { input.focus(); return; }
        api('POST', '/api/crm/people/' + personId + '/notes', { text: input.value }).then(function () { renderNotes(el, personId); }).catch(fail);
      }
      input.addEventListener('keydown', function (e) { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); add(); } });
      var list = r.notes.map(function (n) { return noteItem(n, function () { renderNotes(el, personId); }); });
      el.replaceChildren(h('div', { class: 'crm-detail-card-header' }, [h('span', { class: 'block-label' }, 'Notes' + (r.notes.length ? ' (' + r.notes.length + ')' : ''))]),
        h('div', { class: 'lko-notes' }, [h('div', { class: 'lko-note-add' }, [input, h('button', { class: 'btn btn-secondary btn-small', type: 'button', title: 'Or press Ctrl/⌘ + Enter', onclick: add }, 'Add note')])]
          .concat(list.length ? list : [h('p', { class: 'lks-note' }, 'No notes yet.')])));
    }).catch(function () { el.replaceChildren(); });
  }
  function noteItem(n, refresh) {
    var text = h('div', { class: 'lko-note-text' }, n.text);
    return h('div', { class: 'lko-note' }, [
      h('div', { class: 'lko-note-head' }, [h('small', {}, stamp(n.happened_at) + (n.who ? ' · by ' + n.who : '')), n.mine ? editTools(n, text, refresh) : null]), text]);
  }
  function editTools(n, textEl, refresh) {
    return h('span', {}, [
      h('button', { class: 'lko-link', type: 'button', onclick: function () {
        var ta = h('textarea', { rows: '4' }, n.text);
        textEl.replaceWith(h('div', {}, [ta, h('div', { class: 'lks-row' }, [
          h('button', { class: 'btn btn-primary btn-small', type: 'button', onclick: function () { api('PUT', '/api/crm/records/' + n.record_id, { text: ta.value }).then(refresh).catch(fail); } }, 'Save'),
          h('button', { class: 'btn lks-soft btn-small', type: 'button', onclick: refresh }, 'Cancel')])]));
        ta.focus();
      } }, 'Edit'),
      h('button', { class: 'lko-link', type: 'button', onclick: function () {
        confirmBox('Delete this ' + (n.kind === 'note' ? 'note' : 'record') + '?', 'Delete').then(function (yes) { if (yes) api('POST', '/api/crm/records/' + n.record_id + '/delete').then(refresh).catch(fail); });
      } }, 'Delete')]);
  }

  /* ---------- log a call, meeting or conversation ---------- */
  function logForm(personId, personName, done) {
    var type = h('select', { class: 'demo-reset-input', 'aria-label': 'Type' }, TYPES.map(function (t) { return h('option', { value: t[0] }, t[1]); }));
    var when = h('input', { type: 'datetime-local', class: 'demo-reset-input', value: localNow(), 'aria-label': 'When' });
    var text = h('textarea', { rows: '5', placeholder: 'What was said, decided or asked…' });
    var opp = h('select', { class: 'demo-reset-input', 'aria-label': 'Opportunity' }, [h('option', { value: '' }, 'Not about an opportunity')]);
    api('GET', '/api/crm/people/' + personId + '/opportunities').then(function (r) {
      (r.opportunities || []).filter(function (o) { return o.active; }).forEach(function (o) { opp.appendChild(h('option', { value: o.opportunity_id }, o.name)); });
    }).catch(function () {});
    var err = h('p', { class: 'lks-err', hidden: true });
    var save = h('button', { class: 'btn btn-primary', type: 'button', onclick: function () {
      if (!text.value.trim()) { text.focus(); return; }
      save.disabled = true;
      api('POST', '/api/crm/people/' + personId + '/conversations', { type: type.value, when: when.value, text: text.value, opportunity_id: opp.value ? +opp.value : null })
        .then(function () { C().closeModal(true); if (done) done(); })
        .catch(function (e) { err.textContent = e.message; err.hidden = false; save.disabled = false; });
    } }, 'Save');
    C().modal(h('div', { class: 'lks-card lko-window' }, [
      h('h2', {}, 'Log a call or conversation' + (personName ? ' with ' + personName : '')),
      h('div', { class: 'lks-row' }, [type, when]), text, opp, err,
      h('div', { class: 'lks-row' }, [save, h('button', { class: 'btn lks-soft', type: 'button', onclick: function () { C().closeModal(true); } }, 'Cancel')])]));
    text.focus();
  }

  /* ---------- a logged conversation in a timeline ---------- */
  function row(m, refresh, showPeople) {
    var text = h('p', { class: 'crm-detail-message-text' }, m.message_text || '');
    var meta = h('div', { class: 'crm-detail-message-meta' }, [
      h('span', { class: tagClass(m.channel) }, m.channel),
      h('span', { class: 'crm-detail-message-sender' }, 'Logged' + (m.sender ? ' by ' + m.sender : '')),
      h('span', { class: 'crm-detail-message-time' }, stamp(m.timestamp))]);
    var with_ = (m.people || []).map(function (p) { return p.name; });
    var extra = [showPeople && with_.length ? 'With ' + with_.join(', ') : '', m.opportunity ? 'About: ' + m.opportunity : ''].filter(Boolean).join(' · ');
    return h('div', { class: 'crm-detail-message is-logged' }, [meta, extra ? h('p', { class: 'crm-call-who' }, extra) : null, text,
      refresh && m.mine ? editTools({ record_id: m.record_id, text: m.message_text, kind: 'conversation' }, text, refresh) : null]);
  }

  /* ---------- an opportunity's combined history ---------- */
  function oppHistory(el, oppId) {
    var data = null, offPeople = {}, offChannels = {};
    el.replaceChildren(h('p', { class: 'lks-note' }, 'Loading the history…'));
    api('GET', '/api/crm/opportunities/' + oppId + '/history').then(function (r) { data = r; draw(); })
      .catch(function (e) { el.replaceChildren(h('p', { class: 'lks-note' }, e.message)); });
    function draw() {
      var channels = [];
      data.history.forEach(function (m) { if (channels.indexOf(m.channel) < 0) channels.push(m.channel); });
      var chips = h('div', { class: 'crm-msg-controls' }, data.people.map(function (p) {
        return h('button', { type: 'button', class: 'lks-chipbtn' + (offPeople[p.contact_id] ? '' : ' on'), onclick: function () { offPeople[p.contact_id] = !offPeople[p.contact_id]; draw(); } },
          (offPeople[p.contact_id] ? '☐ ' : '☑ ') + p.name);
      }).concat(channels.length > 1 ? channels.map(function (ch) {
        return h('button', { type: 'button', class: tagClass(ch) + ' crm-msg-chan' + (offChannels[ch] ? ' off' : ''), onclick: function () { offChannels[ch] = !offChannels[ch]; draw(); } },
          (offChannels[ch] ? '☐ ' : '☑ ') + ch);
      }) : []));
      var shown = data.history.filter(function (m) {
        return !offChannels[m.channel] && (m.contacts || []).some(function (c) { return !offPeople[c.contact_id]; });
      });
      var rows = shown.slice().reverse().map(function (m) {
        var names = (m.contacts || []).map(function (c) { return c.name; }).join(', ');
        if (m.logged) return row(m, function () { oppHistory(el, oppId); }, true);
        var who = m.channel === 'call' ? 'Call' : m.direction === 'outgoing' ? 'You → ' + names : names + ' → you';
        var text = m.channel === 'call' ? ((m.call && (m.call.title || '')) + (m.call && m.call.summary ? '\n' + m.call.summary : '')) : (m.message_text || '');
        return h('div', { class: 'crm-detail-message ' + (m.direction === 'outgoing' ? 'is-mine' : 'is-theirs') }, [
          h('div', { class: 'crm-detail-message-meta' }, [h('span', { class: tagClass(m.channel) }, m.channel), h('span', { class: 'crm-detail-message-sender' }, who),
            h('span', { class: 'crm-detail-message-time' }, stamp(m.timestamp))]),
          m.channel === 'call' && names ? h('p', { class: 'crm-call-who' }, 'With ' + names) : null,
          h('p', { class: 'crm-detail-message-text' }, text)]);
      });
      el.replaceChildren(chips, rows.length ? h('div', { class: 'lkr-history' }, rows)
        : h('p', { class: 'lks-note' }, data.history.length ? 'Nothing matches the filters.' : 'No messages, calls or logged conversations with these people yet.'));
    }
  }

  window.lkRecords = { renderNotes: renderNotes, logForm: logForm, row: row, oppHistory: oppHistory };
})();
