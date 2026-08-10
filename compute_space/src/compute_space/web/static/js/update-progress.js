// Drives the /updating page: polls /updates for live progress and reloads into
// /settings once the update finishes and the dashboard is back. /updates is
// served by compute_space (owner-authed) during the apply phase and by the
// detached updater (token-authed) during the brief final restart.
(function () {
  var params = new URLSearchParams(window.location.search);
  var token = params.get('token') || '';
  // Persist the token so it survives reloads served by the updater, which serves
  // the same page for every path, sometimes without the token in the URL.
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
    // and would never report reachable. Any /health response means we're back.
    return fetch('/health', { method: 'GET', cache: 'no-store' })
      .then(function (r) { return r.ok; })
      .catch(function () { return false; });
  }

  function poll() {
    fetch('/updates?token=' + encodeURIComponent(token), { cache: 'no-store' })
      .then(function (r) {
        if (!r.ok) { setTimeout(poll, 800); return null; }
        return r.json();
      })
      .then(function (d) {
        if (!d) return;
        render(d.entries || []);
        if (d.terminal) {
          terminalSeen = true;
          if (spEl) spEl.style.display = 'none';
          dashboardReachable().then(function (up) {
            if (up) { finish(); return; }
            setTimeout(poll, 1000);
          });
          return;
        }
        setTimeout(poll, 800);
      })
      .catch(function () {
        // Transient error = the brief restart window; keep polling unless we
        // already saw terminal and the dashboard is back.
        if (terminalSeen) {
          dashboardReachable().then(function (up) {
            if (up) { finish(); return; }
            setTimeout(poll, 1000);
          });
        } else {
          setTimeout(poll, 800);
        }
      });
  }

  poll();
})();
