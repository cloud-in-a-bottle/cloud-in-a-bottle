// Drives the /updating page: polls /updates for progress and returns to /settings
// once the update is done and the dashboard is back.
(function () {
  var params = new URLSearchParams(window.location.search);
  var token = params.get('token') || '';
  // Persist the token so it survives updater-served reloads that lack it in the URL.
  try {
    if (token) {
      sessionStorage.setItem('openhost_update_token', token);
    } else {
      token = sessionStorage.getItem('openhost_update_token') || '';
    }
  } catch (e) { /* sessionStorage unavailable: fall back to the URL token */ }
  var logEl = document.getElementById('log');
  var spEl = document.getElementById('sp');
  var terminalSeen = false;
  var failedShownAt = null;
  var emptyPolls = 0;
  // ~5s: rides out the instant the log is legitimately blank at the start of an
  // update, without leaving a stranded page there for long.
  var EMPTY_POLLS_BEFORE_RECOVERY = 6;

  function clearToken() {
    try { sessionStorage.removeItem('openhost_update_token'); } catch (e) { /* ignore */ }
  }

  // Built node-by-node rather than via innerHTML: this file is also inlined
  // into the updater's standalone page, where dom.js is not available.
  function render(entries) {
    if (!entries || !entries.length) return;
    while (logEl.firstChild) logEl.removeChild(logEl.firstChild);
    entries.forEach(function (e) {
      var li = document.createElement('li');
      if (e.phase === 'done') li.className = 'done';
      if (e.phase === 'failed') li.className = 'failed';
      var ts = document.createElement('span');
      ts.className = 'ts';
      ts.textContent = (e.ts || '').substr(11, 8);
      li.appendChild(ts);
      li.appendChild(document.createTextNode(e.message || e.phase || ''));
      logEl.appendChild(li);
    });
  }

  function finish() {
    if (spEl) spEl.hidden = true;
    clearToken();
    window.location.href = '/settings';
  }

  function dashboardReachable() {
    // Probe /health, not /settings: /settings answers HEAD with 405 (GET-only)
    // and would never report reachable. The detached updater answers 503, so
    // "ok" can only come from a live compute_space.
    return fetch('/health', { method: 'GET', cache: 'no-store' })
      .then(function (r) { return r.ok; })
      .catch(function () { return false; });
  }

  // A viewer without a valid token can never read /updates, but must not be
  // stranded here once the instance is back. When /updates is unreadable and
  // /health is ok, reload so the real server serves this URL. Rate-limited so a
  // flapping health check can't cause a reload storm.
  function maybeRecoverWithoutLogs() {
    var now = Date.now();
    var last = 0;
    try { last = Number(sessionStorage.getItem('openhost_update_reload_ts') || 0); } catch (e) { /* ignore */ }
    if (now - last < 10000) return;
    dashboardReachable().then(function (up) {
      if (!up) return;
      try { sessionStorage.setItem('openhost_update_reload_ts', String(Date.now())); } catch (e) { /* ignore */ }
      window.location.reload();
    });
  }

  // Leave rather than reload: /updating always renders this page, so a reload
  // would spin here forever.
  function recoverFromEmptyLog() {
    dashboardReachable().then(function (up) {
      if (up) { finish(); return; }
      emptyPolls = 0;
      setTimeout(poll, 800);
    });
  }

  function handleTerminal(d) {
    terminalSeen = true;
    if (spEl) spEl.hidden = true;
    var last = d.entries && d.entries.length ? d.entries[d.entries.length - 1] : null;
    var failed = last && last.phase === 'failed';
    if (failed && failedShownAt === null) {
      // Leave the failure on screen long enough to read before returning.
      failedShownAt = Date.now();
    }
    dashboardReachable().then(function (up) {
      if (up && (!failed || Date.now() - failedShownAt > 5000)) { finish(); return; }
      setTimeout(poll, 1000);
    });
  }

  function poll() {
    fetch('/updates?token=' + encodeURIComponent(token), { cache: 'no-store', headers: { 'Accept': 'application/json' } })
      .then(function (r) {
        if (!r.ok) {
          if (!terminalSeen) maybeRecoverWithoutLogs();
          setTimeout(poll, 800);
          return null;
        }
        return r.json();
      })
      .then(function (d) {
        if (!d) return;
        var entries = d.entries || [];
        // An empty log will never become terminal: either no update ran, or one
        // died before writing its first line. Without this the page polls a
        // healthy instance forever.
        if (!entries.length) {
          if (++emptyPolls >= EMPTY_POLLS_BEFORE_RECOVERY) { recoverFromEmptyLog(); return; }
        } else {
          emptyPolls = 0;
        }
        render(entries);
        if (d.terminal) { handleTerminal(d); return; }
        setTimeout(poll, 800);
      })
      .catch(function () {
        // Transient error = the brief restart window; keep polling. If we already
        // saw terminal, we may just be waiting for the dashboard to come back.
        if (terminalSeen) {
          dashboardReachable().then(function (up) {
            if (up) { finish(); return; }
            setTimeout(poll, 1000);
          });
        } else {
          maybeRecoverWithoutLogs();
          setTimeout(poll, 800);
        }
      });
  }

  poll();
})();
