/* Remembers which screen you were on, so a refresh puts you back in the same place (kept per browser tab, nothing is sent anywhere). */
(function () {
  'use strict';
  var KEY = 'lk-view', restoring = true, timer = null;
  function g(id) { return document.getElementById(id); }
  function shown(id) { var e = g(id); return !!e && !e.hidden; }
  function active(id) { var e = g(id); return !!e && e.classList.contains('is-active'); }
  function read() { try { return JSON.parse(sessionStorage.getItem(KEY) || 'null'); } catch (e) { return null; } }
  function write(v) { try { sessionStorage.setItem(KEY, JSON.stringify(v)); } catch (e) { /* storage blocked: nothing to remember */ } }

  function snapshot() {
    if (restoring) return;
    var s = { tab: 'home' };
    if (shown('settingsPanel')) s.tab = 'settings';
    else if (shown('calendarPanel')) s.tab = 'calendar';
    else if (shown('crmPersonDetailView')) { s.tab = 'crm'; s.person = window.lkCurrentPersonId ? window.lkCurrentPersonId() : null; }
    else if (shown('crmPanel')) {
      s.tab = 'crm';
      s.main = active('crmFunnelsTabBtn') ? 'funnels' : 'contacts';
      s.sub = active('crmImportSubTabBtn') ? 'import' : active('crmCompaniesSubTabBtn') ? 'companies' : 'people';
      var sel = g('crmFunnelBoardSelect');
      if (sel && sel.value) s.funnel = sel.value;
    } else if (shown('linkedinPanel')) s.tab = 'linkedin';
    write(s);
  }
  function later() { clearTimeout(timer); timer = setTimeout(snapshot, 120); }

  function click(id) { var e = g(id); if (e) e.click(); }
  function reveal() { var h = g('lkHold'); if (h) h.remove(); restoring = false; snapshot(); }
  function waitFor(test, then, tries) {
    if (test() || (tries || 0) > 40) { then(); return; }
    setTimeout(function () { waitFor(test, then, (tries || 0) + 1); }, 50);
  }
  function restore() {
    var s = read();
    if (!s || s.tab === 'home') { reveal(); return; }
    if (s.tab === 'calendar') { click('calendarTab'); waitFor(function () { return shown('calendarPanel'); }, reveal); }
    else if (s.tab === 'linkedin') { click('linkedinTab'); waitFor(function () { return shown('linkedinPanel'); }, reveal); }
    else if (s.tab === 'settings') { click('settingsBtn'); waitFor(function () { return shown('settingsPanel'); }, reveal); }
    else if (s.tab === 'crm') {
      click('crmTab');
      if (s.person && window.lkOpenPerson) { window.lkOpenPerson(s.person); waitFor(function () { return shown('crmPersonDetailView'); }, reveal); return; }
      if (s.main === 'funnels') {
        click('crmFunnelsTabBtn');
        var sel = g('crmFunnelBoardSelect');
        if (sel && s.funnel) { sel.value = s.funnel; sel.dispatchEvent(new Event('change')); }
        waitFor(function () { var w = g('crmFunnelBoardWrap'); return w && w.children.length > 0; }, reveal);
      } else {
        if (s.sub === 'companies') click('crmCompaniesSubTabBtn');
        else if (s.sub === 'import') click('crmImportSubTabBtn');
        waitFor(function () { return shown('crmPanel'); }, reveal);
      }
    } else reveal();
  }

  document.addEventListener('click', later, true);
  window.addEventListener('load', function () {
    ['crmPanel', 'crmPersonDetailView', 'calendarPanel', 'settingsPanel', 'linkedinPanel', 'caseOverviewPanel'].forEach(function (id) {
      var e = g(id);
      if (e) new MutationObserver(later).observe(e, { attributes: true, attributeFilter: ['hidden'] });
    });
    restore();
  });
})();
