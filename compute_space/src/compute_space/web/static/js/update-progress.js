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

  function clearToken() {
    try { sessionStorage.removeItem('openhost_update_token'); } catch (e) { /* ignore */ }
  }

  function esc(s) {
    var d = document.createElement('div');
    d.textContent = (s == null) ? '' : String(s);
    return d.innerHTML;
  }

  function render(entries) {
    if (!entries || !entries.length) return;
    logEl.innerHTML = '';
    entries.forEach(function (e) {
      var li = document.createElement('li');
      if (e.phase === 'done') li.className = 'done';
      if (e.phase === 'failed') li.className = 'failed';
      var ts = (e.ts || '').substr(11, 8);
      li.innerHTML = '<span class="ts">' + esc(ts) + '</span>' + esc(e.message || e.phase || '');
      logEl.appendChild(li);
    });
  }

  function finish() {
    if (spEl) spEl.style.display = 'none';
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

  // Viewers without a valid token (an anonymous visitor who landed on the
  // updater's page mid-downtime, or a tab that lost its token) can never read
  // /updates — but they must not be stranded on this page after the instance is
  // back. When /updates is unreadable and /health is ok again, reload: the real
  // server then serves the actual content for this URL. Rate-limited to avoid a
  // reload storm if health flaps.
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

  function handleTerminal(d) {
    terminalSeen = true;
    if (spEl) spEl.style.display = 'none';
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
        render(d.entries || []);
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
