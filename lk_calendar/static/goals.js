/* All goals, only when asked for: the "All goals" button on the Daily goals card opens them in place of the chat (Back to chat
   returns). Long term, mid term and short term, for Personal or one brand, each with its steps. Home's daily and weekly goals
   stay where they are. */
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
  function $(id) { return document.getElementById(id); }
  function fail(e) { if (window.lkContacts && window.lkContacts.notice) window.lkContacts.notice(e.message); else alert(e.message); }
  function day(iso) {
    if (!iso) return '';
    var p = iso.slice(0, 10).split('-');
    return new Date(Date.UTC(+p[0], +p[1] - 1, +p[2])).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
  }
  function doneAt(iso) {          // "done Nov 3, 4:10 PM"
    if (!iso) return '';
    var d = new Date(iso);
    return 'done ' + (isNaN(d) ? iso : d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }));
  }

  // Only the longer levels here: today's and this week's goals stay on Home
  var ORDER = [['year', 'Long term', '1 year or more'], ['mid', 'Mid term', '2–4 months'], ['month', 'Short term', 'about 1 month']];
  var LEVEL_NAMES = [['year', 'Long term'], ['mid', 'Mid term'], ['month', 'Short term'], ['week', 'This week'], ['day', 'Today']];
  var STEP = { year: 'month', mid: 'month', month: 'week', week: 'day', day: 'day' };     // a step's level: one below its goal
  var state = { brand: 'personal', data: null, open: {}, editing: null };
  function remember() { try { localStorage.setItem('lkGoalsBrand', state.brand); } catch (e) { /* storage blocked */ } }
  try { state.brand = localStorage.getItem('lkGoalsBrand') || 'personal'; } catch (e) { /* storage blocked */ }

  function panel() { return $('aiPanel'); }
  function show(on) {
    panel().classList.toggle('goals-open', on);
    if (on) load(); else if (window.lkHome) window.lkHome.reload();
  }
  function load() {
    api('GET', '/api/calendar/goals/plan?brand=' + encodeURIComponent(state.brand)).then(function (d) { state.data = d; render(); })
      .catch(function (e) { state.data = { error: e.message }; render(); });
  }
  function put(g, body) { return api('PUT', '/api/calendar/goals/' + g.goal_id, body).then(load).catch(fail); }

  function row(g, isStep) {
    if (state.editing === g.goal_id) return editRow(g);
    var key = 'g' + g.goal_id, open = !!state.open[key], steps = g.steps || [];
    var meta = [g.target_date ? h('span', {}, 'by ' + day(g.target_date)) : null, g.done ? h('span', {}, doneAt(g.done_at)) : null,
                g.part_of ? h('span', {}, 'step of “' + g.part_of + '”') : null,
                steps.length ? h('button', { class: 'hm-link gl-steps', type: 'button', onclick: function () { state.open[key] = !open; render(); } },
                  (open ? '▾ ' : '▸ ') + g.steps_done + ' of ' + steps.length + ' steps done') : null].filter(Boolean);
    var tools = h('span', { class: 'hm-tools' }, [
      isStep || steps.length ? null : h('button', { class: 'hm-icon', type: 'button', title: 'Add steps', onclick: function () { state.open[key] = true; render(); } }, '+'),
      h('button', { class: 'hm-icon', type: 'button', title: 'Change', onclick: function () { state.editing = g.goal_id; render(); } }, '✎'),
      h('button', { class: 'hm-icon', type: 'button', title: 'Delete' + (steps.length ? ' (with its steps)' : ''), onclick: function () {
        if (confirm('Delete “' + g.text + '”' + (steps.length ? ' and its ' + steps.length + ' steps' : '') + '?')) api('POST', '/api/calendar/goals/' + g.goal_id + '/delete').then(load).catch(fail);
      } }, '×')]);
    var line = h('div', { class: 'hm-row hm-goal' + (isStep ? ' gl-step' : '') }, [
      h('button', { class: 'hm-check' + (g.done ? ' on' : ''), type: 'button', title: g.done ? 'Mark not done' : 'Mark done', onclick: function () { put(g, { done: !g.done }); } }, g.done ? '✓' : ''),
      h('div', { class: 'hm-main' }, [h('span', { class: 'hm-title' + (g.done ? ' done' : '') }, g.text), meta.length ? h('div', { class: 'hm-meta' }, meta) : null]), tools]);
    if (isStep || !open) return line;
    return h('div', {}, [line, h('div', { class: 'gl-sub' }, steps.map(function (s) { return row(s, true); }).concat([adder(STEP[g.period] || 'month', g.goal_id, 'Add a step…')]))]);
  }
  function editRow(g) {
    var text = h('input', { type: 'text', class: 'hm-input', value: g.text });
    var level = h('select', { class: 'hm-input hm-days' }, LEVEL_NAMES.map(function (o) { return h('option', { value: o[0], selected: o[0] === g.period }, o[1]); }));
    var date = h('input', { type: 'date', class: 'hm-input hm-date', value: g.target_date || '' });
    function save() {
      var body = { text: text.value };
      if (level.value !== g.period) body.period = level.value;
      if (date.value && date.value !== g.target_date && /^(month|mid|year)$/.test(level.value)) body.target_date = date.value;
      state.editing = null;
      put(g, body);
    }
    return h('div', { class: 'gl-edit' }, [text, h('div', { class: 'gl-edit-row' }, [level, date,
      h('button', { class: 'btn btn-primary btn-small', type: 'button', onclick: save }, 'Save'),
      h('button', { class: 'hm-mini', type: 'button', onclick: function () { state.editing = null; render(); } }, 'Cancel')])]);
  }
  function adder(level, parentId, placeholder) {
    var input = h('input', { type: 'text', class: 'hm-input', placeholder: placeholder });
    input.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' || !input.value.trim()) return;
      var body = { text: input.value, period: level };
      if (parentId) body.parent_id = parentId; else body.brand = state.brand;
      api('POST', '/api/calendar/goals', body).then(load).catch(fail);
    });
    return input;
  }

  function render() {
    var d = state.data, out = [];
    var brands = (d && d.brands) || [{ key: 'personal', name: 'Personal' }];
    var pick = h('select', { class: 'hm-input hm-days', onchange: function () { state.brand = pick.value; remember(); load(); } },
      brands.map(function (b) { return h('option', { value: b.key, selected: b.key === state.brand }, b.name); }));
    out.push(h('div', { class: 'gl-head' }, [h('button', { class: 'hm-mini', type: 'button', onclick: function () { show(false); } }, '← Back to chat'),
      h('span', { class: 'gl-title' }, 'Goals'), pick]));
    if (!d) out.push(h('p', { class: 'hm-empty' }, 'Loading…'));
    else if (d.error) out.push(h('p', { class: 'hm-empty' }, d.error));
    else ORDER.forEach(function (o) {
      var list = d.goals[o[0]] || [];
      out.push(h('div', { class: 'gl-level gl-' + o[0] }, [h('div', { class: 'gl-level-head' }, [h('span', { class: 'gl-level-name' }, o[1]),
        h('span', { class: 'gl-level-span' }, o[2]), list.length ? h('span', { class: 'gl-level-count' }, String(list.length)) : null]),
        h('div', { class: 'home-list' }, list.map(function (g) { return row(g, false); })),
        adder(o[0], null, { year: 'Add a long-term goal…', mid: 'Add a mid-term goal…', month: 'Add a short-term goal…' }[o[0]])]));
    });
    $('aiGoalsAll').replaceChildren.apply($('aiGoalsAll'), out);
  }

  document.addEventListener('click', function (e) {
    if (e.target && e.target.id === 'aiGoalsOpen') show(true);
  });
  window.lkGoals = { open: function () { show(true); }, close: function () { show(false); } };
  if (location.hash === '#goals') setTimeout(function () { show(true); }, 0);         // a link straight to all goals
})();
