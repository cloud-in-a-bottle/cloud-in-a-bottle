// Drives the /updating page. Polls /updates for live progress and reloads into
// /settings once the update finishes and the dashboard is back.
//
// /updates is served by TWO processes over the update's lifetime and returns the
// same {entries, terminal} shape from both:
//   - compute_space, while it is UP during the (long) apply phase — owner-authed;
//   - the detached updater, during the brief final restart — token-authed (the
//     token in the URL is what lets it recognize this tab).
// So we just keep polling /updates: transient failures are the restart window,
// and a terminal "done"/"failed" plus a reachable dashboard means we're finished.
(function () {
  var params = new URLSearchParams(window.location.search);
  var token = params.get('token') || '';
  var logEl = document.getElementById('log');
  var spEl = document.getElementById('sp');
  var terminalSeen = false;

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
    window.location.href = '/settings';
  }

  function dashboardReachable() {
    return fetch('/settings', { method: 'HEAD', cache: 'no-store', redirect: 'manual' })
      .then(function (r) {
        return r.ok || r.type === 'opaqueredirect' || (r.status >= 200 && r.status < 400);
      })
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
          // Update is over. Wait for the (possibly restarting) dashboard, then go.
          dashboardReachable().then(function (up) {
            if (up) { finish(); return; }
            setTimeout(poll, 1000);
          });
          return;
        }
        setTimeout(poll, 800);
      })
      .catch(function () {
        // Transient error = the brief restart window (compute_space handing off
        // to/from the updater). If we already saw "terminal" and the dashboard is
        // back, we're done; otherwise keep polling through the blip.
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
