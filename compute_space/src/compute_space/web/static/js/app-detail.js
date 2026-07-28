var config = JSON.parse(document.getElementById('page-config').textContent);

// ─── Rename ───

function editName() {
  document.getElementById('name-display').style.display = 'none';
  document.getElementById('name-edit').style.display = '';
  document.getElementById('name-input').focus();
  document.getElementById('name-error').textContent = '';
}

function cancelName() {
  document.getElementById('name-edit').style.display = 'none';
  document.getElementById('name-display').style.display = '';
}

function saveName() {
  var input = document.getElementById('name-input');
  var errEl = document.getElementById('name-error');
  fetch(config.renameAppUrl, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: input.value}),
  })
    .then(function(r) { return r.json().then(function(d) { return {ok: r.ok, data: d}; }); })
    .then(function(res) {
      if (!res.ok) { errEl.textContent = res.data.error; return; }
      // The detail URL is keyed by name, so a rename changes it.
      window.location.href = '/app_detail/' + encodeURIComponent(res.data.name);
    });
}

// ─── Edit git upstream ───

function editRemote() {
  document.getElementById('remote-display').style.display = 'none';
  document.getElementById('remote-edit').style.display = '';
  document.getElementById('remote-input').focus();
  document.getElementById('remote-error').textContent = '';
}

function cancelRemote() {
  document.getElementById('remote-edit').style.display = 'none';
  document.getElementById('remote-display').style.display = '';
}

function saveRemote() {
  var input = document.getElementById('remote-input');
  var errEl = document.getElementById('remote-error');
  errEl.textContent = '';
  fetch(config.setAppRemoteUrl, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({repo_url: input.value}),
  })
    .then(function(r) { return r.json().then(function(d) { return {ok: r.ok, data: d}; }); })
    .then(function(res) {
      if (!res.ok || (res.data && res.data.error)) {
        errEl.textContent = (res.data && res.data.error) || 'Failed to save';
        return;
      }
      // Upstream persisted; now pull the new ref and rebuild. Reuses the
      // oauth-aware /reload_app?update flow (it may redirect for github auth).
      appAction(config.reloadAppUrl, {update: true}, {label: 'Updating & reloading'});
    })
    .catch(function() { errEl.textContent = 'Failed to save'; });
}

// ─── App Actions (stop, reload, remove) ───

function setActionsBusy(label) {
  var container = document.getElementById('app-action-buttons');
  if (!container) return null;
  var buttons = container.querySelectorAll('button');
  buttons.forEach(function(b) { b.disabled = true; });
  var msg = document.getElementById('app-action-msg');
  if (msg) {
    msg.style.color = '#d97706';
    msg.textContent = label + '\u2026';
  }
  return function clear(errText) {
    buttons.forEach(function(b) { b.disabled = false; });
    if (msg) {
      if (errText) {
        msg.style.color = '#dc3545';
        msg.textContent = errText;
      } else {
        msg.textContent = '';
      }
    }
  };
}

function appAction(url, data, opts) {
  // opts: { isRemove?: bool, label?: string }. isRemove navigates to
  // /dashboard on success; otherwise location.reload(). label is the
  // text shown next to the action buttons while the request is in flight.
  opts = opts || {};
  var label = opts.label || (opts.isRemove ? 'Removing' : 'Working');
  var clear = setActionsBusy(label);
  fetch(url, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data || {}),
  })
    .then(function(r) { return r.json().then(function(d) { return {ok: r.ok, data: d}; }); })
    .then(function(res) {
      // An update whose pulled manifest differs from the running one is refused
      // until the owner reviews the changes (mirrors install-time approval).
      // Stash the grouped diff and hand off to the full-page review, which
      // re-issues the reload with approve_new_permissions once approved.
      if (res.ok && res.data && res.data.review_required) {
        if (clear) clear();
        try {
          sessionStorage.setItem('openhost.updateReview.' + config.appId, JSON.stringify({
            settings_changed: res.data.settings_changed || [],
            permissions_required: res.data.permissions_required || [],
          }));
        } catch (e) { /* sessionStorage unavailable; review page shows a fallback */ }
        window.location.href = config.updateReviewUrl;
        return;
      }
      if (!res.ok || (res.data && res.data.error)) {
        var msg = (res.data && res.data.error) || 'Request failed';
        if (clear) clear(msg);
        alert(msg);
        return;
      }
      if (opts.isRemove) { window.location.href = '/dashboard'; }
      else { location.reload(); }
    })
    .catch(function() {
      if (clear) clear('Request failed');
      alert('Request failed');
    });
}

// ─── Toast ───

function showToast(message, actions) {
    var existing = document.querySelector('.toast');
    if (existing) existing.remove();
    var toast = document.createElement('div');
    toast.className = 'toast';
    var p = document.createElement('p');
    p.textContent = message;
    toast.appendChild(p);
    var actionsDiv = document.createElement('div');
    actionsDiv.className = 'toast-actions';
    actions.forEach(function(a) {
        var btn = document.createElement('button');
        btn.className = 'btn' + (a.primary ? ' btn-primary' : '');
        btn.textContent = a.label;
        btn.onclick = function() { toast.remove(); a.onClick(); };
        actionsDiv.appendChild(btn);
    });
    toast.appendChild(actionsDiv);
    document.body.appendChild(toast);
    return toast;
}

function clearCacheAndReload() {
    showToast('Clearing build cache...', []);
    fetch(config.dropBuildCacheUrl, {method: 'POST', credentials: 'same-origin'})
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (!data.ok) { alert('Failed to clear cache: ' + (data.error || 'unknown error')); return; }
            appAction(config.reloadAppUrl, null, {label: 'Reloading'});
        })
        .catch(function() { alert('Failed to clear cache'); });
}

// ─── Logs & Status Polling ───

(function() {
    var logEl = document.getElementById('app-logs');
    var statusEl = document.getElementById('app-status');
    var appStatus = config.appStatus;
    var nextUrl = config.nextUrl;
    var toastKey = 'cache-toast-shown-' + config.appStatusUrl;

    function isNearBottom(el) {
        return el.scrollHeight - el.scrollTop - el.clientHeight < 50;
    }

    function updateLog(el, text) {
        var sel = window.getSelection();
        if (sel && !sel.isCollapsed && el.contains(sel.anchorNode)) return;
        var wasAtBottom = isNearBottom(el);
        el.textContent = text || 'No log output available.';
        if (wasAtBottom) el.scrollTop = el.scrollHeight;
    }

    function fetchLogs() {
        fetch(config.appLogsUrl)
            .then(function(r) { return r.text(); })
            .then(function(text) { updateLog(logEl, text); });
    }

    function showCacheCorruptToast() {
        if (sessionStorage.getItem(toastKey)) return;
        sessionStorage.setItem(toastKey, '1');
        showToast(
            'Container build cache is corrupted. Clear it and rebuild?',
            [
                { label: 'Clear Cache & Rebuild', primary: true, onClick: clearCacheAndReload },
                { label: 'Dismiss', primary: false, onClick: function() {} }
            ]
        );
    }

    // While status='removing', disable the action buttons and show
    // "Removing…". Re-enable on transition to 'error' (failed teardown);
    // a successful teardown deletes the row and we redirect via the
    // 404 branch in pollStatus.
    var clearRemovingChrome = null;
    function applyRemovingChrome() {
        if (clearRemovingChrome) return;
        clearRemovingChrome = setActionsBusy('Removing');
    }
    function clearRemovingChromeIfApplied(errText) {
        if (!clearRemovingChrome) return;
        clearRemovingChrome(errText || null);
        clearRemovingChrome = null;
    }

    function pollStatus() {
        fetch(config.appStatusUrl)
            .then(function(r) {
                if (r.status === 404) {
                    window.location.href = '/dashboard';
                    return null;
                }
                return r.json();
            })
            .then(function(data) {
                if (!data) return;
                if (data.status !== appStatus) {
                    appStatus = data.status;
                    statusEl.textContent = appStatus;
                    statusEl.className = 'status-' + appStatus;
                }
                if (appStatus === 'removing') {
                    applyRemovingChrome();
                } else {
                    clearRemovingChromeIfApplied(
                        appStatus === 'error' ? (data.error || 'Removal failed') : null
                    );
                }
                if (appStatus === 'running' && nextUrl) {
                    window.location.href = nextUrl;
                }
                if (appStatus === 'error' && data.error_kind === 'build_cache_corrupt') {
                    showCacheCorruptToast();
                }
            });
    }

    // Check on initial load too (for when you navigate to an already-errored app)
    if (appStatus === 'error') {
        fetch(config.appStatusUrl)
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.error_kind === 'build_cache_corrupt') showCacheCorruptToast();
            });
    }

    // If the page loads with the app already in 'removing', reflect
    // that before the first poll fires.
    if (appStatus === 'removing') {
        applyRemovingChrome();
    }

    logEl.scrollTop = logEl.scrollHeight;

    // 'removing' polls so the page learns when the row vanishes (404).
    if (
        appStatus === 'running' ||
        appStatus === 'starting' ||
        appStatus === 'building' ||
        appStatus === 'removing'
    ) {
        var interval = (appStatus === 'building') ? 1000 : 3000;
        setInterval(fetchLogs, interval);
        setInterval(pollStatus, interval);
    }
})();
