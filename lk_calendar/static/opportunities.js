/* Opportunities on the desktop: cards on the funnel board, the opportunity window (participants, roles, primary,
   active/inactive, introduced by, notes, tasks, history), "New opportunity", and the list on a contact's page.
   Contact cards on the board are untouched; this only adds the second kind of entry. */
(function () {
  'use strict';
  var ROLES = [['', 'No role'], ['decision-maker', 'Decision-maker'], ['influencer', 'Influencer'], ['advisor', 'Advisor'], ['introducer', 'Introducer'], ['other', 'Other']];

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
  function initials(n) { return String(n || '?').trim().split(/\s+/).map(function (w) { return w[0]; }).slice(0, 2).join('').toUpperCase(); }
  function when(iso) { if (!iso) return ''; var d = new Date(iso); return isNaN(d) ? iso : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }); }
  function openContact(id) { if (typeof window.openCrmPersonDetail === 'function') window.openCrmPersonDetail(id); else if (C().editContact) C().editContact(id); }

  /* ---------- the card on the funnel board ---------- */
  function card(o, onOpen) {
    var c = h('div', { class: 'crm-funnel-person-card lko-card', draggable: 'true', title: 'Opportunity' }, [
      h('div', { class: 'lko-kind' }, [h('span', { class: 'lko-icon', 'aria-hidden': 'true' }, '◆'), 'Opportunity']),
      h('div', { class: 'crm-funnel-person-name' }, o.participants === 1 && o.single_name ? o.single_name : o.name),
      o.participants === 1 && o.single_name ? h('div', { class: 'crm-funnel-person-company' }, o.name) : h('div', { class: 'lko-people' }, o.summary),
      o.companies && o.companies.length ? h('div', { class: 'crm-funnel-person-company' }, o.companies.slice(0, 3).join(' · ') + (o.companies.length > 3 ? ' +' + (o.companies.length - 3) : '')) : null
    ]);
    c.addEventListener('dragstart', function (e) { e.dataTransfer.setData('text/plain', 'opp:' + o.opportunity_id); e.dataTransfer.effectAllowed = 'move'; c.classList.add('crm-funnel-person-card-dragging'); });
    c.addEventListener('dragend', function () { c.classList.remove('crm-funnel-person-card-dragging'); });
    c.addEventListener('click', function () { onOpen(o.opportunity_id); });
    return c;
  }

  /* ---------- the opportunity window ---------- */
  var current = null, onChange = null;
  function open(id, changed) {
    onChange = changed || onChange;
    api('GET', '/api/crm/opportunities/' + id).then(function (o) { current = o; render(); }).catch(fail);
  }
  function refreshWith(promise, msg) {
    return promise.then(function (o) { current = o; render(); if (onChange) onChange(); }).catch(fail);
  }
  function render() {
    var o = current;
    var nameInput = h('input', { type: 'text', value: o.name, class: 'lko-title', 'aria-label': 'Opportunity name' });
    nameInput.addEventListener('change', function () { refreshWith(api('PUT', '/api/crm/opportunities/' + o.opportunity_id, { name: nameInput.value })); });
    var stageSel = h('select', { class: 'demo-reset-input', 'aria-label': 'Stage' }, o.stages.map(function (s) { return h('option', { value: s.stage_id, selected: s.stage_id === o.stage_id }, s.name); }));
    stageSel.addEventListener('change', function () { refreshWith(api('POST', '/api/crm/opportunities/' + o.opportunity_id + '/move', { stage_id: +stageSel.value })); });
    var company = h('input', { type: 'text', value: o.company, placeholder: 'Main company (optional)' });
    company.addEventListener('change', function () { refreshWith(api('PUT', '/api/crm/opportunities/' + o.opportunity_id, { company: company.value })); });

    var active = o.participants.filter(function (p) { return p.active; }), past = o.participants.filter(function (p) { return !p.active; });
    var list = h('div', { class: 'lko-people-list' }, active.length ? active.map(row) : [h('p', { class: 'lks-note' }, 'No one is on this opportunity yet. Add the people involved below.')]);
    var pastBox = past.length ? h('details', { class: 'lko-past' }, [h('summary', {}, 'No longer involved (' + past.length + ')'), h('div', { class: 'lko-people-list' }, past.map(row))]) : null;

    var notes = h('textarea', { rows: '3', placeholder: 'Notes about this opportunity' }, o.notes);
    notes.addEventListener('change', function () { refreshWith(api('PUT', '/api/crm/opportunities/' + o.opportunity_id, { notes: notes.value })); });

    var taskTitle = h('input', { type: 'text', placeholder: 'Next step, e.g. Send the term sheet' }), taskDate = h('input', { type: 'date', value: new Date(Date.now() + 864e5).toISOString().slice(0, 10) });
    var tasks = h('div', { class: 'lko-tasks' }, [
      o.tasks.length ? h('ul', { class: 'lko-task-list' }, o.tasks.map(function (t) { return h('li', { class: t.done ? 'done' : '' }, [h('span', {}, t.title), h('small', {}, when(t.start_date) + (t.done ? ' · done' : ''))]); })) : h('p', { class: 'lks-note' }, 'No tasks yet.'),
      h('div', { class: 'lks-row' }, [taskTitle, taskDate, h('button', { class: 'btn btn-secondary btn-small', type: 'button', onclick: function () {
        if (!taskTitle.value.trim()) { taskTitle.focus(); return; }
        refreshWith(api('POST', '/api/crm/opportunities/' + o.opportunity_id + '/tasks', { title: taskTitle.value, date: taskDate.value }));
      } }, 'Add task')])
    ]);

    var hist = o.history || [];
    var history = h('details', { class: 'lko-history' }, [h('summary', {}, 'History (' + hist.length + ')'),
      h('ul', {}, hist.map(function (x) { return h('li', {}, [h('small', {}, when(x.at) + (x.who ? ' · ' + x.who : '')), ' ', x.text]); }))]);

    var del = h('button', { class: 'btn btn-remove btn-small', type: 'button', onclick: function () {
      confirmBox('Delete the opportunity “' + o.name + '”? Its participant links, tasks and history go. The contacts themselves stay.', 'Delete').then(function (yes) {
        if (!yes) return;
        api('POST', '/api/crm/opportunities/' + o.opportunity_id + '/delete').then(function () { if (C().closeModal) C().closeModal(true); if (onChange) onChange(); }).catch(fail);
      });
    } }, 'Delete opportunity');

    var cardEl = h('div', { class: 'lks-card lko-window' }, [
      h('div', { class: 'lko-head' }, [h('span', { class: 'lko-kind' }, [h('span', { class: 'lko-icon' }, '◆'), 'Opportunity · ' + o.funnel]),
        h('button', { class: 'lko-x', type: 'button', 'aria-label': 'Close', onclick: function () { C().closeModal(true); } }, '×')]),
      nameInput,
      h('div', { class: 'lks-row lko-meta' }, [h('label', {}, ['Stage ', stageSel]), company]),
      h('h3', {}, 'People' + (active.length ? ' (' + active.length + ')' : '')),
      list, pastBox, suggestBox(o), addBox(o),
      h('h3', {}, 'Notes'), notes,
      h('h3', {}, 'Tasks'), tasks,
      history,
      h('div', { class: 'lks-row lko-foot' }, [del])
    ]);
    C().modal(cardEl);
  }

  function row(p) {
    var o = current;
    var roleSel = h('select', { class: 'demo-reset-input', 'aria-label': 'Role of ' + p.name }, ROLES.map(function (r) { return h('option', { value: r[0], selected: (p.role || '') === r[0] }, r[1]); }));
    roleSel.addEventListener('change', function () { refreshWith(api('PUT', '/api/crm/opportunities/' + o.opportunity_id + '/participants/' + p.contact_id, { role: roleSel.value })); });
    var others = o.participants.filter(function (x) { return x.contact_id !== p.contact_id; });
    var introSel = h('select', { class: 'demo-reset-input', 'aria-label': 'Introduced by' }, [h('option', { value: '' }, 'Introduced by: —')].concat(others.map(function (x) {
      return h('option', { value: x.contact_id, selected: p.introduced_by === x.contact_id }, 'Introduced by ' + x.name); })));
    introSel.addEventListener('change', function () { refreshWith(api('PUT', '/api/crm/opportunities/' + o.opportunity_id + '/participants/' + p.contact_id, { introduced_by: introSel.value ? +introSel.value : 0 })); });
    var star = h('button', { class: 'lko-star' + (p.primary ? ' on' : ''), type: 'button', title: p.primary ? 'Primary contact (click to clear)' : 'Make primary contact', disabled: !p.active, onclick: function () {
      refreshWith(api('PUT', '/api/crm/opportunities/' + o.opportunity_id + '/participants/' + p.contact_id, { primary: !p.primary }));
    } }, p.primary ? '★ Primary' : '☆ Primary');
    var toggle = h('button', { class: 'btn btn-secondary btn-small', type: 'button', onclick: function () {
      refreshWith(api('PUT', '/api/crm/opportunities/' + o.opportunity_id + '/participants/' + p.contact_id, { active: !p.active }));
    } }, p.active ? 'No longer involved' : 'Involved again');
    var remove = h('button', { class: 'lko-link', type: 'button', onclick: function () {
      confirmBox('Remove ' + p.name + ' from this opportunity? Use this only if they were added by mistake: the link is deleted, their contact stays. (If they stopped being involved, use “No longer involved” to keep the history.)', 'Remove link').then(function (yes) {
        if (yes) refreshWith(api('POST', '/api/crm/opportunities/' + o.opportunity_id + '/participants/' + p.contact_id + '/remove'));
      });
    } }, 'Remove (added by mistake)');
    return h('div', { class: 'lko-person' + (p.active ? '' : ' off') }, [
      h('span', { class: 'lko-avatar' }, initials(p.name)),
      h('div', { class: 'lko-who' }, [h('button', { class: 'lko-name', type: 'button', onclick: function () { C().closeModal(true); openContact(p.contact_id); } }, p.name),
        h('small', {}, [p.company || 'No company', p.active ? '' : ' · left ' + when(p.left_at), p.introduced_by_name ? ' · introduced by ' + p.introduced_by_name : ''].join(''))]),
      h('div', { class: 'lko-controls' }, [roleSel, star, introSel, toggle, remove])
    ]);
  }

  // People LyteKeepr noticed in emails with this opportunity's people (plain counting of who emails with whom)
  function suggestBox(o) {
    var box = h('div', { class: 'lko-suggested' });
    api('GET', '/api/crm/suggestions').then(function (r) {
      var mine = (r.suggestions || []).filter(function (x) { return x.kind === 'add_to_opportunity' && x.opportunity_id === o.opportunity_id; });
      if (!mine.length) return;
      box.appendChild(h('p', { class: 'lks-note' }, 'Suggested from your emails:'));
      mine.forEach(function (x) {
        box.appendChild(h('div', { class: 'lko-sugg' }, [h('div', {}, [h('b', {}, x.name), h('small', {}, x.reason)]),
          h('button', { class: 'btn btn-primary btn-small', type: 'button', onclick: function () { refreshWith(api('POST', '/api/crm/suggestions/' + x.suggestion_id + '/accept').then(function () { return api('GET', '/api/crm/opportunities/' + o.opportunity_id); })); } }, 'Add'),
          h('button', { class: 'lko-link', type: 'button', style: 'color:var(--ink-soft)', onclick: function () { refreshWith(api('POST', '/api/crm/suggestions/' + x.suggestion_id + '/dismiss').then(function () { return api('GET', '/api/crm/opportunities/' + o.opportunity_id); })); } }, 'Dismiss')]));
      });
    }).catch(function () {});
    return box;
  }

  function addBox(o) {
    var q = h('input', { type: 'text', placeholder: 'Add a person: type a name, company or email', autocomplete: 'off' });
    var role = h('select', { class: 'demo-reset-input', 'aria-label': 'Role' }, ROLES.map(function (r) { return h('option', { value: r[0] }, r[1]); }));
    var res = h('div', { class: 'lko-suggest' });
    var t = null;
    function add(id) { refreshWith(api('POST', '/api/crm/opportunities/' + o.opportunity_id + '/participants', { contact_id: id, role: role.value })); }
    q.addEventListener('input', function () {
      clearTimeout(t); t = setTimeout(function () {
        var text = q.value.trim(); res.replaceChildren();
        if (text.length < 2) return;
        api('GET', '/api/calendar/contacts?limit=8&q=' + encodeURIComponent(text)).then(function (r) {
          if (q.value.trim() !== text) return;
          var on = {}; o.participants.forEach(function (p) { if (p.active) on[p.contact_id] = 1; });
          (r.contacts || []).forEach(function (c) {
            res.appendChild(h('button', { type: 'button', disabled: !!on[c.id], onclick: function () { add(c.id); } },
              [h('b', {}, c.display), h('span', {}, on[c.id] ? 'already on it' : [c.job_title, c.company].filter(Boolean).join(' · ') || c.email || '')]));
          });
          res.appendChild(h('button', { type: 'button', class: 'lko-new', onclick: function () {
            if (!C().addContact) return;
            C().addContact({ name: text }, function () {          // the new contact, then onto the opportunity
              api('GET', '/api/calendar/contacts?limit=5&q=' + encodeURIComponent(text)).then(function (r2) {
                var newest = (r2.contacts || []).slice().sort(function (a, b) { return b.id - a.id; })[0];
                if (newest) { add(newest.id); setTimeout(function () { open(o.opportunity_id); }, 300); }
              });
            });
          } }, [h('b', {}, '+ New contact “' + text + '”'), h('span', {}, 'Create the contact, then add them')]));
        }).catch(fail);
      }, 200);
    });
    return h('div', { class: 'lko-add' }, [h('div', { class: 'lks-row' }, [q, role]), res]);
  }

  /* ---------- "New opportunity" ---------- */
  function create(funnelId, stages, changed) {
    var name = h('input', { type: 'text', placeholder: 'e.g. Northwind pilot partnership' });
    var stage = h('select', { class: 'demo-reset-input' }, (stages || []).map(function (s) { return h('option', { value: s.stage_id }, s.name); }));
    var company = h('input', { type: 'text', placeholder: 'Main company (optional)' });
    var err = h('p', { class: 'lks-err', hidden: true });
    var save = h('button', { class: 'btn btn-primary', type: 'button', onclick: function () {
      if (!name.value.trim()) { name.focus(); return; }
      save.disabled = true;
      api('POST', '/api/crm/opportunities', { name: name.value, funnel_id: funnelId, stage_id: +stage.value, company: company.value })
        .then(function (o) { if (changed) changed(); open(o.opportunity_id, changed); })
        .catch(function (e) { save.disabled = false; err.hidden = false; err.textContent = e.message; });
    } }, 'Create and add people');
    C().modal(h('div', { class: 'lks-card lko-window' }, [
      h('div', { class: 'lko-head' }, [h('h2', {}, 'New opportunity'), h('button', { class: 'lko-x', type: 'button', 'aria-label': 'Close', onclick: function () { C().closeModal(true); } }, '×')]),
      h('p', { class: 'lks-note' }, 'One shared deal, project or partnership that moves through the funnel as a single card, with several people on it. Contacts in this funnel stay as they are.'),
      h('label', { class: 'lko-field' }, ['Name', name]), h('label', { class: 'lko-field' }, ['Stage', stage]), h('label', { class: 'lko-field' }, ['Main company', company]),
      err, h('div', { class: 'lks-row' }, [save])
    ]));
    setTimeout(function () { name.focus(); }, 50);
  }

  /* ---------- on a contact's page ---------- */
  function renderForPerson(box, personId) {
    if (!box) return;
    box.replaceChildren();
    api('GET', '/api/crm/people/' + personId + '/connections').then(function (r) {
      var cs = (r.connections || []).slice(0, 6);
      if (!cs.length) return;
      var line = h('p', { class: 'crm-detail-field lko-conn' }, ['Often in emails with: ']);
      cs.forEach(function (c, i) {
        if (i) line.appendChild(document.createTextNode(', '));
        line.appendChild(c.contact_id ? h('button', { type: 'button', class: 'lko-name', style: 'font-weight:500;font-size:inherit', onclick: function () { openContact(c.contact_id); } }, c.name)
                                      : h('span', { title: c.email }, c.name));
        line.appendChild(h('small', {}, ' (' + c.threads + ')'));
      });
      box.appendChild(line);
    }).catch(function () {});
    api('GET', '/api/crm/people/' + personId + '/opportunities').then(function (r) {
      var list = r.opportunities || [];
      if (!list.length) return;
      box.appendChild(h('p', { class: 'crm-detail-field' }, 'Opportunities:'));
      box.appendChild(h('div', { class: 'lko-mine' }, list.map(function (x) {
        return h('button', { type: 'button', class: 'lko-chip' + (x.active ? '' : ' off'), onclick: function () { open(x.opportunity_id, function () { renderForPerson(box, personId); }); } },
          [h('span', { class: 'lko-icon' }, '◆'), x.name, h('small', {}, ' · ' + [x.primary ? 'primary' : x.role, x.stage, x.active ? '' : 'no longer involved'].filter(Boolean).join(' · '))]);
      })));
    }).catch(function () {});
  }

  window.lkOpps = { card: card, open: open, create: create, renderForPerson: renderForPerson,
    move: function (id, stageId) { return api('POST', '/api/crm/opportunities/' + id + '/move', { stage_id: stageId }); } };
})();
