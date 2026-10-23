/* Home, the daily workspace: the automatic items (follow-ups, calls to review, reminders) as a bar of counts, then your tasks
   (overdue, today, the next days) next to today's and this week's goals and the meetings coming up. Tasks live here, not in the calendar (the calendar shows meetings). Goals are per person. */
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

  var state = { agenda: null, goals: null, brands: {}, open: {} };
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
    var b = state.brands[key];
    return b ? h('span', { class: 'hm-brand' }, [h('i', { style: 'background:' + b.color }), b.name]) : null;
  }

  function load() {
    return Promise.all([api('GET', '/api/calendar/agenda'), api('GET', '/api/calendar/goals'),
                        api('GET', '/api/crm/brands').catch(function () { return { brands: [] }; })])
      .then(function (r) {
        state.agenda = r[0]; state.goals = r[1];
        state.brands = {}; (r[2].brands || []).forEach(function (b) { state.brands[b.key] = b; });
        render();
      }).catch(function () {});
  }
  window.lkHome = { reload: load };

  function render() {
    var a = state.agenda, today = a.now.slice(0, 10);
    $('homeDateLine').textContent = toUTC(today).toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric', timeZone: 'UTC' });
    var overdue = (a.overdue || []).filter(function (e) { return !isMeeting(e); });
    var todays = (a.today || []).filter(function (e) { return !isMeeting(e) && !e.done; });
    renderActions(overdue, todays, today);
    countRows();
    renderGoals();
    renderMeetings(a, today);
    renderTasks((a.upcoming || []).filter(function (e) { return !isMeeting(e) && !e.lead; }), today);
  }

  /* ---------- your tasks due (overdue + today); lead follow-ups go to the automatic bar, cold ones folded per channel ---------- */
  function fold(list) {
    var groups = {}, out = [];
    list.forEach(function (e) {
      if (e.lead && e.lead.bucket === 'cold') {
        var key = (e.lead.channel || '') + ':' + (/^send dm/i.test(e.title) ? 'dm' : 'follow');
        var g = groups[key];
        if (!g) { g = groups[key] = { group: true, channel: e.lead.channel, dm: /^send dm/i.test(e.title), members: [], project: e.project }; out.push(g); }
        g.members.push(e);
      } else out.push(e);
    });
    return out;
  }
  function check(done, onToggle, title) {
    return h('button', { class: 'hm-check' + (done ? ' on' : ''), type: 'button', title: title || (done ? 'Mark not done' : 'Mark done'), 'aria-label': title || 'Done',
      onclick: function (ev) { ev.stopPropagation(); onToggle(); } }, done ? '✓' : '');
  }
  function markDone(e) {
    return api('POST', '/api/calendar/events/' + e.id + '/done', { date: e.date, done: true }).then(load).catch(fail);
  }
  function itemRow(e, today, overdue) {
    var who = e.lead && e.lead.person_id;
    var title = h(who ? 'button' : 'span', who ? { class: 'hm-title hm-link', type: 'button', onclick: function () { if (window.openCrmPersonDetail) window.openCrmPersonDetail(e.lead.person_id); } }
                                               : { class: 'hm-title' }, e.title);
    var meta = [overdue ? h('span', { class: 'hm-late' }, 'Overdue · ' + dayLabel(e.date, today)) : (e.untimed ? null : h('span', {}, time12(e.start_time))),
                brandDot(e.project)].filter(Boolean);
    return h('div', { class: 'hm-row' }, [check(false, function () { markDone(e); }), h('div', { class: 'hm-main' }, [title, meta.length ? h('div', { class: 'hm-meta' }, meta) : null])]);
  }
  function groupRow(g, today) {
    var key = 'g:' + g.channel + (g.dm ? ':dm' : '');
    var label = (g.dm ? 'Send DM' : 'Follow up') + ' · ' + g.members.length + ' ' + (g.channel || '') + (g.members.length === 1 ? ' lead' : ' leads');
    var late = g.members.filter(function (m) { return m.date < today; }).length;
    var open = !!state.open[key];
    var all = h('button', { class: 'hm-mini', type: 'button', onclick: function (ev) {
      ev.stopPropagation();
      api('POST', '/api/calendar/events/done-bulk', { items: g.members.map(function (m) { return { id: m.id, date: m.date }; }), done: true }).then(load).catch(fail);
    } }, 'All done');
    var row = h('div', { class: 'hm-row hm-group' }, [check(false, function () { all.click(); }, 'Mark all done'),
      h('div', { class: 'hm-main' }, [h('button', { class: 'hm-title hm-link', type: 'button', onclick: function () { state.open[key] = !open; render(); } }, (open ? '▾ ' : '▸ ') + label),
        h('div', { class: 'hm-meta' }, [late ? h('span', { class: 'hm-late' }, late + ' overdue') : h('span', {}, g.members[0].date === today ? 'Due today' : dayLabel(g.members[0].date, today)), brandDot(g.project)].filter(Boolean))]),
      all]);
    var wrap = h('div', {}, [row]);
    if (open) wrap.appendChild(h('div', { class: 'hm-sub' }, g.members.map(function (m) { return itemRow(m, today, m.date < today); })));
    return wrap;
  }
  function renderActions(overdue, todays, today) {
    var due = overdue.concat(todays);
    var own = due.filter(function (e) { return !e.lead; }), leads = due.filter(function (e) { return !!e.lead; });
    state.counts.Followups = leads.length; state.ownDue = own.length;
    var late = own.filter(function (e) { return e.date < today; }), now = own.filter(function (e) { return e.date >= today; });
    fill($('homeActionsTools'), [late.length ? h('button', { class: 'hm-mini', type: 'button', onclick: function () {
      api('POST', '/api/calendar/overdue/move', { to: 'today' }).then(load).catch(fail);
    } }, 'Move overdue to today') : null]);
    fill($('homeActions'), [late.length ? h('div', { class: 'hm-day hm-late' }, 'Overdue') : null].concat(late.map(function (e) { return itemRow(e, today, true); }),
      [h('div', { class: 'hm-day' }, 'Today')], now.length ? now.map(function (e) { return itemRow(e, today, false); }) : [h('p', { class: 'hm-empty' }, 'Nothing due today.')]));
    fill($('homeFollowups'), fold(leads).map(function (it) { return it.group ? groupRow(it, today) : itemRow(it, today, it.date < today); }));
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
  function openKey() { try { return localStorage.getItem(OPEN_KEY) || ''; } catch (e) { return ''; } }      // nothing open by default
  var rows = [].slice.call(document.querySelectorAll('#hxAll > .hx')), chips = {};
  function showOpen() {
    var key = openKey();
    rows.forEach(function (row) {
      var k = row.getAttribute('data-key'), on = k === key && !chips[k].hidden;
      if (row.hidden === on) row.hidden = !on;
      var body = row.querySelector('.hx-body'); if (body.hidden === on) body.hidden = !on;
      chips[k].classList.toggle('on', on);
    });
  }
  rows.forEach(function (row) {
    var k = row.getAttribute('data-key'), calm = row.querySelector('.hx-toggle').classList.contains('is-calm');
    var chip = h('button', { class: 'home-chip' + (calm ? ' is-calm' : ''), type: 'button', onclick: function () {
      var next = openKey() === k ? '' : k;
      try { localStorage.setItem(OPEN_KEY, next); } catch (e) { /* storage blocked */ }
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
      var hide = !n && !KEEP_SHOWN[k]; if (chip.hidden !== hide) chip.hidden = hide;
      if (!KEEP_SHOWN[k]) total += n;
    });
    window.lkHomeActionCount = total + (state.ownDue || 0);
    showOpen();
  }
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
  function renderGoals() {
    var g = state.goals;
    var earlier = (g.earlier_open || []).map(function (gl) {
      return h('div', { class: 'hm-row hm-earlier' }, [h('div', { class: 'hm-main' }, [h('span', { class: 'hm-title' }, gl.text),
        h('div', { class: 'hm-meta' }, 'Not done ' + dayLabel(gl.period_start, g.today).toLowerCase())]),
        h('button', { class: 'hm-mini', type: 'button', onclick: function () { api('POST', '/api/calendar/goals/' + gl.goal_id + '/to-today').then(load).catch(fail); } }, 'Move to today')]);
    });
    fill($('homeGoalsToday'), [h('div', { class: 'home-section-head' }, [h('span', { class: 'block-label' }, "Today's goals")]),
      progress(g.today_goals), h('div', { class: 'home-list' }, g.today_goals.map(function (x) { return goalRow(x, true); })),
      goalInput('day', g.today_goals.length ? 'Add another goal…' : 'What must get done today?'),
      earlier.length ? h('div', { class: 'hm-earlier-box' }, [h('span', { class: 'block-label' }, 'Still open from earlier')].concat(earlier)) : null]);
    fill($('homeGoalsWeek'), [h('div', { class: 'home-section-head' }, [h('span', { class: 'block-label' }, 'This week')]),
      progress(g.week_goals), h('div', { class: 'home-list' }, g.week_goals.map(function (x) { return goalRow(x, true); })),
      goalInput('week', g.week_goals.length ? 'Add another goal for the week…' : 'Main goals for this week')]);
    fill($('homeTeamGoals'), (g.team || []).map(function (t) {
      return h('div', { class: 'home-card hm-team' }, [h('span', { class: 'block-label' }, t.who + ' · shared goals'),
        h('div', { class: 'home-list' }, t.today.concat(t.week).map(function (x) { return goalRow(x, false); }))]);
    }));
  }

  /* ---------- meetings: today's still to come, then the next days ---------- */
  function renderMeetings(a, today) {
    var nowHm = a.now.slice(11, 16);
    var list = (a.today || []).filter(function (e) { return isMeeting(e) && (e.untimed || (e.start_time || '') >= nowHm); })      // today's still to come
      .concat((a.upcoming || []).filter(isMeeting));
    var rows = list.slice(0, 8).map(function (e) {
      var guests = [];
      try { guests = (typeof e.attendees === 'string' ? JSON.parse(e.attendees || '[]') : e.attendees || []).map(function (x) { return x.name || x.email; }); } catch (err) { guests = []; }
      return h('div', { class: 'hm-row hm-meeting', onclick: function () { var tab = $('calendarTab'); if (tab) tab.click(); } }, [
        h('div', { class: 'hm-when' }, [h('b', {}, dayLabel(e.date, today)), h('span', {}, e.untimed ? '' : time12(e.start_time))]),
        h('div', { class: 'hm-main' }, [h('span', { class: 'hm-title' }, e.title), guests.length ? h('div', { class: 'hm-meta' }, 'With ' + guests.slice(0, 3).join(', ') + (guests.length > 3 ? ' +' + (guests.length - 3) : '')) : null]),
        e.join_url ? h('a', { class: 'hm-mini', href: e.join_url, target: '_blank', rel: 'noopener', onclick: function (ev) { ev.stopPropagation(); } }, 'Join') : null]);
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
      function addTask() {
        if (!title.value.trim()) { title.focus(); return; }
        api('POST', '/api/calendar/events', { title: title.value.trim(), kind: 'task', start_date: day.value || today, start_time: '08:00', untimed: true })
          .then(function () { title.value = ''; return load(); }).then(function () { title.focus(); }).catch(fail);
      }
      go.addEventListener('click', addTask);
      title.addEventListener('keydown', function (e) { if (e.key === 'Enter') addTask(); });
      add.append(title, day, go);
    }
    var byDay = {};
    fold(list).forEach(function (it) { var d = it.group ? it.members[0].date : it.date; (byDay[d] = byDay[d] || []).push(it); });
    var days = Object.keys(byDay).sort();
    var out = [];
    days.forEach(function (d) {
      out.push(h('div', { class: 'hm-day' }, dayLabel(d, today)));
      byDay[d].forEach(function (it) { out.push(it.group ? groupRow(it, today) : itemRow(it, today, false)); });
    });
    fill($('homeTasks'), out.length ? out : [h('p', { class: 'hm-empty' }, 'Nothing planned for the next 7 days.')]);
  }

  $('homeNewTask').addEventListener('click', function () { var t = $('homeTaskTitle'); if (t) { t.scrollIntoView({ block: 'center' }); t.focus(); } });
  $('homeNewMeeting').addEventListener('click', function () {
    var tab = $('calendarTab'); if (tab) tab.click();
    if (window.lkCalendarNew) window.lkCalendarNew('meeting');
  });

  load();
  setInterval(load, 120000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) load(); });
  var homeTab = $('homeTab'); if (homeTab) homeTab.addEventListener('click', function () { load(); });
})();
