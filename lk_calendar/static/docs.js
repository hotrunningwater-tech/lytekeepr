/* A simple read-only view of the documents Claude keeps in LyteKeepr: folders and documents of a funnel (or of its brand),
   and a document's text and tabs. Writing and organising happen through Claude. */
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
  function api(url) {
    return fetch(url).then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { if (!r.ok) throw new Error(d.error || 'Something went wrong'); return d; }); });
  }
  function C() { return window.lkContacts || {}; }
  function when(iso) { if (!iso) return ''; var d = new Date(iso); return isNaN(d) ? iso : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }); }

  var state = null;          // {funnelId, brand, scope: 'funnel'|'brand', tree, doc, tab}
  function open(funnelId, brand) {
    state = { funnelId: funnelId, brand: brand || '', scope: 'funnel', tree: null, doc: null, tab: null };
    load();
  }
  function openBrand(brand) {          // a brand's own documents, or Personal (no funnels)
    state = { funnelId: null, brand: brand, scope: 'brand', tree: null, doc: null, tab: null, only: true };
    load();
  }
  function load() {
    var q = state.scope === 'funnel' ? 'funnel_id=' + state.funnelId : 'brand=' + encodeURIComponent(state.brand);
    api('/api/crm/docs/tree?' + q).then(function (t) { state.tree = t; state.doc = null; render(); })
      .catch(function (e) { state.tree = { error: e.message }; render(); });
  }
  function openDoc(id) {
    api('/api/crm/docs/' + id).then(function (d) { state.doc = d; state.tab = null; render(); }).catch(function (e) { alert(e.message); });
  }

  function listing(t) {
    function docRow(d) {
      return h('button', { class: 'lkd-item', type: 'button', onclick: function () { openDoc(d.doc_id); } },
        [h('span', { class: 'lkd-ico' }, '▤'), h('span', {}, d.title), h('small', {}, when(d.updated_at) + (d.tabs.length ? ' · ' + d.tabs.length + ' tab' + (d.tabs.length > 1 ? 's' : '') : ''))]);
    }
    function folder(f, depth) {
      return h('div', { class: 'lkd-folder', style: 'margin-left:' + (depth * 16) + 'px' }, [
        h('div', { class: 'lkd-folder-name' }, [h('span', { class: 'lkd-ico' }, '▸'), f.name])]
        .concat(f.folders.map(function (x) { return folder(x, 1); }), f.documents.map(docRow)));
    }
    var kids = t.folders.map(function (f) { return folder(f, 0); }).concat(t.documents.map(docRow));
    return kids.length ? kids : [h('p', { class: 'lks-note' }, state.brand === 'personal' ? 'No personal documents yet. Ask Claude, e.g. “Save this as a personal note: …”'
                                                                     : 'No documents here yet. Ask Claude, e.g. “Create a Marketing Strategy document under this funnel.”')];
  }

  function docView(d) {
    var tabs = [{ key: null, name: 'Main text' }].concat((d.tabs || []).map(function (t) { return { key: t.tab_id, name: t.name, body: t.body }; }));
    if (!d.content && (d.tabs || []).length && state.tab === null) state.tab = d.tabs[0].tab_id;
    var cur = state.tab === null ? { body: d.content } : tabs.filter(function (t) { return t.key === state.tab; })[0] || { body: '' };
    return [
      h('button', { class: 'lko-link', type: 'button', onclick: function () { state.doc = null; render(); } }, '← All documents'),
      h('h2', { class: 'lkd-title' }, d.title),
      h('p', { class: 'lks-note' }, d.where + (d.created_by ? ' · created by ' + d.created_by : '') + ' · updated ' + when(d.updated_at) + (d.updated_by ? ' by ' + d.updated_by : '')),
      tabs.length > 1 ? h('div', { class: 'demo-reset-tabs lkd-tabs' }, tabs.map(function (t) {
        return h('button', { type: 'button', class: 'demo-reset-tab' + (state.tab === t.key ? ' is-active' : ''), onclick: function () { state.tab = t.key; render(); } }, t.name);
      })) : null,
      h('div', { class: 'lkd-text' }, cur.body || '(empty)')
    ];
  }

  function render() {
    var t = state.tree, body;
    if (!t) body = [h('p', {}, 'Loading…')];
    else if (t.error) body = [h('p', { class: 'lks-note' }, t.error)];
    else if (state.doc) body = docView(state.doc);
    else body = [h('p', { class: 'lks-note' }, t.where)].concat(listing(t));
    var switcher = state.brand && !state.only ? h('div', { class: 'demo-reset-tabs' }, [
      h('button', { type: 'button', class: 'demo-reset-tab' + (state.scope === 'funnel' ? ' is-active' : ''), onclick: function () { state.scope = 'funnel'; load(); } }, 'This funnel'),
      h('button', { type: 'button', class: 'demo-reset-tab' + (state.scope === 'brand' ? ' is-active' : ''), onclick: function () { state.scope = 'brand'; load(); } }, 'Brand documents')]) : null;
    C().modal(h('div', { class: 'lks-card lko-window lkd-window' }, [
      h('div', { class: 'lko-head' }, [h('span', { class: 'lko-kind' }, 'Documents'),
        h('button', { class: 'lko-x', type: 'button', 'aria-label': 'Close', onclick: function () { C().closeModal(true); } }, '×')]),
      state.doc ? null : switcher].concat(body)));
  }

  window.lkDocs = { open: open, openBrand: openBrand };
})();
