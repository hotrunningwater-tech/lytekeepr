/* Work pipelines (a test feature, lk_calendar/work.py): the item card on a work board, the item window with its sub-items, and
   "New <item>". Kept apart from opportunities.js so a change here never touches sales; it borrows only that file's look. */
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
  function when(iso) { if (!iso) return ''; var d = new Date(iso); return isNaN(d) ? iso : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }); }
  function word(w) { return (w || '').trim() || 'Item'; }

  /* ---------- the card on a work board ---------- */
  function card(o, itemWord, onOpen) {
    var s = o.subitems || { done: 0, total: 0 };
    var c = h('div', { class: 'crm-funnel-person-card lko-card', draggable: 'true', title: word(itemWord) }, [
      h('div', { class: 'lko-kind' }, [h('span', { class: 'lko-icon', 'aria-hidden': 'true' }, '▪'), word(itemWord)]),
      h('div', { class: 'crm-funnel-person-name' }, o.name),
      s.total ? h('div', { class: 'lko-people' }, s.done + ' of ' + s.total + ' done') : null
    ]);
    c.addEventListener('dragstart', function (e) { e.dataTransfer.setData('text/plain', 'work:' + o.opportunity_id); e.dataTransfer.effectAllowed = 'move'; c.classList.add('crm-funnel-person-card-dragging'); });
    c.addEventListener('dragend', function () { c.classList.remove('crm-funnel-person-card-dragging'); });
    c.addEventListener('click', function () { onOpen(o.opportunity_id); });
    return c;
  }

  /* ---------- the item window ---------- */
  var current = null, onChange = null;
  function open(id, changed) {
    onChange = changed || onChange;
    api('GET', '/api/crm/work/items/' + id).then(function (o) { current = o; render(); }).catch(fail);
  }
  function refreshWith(promise) {
    return promise.then(function (o) { current = o; render(); if (onChange) onChange(); }).catch(fail);
  }
  function render() {
    var o = current, w = word(o.item_name);
    var nameInput = h('input', { type: 'text', value: o.name, class: 'lko-title', 'aria-label': w + ' name' });
    nameInput.addEventListener('change', function () { refreshWith(api('PUT', '/api/crm/work/items/' + o.opportunity_id, { name: nameInput.value })); });
    var stageSel = h('select', { class: 'demo-reset-input', 'aria-label': 'Stage' }, o.stages.map(function (s) { return h('option', { value: s.stage_id, selected: s.stage_id === o.stage_id }, s.name); }));
    stageSel.addEventListener('change', function () { refreshWith(api('POST', '/api/crm/work/items/' + o.opportunity_id + '/move', { stage_id: +stageSel.value })); });

    var subs = o.subitems || [];
    var list = h('div', { class: 'lko-people-list' }, subs.length ? subs.map(subRow) : [h('p', { class: 'lks-note' }, 'No sub-items yet. Add them below.')]);
    var newSub = h('input', { type: 'text', placeholder: 'New sub-item' });
    function addSub() {
      if (!newSub.value.trim()) { newSub.focus(); return; }
      refreshWith(api('POST', '/api/crm/work/items/' + o.opportunity_id + '/subitems', { name: newSub.value }));
    }
    newSub.addEventListener('keydown', function (e) { if (e.key === 'Enter') addSub(); });

    var notes = h('textarea', { rows: '3', placeholder: 'Notes' }, o.notes);
    notes.addEventListener('change', function () { refreshWith(api('PUT', '/api/crm/work/items/' + o.opportunity_id, { notes: notes.value })); });
    var hist = o.history || [];
    var history = h('details', { class: 'lko-history' }, [h('summary', {}, 'History (' + hist.length + ')'),
      h('ul', {}, hist.map(function (x) { return h('li', {}, [h('small', {}, when(x.at) + (x.who ? ' · ' + x.who : '')), ' ', x.text]); }))]);
    var del = h('button', { class: 'btn btn-remove btn-small', type: 'button', onclick: function () {
      confirmBox('Delete “' + o.name + '” with its sub-items and history?', 'Delete').then(function (yes) {
        if (!yes) return;
        api('POST', '/api/crm/work/items/' + o.opportunity_id + '/delete').then(function () { if (C().closeModal) C().closeModal(true); if (onChange) onChange(); }).catch(fail);
      });
    } }, 'Delete ' + w.toLowerCase());

    C().modal(h('div', { class: 'lks-card lko-window' }, [
      h('div', { class: 'lko-head' }, [h('span', { class: 'lko-kind' }, [h('span', { class: 'lko-icon' }, '▪'), w + ' · ' + o.funnel]),
        h('button', { class: 'lko-x', type: 'button', 'aria-label': 'Close', onclick: function () { C().closeModal(true); } }, '×')]),
      nameInput,
      h('div', { class: 'lks-row lko-meta' }, [h('label', {}, ['Stage ', stageSel])]),
      h('h3', {}, 'Sub-items' + (subs.length ? ' (' + subs.filter(function (s) { return s.done; }).length + ' of ' + subs.length + ' done)' : '')),
      list,
      h('div', { class: 'lks-row' }, [newSub, h('button', { class: 'btn btn-secondary btn-small', type: 'button', onclick: addSub }, 'Add')]),
      h('h3', {}, 'Notes'), notes,
      history,
      h('div', { class: 'lks-row lko-foot' }, [del])
    ]));
  }
  function subRow(s) {
    var o = current;
    var box = h('input', { type: 'checkbox', checked: s.done, 'aria-label': 'Done' });
    box.addEventListener('change', function () { refreshWith(api('PUT', '/api/crm/work/subitems/' + s.subitem_id, { done: box.checked })); });
    var name = h('input', { type: 'text', value: s.name, 'aria-label': 'Sub-item' });
    name.addEventListener('change', function () { refreshWith(api('PUT', '/api/crm/work/subitems/' + s.subitem_id, { name: name.value })); });
    var note = h('input', { type: 'text', value: s.note, placeholder: 'Note', 'aria-label': 'Note' });
    note.addEventListener('change', function () { refreshWith(api('PUT', '/api/crm/work/subitems/' + s.subitem_id, { note: note.value })); });
    var rm = h('button', { class: 'lko-x', type: 'button', 'aria-label': 'Remove', title: 'Remove', onclick: function () {
      refreshWith(api('POST', '/api/crm/work/subitems/' + s.subitem_id + '/delete'));
    } }, '×');
    return h('div', { class: 'lks-row lko-sub' + (s.done ? ' done' : '') }, [box, name, note, rm]);
  }

  /* ---------- "New <item>" ---------- */
  function create(funnelId, stages, itemWord, changed) {
    var w = word(itemWord);
    var name = h('input', { type: 'text', placeholder: w + ' name' });
    var stage = h('select', { class: 'demo-reset-input' }, (stages || []).map(function (s) { return h('option', { value: s.stage_id }, s.name); }));
    var err = h('p', { class: 'lks-err', hidden: true });
    var save = h('button', { class: 'btn btn-primary', type: 'button', onclick: function () {
      if (!name.value.trim()) { name.focus(); return; }
      save.disabled = true;
      api('POST', '/api/crm/work/items', { name: name.value, funnel_id: funnelId, stage_id: +stage.value })
        .then(function (o) { if (changed) changed(); open(o.opportunity_id, changed); })
        .catch(function (e) { save.disabled = false; err.hidden = false; err.textContent = e.message; });
    } }, 'Create');
    C().modal(h('div', { class: 'lks-card lko-window' }, [
      h('div', { class: 'lko-head' }, [h('h2', {}, 'New ' + w.toLowerCase()), h('button', { class: 'lko-x', type: 'button', 'aria-label': 'Close', onclick: function () { C().closeModal(true); } }, '×')]),
      h('label', { class: 'lko-field' }, ['Name', name]), h('label', { class: 'lko-field' }, ['Stage', stage]),
      err, h('div', { class: 'lks-row' }, [save])
    ]));
    setTimeout(function () { name.focus(); }, 50);
  }

  window.lkWork = { card: card, open: open, create: create,
    move: function (id, stageId) { return api('POST', '/api/crm/work/items/' + id + '/move', { stage_id: stageId }); } };
})();
