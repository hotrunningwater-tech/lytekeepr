/* Calendar tab (Stage 2). Plain script; talks to /api/calendar. */
(function () {
  'use strict';

  var PROJECTS = {
    lytekeepr: { label: 'LyteKeepr', color: '#7fa8b8' },
    aspan: { label: 'Aspan CRM', color: '#e98b74' },
    nexy: { label: 'Nexy', color: '#21493f' },
    google: { label: 'Google', color: '#7b6fa8' }
  };
  var KINDS = { task: 'Task', meeting: 'Meeting', reminder: 'Reminder' };
  var REPEAT_LABELS = { none: 'Does not repeat', daily: 'Every day', weekdays: 'Every weekday', weekly: 'Weekly on selected days' };
  var DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  var DAY_LETTERS = ['M', 'T', 'W', 'T', 'F', 'S', 'S'];
  var PX_PER_HOUR = 56;

  var tab = document.getElementById('calendarTab');
  var panel = document.getElementById('calendarPanel');
  if (!tab || !panel) return;

  var state = {
    weekStart: null,
    events: [],
    agenda: { overdue: [], today: [], upcoming: [], badge: 0 },
    now: null,
    hidden: {},
    groupIndex: {},
    density: 'fit',
    selected: null,      // {id, date}
    mode: 'agenda',      // agenda | detail | form
    pane: 'info',        // info | chat
    form: null
  };

  /* ---------- date helpers (UTC math on YYYY-MM-DD strings, no DST drift) ---------- */
  function toUTC(iso) { var p = iso.split('-'); return new Date(Date.UTC(+p[0], +p[1] - 1, +p[2])); }
  function fmt(d) { return d.toISOString().slice(0, 10); }
  function addDays(iso, n) { var d = toUTC(iso); d.setUTCDate(d.getUTCDate() + n); return fmt(d); }
  function mondayOf(iso) { var d = toUTC(iso); return addDays(iso, -((d.getUTCDay() + 6) % 7)); }
  function weekday(iso) { return (toUTC(iso).getUTCDay() + 6) % 7; }
  function minutes(hhmm) { var p = hhmm.split(':'); return +p[0] * 60 + +p[1]; }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function hhmm(min) { return pad(Math.floor(min / 60) % 24) + ':' + pad(min % 60); }
  function label12(hhmmStr) {
    var m = minutes(hhmmStr), h = Math.floor(m / 60), ap = h >= 12 ? 'PM' : 'AM';
    return ((h + 11) % 12 + 1) + (m % 60 ? ':' + pad(m % 60) : '') + ' ' + ap;
  }
  function longDate(iso) {
    return DAY_NAMES[weekday(iso)] + ' ' + toUTC(iso).toLocaleString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });
  }
  function firstOccurrence(ev) {
    var d = ev.start_date;
    for (var i = 0; i < 7; i++, d = addDays(d, 1)) {
      var wd = weekday(d);
      if (ev.repeat === 'weekly' ? ev.repeat_days.split(',').indexOf(String(wd)) >= 0 : ev.repeat === 'weekdays' ? wd < 5 : true) return d;
    }
    return ev.start_date;
  }
  function today() { return state.now ? state.now.slice(0, 10) : fmt(new Date()); }

  /* ---------- tiny DOM helper (no innerHTML with user text) ---------- */
  function h(tag, attrs, kids) {
    var el = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === 'class') el.className = attrs[k];
      else if (k === 'style') el.style.cssText = attrs[k];
      else if (k.slice(0, 2) === 'on') el.addEventListener(k.slice(2), attrs[k]);
      else if (attrs[k] === true) el.setAttribute(k, '');
      else if (attrs[k] !== false && attrs[k] != null) el.setAttribute(k, attrs[k]);
    });
    [].concat(kids == null ? [] : kids).forEach(function (c) {
      if (c == null || c === false) return;
      el.appendChild(typeof c === 'string' || typeof c === 'number' ? document.createTextNode(String(c)) : c);
    });
    return el;
  }
  function $(sel, root) { return (root || panel).querySelector(sel); }
  function api(method, url, body) {
    return fetch(url, {
      method: method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined
    }).then(function (res) {
      if (res.status === 401) { location.href = (window.LK_PREFIX || '') + '/login'; throw new Error('login required'); }
      return res.text().then(function (text) {
        var data = null;
        try { data = JSON.parse(text); } catch (e) { /* not JSON: a web server error page */ }
        if (!res.ok || data === null) {
          if (data && data.error) throw new Error(data.error);
          if (res.status === 502 || res.status === 504) throw new Error('The server took too long to answer. The change may still have gone through, so check before repeating it.');
          throw new Error('The server returned an unexpected answer (HTTP ' + res.status + '). Please try again.');
        }
        return data;
      });
    });
  }

  /* ---------- data ---------- */
  function load() {
    callCache = {};              // a recording may have arrived since: look again when the calendar reloads
    var start = state.weekStart, end = addDays(start, 6);
    return Promise.all([
      api('GET', '/api/calendar/events?start=' + start + '&end=' + end),
      api('GET', '/api/calendar/agenda'),
      api('GET', '/api/calendar/google/status').catch(function () { return null; })
    ]).then(function (r) {
      state.google = r[2];
      state.events = r[0].events;
      state.now = r[0].now;
      state.agenda = r[1];
      render();
      updateBadge();
    }).catch(function (e) { if (e.message !== 'login required') showError(e.message); });
  }
  function showError(msg) {
    var box = $('#lkcSideInfo');
    if (box) box.prepend(h('p', { class: 'lkc-err' }, msg));
  }
  function updateBadge() {
    var badge = document.getElementById('calendarTabBadge');
    if (!badge) return;
    var a = state.agenda;
    var n = buildItems(a.overdue).length + buildItems(a.today.filter(function (o) { return !o.done; })).length;
    badge.textContent = n;
    badge.hidden = !n;
  }

  /* ---------- week grid ---------- */
  var HEAD_PX = 66, STRIP_PX = 56;                         // day-name row, and the "No specific time" strip above the hours
  var ICONS = { follow: '\u21BB', dm: '\u2709', reply: '\u21A9' };

  function itemKind(e) { return /^send dm/i.test(e.title) ? 'dm' : /^reply to/i.test(e.title) ? 'reply' : 'follow'; }
  function pillLabel(e) {                                  // "Follow up: Dana" -> "↻ Dana"
    var m = /^(Follow up|Send DM): (.*)$/i.exec(e.title) || /^(Reply to) (.*)$/i.exec(e.title);
    return m ? ICONS[itemKind(e)] + ' ' + m[2] : e.title;
  }

  // Cold lead tasks (reached out, no reply yet) are folded into ONE group per channel, day and kind; warm replies,
  // call follow-ups and your own untimed tasks stay individual. The people underneath are untouched.
  function buildItems(list) {
    var groups = {}, out = [];
    list.forEach(function (e) {
      if (e.lead && e.lead.bucket === 'cold') {
        if (e.done) return;                                // finished cold tasks simply drop out of the group
        var kind = itemKind(e), key = 'g:' + e.date + ':' + e.lead.channel + ':' + kind, g = groups[key];
        if (!g) {
          g = groups[key] = { group: true, id: key, date: e.date, project: e.project, kind: 'reminder', untimed: 1, source: 'auto', repeat: 'none',
            start_time: '08:00', duration_min: 15, notes: '', done: false, members: [], gkind: kind, channel: e.lead.channel };
          out.push(g);
        }
        g.members.push(e);
      } else out.push(e);
    });
    out = out.map(function (x) { return x.group && x.members.length === 1 ? x.members[0] : x; });
    Object.keys(groups).forEach(function (k) {
      var g = groups[k], n = g.members.length;
      if (n === 1) return;
      g.title = (g.gkind === 'dm' ? 'Send DM' : 'Follow up') + ' \u00b7 ' + n + ' ' + channelLabel(g.channel) + ' lead' + (n === 1 ? '' : 's');
      state.groupIndex[k] = g;
    });
    return out;
  }

  // Timed items never overlap: when two collide, the later one drops below the earlier one.
  function stackLayout(items, lo) {
    items.sort(function (a, b) { return a._s - b._s || b._e - a._e; });
    var bottom = -100;
    items.forEach(function (e) {
      var natural = (e._s - lo * 60) * PX_PER_HOUR / 60;
      e._h = Math.max((e._e - e._s) * PX_PER_HOUR / 60 - 2, 20);
      e._top = Math.max(natural, bottom + 2);
      bottom = e._top + e._h;
    });
    return Math.max(bottom, 0);
  }

  function stripCell(dayIso) {
    var items = buildItems(state.events.filter(function (e) { return e.untimed && e.date === dayIso && !state.hidden[e.project]; }));
    var cell = h('div', { class: 'lkc-strip-cell' + (items.length > 2 ? ' has-more' : '') + (dayIso === today() ? ' today' : '') });
    var list = h('div', { class: 'lkc-strip-list' });
    items.forEach(function (e) {
      var sel = state.selected && state.selected.id === e.id && state.selected.date === e.date;
      var label = e.group ? ICONS[e.gkind] + ' ' + e.members.length + ' ' + channelLabel(e.channel) : pillLabel(e);
      list.appendChild(h('button', { type: 'button', class: 'lkc-pill' + (e.group ? ' group' : '') + (e.done ? ' done' : '') + (sel ? ' sel' : ''),
        title: e.title, style: '--c:' + PROJECTS[e.project].color, onclick: function (ev) { ev.stopPropagation(); select(e.id, e.date); } }, label));
    });
    cell.appendChild(list);
    if (items.length > 2) cell.appendChild(h('span', { class: 'lkc-strip-more', title: 'Show all' }, '+' + (items.length - 2)));
    cell.addEventListener('click', function (ev) {        // hover expands on a mouse; a tap does it on a touchscreen
      if (ev.target === cell || ev.target === list || ev.target.classList.contains('lkc-strip-more')) cell.classList.toggle('open');
    });
    return cell;
  }

  function renderGrid() {
    var wrap = $('#lkcGridWrap');
    var prevScroll = wrap ? wrap.scrollTop : null;
    state.groupIndex = {};
    var visible = state.events.filter(function (e) { return !state.hidden[e.project]; });
    var timed = visible.filter(function (e) { return !e.untimed; });
    var lo, hi;
    if (state.density === 'detailed') {                     // roomy, scrolling: fixed 7 AM - 9 PM, wider if events fall outside
      lo = 7; hi = 21;
      timed.forEach(function (e) {
        var s = minutes(e.start_time), en = Math.min(s + e.duration_min, 24 * 60);
        lo = Math.min(lo, Math.floor(s / 60)); hi = Math.max(hi, Math.ceil(en / 60));
      });
      PX_PER_HOUR = 56;
    } else {                                                // fit the day on screen: trim empty hours, then size the hours to the space
      var first = 24, last = 0;
      timed.forEach(function (e) {
        var s = minutes(e.start_time), en = Math.min(s + e.duration_min, 24 * 60);
        first = Math.min(first, Math.floor(s / 60)); last = Math.max(last, Math.ceil(en / 60));
      });
      if (last === 0) { lo = 8; hi = 18; } else { lo = Math.max(0, first - 1); hi = Math.min(24, last + 1); }
      for (var guard = 0; hi - lo < 8 && guard < 24; guard++) { if (guard % 2 === 0 && lo > 0) lo--; else if (hi < 24) hi++; else if (lo > 0) lo--; }
      var avail = wrap ? wrap.clientHeight - HEAD_PX - STRIP_PX : 0;
      PX_PER_HOUR = avail > 120 ? Math.max(34, Math.min(64, Math.floor(avail / (hi - lo)))) : 56;
    }
    var span = (hi - lo) * PX_PER_HOUR;

    // lay out each day first (stacked, no overlap) so every column can be as tall as the tallest
    var days = [], maxBottom = 0;
    for (var d0 = 0; d0 < 7; d0++) {
      var dayIso0 = addDays(state.weekStart, d0);
      var items = timed.filter(function (e) { return e.date === dayIso0; }).map(function (e) {
        e._s = minutes(e.start_time); e._e = e._s + Math.max(e.duration_min, 15); return e;
      });
      maxBottom = Math.max(maxBottom, stackLayout(items, lo));
      days.push({ iso: dayIso0, items: items });
    }
    var colH = Math.max(span, Math.ceil(maxBottom) + 4);

    var grid = h('div', { class: 'lkc-grid' });
    grid.appendChild(h('div', { class: 'lkc-dayhead' }));
    days.forEach(function (day, i) {
      grid.appendChild(h('div', { class: 'lkc-dayhead' + (day.iso === today() ? ' today' : '') }, [DAY_NAMES[i], h('b', {}, String(+day.iso.slice(8)))]));
    });
    grid.appendChild(h('div', { class: 'lkc-stripcorner', title: 'To-dos that belong to the day but have no specific time: follow-ups, outreach, replies' }, 'Any time'));
    days.forEach(function (day) { grid.appendChild(stripCell(day.iso)); });

    var hours = h('div', { class: 'lkc-hours', style: 'height:' + colH + 'px' });
    for (var hr = lo; hr < hi; hr++) {
      hours.appendChild(h('div', { class: 'lkc-hour', style: 'top:' + (hr - lo) * PX_PER_HOUR + 'px' + (hr === lo ? ';transform:none' : '') }, label12(hhmm(hr * 60))));
    }
    grid.appendChild(hours);

    days.forEach(function (day) {
      var dayIso = day.iso;
      var col = h('div', { class: 'lkc-col', style: 'height:' + colH + 'px', onclick: function (ev) {
        if (ev.target !== col && !ev.target.classList.contains('lkc-line')) return;
        var rect = col.getBoundingClientRect();
        var min = Math.round(((ev.clientY - rect.top) / PX_PER_HOUR * 60) / 30) * 30 + lo * 60;
        openForm({ start_date: dayIso, start_time: hhmm(Math.min(min, 23 * 60 + 30)) });
      } });
      for (var l = 0; l < hi - lo; l++) col.appendChild(h('div', { class: 'lkc-line', style: 'top:' + l * PX_PER_HOUR + 'px' }));
      day.items.forEach(function (e) {
        var sel = state.selected && state.selected.id === e.id && state.selected.date === e.date;
        col.appendChild(h('button', {
          type: 'button',
          title: e.title + ' \u00b7 ' + label12(e.start_time),
          class: 'lkc-ev ' + e.kind + (e.done ? ' done' : '') + (sel ? ' sel' : '') + (e._h < 40 ? ' short' : ''),
          style: 'top:' + e._top + 'px;height:' + e._h + 'px;left:2px;width:calc(100% - 4px);background:' + PROJECTS[e.project].color + ';--c:' + PROJECTS[e.project].color,
          onclick: function () { select(e.id, e.date); }
        }, [e.title, h('small', {}, label12(e.start_time) + (e.repeat !== 'none' ? ' \u00b7 repeats' : ''))]));
      });
      if (dayIso === today() && state.now) {
        var nm = minutes(state.now.slice(11, 16));
        if (nm >= lo * 60 && nm <= hi * 60) col.appendChild(h('div', { class: 'lkc-now', style: 'top:' + (nm - lo * 60) * PX_PER_HOUR / 60 + 'px' }));
      }
      grid.appendChild(col);
    });

    var target = $('#lkcGridWrap');
    target.replaceChildren(grid);
    if (prevScroll != null) target.scrollTop = prevScroll;
    else {
      var nowMin = state.now ? minutes(state.now.slice(11, 16)) : 8 * 60;
      target.scrollTop = Math.max(0, (Math.min(nowMin, 18 * 60) - lo * 60 - 60) * PX_PER_HOUR / 60);
    }
  }

  /* ---------- sidebar ---------- */
  function dot(project) { return h('i', { style: 'background:' + PROJECTS[project].color }); }

  function agendaRow(o, over) {
    var when = o.untimed ? 'no specific time' : label12(o.start_time);
    return h('div', { class: 'lkc-row' + (over ? ' over' : '') + (o.done ? ' done' : ''), onclick: function () { jumpTo(o.id, o.date); } }, [
      dot(o.project),
      h('div', {}, [h('b', {}, (o.group ? ICONS[o.gkind] + ' ' : '') + o.title),
        h('span', {}, (over ? 'Overdue \u00b7 ' : '') + longDate(o.date) + ' \u00b7 ' + when + ' \u00b7 ' + PROJECTS[o.project].label)])
    ]);
  }
  var SECT_KEY = 'lkcSections';
  try { state.sect = JSON.parse(localStorage.getItem(SECT_KEY) || '{}') || {}; } catch (e) { state.sect = {}; }
  function sectionOpen(key, count) {
    if (state.sect[key] != null) return state.sect[key];
    return count <= 3;                    // long lists start folded to one line
  }
  function toggleSection(key, count) {
    state.sect[key] = !sectionOpen(key, count);
    try { localStorage.setItem(SECT_KEY, JSON.stringify(state.sect)); } catch (e) { /* storage unavailable */ }
    render();
  }

  function agendaSection(key, title, rows, emptyText, action) {
    var open = sectionOpen(key, rows.length);
    var head = h('div', { class: 'lkc-sect-head' }, [
      h('button', { type: 'button', class: 'lkc-sect-toggle', 'aria-expanded': open ? 'true' : 'false',
        onclick: function () { toggleSection(key, rows.length); } }, [
        h('span', { class: 'lkc-caret' }, open ? '\u25BE' : '\u25B8'),
        h('span', { class: 'block-label', style: 'margin:0' }, title),
        h('span', { class: 'lkc-count' }, String(rows.length))
      ]),
      action || null
    ]);
    var body = !open ? [] : rows.length ? rows : [h('p', { class: 'lkc-empty' }, emptyText)];
    return h('div', { class: 'lkc-card' }, [head].concat(body));
  }

  function moveAllOverdue() {
    var n = state.agenda.overdue.length;
    if (!n || !confirm('Move all ' + n + ' overdue items to today?')) return;
    api('POST', '/api/calendar/overdue/move', { to: 'today' }).then(load).catch(function (e) { showError(e.message); });
  }

  function renderAgenda() {
    var a = state.agenda, out = [];
    var g = state.google;
    if (g && g.needs_reconnect) {
      out.push(h('div', { class: 'lkc-reconnect' }, [
        h('b', {}, 'Google needs to be reconnected'),
        h('span', {}, 'Your Google authorization expired or was revoked, so the hourly sync is paused. Your events, notes and statuses are safe.'),
        h('a', { class: 'btn primary', href: (window.LK_PREFIX || '') + '/auth/google/start' }, 'Reconnect Google')
      ]));
    }
    if (state.syncNote) out.push(h('p', { class: 'lkc-synced' }, state.syncNote));
    else if (g && g.last_sync) out.push(h('p', { class: 'lkc-synced' }, 'Google last synced ' + g.last_sync + ' \u00b7 automatic every hour'));
    var overdue = buildItems(a.overdue), todays = buildItems(a.today), upcoming = buildItems(a.upcoming);
    if (overdue.length) {
      out.push(agendaSection('overdue', 'Overdue', overdue.map(function (o) { return agendaRow(o, true); }), '',
        h('button', { type: 'button', class: 'btn lkc-mini', onclick: moveAllOverdue }, 'Move all to today')));
    }
    out.push(agendaSection('today', 'Today', todays.map(function (o) { return agendaRow(o, false); }), 'Nothing today.'));
    out.push(agendaSection('upcoming', 'Up next', upcoming.map(function (o) { return agendaRow(o, false); }), 'Nothing in the next 7 days.'));
    out.push(h('p', { class: 'lkc-empty' }, 'Click an event to see it. Click an empty slot to add one.'));
    return out;
  }

  function findSelected() {
    if (!state.selected) return null;
    var grp = state.groupIndex[state.selected.id];
    if (grp && grp.date === state.selected.date) return grp;
    return state.events.concat(state.agenda.overdue, state.agenda.today, state.agenda.upcoming).filter(function (e) {
      return e.id === state.selected.id && e.date === state.selected.date;
    })[0] || null;
  }

  var MEETING_STATUS_LABELS = { '': 'Scheduled', completed: 'Completed', no_show: 'No-show', cancelled: 'Cancelled' };

  function meetingBlock(e) {
    // My own notes + meeting status: stored separately from anything Google sends, so a sync never overwrites them.
    var notes = h('textarea', { id: 'lkcMyNotes', placeholder: 'Your notes (kept in LyteKeepr, never overwritten by Google)' }, e.my_notes || '');
    var save = h('button', { class: 'btn', type: 'button', onclick: function () {
      save.textContent = 'Saving…';
      api('PUT', '/api/calendar/events/' + e.id + '/meeting', { my_notes: document.getElementById('lkcMyNotes').value })
        .then(load).catch(function (err) { save.textContent = 'Save notes'; showError(err.message); });
    } }, 'Save notes');
    var statuses = h('div', { class: 'lkc-actions' }, Object.keys(MEETING_STATUS_LABELS).map(function (k) {
      return h('button', { type: 'button', class: 'btn' + ((e.meeting_status || '') === k ? ' primary' : ''), onclick: function () {
        api('PUT', '/api/calendar/events/' + e.id + '/meeting', { meeting_status: k || 'scheduled' }).then(load).catch(function (err) { showError(err.message); });
      } }, MEETING_STATUS_LABELS[k]);
    }));
    return h('div', { class: 'lkc-meeting' }, [
      h('span', { class: 'lkc-lbl' }, 'Meeting status'), statuses,
      e.has_call ? h('p', { class: 'lkc-empty' }, 'Conference call: marking it Completed adds a follow-up reminder 24 hours after it ends.') : null,
      h('span', { class: 'lkc-lbl', style: 'margin-top:6px' }, 'My notes'), notes, save
    ]);
  }

  // The recording of a meeting (Fathom), fetched once per meeting when its details open.
  var callCache = {};
  var callOpen = {};
  function recordingBlock(e) {
    if (!(e.has_call || e.kind === 'meeting' || e.expect_recording)) return null;
    if (!(e.id in callCache)) {
      callCache[e.id] = 'loading';
      api('GET', '/api/calls/event/' + e.id).then(function (r) { callCache[e.id] = r.call || null; render(); })
        .catch(function () { callCache[e.id] = null; render(); });
    }
    var c = callCache[e.id];
    if (c === 'loading') return null;
    if (!c) return h('div', { class: 'lkc-call' }, [h('span', { class: 'lkc-lbl' }, 'Recording (Fathom)'),
      h('p', { class: 'lkc-empty', style: 'margin:0' }, 'No recording from Fathom for this meeting yet. If it did not happen, mark it No-show below.')]);
    function post(url, body, btn, busy, after) {
      btn.disabled = true; btn.textContent = busy;
      api('POST', url, body || {}).then(after).catch(function (err) { showError(err.message); btn.disabled = false; });
    }
    var people = [h('span', { class: 'lkc-lbl' }, 'Who was on it')].concat((c.contacts || []).map(function (p) {
      return h('div', { class: 'lkc-member' }, [h('div', { class: 'lkc-member-main' }, [h('b', {}, p.name || 'Contact'), h('span', { class: 'lkc-leadmeta' }, 'Contact')]),
        h('div', { class: 'lkc-actions' }, [h('button', { class: 'btn', type: 'button', onclick: function () { if (window.lkOpenPerson) window.lkOpenPerson(p.person_id); } }, 'Open')])]);
    }));
    if ((c.invitees || []).length) people.push(h('p', { class: 'lkc-empty', style: 'margin:0' }, 'Invited: ' + c.invitees.map(function (i) { return i.name || i.email; }).join(', ')));
    var transcript = callOpen[c.call_id] ? h('div', { class: 'crm-call-transcript' }, (c.transcript || []).map(function (t) {
      return h('p', {}, [h('i', {}, t.at), h('b', {}, (t.speaker || 'Someone') + ': '), t.text]);
    })) : null;
    var buttons = [];
    if (c.has_transcript && !c.summary) buttons.push(h('button', { class: 'btn primary', type: 'button', onclick: function (ev) {
      post('/api/calls/' + c.call_id + '/summarize', {}, ev.target, 'Summarizing…', function () { delete callCache[e.id]; load(); });
    } }, 'Summarize'));
    if (c.has_transcript) buttons.push(h('button', { class: 'btn', type: 'button', onclick: function (ev) {
      post('/api/calls/' + c.call_id + '/draft-followup', {}, ev.target, 'Writing…', function (d) {
        ev.target.disabled = false; ev.target.textContent = 'Draft follow-up';
        if (window.lkHomeChat && window.lkHomeChat.showDraft) window.lkHomeChat.showDraft('Your follow-up email to ' + (d.person_name || 'them') + ' after "' + c.title + '" is ready below. Edit it, then press Send now.');
        var home = document.getElementById('homeTab'); if (home) home.click();
      });
    } }, 'Draft follow-up'));
    if (c.has_transcript) buttons.push(h('button', { class: 'btn', type: 'button', onclick: function () { callOpen[c.call_id] = !callOpen[c.call_id]; render(); } }, callOpen[c.call_id] ? 'Hide transcript' : 'Show transcript'));
    return h('div', { class: 'lkc-call' }, people.concat([
      h('span', { class: 'lkc-lbl', style: 'margin-top:6px' }, 'Recording (Fathom)'),
      h('p', { class: 'lkc-empty', style: 'margin:0' }, (c.summary ? 'Summarized' : 'Transcript ready') + (c.duration_min ? ' · ' + c.duration_min + ' min' : '') + '. It also shows on the contact\'s page.'),
      c.summary ? h('div', { class: 'crm-call-summary' }, c.summary) : null,
      transcript,
      buttons.length ? h('div', { class: 'lkc-actions' }, buttons) : null
    ]));
  }

  function renderDetail() {
    var e = findSelected();
    if (!e) { state.mode = 'agenda'; return renderAgenda(); }
    if (e.group) return renderGroupDetail(e);
    var google = e.source === 'google';
    var repeatText = e.repeat === 'weekly'
      ? 'Weekly: ' + e.repeat_days.split(',').map(function (d) { return DAY_NAMES[+d]; }).join(', ')
      : REPEAT_LABELS[e.repeat];
    var endMin = minutes(e.start_time) + e.duration_min;
    var typeText = google ? (e.has_call ? 'Conference call' : 'Meeting') + ' (Google Calendar, read-only)'
      : KINDS[e.kind] + (e.source === 'auto' ? ' (auto follow-up)' : '');
    var rows = [
      ['Type', typeText],
      ['When', e.untimed ? longDate(e.date) + ' \u00b7 no specific time' : longDate(e.date) + ' \u00b7 ' + label12(e.start_time) + ' \u2013 ' + label12(hhmm(endMin))]
    ];
    if (!google) { rows.push(['Repeats', repeatText]); rows.push(['Status', e.done ? 'Done' : 'Open']); }
    var grid = h('div', { class: 'lkc-grid2' });
    rows.forEach(function (r) { grid.appendChild(h('label', { class: 'lkc-lbl' }, r[0])); grid.appendChild(h('div', { class: 'lkc-v' }, r[1])); });
    var actions = [];
    if (!google) {
      actions.push(h('button', { class: 'btn primary', type: 'button', onclick: function () {
        api('POST', '/api/calendar/events/' + e.id + '/done', { date: e.date, done: !e.done }).then(load);
      } }, e.done ? 'Mark not done' : 'Mark done'));
      actions.push(h('button', { class: 'btn', type: 'button', onclick: function () { openForm(e); } }, 'Edit'));
      if (e.repeat === 'none') {
        actions.push(h('button', { class: 'btn', type: 'button', onclick: function () {
          api('PUT', '/api/calendar/events/' + e.id, { start_date: addDays(e.start_date, 1) }).then(function () {
            state.selected = { id: e.id, date: addDays(e.date, 1) }; load();
          });
        } }, 'Snooze 1 day'));
      }
      actions.push(h('button', { class: 'btn danger', type: 'button', onclick: function () {
        if (!confirm(e.repeat === 'none' ? 'Delete this item?' : 'Delete this item and all its repeats?')) return;
        api('DELETE', '/api/calendar/events/' + e.id).then(function () { state.selected = null; state.mode = 'agenda'; load(); });
      } }, 'Delete'));
    }
    return [h('div', { class: 'lkc-card lkc-detail' }, [
      h('div', { class: 'lkc-top' }, [
        h('span', { class: 'lkc-chip' }, [dot(e.project), PROJECTS[e.project].label]),
        h('button', { class: 'x', type: 'button', 'aria-label': 'Close', onclick: closeSide }, '\u00d7')
      ]),
      h('h2', { class: 'lkc-title' }, e.title), grid,
      e.lead ? leadCard(e.lead) : null,
      e.notes ? h('div', { class: 'lkc-notes' }, [google ? h('span', { class: 'lkc-lbl' }, 'From Google') : null, e.notes]) : null,
      recordingBlock(e),
      google || e.kind === 'meeting' ? meetingBlock(e) : null,
      actions.length ? h('div', { class: 'lkc-actions' }, actions) : null
    ])];
  }

  function safeUrl(u) { return /^https?:\/\//i.test(u || '') ? u : ''; }

  function memberRow(m) {
    var lead = m.lead, next = (NEXT_STEPS[lead.channel] || {})[lead.status] || [], url = safeUrl(lead.url);
    var buttons = next.map(function (step) {
      return h('button', { class: 'btn', type: 'button', onclick: function () { api('PUT', '/api/calendar/leads/' + lead.id, { status: step[0] }).then(load); } }, step[1]);
    });
    buttons.push(h('button', { class: 'btn', type: 'button', onclick: function () {
      api('POST', '/api/calendar/events/' + m.id + '/done', { date: m.date, done: true }).then(load);
    } }, 'Done'));
    return h('div', { class: 'lkc-member' }, [
      h('div', { class: 'lkc-member-main' }, [h('b', {}, lead.contact), h('span', { class: 'lkc-leadmeta' }, statusLabel(lead)),
        url ? h('a', { href: url, target: '_blank', rel: 'noopener noreferrer', class: 'lkc-leadlink' }, 'Open profile') : null]),
      lead.notes ? h('span', { class: 'lkc-leadmeta' }, lead.notes) : null,
      h('div', { class: 'lkc-actions' }, buttons)
    ]);
  }

  function renderGroupDetail(g) {
    var members = g.members.slice().sort(function (a, b) { return a.lead.contact.localeCompare(b.lead.contact); });
    var items = members.map(function (m) { return { id: m.id, date: m.date }; });
    var leadIds = members.map(function (m) { return m.lead.id; });
    var kindText = g.gkind === 'dm' ? 'Send the DM to everyone whose connection was accepted' : 'Follow up with everyone who has not replied yet';
    var grid = h('div', { class: 'lkc-grid2' });
    [['Type', 'Cold leads \u00b7 ' + kindText], ['When', longDate(g.date) + ' \u00b7 no specific time'], ['People', String(members.length)]].forEach(function (r) {
      grid.appendChild(h('label', { class: 'lkc-lbl' }, r[0])); grid.appendChild(h('div', { class: 'lkc-v' }, r[1]));
    });
    return [h('div', { class: 'lkc-card lkc-detail' }, [
      h('div', { class: 'lkc-top' }, [
        h('span', { class: 'lkc-chip' }, [dot(g.project), PROJECTS[g.project].label]),
        h('button', { class: 'x', type: 'button', 'aria-label': 'Close', onclick: closeSide }, '\u00d7')
      ]),
      h('h2', { class: 'lkc-title' }, g.title), grid,
      h('div', { class: 'lkc-actions' }, [h('button', { class: 'btn primary', type: 'button', onclick: function () {
        if (!confirm('Mark all ' + members.length + ' as done?')) return;
        api('POST', '/api/calendar/events/done-bulk', { items: items, done: true }).then(function () { state.selected = null; state.mode = 'agenda'; load(); });
      } }, 'Mark all done')]),
      h('div', { class: 'lkc-moveall' }, [h('span', { class: 'lkc-lbl' }, 'Move all to'), h('input', { type: 'date', id: 'lkcMoveTo', value: addDays(g.date, 1) }),
        h('button', { class: 'btn', type: 'button', onclick: function () {
          var to = document.getElementById('lkcMoveTo').value; if (!to) return;
          api('POST', '/api/calendar/followups/move', { lead_ids: leadIds, to_date: to }).then(function () { state.selected = null; state.mode = 'agenda'; load(); })
            .catch(function (err) { showError(err.message); });
        } }, 'Move')]),
      h('span', { class: 'lkc-lbl', style: 'margin-top:6px' }, 'People'),
      h('div', { class: 'lkc-members' }, members.map(memberRow))
    ])];
  }

  var CHANNELS = { linkedin: { label: 'LinkedIn', project: 'lytekeepr' }, instagram: { label: 'Instagram', project: 'aspan' }, email: { label: 'Email', project: 'nexy' } };
  function channelLabel(c) { return CHANNELS[c] ? CHANNELS[c].label : (c || 'Other'); }      // a channel the page does not know yet never breaks it
  var STATUS_LABELS = {
    instagram: { 'new': 'New', sent: 'DM sent', replied: 'Customer replied', done: 'Customer not interested' },
    linkedin: { 'new': 'New', sent: 'Connection request sent', accepted: 'Connection accepted', message_sent: 'Message sent', replied: 'Replied' },
    email: { 'new': 'New', sent: 'Email sent', replied: 'Replied', bounced: 'Bounced', done: 'Not interested' }
  };
  var NEXT_STEPS = {   // quick buttons only; any status can also be set from the chat
    instagram: { 'new': [['sent', 'DM sent']], sent: [['replied', 'Customer replied'], ['done', 'Not interested']], replied: [['done', 'Not interested']] },
    linkedin: { 'new': [['sent', 'Request sent']], sent: [['accepted', 'Accepted']], accepted: [['message_sent', 'Message sent']],
                message_sent: [['replied', 'They replied']] }
  };
  function statusLabel(lead) { var m = STATUS_LABELS[lead.channel] || {}; return m[lead.status] || lead.status; }

  function leadCard(lead) {
    var next = (NEXT_STEPS[lead.channel] || {})[lead.status] || [];
    var safeUrl = /^https?:\/\//i.test(lead.url || '') ? lead.url : '';
    return h('div', { class: 'lkc-lead' }, [
      h('b', {}, lead.contact),
      h('span', { class: 'lkc-leadmeta' }, (CHANNELS[lead.channel] ? CHANNELS[lead.channel].label : lead.channel) + ' · ' +
        statusLabel(lead) + ' · ' + longDate(lead.status_date)),
      lead.notes ? h('span', { class: 'lkc-leadmeta' }, lead.notes) : null,
      safeUrl ? h('a', { href: safeUrl, target: '_blank', rel: 'noopener noreferrer', class: 'lkc-leadlink' }, 'Open profile') : null,
      next.length ? h('div', { class: 'lkc-actions', style: 'margin-top:8px' }, next.map(function (step) {
        return h('button', { class: 'btn', type: 'button', onclick: function () {
          api('PUT', '/api/calendar/leads/' + lead.id, { status: step[0] }).then(load);
        } }, step[1]);
      })) : null
    ]);
  }

  /* ---------- import leads ---------- */
  function openImport() {
    state.importState = { channel: 'linkedin', file: null, busy: false, result: null, error: '' };
    state.pane = 'info'; state.mode = 'import';
    if (state.collapsed) setCollapsed(false);
    render();
  }

  function runImport(useSample) {
    var im = state.importState;
    var chanSel = document.getElementById('lkcChannel'); if (chanSel) im.channel = chanSel.value;
    if (!useSample && !im.file) { im.error = 'Choose a CSV file first, or use the sample data.'; render(); return; }
    var fd = new FormData();
    fd.append('channel', im.channel);
    if (useSample) fd.append('sample', '1'); else fd.append('file', im.file);
    im.busy = true; im.error = ''; im.result = null; render();
    fetch('/api/calendar/leads/import', { method: 'POST', body: fd }).then(function (res) {
      if (res.status === 401) { location.href = (window.LK_PREFIX || '') + '/login'; throw new Error('login required'); }
      return res.json().then(function (data) { if (!res.ok) throw new Error(data.error || 'Import failed'); return data; });
    }).then(function (result) {
      im.busy = false; im.result = result; im.file = null; return load();
    }).catch(function (e) { im.busy = false; im.error = e.message; render(); });
  }

  function setFile(im, file) {
    // Read into memory right away so the upload doesn't depend on the (re-rendered) input element.
    im.error = '';
    if (!file) { im.file = null; render(); return; }
    file.arrayBuffer().then(function (buf) { im.file = new File([buf], file.name, { type: file.type }); render(); })
      .catch(function () { im.error = 'Could not read that file.'; render(); });
  }

  function renderImport() {
    var im = state.importState;
    var chanSel = h('select', { id: 'lkcChannel', onchange: function () { im.channel = this.value; } }, Object.keys(CHANNELS).map(function (k) {
      return h('option', { value: k, selected: im.channel === k }, CHANNELS[k].label + ' leads → ' + PROJECTS[CHANNELS[k].project].label);
    }));
    var fileInput = h('input', { type: 'file', accept: '.csv,text/csv', hidden: true, id: 'lkcFile', onchange: function () {
      setFile(im, this.files[0]);
    } });
    var drop = h('div', { class: 'lkc-drop' + (im.file ? ' has' : ''),
      ondragover: function (ev) { ev.preventDefault(); this.classList.add('over'); },
      ondragleave: function () { this.classList.remove('over'); },
      ondrop: function (ev) { ev.preventDefault(); setFile(im, ev.dataTransfer.files[0]); },
      onclick: function () { fileInput.click(); } },
      [h('b', {}, im.file ? im.file.name : 'Drop a CSV here'), h('span', {}, im.file ? 'Click to choose a different file' : 'or click to choose a file')]);
    var r = im.result;
    var summary = r ? h('div', { class: 'lkc-card lkc-result' }, [
      h('b', {}, 'Import done'),
      h('span', {}, r.added + ' new · ' + r.updated + ' updated · ' + r.unchanged + ' unchanged'),
      h('span', {}, r.reminders_created + ' follow-ups scheduled' + (r.reminders_moved ? ' · ' + r.reminders_moved + ' moved' : '') +
        (r.reminders_closed ? ' · ' + r.reminders_closed + ' closed' : '')),
      r.errors.length ? h('div', { class: 'lkc-errs' }, [h('span', {}, r.errors.length + ' row(s) skipped:')].concat(
        r.errors.slice(0, 6).map(function (m) { return h('span', {}, '• ' + m); }),
        r.errors.length > 6 ? [h('span', {}, '…and ' + (r.errors.length - 6) + ' more')] : [])) : null
    ]) : null;
    return [h('div', { class: 'lkc-card lkc-form' }, [
      h('div', { class: 'lkc-top' }, [h('h2', { class: 'lkc-title' }, 'Import leads'),
        h('button', { class: 'x', type: 'button', 'aria-label': 'Close', onclick: closeSide }, '×')]),
      h('p', { class: 'lkc-empty' }, 'Columns: contact, status (new / sent / replied / done), date, url. A lead marked "sent" gets a follow-up on the calendar 3 days later.'),
      h('div', {}, [h('span', { class: 'lkc-lbl' }, 'List'), chanSel]),
      drop, fileInput,
      im.error ? h('p', { class: 'lkc-err' }, im.error) : null,
      h('div', { class: 'lkc-actions' }, [
        h('button', { class: 'btn primary', type: 'button', disabled: im.busy, onclick: function () { runImport(false); } }, im.busy ? 'Importing…' : 'Import'),
        h('button', { class: 'btn', type: 'button', disabled: im.busy, onclick: function () { runImport(true); } }, 'Use sample data'),
        h('button', { class: 'btn', type: 'button', onclick: closeSide }, 'Close')
      ])
    ]), summary];
  }

  /* ---------- new / edit form ---------- */
  function openForm(seed) {
    var editing = seed && seed.id;
    state.form = {
      id: editing ? seed.id : null,
      kind: seed.kind || 'task',
      project: seed.project || 'lytekeepr',
      title: seed.title || '',
      start_date: seed.start_date || today(),
      start_time: seed.start_time || '09:00',
      untimed: !!seed.untimed,
      duration_min: seed.duration_min || null,
      repeat: seed.repeat || 'none',
      days: seed.repeat_days ? seed.repeat_days.split(',').filter(Boolean).map(Number) : [],
      notes: seed.notes || '',
      error: ''
    };
    state.pane = 'info'; state.mode = 'form';
    if (state.collapsed) setCollapsed(false);
    render();
    var input = $('#lkcTitleInput'); if (input) input.focus();
  }

  function renderForm() {
    var f = state.form;
    var durations = [15, 30, 45, 60, 90, 120, 180];
    if (f.duration_min && durations.indexOf(f.duration_min) < 0) durations.push(f.duration_min);
    var defaultDur = { task: 30, meeting: 60, reminder: 15 };
    var kindTabs = h('div', { class: 'demo-reset-tabs' }, Object.keys(KINDS).map(function (k) {
      return h('button', { type: 'button', class: 'demo-reset-tab' + (f.kind === k ? ' is-active' : ''), onclick: function () { sync(); f.kind = k; render(); } }, KINDS[k]);
    }));
    var projPick = h('div', { class: 'lkc-projpick' }, Object.keys(PROJECTS).map(function (p) {
      return h('span', { class: 'lkc-chip' + (f.project === p ? ' on' : ''), onclick: function () { sync(); f.project = p; render(); } }, [dot(p), PROJECTS[p].label]);
    }));
    var repeatSel = h('select', { id: 'lkcRepeat', onchange: function () { sync(); render(); } }, Object.keys(REPEAT_LABELS).map(function (r) {
      return h('option', { value: r, selected: f.repeat === r }, REPEAT_LABELS[r]);
    }));
    var daysRow = f.repeat === 'weekly' ? h('div', {}, [h('span', { class: 'lkc-lbl' }, 'On'), h('div', { class: 'lkc-days' }, DAY_LETTERS.map(function (l, i) {
      return h('button', { type: 'button', class: f.days.indexOf(i) >= 0 ? 'on' : '', onclick: function () {
        sync(); var at = f.days.indexOf(i); if (at >= 0) f.days.splice(at, 1); else f.days.push(i); render();
      } }, l);
    }))]) : null;
    var durSel = h('select', { id: 'lkcDur' }, durations.sort(function (a, b) { return a - b; }).map(function (m) {
      return h('option', { value: m, selected: (f.duration_min || defaultDur[f.kind]) === m }, m < 60 ? m + ' min' : (m / 60) + (m === 60 ? ' hour' : ' hours'));
    }));
    return [h('form', { class: 'lkc-card lkc-form', onsubmit: function (ev) { ev.preventDefault(); save(); } }, [
      h('div', { class: 'lkc-top' }, [h('h2', { class: 'lkc-title' }, f.id ? 'Edit item' : 'New item'),
        h('button', { class: 'x', type: 'button', 'aria-label': 'Close', onclick: closeSide }, '×')]),
      kindTabs,
      h('div', {}, [h('span', { class: 'lkc-lbl' }, 'Title'), h('input', { type: 'text', id: 'lkcTitleInput', maxlength: 200, value: f.title, placeholder: 'e.g. Send LinkedIn batch' })]),
      h('div', {}, [h('span', { class: 'lkc-lbl' }, 'Project'), projPick]),
      h('div', { class: 'lkc-row2' }, [
        h('div', {}, [h('span', { class: 'lkc-lbl' }, f.repeat === 'none' ? 'Date' : 'Starts'), h('input', { type: 'date', id: 'lkcDate', value: f.start_date })]),
        f.untimed ? h('div', {}, [h('span', { class: 'lkc-lbl' }, 'Time'), h('p', { class: 'lkc-empty', style: 'margin:9px 0 0' }, 'No specific time')])
          : h('div', {}, [h('span', { class: 'lkc-lbl' }, 'Time'), h('input', { type: 'time', id: 'lkcTime', value: f.start_time })])
      ]),
      h('label', { class: 'lkc-check' }, [h('input', { type: 'checkbox', id: 'lkcUntimed', checked: !!f.untimed, onchange: function () { sync(); render(); } }),
        'No specific time (shows in the top strip)']),
      f.untimed ? null : h('div', {}, [h('span', { class: 'lkc-lbl' }, 'Length'), durSel]),
      h('div', {}, [h('span', { class: 'lkc-lbl' }, 'Repeat'), repeatSel]),
      daysRow,
      h('div', {}, [h('span', { class: 'lkc-lbl' }, 'Notes'), h('textarea', { id: 'lkcNotes', placeholder: 'Optional' }, f.notes)]),
      f.error ? h('p', { class: 'lkc-err' }, f.error) : null,
      h('div', { class: 'lkc-actions' }, [
        h('button', { class: 'btn primary', type: 'submit' }, 'Save'),
        h('button', { class: 'btn', type: 'button', onclick: closeSide }, 'Cancel')
      ])
    ])];
  }

  function sync() {   // copy field values into state before any re-render
    var f = state.form; if (!f) return;
    var g = function (id) { var el = document.getElementById(id); return el ? el.value : null; };
    if (g('lkcTitleInput') != null) f.title = g('lkcTitleInput');
    if (g('lkcDate')) f.start_date = g('lkcDate');
    if (g('lkcTime')) f.start_time = g('lkcTime');
    if (g('lkcDur')) f.duration_min = +g('lkcDur');
    if (g('lkcRepeat')) f.repeat = g('lkcRepeat');
    if (g('lkcNotes') != null) f.notes = g('lkcNotes');
    var un = document.getElementById('lkcUntimed'); if (un) f.untimed = un.checked;
  }

  function save() {
    sync();
    var f = state.form;
    if (!f.title.trim()) { f.error = 'Please enter a title.'; render(); return; }
    if (f.repeat === 'weekly' && !f.days.length) { f.error = 'Pick at least one day.'; render(); return; }
    if (!f.start_date || !f.start_time) { f.error = 'Date and time are required.'; render(); return; }
    var body = {
      title: f.title.trim(), project: f.project, kind: f.kind, start_date: f.start_date, start_time: f.untimed ? '08:00' : f.start_time, untimed: !!f.untimed,
      duration_min: f.duration_min, repeat: f.repeat, repeat_days: f.days.slice().sort().join(','), notes: f.notes
    };
    var req = f.id ? api('PUT', '/api/calendar/events/' + f.id, body) : api('POST', '/api/calendar/events', body);
    req.then(function (saved) {
      var first = firstOccurrence(saved);
      state.weekStart = mondayOf(first);
      state.selected = { id: saved.id, date: first };
      state.mode = 'detail'; state.form = null;
      return load();
    }).catch(function (e) { f.error = e.message; render(); });
  }

  /* ---------- chat ("Ask LyteKeepr") ---------- */
  var chat = { messages: [], busy: false, node: null, log: null, input: null, send: null };

  var URL_RE = /https?:\/\/[^\s<>"']+/g;

  function linkify(text) {            // plain text -> text + safe, clickable http(s) links
    var out = [], last = 0, m;
    URL_RE.lastIndex = 0;
    while ((m = URL_RE.exec(text))) {
      var url = m[0].replace(/[.,;:)]+$/, '');
      if (m.index > last) out.push(text.slice(last, m.index));
      out.push(h('a', { href: url, target: '_blank', rel: 'noopener noreferrer', class: 'lkc-leadlink' }, url));
      last = m.index + url.length; URL_RE.lastIndex = last;
    }
    if (last < text.length) out.push(text.slice(last));
    return out;
  }

  function chatBubble(role, text, loading) {
    var kids = role === 'user' || loading ? [text] : linkify(text);
    var bubble = h('div', { class: 'chat-bubble' + (role === 'user' ? ' user' : '') + (loading ? ' chat-bubble-loading' : '') }, kids);
    var urls = role === 'user' || loading ? [] : (text.match(URL_RE) || []).map(function (u) { return u.replace(/[.,;:)]+$/, ''); });
    if (urls.length >= 2) {
      var btn = h('button', { class: 'btn', type: 'button', style: 'margin-top:8px;padding:6px 12px', onclick: function () {
        var text = urls.join('\n');
        var done = function () { btn.textContent = 'Copied ' + urls.length + ' links'; setTimeout(function () { btn.textContent = 'Copy ' + urls.length + ' links'; }, 2000); };
        var fallback = function () {   // older browsers / blocked clipboard API
          var ta = h('textarea', { style: 'position:fixed;left:-9999px;top:0' }); ta.value = text; document.body.appendChild(ta); ta.select();
          var ok = false; try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
          ta.remove(); if (ok) done(); else btn.textContent = 'Copy failed: select the links by hand';
        };
        if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, fallback);
        else fallback();
      } }, 'Copy ' + urls.length + ' links');
      bubble.appendChild(h('div', {}, [btn]));
    }
    return h('div', { class: 'chat-message' + (role === 'user' ? ' user' : '') }, [bubble]);
  }
  function chatScroll() { chat.log.scrollTop = chat.log.scrollHeight; }

  function chatPending(items) {
    items.forEach(function (p) {
      var box = h('div', { class: 'chat-message' });
      function settle(approve) {
        box.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
        api('POST', '/api/calendar/chat/confirm', { id: p.id, approve: approve }).then(function (r) {
          box.replaceWith(chatBubble('assistant', r.message));
          chat.messages.push({ role: 'assistant', content: r.message });
          chatScroll();
          if (r.changed) { state.selected = null; if (state.mode === 'detail') state.mode = 'agenda'; load(); }
        }).catch(function (e) { box.replaceWith(chatBubble('assistant', 'Sorry — ' + e.message)); });
      }
      box.appendChild(h('div', { class: 'chat-bubble' }, [
        p.summary,
        h('div', { class: 'lkc-actions', style: 'margin-top:8px' }, [
          h('button', { class: 'btn primary', type: 'button', onclick: function () { settle(true); } }, 'Confirm'),
          h('button', { class: 'btn', type: 'button', onclick: function () { settle(false); } }, 'Cancel')
        ])
      ]));
      chat.log.appendChild(box);
    });
    chatScroll();
  }

  function chatSend() {
    var text = chat.input.value.trim();
    if (!text || chat.busy) return;
    chat.input.value = '';
    chat.messages.push({ role: 'user', content: text });
    chat.log.appendChild(chatBubble('user', text));
    var loading = chatBubble('assistant', 'Thinking…', true);
    chat.log.appendChild(loading); chatScroll();
    chat.busy = true; chat.send.disabled = true;
    api('POST', '/api/calendar/chat', { messages: chat.messages }).then(function (r) {
      loading.replaceWith(chatBubble('assistant', r.reply || '(no reply)'));
      chat.messages.push({ role: 'assistant', content: r.reply || '' });
      chatPending(r.pending || []);
      if (r.changed) load();
    }).catch(function (e) {
      loading.replaceWith(chatBubble('assistant', 'Sorry — ' + e.message));
      chat.messages.pop();
    }).then(function () { chat.busy = false; chat.send.disabled = false; chatScroll(); chat.input.focus(); });
  }

  var CHAT_WELCOME = 'Ask me to add or move things, or what\'s coming up. e.g. "add a meeting tomorrow at 2", "what\'s today?", "mark Dana Roy as replied".';

  function chatShowWelcome() { chat.log.replaceChildren(chatBubble('assistant', CHAT_WELCOME)); }

  function chatLoadHistory() {          // the conversation is saved on the server, so a refresh or a new sign-in doesn't lose it
    api('GET', '/api/calendar/chat/history').then(function (r) {
      if (!r.messages.length || chat.messages.length) return;       // nothing saved, or the user has already started typing
      chat.messages = r.messages.map(function (m) { return { role: m.role, content: m.content }; });
      chat.log.replaceChildren.apply(chat.log, chat.messages.map(function (m) { return chatBubble(m.role, m.content); }));
      chatScroll();
    }).catch(function () { /* keep the welcome message */ });
  }

  function chatPane() {
    // Built once and reused, so typing, scroll position and history survive calendar refreshes.
    if (chat.node) return chat.node;
    chat.log = h('div', { class: 'lkc-chat-log' }, [chatBubble('assistant', CHAT_WELCOME)]);
    chat.input = h('textarea', { class: 'chat-input', rows: 2, placeholder: 'Add a meeting, mark leads sent, what\'s today?',
      onkeydown: function (ev) { if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); chatSend(); } } });
    chat.send = h('button', { class: 'btn-send', type: 'button', 'aria-label': 'Send', style: 'font-size:16px', onclick: chatSend }, '↑');
    var clear = h('button', { class: 'lkc-clear', type: 'button', title: 'Delete the saved conversation', onclick: function () {
      if (!confirm('Clear the whole chat? This deletes the saved conversation.')) return;
      api('DELETE', '/api/calendar/chat/history').then(function () { chat.messages = []; chatShowWelcome(); }).catch(function (e) { showError(e.message); });
    } }, 'Clear chat');
    chat.node = h('div', { class: 'lkc-card lkc-chat' }, [h('div', { class: 'lkc-chat-tools' }, [clear]), chat.log, h('div', { class: 'chat-input-row' }, [chat.input, chat.send])]);
    chatLoadHistory();
    return chat.node;
  }

  /* ---------- selection / navigation ---------- */
  function select(id, date) { state.selected = { id: id, date: date }; state.mode = 'detail'; state.pane = 'info'; render(); }
  function jumpTo(id, date) {
    var week = mondayOf(date);
    state.selected = { id: id, date: date }; state.mode = 'detail'; state.pane = 'info';
    if (week !== state.weekStart) { state.weekStart = week; load(); } else render();
  }
  function closeSide() { state.mode = 'agenda'; state.selected = null; state.form = null; state.importState = null; render(); }
  function shiftWeek(n) { state.weekStart = addDays(state.weekStart, n * 7); state.selected = null; if (state.mode === 'detail') state.mode = 'agenda'; load(); }

  /* ---------- main render ---------- */
  function render() {
    var weekEnd = addDays(state.weekStart, 6);
    var s = toUTC(state.weekStart), e = toUTC(weekEnd);
    var opts = { month: 'short', timeZone: 'UTC' };
    $('#lkcRange').textContent = s.toLocaleString('en-US', opts) + ' ' + s.getUTCDate() + ' – ' +
      (s.getUTCMonth() === e.getUTCMonth() ? '' : e.toLocaleString('en-US', opts) + ' ') + e.getUTCDate() + ', ' + e.getUTCFullYear();
    $('#lkcFilters').replaceChildren.apply($('#lkcFilters'), Object.keys(PROJECTS).map(function (p) {
      return h('span', { class: 'lkc-chip' + (state.hidden[p] ? ' off' : ''), onclick: function () { state.hidden[p] = !state.hidden[p]; render(); } }, [dot(p), PROJECTS[p].label]);
    }));
    renderGrid();
    $('#lkcDensity').textContent = state.density === 'fit' ? 'Fit day' : 'Detailed';
    $('#lkcTabInfo').classList.toggle('is-active', state.pane === 'info');
    $('#lkcTabChat').classList.toggle('is-active', state.pane === 'chat');
    var side = $('#lkcSideInfo');
    if (state.lastMode !== state.mode + state.pane && state.pane !== 'chat') side.scrollTop = 0;
    state.lastMode = state.mode + state.pane;
    if (state.pane === 'chat') { var cn = chatPane(); if (side.firstChild !== cn || side.childNodes.length !== 1) side.replaceChildren(cn); }
    else side.replaceChildren.apply(side, (state.mode === 'form' ? renderForm() : state.mode === 'import' ? renderImport() : state.mode === 'detail' ? renderDetail() : renderAgenda()).filter(Boolean));
  }

  /* ---------- collapsible side panel ---------- */
  function applyCollapse() {
    var side = document.getElementById('lkcSide'), btn = document.getElementById('lkcCollapse');
    side.classList.toggle('collapsed', !!state.collapsed);
    btn.textContent = state.collapsed ? '\u2039' : '\u203A';
    btn.setAttribute('aria-expanded', state.collapsed ? 'false' : 'true');
    btn.setAttribute('aria-label', state.collapsed ? 'Expand side panel' : 'Collapse side panel');
    btn.title = state.collapsed ? 'Expand side panel' : 'Collapse side panel';
  }
  function setCollapsed(on) {
    state.collapsed = on; applyCollapse();
    try { localStorage.setItem('lkcCollapsed', on ? '1' : '0'); } catch (e) { /* storage unavailable */ }
  }
  try { state.collapsed = localStorage.getItem('lkcCollapsed') === '1'; } catch (e) { state.collapsed = false; }

  /* ---------- day density: fit the day on screen, or the roomy scrolling view ---------- */
  try { state.density = localStorage.getItem('lkcDensity') === 'detailed' ? 'detailed' : 'fit'; } catch (e) { state.density = 'fit'; }
  function setDensity(mode) {
    state.density = mode;
    try { localStorage.setItem('lkcDensity', mode); } catch (e) { /* storage unavailable */ }
    var wrap = $('#lkcGridWrap'); if (wrap) wrap.scrollTop = 0;
    render();
  }

  /* ---------- wiring ---------- */
  var others = ['homeTab', 'linkedinTab', 'crmTab'];
  function show() {
    ['caseOverviewPanel', 'aiPanel'].forEach(function (id) { var el = document.getElementById(id); if (el) el.hidden = true; });
    panel.hidden = false;
    if (!state.weekStart) state.weekStart = mondayOf(fmt(new Date()));
    load().then(function () {
      var m = mondayOf(today());
      if (state.weekStart !== m && !state.selected && !state.weekTouched) { state.weekStart = m; load(); }
    });
  }
  function hide() {
    if (panel.hidden) return;
    panel.hidden = true;
    var overview = document.getElementById('caseOverviewPanel'); if (overview) overview.hidden = false;
  }
  tab.addEventListener('click', show);
  others.forEach(function (id) { var el = document.getElementById(id); if (el) el.addEventListener('click', hide); });

  $('#lkcPrev').addEventListener('click', function () { state.weekTouched = true; shiftWeek(-1); });
  $('#lkcNext').addEventListener('click', function () { state.weekTouched = true; shiftWeek(1); });
  $('#lkcToday').addEventListener('click', function () { state.weekTouched = false; state.weekStart = mondayOf(today()); load(); });
  document.getElementById('lkcCollapse').addEventListener('click', function () { setCollapsed(!state.collapsed); });
  applyCollapse();
  $('#lkcNew').addEventListener('click', function () { openForm({}); });
  $('#lkcDensity').addEventListener('click', function () { setDensity(state.density === 'fit' ? 'detailed' : 'fit'); });
  var resizeTimer = null;
  window.addEventListener('resize', function () {          // keep the day fitted when the window changes size
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () { if (!panel.hidden && state.density === 'fit') renderGrid(); }, 150);
  });
  $('#lkcImport').addEventListener('click', openImport);
  $('#lkcSyncGoogle').addEventListener('click', function () {
    var btn = this; btn.disabled = true; btn.textContent = 'Syncing\u2026';
    api('POST', '/api/calendar/google/sync').then(function (r) {
      state.syncNote = 'Google synced: ' + r.added + ' new, ' + r.updated + ' updated, ' + r.removed + ' removed' +
        (r.kept_as_cancelled ? ', ' + r.kept_as_cancelled + ' cancelled (kept, has your notes)' : '') +
        (r.skipped_all_day ? '. ' + r.skipped_all_day + ' all-day skipped.' : '.');
      if (state.mode !== 'agenda') { state.mode = 'agenda'; state.selected = null; }
      return load();
    }).catch(function (e) { state.syncNote = 'Google sync failed: ' + e.message; state.mode = 'agenda'; return load(); })
      .then(function () { btn.disabled = false; btn.textContent = 'Sync Google'; });
  });
  $('#lkcTabInfo').addEventListener('click', function () { state.pane = 'info'; render(); });
  $('#lkcTabChat').addEventListener('click', function () { state.pane = 'chat'; render(); });
  document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape' && !panel.hidden && state.mode !== 'agenda') closeSide(); });

  // Keep "now" line and badge fresh while the tab is open.
  setInterval(function () { if (!panel.hidden && state.mode !== 'form' && state.mode !== 'import') load(); }, 60000);
  // Badge on first page load, even before the tab is opened.
  api('GET', '/api/calendar/agenda').then(function (a) { state.agenda = a; state.now = a.now; updateBadge(); }).catch(function () {});
})();
