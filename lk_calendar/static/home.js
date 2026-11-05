/* Home, the daily workspace: the automatic items (follow-ups, calls to review, reminders) as a bar of counts, then your tasks
   (overdue, today, the next days) under today's and this week's goals and the meetings coming up. Tasks live here, not in the calendar (the calendar shows meetings). Goals are per person. */
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
  function fill(el, kids) { el.replaceChildren.apply(el, (kids || []).filter(function (k) { return k != null && k !== false; })); }
  if (!$('homeActions')) return;
  function fail(e) { if (window.lkContacts && window.lkContacts.notice) window.lkContacts.notice(e.message); else alert(e.message); }

  var state = { agenda: null, goals: null, brands: {}, open: {}, quiet: null, quietDays: 14, quietYouDays: 3, quietBrand: '', taskShow: 'all' };
  function isMeeting(e) { return e.kind === 'meeting' || e.source === 'google' || !!e.has_call || !!e.expect_recording; }
  function toUTC(iso) { var p = iso.split('-'); return new Date(Date.UTC(+p[0], +p[1] - 1, +p[2])); }
  function dayLabel(iso, today) {
    if (iso === today) return 'Today';
    var diff = Math.round((toUTC(iso) - toUTC(today)) / 864e5);
    if (diff === 1) return 'Tomorrow';
    if (diff === -1) return 'Yesterday';
    return toUTC(iso).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC' });
  }
  function time12(hm) {
    if (!hm) return '';
    var p = hm.split(':'), hr = +p[0], m = p[1];
    return (hr % 12 || 12) + (m === '00' ? '' : ':' + m) + (hr < 12 ? ' AM' : ' PM');
  }
  function brandDot(key) {
    if (key === 'personal') return h('span', { class: 'hm-brand', title: 'Only you see it' }, [h('i', { style: 'background:#9a8fb0' }), 'Personal']);
    var b = state.brands[key];
    return b ? h('span', { class: 'hm-brand' }, [h('i', { style: 'background:' + b.color }), b.name]) : null;
  }

  function load() {
    return Promise.all([api('GET', '/api/calendar/agenda'), api('GET', '/api/calendar/goals'),
                        api('GET', '/api/crm/brands').catch(function () { return { brands: [] }; }),
                        api('GET', '/api/crm/quiet?days=' + state.quietDays + '&you_days=' + state.quietYouDays + '&brand=' + encodeURIComponent(state.quietBrand || '')).catch(function () { return null; }),
                        api('GET', '/api/calendar/inbox/flags').catch(function () { return { flags: [] }; }),
                        api('GET', '/api/crm/brands/unassigned').catch(function () { return { contacts: [] }; })])
      .then(function (r) {
        state.agenda = r[0]; state.goals = r[1]; state.quiet = r[3]; state.flags = r[4].flags || []; state.noBrand = r[5].contacts || [];
        state.brands = {}; (r[2].brands || []).forEach(function (b) { state.brands[b.key] = b; });
        render();
      }).catch(function () {});
  }
  window.lkHome = { reload: load };

  /* ---------- Get started: a short list for someone new; it goes away when done or dismissed ---------- */
  function loadStart() {
    api('GET', '/api/calendar/getting-started').then(renderStart).catch(function () { /* never in the way of Home */ });
  }
  function startAction(s) {
    if (s.action === 'gmail') location.href = (window.LK_PREFIX || '') + '/auth/google/start';
    else if (s.action === 'linkedin_approve') location.href = s.link;     // here, in the window and account the USER is signed in to
    else if (s.action === 'claude') window.open(s.link, '_blank', 'noopener');   // Claude's add-connector form, already filled in
    else if (s.action === 'import') { imp = imp ? null : { stage: 'drop' }; loadStart(); }
    else if (s.action === 'import_review') {             // Claude's plan, on CRM → Import (Match fields, filled in)
      var token = (s.link.split('import=')[1] || '');
      if (window.lkOpenImportPlan) window.lkOpenImportPlan(decodeURIComponent(token)); else location.href = s.link;
    }
    else if (s.action === 'crm') { var crm = $('crmTab'); if (crm) crm.click(); }
    else if (s.action === 'linkedin') {                 // downloads the Claude add-on, then says exactly what to do with the file
      location.href = (window.LK_PREFIX || '') + '/api/linkedin/addon';
      try { sessionStorage.setItem('lkLinkedinDownloaded', '1'); } catch (e) { /* storage blocked */ }
      loadStart();
    }
    else if (s.action && s.action.indexOf('settings:') === 0 && window.lkOpenSettings) window.lkOpenSettings(s.action.slice(9));
  }
  function linkedinNext(s) {           // after the download: the next steps, one per line
    var downloaded = false;
    try { downloaded = s.key === 'linkedin' && sessionStorage.getItem('lkLinkedinDownloaded') === '1'; } catch (e) { /* storage blocked */ }
    if (!downloaded || s.action !== 'linkedin') return null;          // only before the add-on is installed
    return h('ol', { class: 'gs-why gs-next' }, ['Open your Downloads folder.', 'Double-click "LyteKeepr LinkedIn.mcpb". The Claude app opens.',
      'In Claude, click Install.', 'Come back here and click Approve.', 'Log in to LinkedIn if a window asks.']
      .map(function (t) { return h('li', {}, t); }));
  }
  var startTimer = null;
  /* ---------- Import a list, right in the step: drop a file; LyteKeepr keeps it, then Claude plans the import and USER checks it ---------- */
  var imp = null;                      // null: closed; {stage: 'drop' | 'busy', error}
  function importBox() {
    if (!imp) return null;
    function take(file) {
      if (!file) return;
      var fd = new FormData(); fd.append('file', file);
      imp = { stage: 'busy' }; loadStart();
      fetch('/api/calendar/import/scan', { method: 'POST', body: fd })
        .then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { if (!r.ok) throw new Error(d.error || 'Something went wrong'); return d; }); })
        .then(function () { imp = null; loadStart(); })          // the step now says: Tell Claude
        .catch(function (e) { imp = { stage: 'drop', error: e.message }; loadStart(); });
    }
    if (imp.stage === 'busy') return h('div', { class: 'gs-drop' }, 'Reading your file…');
    var input = h('input', { type: 'file', accept: '.csv,.xlsx,.tsv,.txt', hidden: true, onchange: function () { take(input.files[0]); } });
    var box = h('div', { class: 'gs-drop gs-dropzone', onclick: function () { input.click(); } }, [
      h('p', {}, [h('b', {}, 'Drop your CSV here'), ', or click to choose a file. Excel (.xlsx) works too.']),
      imp.error ? h('p', { class: 'lks-err' }, imp.error) : null, input]);
    box.addEventListener('dragover', function (e) { e.preventDefault(); box.classList.add('over'); });
    box.addEventListener('dragleave', function () { box.classList.remove('over'); });
    box.addEventListener('drop', function (e) { e.preventDefault(); box.classList.remove('over'); take(e.dataTransfer.files[0]); });
    return box;
  }
  function howTo(s) {                  // a step done by talking to Claude: exactly what to click, send and expect
    if (!s.how || !s.how.length) return null;
    return h('ol', { class: 'gs-why gs-next' }, s.how.map(function (t) { return h('li', {}, t); }));
  }
  function renderStart(d) {
    var box = $('homeStart');
    clearTimeout(startTimer);
    box.hidden = !d.show;
    if (!d.show) { box.replaceChildren(); return; }
    function send(body) { api('POST', '/api/calendar/getting-started', body).then(renderStart).catch(fail); }
    if (d.steps.some(function (s) { return s.busy; })) startTimer = setTimeout(loadStart, 20000);   // something is coming in: check again by itself
    if (d.complete) {                                  // every step done or skipped: it stays until USER closes it
      fill(box, [h('div', { class: 'gs-card gs-complete' }, [h('div', { class: 'gs-head' }, [h('span', { class: 'gs-title' }, 'Setup complete'),
        h('span', { class: 'gs-count' }, 'Everything is connected. LyteKeepr is ready.'),
        h('button', { class: 'btn btn-primary btn-small', type: 'button', onclick: function () { send({ dismissed: true }); } }, 'Close')])])]);
      return;
    }
    fill(box, [h('div', { class: 'gs-card' }, [
      h('div', { class: 'gs-head' }, [h('span', { class: 'gs-title' }, 'Get started'), h('span', { class: 'gs-count' }, d.done + ' of ' + d.total + ' done'),
        h('button', { class: 'hm-mini', type: 'button', onclick: function () { send({ dismissed: true }); } }, 'Hide')]),
      h('div', { class: 'gs-list' }, d.steps.map(function (s) {
        var skip = s.skippable && !s.done && !s.skipped ? h('button', { class: 'hm-mini', type: 'button', onclick: function () { send({ skip: s.key }); } }, 'Skip') : null;
        var again = s.start_over && !s.done && !s.skipped ? h('button', { class: 'hm-mini', type: 'button', title: 'Back to a new file',
          onclick: function () { imp = null; send({ start_over: s.key }); } }, 'Start over') : null;
        var doit = s.done ? h('span', { class: 'gs-done' }, 'Done')
          : s.skipped ? h('button', { class: 'hm-mini', type: 'button', title: 'Undo skip', onclick: function () { send({ unskip: s.key }); } }, 'Skipped · undo')
          : s.busy ? h('span', { class: 'gs-busy' }, 'In progress…')
          : s.say ? h('button', { class: 'hm-mini', type: 'button', onclick: function (ev) {          // the sentence to say to Claude
              var b = ev.currentTarget;
              try { navigator.clipboard.writeText(s.say).then(function () { b.textContent = 'Copied'; }, function () {}); } catch (e) { /* no clipboard */ }
              if (s.chat) window.open('https://claude.ai/new?q=' + encodeURIComponent(s.say), '_blank', 'noopener');   // a new chat with it filled in; if not, it's copied
            } }, s.chat ? 'Tell Claude' : 'Copy')
          : s.action ? h('button', { class: 'hm-mini', type: 'button', onclick: function () { startAction(s); } }, s.action === 'linkedin' ? 'Download' : s.action === 'linkedin_approve' ? 'Approve' : s.action === 'claude' ? 'Connect' : s.action === 'import' ? (imp ? 'Cancel' : 'Start') : s.action === 'import_review' ? 'Review' : 'Open')
          : null;
        var finished = s.done || s.skipped;
        return h('div', { class: 'gs-step' + (finished ? ' done' : '') }, [h('span', { class: 'hm-check ro' + (s.done ? ' on' : '') }, s.done ? '✓' : ''),
          h('div', { class: 'hm-main' }, [h('span', { class: 'hm-title' }, s.title),
            finished ? null : howTo(s) || linkedinNext(s) || h('div', { class: 'gs-why' }, s.why),
            s.key === 'import' && (!finished || (imp && imp.stage === 'done')) ? importBox() : null]), again, skip, doit]);
      }))])]);
  }
  loadStart();
  document.addEventListener('visibilitychange', function () { if (!document.hidden) { loadStart(); loadAlerts(); } });

  /* ---------- Not syncing: email or LinkedIn stopped, with the one step that fixes it ---------- */
  function loadAlerts() {
    api('GET', '/api/calendar/sync-problems').then(renderAlerts).catch(function () { /* never in the way of Home */ });
  }
  function alertAction(p) {
    if (p.action === 'gmail') return h('button', { class: 'hm-mini', type: 'button', onclick: function () { location.href = (window.LK_PREFIX || '') + '/auth/google/start'; } }, 'Reconnect');
    if (p.action === 'linkedin') return h('button', { class: 'hm-mini', type: 'button', onclick: function () { var t = $('linkedinTab'); if (t) t.click(); } }, 'Open LinkedIn tab');
    if (p.action === 'linkedin_solved') return h('button', { class: 'hm-mini', type: 'button', onclick: function () { api('POST', '/api/calendar/sync-problems/linkedin-solved', {}).then(renderAlerts).catch(fail); } }, 'Solved');
    return null;
  }
  function renderAlerts(d) {
    var box = $('homeAlerts'), list = (d && d.problems) || [];
    box.hidden = !list.length;
    fill(box, list.map(function (p) {
      return h('div', { class: 'lk-alert' }, [h('div', { class: 'hm-main' }, [h('span', { class: 'hm-title' }, p.title), h('div', { class: 'gs-why' }, p.fix)]),
        alertAction(p), h('button', { class: 'hm-mini', type: 'button', onclick: function () { openHelp(p.title + ': '); } }, 'Help')]);
    }));
  }
  loadAlerts();
  setInterval(loadAlerts, 10 * 60 * 1000);

  /* ---------- Help: USER writes what's wrong; it reaches the LyteKeepr team by email, and the reply comes back to USER's inbox ---------- */
  function openHelp(start) {
    var old = $('lkHelp'); if (old) old.remove();
    var text = h('textarea', { class: 'lk-help-text', rows: '5', placeholder: 'What happened, and what did you expect?' });
    text.value = start || '';
    var note = h('div', { class: 'gs-why' }, 'We see which account this is from and what isn\'t working, so a sentence is enough.');
    var send = h('button', { class: 'btn btn-primary', type: 'button' }, 'Send');
    var close = function () { wrap.remove(); };
    var wrap = h('div', { class: 'lk-help-back', id: 'lkHelp', onclick: function (ev) { if (ev.target === wrap) close(); } }, [
      h('div', { class: 'lk-help-card', role: 'dialog', 'aria-label': 'Help' }, [
        h('div', { class: 'gs-head' }, [h('span', { class: 'gs-title' }, 'Tell us what\'s not working'), h('button', { class: 'hm-mini', type: 'button', onclick: close }, 'Close')]),
        text, note, h('div', { class: 'lk-help-row' }, [send])])]);
    send.onclick = function () {
      send.disabled = true;
      api('POST', '/api/calendar/help', { text: text.value }).then(function (r) {
        fill(wrap.firstChild, [h('div', { class: 'gs-head' }, [h('span', { class: 'gs-title' }, 'Sent'), h('button', { class: 'hm-mini', type: 'button', onclick: close }, 'Close')]),
          h('div', { class: 'gs-why' }, r.reply_to ? 'We\'ll reply to ' + r.reply_to + '.' : 'We\'ll get back to you by email.')]);
      }).catch(function (e) { send.disabled = false; note.textContent = e.message; });
    };
    document.body.appendChild(wrap);
    text.focus();
  }
  window.lkOpenHelp = openHelp;
  var helpBtn = $('helpBtn'); if (helpBtn) helpBtn.addEventListener('click', function () { openHelp(''); });

  // Tasks: All, Work only or Personal only (lead follow-ups and meetings are work and aren't affected)
  function shown(e) {
    if (e.lead || state.taskShow === 'all') return true;
    return (e.project === 'personal') === (state.taskShow === 'personal');
  }
  function render() {
    var a = state.agenda, today = a.now.slice(0, 10);
    $('homeDateLine').textContent = toUTC(today).toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric', timeZone: 'UTC' });
    var overdue = (a.overdue || []).filter(function (e) { return !isMeeting(e); });
    var todays = (a.today || []).filter(function (e) { return !isMeeting(e) && !e.done; });
    renderActions(overdue, todays, today, a.upcoming || []);
    renderQuiet();
    renderFlags(today);
    renderNoBrand();
    countRows();
    renderGoals();
    renderMeetings(a, today);
    renderTasks((a.upcoming || []).filter(function (e) { return !isMeeting(e) && !e.lead; }), today);
  }

  /* ---------- your tasks due (overdue + today); lead follow-ups go to the automatic bar, cold ones folded per channel ---------- */
  function fold(list) {             // every lead follow-up of one channel in one row (DMs, follow-ups and replies together)
    var groups = {}, out = [];
    list.forEach(function (e) {
      if (e.lead) {
        var key = (e.lead.channel || 'other').toLowerCase();
        var g = groups[key];
        if (!g) { g = groups[key] = { group: true, channel: key, members: [] }; out.push(g); }
        g.members.push(e);
      } else out.push(e);
    });
    return out;
  }
  function kindOf(e) { return /^send dm/i.test(e.title) ? 'Send DM' : /^reply/i.test(e.title) ? 'Reply' : 'Follow up'; }
  function check(done, onToggle, title) {
    return h('button', { class: 'hm-check' + (done ? ' on' : ''), type: 'button', title: title || (done ? 'Mark not done' : 'Mark done'), 'aria-label': title || 'Done',
      onclick: function (ev) { ev.stopPropagation(); onToggle(); } }, done ? '✓' : '');
  }
  function markDone(e) {
    if (e.lead && !confirm('Mark "' + itemTitle(e) + '" done?')) return;         // a follow-up is never cleared by a stray click
    return api('POST', '/api/calendar/events/' + e.id + '/done', { date: e.date, done: true })
      .then(function () { showUndo(e.lead ? 'homeFollowups' : 'homeActions', [{ id: e.id, date: e.date }], '"' + itemTitle(e) + '" marked done'); return load(); }).catch(fail);
  }
  // Undo sits at the top of the same list, where you just clicked, for 15 seconds
  var undoTimer = null;
  function showUndo(listId, items, text) {
    state.undo = { list: listId, items: items, text: text };
    clearTimeout(undoTimer); undoTimer = setTimeout(function () { state.undo = null; render(); }, 15000);
  }
  function undoRow(listId) {
    var u = state.undo;
    if (!u || u.list !== listId) return null;
    return h('div', { class: 'hm-undo' }, [h('span', {}, u.text), h('button', { type: 'button', onclick: function () {
      state.undo = null; clearTimeout(undoTimer);
      api('POST', '/api/calendar/events/done-bulk', { items: u.items, done: false }).then(load).catch(fail);
    } }, 'Undo')]);
  }
  // "Reply to david@x.com" reads as "Reply to David Woodbridge" when the contact has a name
  function itemTitle(e) {
    var l = e.lead;
    if (!l || !l.person_name || !l.contact) return e.title;
    return e.title.indexOf(l.contact) >= 0 ? e.title.replace(l.contact, l.person_name) : e.title + ' · ' + l.person_name;
  }
  function itemRow(e, today, overdue) {
    var who = e.lead && e.lead.person_id;
    var title = h(who ? 'button' : 'span', who ? { class: 'hm-title hm-link', type: 'button', onclick: function () { if (window.openCrmPersonDetail) window.openCrmPersonDetail(e.lead.person_id); } }
                                               : { class: 'hm-title' }, itemTitle(e));
    var meta = [overdue ? h('span', { class: 'hm-late' }, 'Overdue · ' + dayLabel(e.date, today)) : (e.untimed ? null : h('span', {}, time12(e.start_time))),
                brandDot(e.project)].filter(Boolean);
    return h('div', { class: 'hm-row' }, [check(false, function () { markDone(e); }), h('div', { class: 'hm-main' }, [title, meta.length ? h('div', { class: 'hm-meta' }, meta) : null])]);
  }
  function groupRow(g, today) {
    var key = 'g:' + (g.section || '') + ':' + g.channel;
    var name = { linkedin: 'LinkedIn', instagram: 'Instagram', email: 'Email', other: 'Other' }[g.channel] || (g.channel.charAt(0).toUpperCase() + g.channel.slice(1));
    var label = name + ' · ' + g.members.length;
    var kinds = {}; g.members.forEach(function (m) { var k = kindOf(m); kinds[k] = (kinds[k] || 0) + 1; });
    var brands = {}; g.members.forEach(function (m) { if (m.project) brands[m.project] = 1; });
    var late = g.members.filter(function (m) { return m.date < today; }).length;
    var open = !!state.open[key];
    var all = h('button', { class: 'hm-mini', type: 'button', onclick: function (ev) {
      ev.stopPropagation();
      var n = g.members.length;
      if (!confirm('Mark all ' + n + ' ' + name + ' follow-ups done?')) return;      // one click never clears a whole group unasked
      var items = g.members.map(function (m) { return { id: m.id, date: m.date }; });
      api('POST', '/api/calendar/events/done-bulk', { items: items, done: true })
        .then(function () { showUndo('homeFollowups', items, n + ' ' + name + ' follow-up' + (n === 1 ? '' : 's') + ' marked done'); return load(); }).catch(fail);
    } }, 'All done');
    var row = h('div', { class: 'hm-row hm-group' }, [check(false, function () { all.click(); }, 'Mark all done'),
      h('div', { class: 'hm-main' }, [h('button', { class: 'hm-title hm-link', type: 'button', onclick: function () { state.open[key] = !open; render(); } }, (open ? '▾ ' : '▸ ') + label),
        h('div', { class: 'hm-meta' }, [late ? h('span', { class: 'hm-late' }, late + ' overdue') : h('span', {}, g.members[0].date === today ? 'Due today' : dayLabel(g.members[0].date, today)),
          h('span', {}, Object.keys(kinds).map(function (k) { return kinds[k] + ' ' + k; }).join(' · '))].concat(Object.keys(brands).map(brandDot)).filter(Boolean))]),
      all]);
    var wrap = h('div', {}, [row]);
    if (open) wrap.appendChild(h('div', { class: 'hm-sub' }, g.members.map(function (m) { return itemRow(m, today, m.date < today); })));
    return wrap;
  }
  function renderActions(overdue, todays, today, coming) {
    var due = overdue.concat(todays);
    var own = due.filter(function (e) { return !e.lead && shown(e); }), leads = due.filter(function (e) { return !!e.lead; });
    state.counts.Followups = leads.length; state.ownDue = own.length;
    var late = own.filter(function (e) { return e.date < today; }), now = own.filter(function (e) { return e.date >= today; });
    var showPick = h('select', { class: 'hm-input hm-days', title: 'Which tasks to show', onchange: function () { state.taskShow = showPick.value; render(); } },
      [['all', 'All tasks'], ['work', 'Work'], ['personal', 'Personal']].map(function (o) { return h('option', { value: o[0], selected: state.taskShow === o[0] }, o[1]); }));
    fill($('homeActionsTools'), [showPick, late.length ? h('button', { class: 'hm-mini', type: 'button', onclick: function () {
      api('POST', '/api/calendar/overdue/move', { to: 'today' }).then(load).catch(fail);
    } }, 'Move overdue to today') : null]);
    fill($('homeActions'), [undoRow('homeActions'), late.length ? h('div', { class: 'hm-day hm-late' }, 'Overdue') : null].concat(late.map(function (e) { return itemRow(e, today, true); }),
      [h('div', { class: 'hm-day' }, 'Today')], now.length ? now.map(function (e) { return itemRow(e, today, false); }) : [h('p', { class: 'hm-empty' }, 'Nothing due today.')]));
    // Follow-ups in three parts: late, today, and the next 7 days (so a planned one is visible before its day)
    function part(label, list, cls, section) {
      if (!list.length) return [];
      return [h('div', { class: 'hm-day' + (cls ? ' ' + cls : '') }, label)].concat(fold(list).map(function (it) {
        if (it.group) { it.section = section; return groupRow(it, today); }
        return itemRow(it, today, it.date < today);
      }));
    }
    var fLate = leads.filter(function (e) { return e.date < today; }), fToday = leads.filter(function (e) { return e.date >= today; });
    var fComing = (coming || []).filter(function (e) { return e.lead && !e.done && e.date > today; });
    state.followComing = fComing.length;
    var parts = part('Overdue', fLate, 'hm-late', 'late').concat(part('Today', fToday, '', 'today'), part('Coming up · next 7 days', fComing, '', 'coming'));
    fill($('homeFollowups'), [undoRow('homeFollowups')].concat(parts.length ? parts : [h('p', { class: 'hm-empty' }, 'No follow-ups due or coming up.')]).filter(Boolean));
  }

  /* ---------- needs a brand: contacts a sync or a one-click suggestion created without one; every contact belongs to a brand ---------- */
  function brandSelect(onPick, label) {
    var sel = h('select', { class: 'hm-input hm-days', onchange: function () { if (sel.value) onPick(sel.value); } },
      [h('option', { value: '' }, label || 'Choose a brand…')].concat(Object.keys(state.brands).map(function (k) { return h('option', { value: k }, state.brands[k].name); })));
    return sel;
  }
  function assignBrand(ids, key) { api('POST', '/api/crm/brands/assign', { person_ids: ids, brand: key }).then(load).catch(fail); }
  function renderNoBrand() {
    var list = state.noBrand || [];
    state.counts.Brands = list.length;
    if (!list.length) { fill($('homeNoBrand'), []); return; }
    var rows = list.map(function (c) {
      return h('div', { class: 'hm-row' }, [
        h('div', { class: 'hm-main' }, [h('button', { class: 'hm-title hm-link', type: 'button', onclick: function () { if (window.openCrmPersonDetail) window.openCrmPersonDetail(c.contact_id); } }, c.name || '(no name)'),
          h('div', { class: 'hm-meta' }, [c.company_name ? h('span', {}, c.company_name) : null, c.source_file ? h('span', {}, c.source_file) : null].filter(Boolean))]),
        brandSelect(function (key) { assignBrand([c.contact_id], key); })]);
    });
    fill($('homeNoBrand'), [h('div', { class: 'hm-quiet-head' }, [h('span', { class: 'hm-empty' }, 'Every contact belongs to a brand. Pick one for each, or for all at once:'),
      brandSelect(function (key) { assignBrand(list.map(function (c) { return c.contact_id; }), key); }, 'All ' + list.length + ' to…')])].concat(rows));
  }

  /* ---------- from messages: the few flags the scheduled Claude run left (interested, waiting for you, commitments) ---------- */
  var FLAG_GROUPS = [['interested', 'Interested'], ['waiting', 'Waiting for your answer'], ['commitment', 'Commitments']];
  function setFlag(f, st) { api('POST', '/api/calendar/inbox/flags/' + f.flag_id, { state: st }).then(load).catch(fail); }
  function flagRow(f, today) {
    var who = f.person_id ? h('button', { class: 'hm-title hm-link', type: 'button', onclick: function () { if (window.openCrmPersonDetail) window.openCrmPersonDetail(f.person_id); } }, f.who)
                          : h('span', { class: 'hm-title' }, f.who + (f.email && f.email !== f.who ? ' · ' + f.email : ''));
    var meta = [f.kind === 'commitment' ? h('span', {}, f.side === 'ours' ? 'You promised' : 'They promised') : null,
                f.due ? h('span', { class: f.due < today ? 'hm-late' : '' }, 'Due ' + dayLabel(f.due, today)) : null,
                f.at ? h('span', {}, 'Said ' + dayLabel(f.at.slice(0, 10), today).toLowerCase()) : null].filter(Boolean);
    return h('div', { class: 'hm-row' }, [
      h('div', { class: 'hm-main' }, [who, h('span', { class: 'hm-flag-sum' }, f.summary),
        f.quote ? h('span', { class: 'hm-quote' }, '“' + f.quote + '”') : null, meta.length ? h('div', { class: 'hm-meta' }, meta) : null]),
      !f.person_id && f.email && window.lkContacts && window.lkContacts.addContact ? h('button', { class: 'hm-mini', type: 'button', onclick: function () {
        window.lkContacts.addContact({ email: f.email, name: f.who !== f.email ? f.who : '', channel: 'email' }, load); } }, 'Add contact') : null,
      f.kind === 'commitment' ? h('button', { class: 'hm-mini', type: 'button', onclick: function () { setFlag(f, 'done'); } }, 'Done') : null,
      h('button', { class: 'hm-mini', type: 'button', onclick: function () { setFlag(f, 'dismissed'); } }, 'Dismiss')]);
  }
  function renderFlags(today) {
    var flags = state.flags || [];
    state.counts.Flags = flags.length;
    var out = [];
    FLAG_GROUPS.forEach(function (g) {
      var list = flags.filter(function (f) { return f.kind === g[0]; });
      if (!list.length) return;
      out.push(h('div', { class: 'hm-day' }, g[1] + ' · ' + list.length));
      list.forEach(function (f) { out.push(flagRow(f, today)); });
    });
    fill($('homeFlags'), out);
  }

  /* ---------- who is waiting: every contact whose conversation stopped, split by who owes the next message ---------- */
  var HOW = { email: 'email', linkedin: 'LinkedIn', instagram: 'Instagram', outreach: 'outreach step' };
  function quietRow(c) {
    var today = state.agenda ? state.agenda.now.slice(0, 10) : '';
    var fu = c.follow_up ? (c.follow_up.overdue ? h('span', { class: 'hm-late' }, 'Follow-up overdue since ' + dayLabel(c.follow_up.date, today))
                                                : h('span', {}, 'Follow-up planned ' + dayLabel(c.follow_up.date, today))) : null;
    var meta = [h('span', { class: 'hm-late' }, c.days_quiet + ' days'), h('span', {}, 'Last: ' + (HOW[c.last_how] || c.last_how)), fu,
                c.funnel_name ? h('span', {}, c.funnel_name + (c.stage_name ? ' · ' + c.stage_name : '')) : null, c.company_name ? h('span', {}, c.company_name) : null,
                brandDot(c.brand)].filter(Boolean);
    return h('div', { class: 'hm-row' }, [
      h('div', { class: 'hm-main' }, [h('button', { class: 'hm-title hm-link', type: 'button', onclick: function () { if (window.openCrmPersonDetail) window.openCrmPersonDetail(c.person_id); } }, c.name),
        c.their_last_words ? h('span', { class: 'hm-quote' }, '“' + c.their_last_words + '”') : null, h('div', { class: 'hm-meta' }, meta)]),
      h('button', { class: 'hm-mini', type: 'button', title: 'Off this list for 14 days', onclick: function () {
        api('POST', '/api/crm/quiet/' + c.person_id + '/snooze', { days: 14 }).then(load).catch(fail);
      } }, 'Not now')]);
  }
  function renderQuiet() {
    var q = state.quiet;
    state.counts.Quiet = q ? q.count : 0;
    if (!q) { fill($('homeQuiet'), []); return; }
    var brandPick = h('select', { class: 'hm-input hm-days', onchange: function () { state.quietBrand = brandPick.value; load(); } },
      [h('option', { value: '', selected: !q.brand }, 'All brands')].concat(Object.keys(state.brands).map(function (k) {
        return h('option', { value: k, selected: q.brand === k }, state.brands[k].name);
      })));
    function dayPick(key, list, value, label) {
      var sel = h('select', { class: 'hm-input hm-days', onchange: function () { state[key] = +sel.value; load(); } }, list.map(function (d) {
        return h('option', { value: d, selected: d === value }, label + ' ' + d + '+ days');
      }));
      return sel;
    }
    var out = [h('div', { class: 'hm-quiet-head' }, [brandPick])];
    out.push(h('div', { class: 'hm-quiet-head' }, [h('div', { class: 'hm-day hm-late' }, 'Waiting on you · ' + q.waiting_on_you.length),
      dayPick('quietYouDays', [1, 3, 7, 14], q.you_days, 'Unanswered')]));
    out = out.concat(q.waiting_on_you.length ? q.waiting_on_you.map(quietRow) : [h('p', { class: 'hm-empty' }, 'Nobody is waiting on you.')]);
    out.push(h('div', { class: 'hm-quiet-head' }, [h('div', { class: 'hm-day' }, 'Waiting on them · ' + q.waiting_on_them.length),
      dayPick('quietDays', [7, 14, 30, 60, 90], q.days, 'No answer')]));
    // Conversations that went quiet one by one; outreach never answered folded per funnel (or per brand when not in a funnel)
    var quietOnes = q.waiting_on_them.filter(function (c) { return c.ever_replied; });
    var cold = {}, coldOrder = [];
    q.waiting_on_them.forEach(function (c) {
      if (c.ever_replied) return;
      var key = c.funnel_name ? c.funnel_name + ' · ' + (c.stage_name || 'no stage') : ((state.brands[c.brand] || {}).name || 'No brand') + ' · no funnel';
      if (!cold[key]) { cold[key] = []; coldOrder.push(key); }
      cold[key].push(c);
    });
    coldOrder.sort(function (x, y) { return cold[y].length - cold[x].length; });
    if (!q.waiting_on_them.length) out.push(h('p', { class: 'hm-empty' }, 'Nobody owes you an answer.'));
    else {
      out.push(h('div', { class: 'hm-day' }, 'Went quiet · ' + quietOnes.length));
      out = out.concat(quietOnes.length ? quietOnes.map(quietRow) : [h('p', { class: 'hm-empty' }, 'No conversation went quiet.')]);
      if (coldOrder.length) out.push(h('div', { class: 'hm-day' }, 'Never replied · ' + (q.waiting_on_them.length - quietOnes.length)));
      coldOrder.forEach(function (key) { out.push(coldGroup(key, cold[key])); });
    }
    fill($('homeQuiet'), out);
  }
  function coldGroup(label, list) {
    var key = 'cold:' + label, open = !!state.open[key];
    var ages = list.map(function (c) { return c.days_quiet; }), lo = Math.min.apply(null, ages), hi = Math.max.apply(null, ages);
    var brands = {}; list.forEach(function (c) { if (c.brand) brands[c.brand] = 1; });
    var row = h('div', { class: 'hm-row hm-group' }, [
      h('div', { class: 'hm-main' }, [h('button', { class: 'hm-title hm-link', type: 'button', onclick: function () { state.open[key] = !open; renderQuiet(); } },
        (open ? '▾ ' : '▸ ') + label + ' · ' + list.length),
        h('div', { class: 'hm-meta' }, [h('span', {}, (lo === hi ? lo : lo + '–' + hi) + ' days, no reply')].concat(Object.keys(brands).map(brandDot)).filter(Boolean))]),
      h('button', { class: 'hm-mini', type: 'button', title: 'All ' + list.length + ' off this list for 14 days', onclick: function () {
        if (!confirm('Hide all ' + list.length + ' (' + label + ') for 14 days?')) return;
        api('POST', '/api/crm/quiet/snooze', { person_ids: list.map(function (c) { return c.person_id; }), days: 14 }).then(load).catch(fail);
      } }, 'Not now')]);
    return open ? h('div', {}, [row, h('div', { class: 'hm-sub' }, list.map(quietRow))]) : row;
  }

  /* ---------- the automatic bar: one chip per type with its count; a chip opens its list below the bar ---------- */
  state.counts = {};
  var KEEP_SHOWN = { Linkedin: 1, Waiting: 1 };          // chips that stay even at 0 (the LinkedIn one has its own form)
  function domCount(key) {
    if (key === 'Calls') return $('callsAttentionList').querySelectorAll(':scope > .attention').length;
    if (key === 'Suggest') return $('suggestionsAttentionList').querySelectorAll(':scope > .attention').length;
    if (key === 'Approve') return $('attentionList').querySelectorAll('.followup-live-card:not(.attention-quiet)').length;
    if (key === 'Linkedin') { var c = $('followupActionCount'); return c ? +c.textContent || 0 : 0; }
    if (key === 'Waiting') return $('upcomingList').querySelectorAll('.attention:not(#upcomingEmpty)').length;
    return state.counts[key] || 0;
  }
  var OPEN_KEY = 'lkHomeChip';
  function openKey() { try { return sessionStorage.getItem(OPEN_KEY) || ''; } catch (e) { return ''; } }      // nothing open when Home opens
  try { localStorage.removeItem(OPEN_KEY); sessionStorage.removeItem(OPEN_KEY); } catch (e) { /* storage blocked */ }      // closed on every visit
  var rows = [].slice.call(document.querySelectorAll('#hxAll > .hx')), chips = {};
  function showOpen() {
    var key = openKey();
    rows.forEach(function (row) {
      var k = row.getAttribute('data-key'), on = k === key && !chips[k].hidden;
      if (row.hidden === on) row.hidden = !on;
      var body = row.querySelector('.hx-body'); if (body.hidden === on) body.hidden = !on;
      chips[k].classList.toggle('on', on);
    });
    var any = rows.some(function (row) { return !row.hidden; });
    if ($('homeChipHide').hidden === any) $('homeChipHide').hidden = !any;
  }
  rows.forEach(function (row) {
    var k = row.getAttribute('data-key'), calm = row.querySelector('.hx-toggle').classList.contains('is-calm');
    var chip = h('button', { class: 'home-chip' + (calm ? ' is-calm' : ''), type: 'button', onclick: function () {
      var next = openKey() === k ? '' : k;
      try { sessionStorage.setItem(OPEN_KEY, next); } catch (e) { /* storage blocked */ }
      showOpen();
    } }, [row.querySelector('.hx-label').textContent, h('b', {}, '0')]);
    chips[k] = chip; $('homeChips').appendChild(chip);
    var list = row.querySelector('.hx-body > .home-list, .hx-body > .attention-list, .hx-body > .upcoming-list');
    if (list) new MutationObserver(function () { countRows(); }).observe(list, { childList: true, subtree: true });
  });
  function countRows() {
    var total = 0;
    rows.forEach(function (row) {
      var k = row.getAttribute('data-key'), n = domCount(k), chip = chips[k], b = chip.querySelector('b');
      if (b.textContent !== String(n)) b.textContent = n;              // only real changes (the counts sit inside what is watched)
      var hide = !n && !KEEP_SHOWN[k] && !(k === 'Followups' && state.followComing); if (chip.hidden !== hide) chip.hidden = hide;   // planned follow-ups keep the chip, at 0 due
      if (!KEEP_SHOWN[k]) total += n;
    });
    window.lkHomeActionCount = total + (state.ownDue || 0);
    showOpen();
  }
  $('homeChipHide').addEventListener('click', function () {       // close the open list, keep just the bar
    try { sessionStorage.setItem(OPEN_KEY, ''); } catch (e) { /* storage blocked */ }
    showOpen();
  });
  var licount = $('followupActionCount');
  if (licount) new MutationObserver(function () { countRows(); }).observe(licount, { childList: true, characterData: true, subtree: true });
  countRows();

  /* ---------- goals: today and this week (per person; "visible to team" is a choice per goal) ---------- */
  function goalRow(gl, mine) {
    var text = h('span', { class: 'hm-title' + (gl.done ? ' done' : '') }, gl.text);
    var tools = mine ? h('span', { class: 'hm-tools' }, [
      h('button', { class: 'hm-icon' + (gl.shared ? ' on' : ''), type: 'button', title: gl.shared ? 'Visible to the team (click to make private)' : 'Private (click to show to the team)',
        onclick: function () { api('PUT', '/api/calendar/goals/' + gl.goal_id, { shared: !gl.shared }).then(load).catch(fail); } }, gl.shared ? '◉' : '○'),
      h('button', { class: 'hm-icon', type: 'button', title: 'Delete', onclick: function () { api('POST', '/api/calendar/goals/' + gl.goal_id + '/delete').then(load).catch(fail); } }, '×')]) : null;
    return h('div', { class: 'hm-row hm-goal' }, [mine ? check(gl.done, function () { api('PUT', '/api/calendar/goals/' + gl.goal_id, { done: !gl.done }).then(load).catch(fail); })
                                                       : h('span', { class: 'hm-check ro' + (gl.done ? ' on' : '') }, gl.done ? '✓' : ''),
      h('div', { class: 'hm-main' }, [text]), tools]);
  }
  function goalInput(period, placeholder) {
    var input = h('input', { type: 'text', class: 'hm-input', placeholder: placeholder, 'data-period': period });
    input.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' || !input.value.trim()) return;
      api('POST', '/api/calendar/goals', { text: input.value, period: period }).then(function () { return load(); })
        .then(function () { var again = document.querySelector('.hm-input[data-period="' + period + '"]'); if (again) again.focus(); }).catch(fail);
    });
    return input;
  }
  function progress(list) {
    var done = list.filter(function (g) { return g.done; }).length;
    return list.length ? h('div', { class: 'hm-progress' }, [h('div', { class: 'hm-bar' }, [h('i', { style: 'width:' + Math.round(100 * done / list.length) + '%' })]),
      h('span', {}, done + ' of ' + list.length + ' done')]) : null;
  }
  function teamBox(key) {           // teammates' shared goals for this period, under your own
    return (state.goals.team || []).filter(function (t) { return t[key].length; }).map(function (t) {
      return h('div', { class: 'hm-team' }, [h('span', { class: 'block-label' }, t.who + ' · shared'),
        h('div', { class: 'home-list' }, t[key].map(function (x) { return goalRow(x, false); }))]);
    });
  }
  function renderGoals() {
    var g = state.goals;
    var earlier = (g.earlier_open || []).map(function (gl) {
      return h('div', { class: 'hm-row hm-earlier' }, [h('div', { class: 'hm-main' }, [h('span', { class: 'hm-title' }, gl.text),
        h('div', { class: 'hm-meta' }, 'Not done ' + dayLabel(gl.period_start, g.today).toLowerCase())]),
        h('button', { class: 'hm-mini', type: 'button', onclick: function () { api('POST', '/api/calendar/goals/' + gl.goal_id + '/to-today').then(load).catch(fail); } }, 'Move to today')]);
    });
    fill($('homeGoalsToday'), [goalInput('day', g.today_goals.length ? 'Add another goal…' : 'What must get done today?'),
      progress(g.today_goals), h('div', { class: 'home-list' }, g.today_goals.map(function (x) { return goalRow(x, true); })),
      earlier.length ? h('div', { class: 'hm-earlier-box' }, [h('span', { class: 'block-label' }, 'Still open from earlier')].concat(earlier)) : null].concat(teamBox('today')));
    fill($('homeGoalsWeek'), [goalInput('week', g.week_goals.length ? 'Add another goal for the week…' : 'Main goals for this week'),
      progress(g.week_goals), h('div', { class: 'home-list' }, g.week_goals.map(function (x) { return goalRow(x, true); }))].concat(teamBox('week')));
  }

  /* ---------- the three tabs: Tasks (the default), Daily goals, Weekly goals ---------- */
  document.querySelectorAll('#homeTabs .home-tab').forEach(function (tab) {
    tab.addEventListener('click', function () {
      var pane = tab.getAttribute('data-pane');
      document.querySelectorAll('#homeTabs .home-tab').forEach(function (t) { t.classList.toggle('on', t === tab); t.setAttribute('aria-selected', t === tab ? 'true' : 'false'); });
      document.querySelectorAll('.home-pane > [data-pane]').forEach(function (p) { p.hidden = p.getAttribute('data-pane') !== pane; });
    });
  });

  /* ---------- meetings: today's still to come, then the next days ---------- */
  function renderMeetings(a, today) {
    var nowHm = a.now.slice(11, 16);
    var list = (a.today || []).filter(function (e) { return isMeeting(e) && (e.untimed || (e.start_time || '') >= nowHm); })      // today's still to come
      .concat((a.upcoming || []).filter(isMeeting));
    var rows = list.slice(0, 8).map(function (e) {
      var guests = [];
      try { guests = (typeof e.attendees === 'string' ? JSON.parse(e.attendees || '[]') : e.attendees || []).map(function (x) { return x.name || x.email; }); } catch (err) { guests = []; }
      var key = 'prep:' + e.id, open = !!state.open[key];
      var row = h('div', { class: 'hm-row hm-meeting', onclick: function () { var tab = $('calendarTab'); if (tab) tab.click(); } }, [
        h('div', { class: 'hm-when' }, [h('b', {}, dayLabel(e.date, today)), h('span', {}, e.untimed ? '' : time12(e.start_time))]),
        h('div', { class: 'hm-main' }, [h('span', { class: 'hm-title' }, e.title), guests.length ? h('div', { class: 'hm-meta' }, 'With ' + guests.slice(0, 3).join(', ') + (guests.length > 3 ? ' +' + (guests.length - 3) : '')) : null]),
        e.prep ? h('button', { class: 'hm-mini', type: 'button', title: 'Claude\'s prep for this meeting (in your private notes)', onclick: function (ev) { ev.stopPropagation(); state.open[key] = !open; render(); } }, open ? 'Hide prep' : 'Prep') : null,
        e.join_url ? h('a', { class: 'hm-mini', href: e.join_url, target: '_blank', rel: 'noopener', onclick: function (ev) { ev.stopPropagation(); } }, 'Join') : null]);
      return open ? h('div', {}, [row, h('div', { class: 'hm-brief' }, e.prep)]) : row;
    });
    fill($('homeMeetings'), rows);
    $('homeMeetingsSection').hidden = !rows.length;             // only when there is a meeting coming up
  }

  /* ---------- upcoming tasks (the next 7 days), and adding one ---------- */
  function renderTasks(list, today) {
    var add = $('homeTaskAdd');
    if (!add.firstChild) {
      var title = h('input', { type: 'text', class: 'hm-input', placeholder: 'Add a task…', id: 'homeTaskTitle' });
      var day = h('input', { type: 'date', class: 'hm-input hm-date', value: today, id: 'homeTaskDate' });
      var go = h('button', { class: 'btn btn-primary btn-small', type: 'button' }, 'Add task');
      var mine = h('input', { type: 'checkbox', id: 'homeTaskPersonal' });
      var personal = h('label', { class: 'hm-personal', title: 'A personal task: only you see it' }, [mine, ' Personal']);
      function addTask() {
        if (!title.value.trim()) { title.focus(); return; }
        var body = { title: title.value.trim(), kind: 'task', start_date: day.value || today, start_time: '08:00', untimed: true };
        if (mine.checked) body.project = 'personal';
        api('POST', '/api/calendar/events', body)
          .then(function () { title.value = ''; return load(); }).then(function () { title.focus(); }).catch(fail);
      }
      go.addEventListener('click', addTask);
      title.addEventListener('keydown', function (e) { if (e.key === 'Enter') addTask(); });
      add.append(title, day, personal, go);
    }
    var byDay = {};
    fold(list.filter(shown)).forEach(function (it) { var d = it.group ? it.members[0].date : it.date; (byDay[d] = byDay[d] || []).push(it); });
    var days = Object.keys(byDay).sort();
    var out = [];
    days.forEach(function (d) {
      out.push(h('div', { class: 'hm-day' }, dayLabel(d, today)));
      byDay[d].forEach(function (it) { out.push(it.group ? groupRow(it, today) : itemRow(it, today, false)); });
    });
    fill($('homeTasks'), out.length ? out : [h('p', { class: 'hm-empty' }, 'Nothing planned for the next 7 days.')]);
  }

  load();
  setInterval(load, 120000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) load(); });
  var homeTab = $('homeTab'); if (homeTab) homeTab.addEventListener('click', function () { load(); });
})();
