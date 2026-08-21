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
      if (!res.ok) { errEl.textContent = responseErrorMessage(res.data, 'Failed to rename app'); return; }
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
      if (!res.ok) {
        errEl.textContent = responseErrorMessage(res.data, 'Failed to save');
        return;
      }
      // Reuse the oauth-aware update flow, which may redirect for github auth.
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
  // opts: { isRemove?: bool, label?: string }. isRemove navigates to /dashboard
  // on success rather than reloading the (now-gone) detail page.
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
      // Hand off to full-page review when manifest changes require approval;
      // re-issues reload with approve_new_permissions once approved.
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
      if (!res.ok) {
        var msg = responseErrorMessage(res.data, 'Request failed');
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
        .then(readJsonResponse)
        .then(function(res) {
            if (!res.ok) {
                var data = res.data;
                alert('Failed to clear cache: ' + responseErrorMessage(data, 'unknown error'));
                return;
            }
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

    // The container the stream follows; when it changes we reset to the new logs.
    var streamContainerId = null;

    var MAX_LOG_CHARS = 2 * 1024 * 1024;
    var logPrimed = false;
    // Set when a (re)connect's first message arrives; the next flush replaces the pane's
    // contents in one paint rather than blanking it first (which flashed the scroll to top).
    var needsReset = false;

    // Buffer incoming lines and flush once per animation frame: the backlog arrives
    // one WebSocket message per line, so batching avoids reflowing the <pre> each line.
    var pending = [];
    var pendingLen = 0;
    var flushQueued = false;
    var logLen = 0;

    function isNearBottom(el) {
        return el.scrollHeight - el.scrollTop - el.clientHeight < 50;
    }

    function hasSelectionIn(el) {
        var sel = window.getSelection();
        return !!(sel && !sel.isCollapsed && el.contains(sel.anchorNode));
    }

    function flushLog() {
        flushQueued = false;
        if (!pending.length) return;
        var wasAtBottom = isNearBottom(logEl);
        var text = pending.join('');
        pending = [];
        pendingLen = 0;
        if (needsReset) {
            // Replace the stale contents in the same flush that refills them, so the
            // emptied <pre> never paints on its own and jerks the scroll to the top.
            needsReset = false;
            logEl.textContent = text;
            logLen = text.length;
        } else {
            // A text node leaves existing content and any active selection intact.
            logEl.appendChild(document.createTextNode(text));
            logLen += text.length;
        }
        if (logLen > MAX_LOG_CHARS && !hasSelectionIn(logEl)) {
            var trimmed = logEl.textContent.slice(-MAX_LOG_CHARS);
            logEl.textContent = trimmed;
            logLen = trimmed.length;
        }
        if (wasAtBottom) logEl.scrollTop = logEl.scrollHeight;
    }

    function appendLog(text) {
        if (!text) return;
        pending.push(text);
        pendingLen += text.length;
        // If frames are paused (backgrounded tab) drop all but the most recent MAX_LOG_CHARS.
        while (pending.length > 1 && pendingLen - pending[0].length > MAX_LOG_CHARS) {
            pendingLen -= pending.shift().length;
        }
        if (!flushQueued) {
            flushQueued = true;
            requestAnimationFrame(flushLog);
        }
    }

    // Tracked so a reset can tell the old socket's onclose not to reconnect.
    var currentWs = null;

    function startLogStream() {
        var proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        var ws = new WebSocket(proto + '//' + window.location.host + config.appLogsStreamUrl);
        currentWs = ws;
        ws.onmessage = function(e) {
            if (ws !== currentWs) return;  // superseded by a reset; don't consume the reprime
            // First message of a (re)connect replays the tail: drop anything buffered from
            // the old socket and let the next flush swap in the fresh contents atomically.
            if (!logPrimed) {
                logPrimed = true;
                needsReset = true;
                pending = []; pendingLen = 0;
            }
            appendLog(e.data + '\n');
        };
        ws.onclose = function() {
            if (ws !== currentWs) return;  // superseded by a reset
            // The server holds the socket open past end-of-log, so a close is a real
            // drop — reconnect (which re-primes to replay the tail).
            logPrimed = false;
            setTimeout(startLogStream, 2000);
        };
        ws.onerror = function() { ws.close(); };
    }

    function resetLogStream() {
        var old = currentWs;
        logPrimed = false;
        startLogStream();  // reassigns currentWs
        if (old && old !== currentWs) old.close();
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

    // Disables the action buttons while removing; re-enabled only if teardown
    // fails (status 'error'). A successful teardown 404s and redirects instead.
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

    function handleStatus(data) {
        if (!data) return;
        if (data.status !== appStatus) {
            appStatus = data.status;
            statusEl.textContent = appStatus;
            statusEl.className = 'status-' + appStatus;
        }
        // Adopt the first container_id silently; reset only on a later change.
        if (data.container_id && data.container_id !== streamContainerId) {
            if (streamContainerId !== null) resetLogStream();
            streamContainerId = data.container_id;
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
        var errorRow = document.getElementById('app-error-row');
        var errorCell = document.getElementById('app-error-cell');
        if (errorRow && errorCell) {
            if (appStatus === 'error' && data.error) {
                errorCell.textContent = data.error;
                errorRow.style.display = '';
            } else {
                errorRow.style.display = 'none';
                errorCell.textContent = '';
            }
        }
    }

    function startStatusStream() {
        var proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        var ws = new WebSocket(proto + '//' + window.location.host + config.appStatusStreamUrl);
        ws.onmessage = function(e) { handleStatus(JSON.parse(e.data)); };
        ws.onclose = function(e) {
            // 4404 means the app row is gone (removed); anything else is a drop — reconnect.
            if (e.code === 4404) { window.location.href = '/dashboard'; return; }
            setTimeout(startStatusStream, 2000);
        };
        ws.onerror = function() { ws.close(); };
    }

    if (appStatus === 'removing') {
        applyRemovingChrome();
    }

    logEl.scrollTop = logEl.scrollHeight;

    // Stream regardless of status: a stopped/errored app still has a build log to replay.
    startLogStream();

    // Stream status unconditionally so runtime errors and initial state changes
    // sync immediately without waiting for a page reload.
    startStatusStream();
})();
