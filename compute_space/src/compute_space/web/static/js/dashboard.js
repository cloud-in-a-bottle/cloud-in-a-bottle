var config = JSON.parse(document.getElementById('page-config').textContent);

// ─── App List ───
//
// Action buttons are rendered server-side by the dashboard template.
// A row's action buttons are disabled while an action is in flight and
// while the app sits in a transient state (building/starting/removing),
// so spamming "Reload" can't fire off overlapping reloads — which used
// to race in the backend and fail with "container name already in use".
// Server-side guards (409 on a reload/stop/remove already in progress)
// still make any stray click a safe no-op.

// App states in which the action buttons must stay disabled: an
// operation is already running, so a second one would be refused.
var TRANSIENT_STATUSES = {building: true, starting: true, removing: true};

function setRowBusy(row, busy) {
  row.querySelectorAll('button').forEach(function(b) { b.disabled = busy; });
}

function refreshApps() {
  if (!config.apiAppsUrl) return;
  fetch(config.apiAppsUrl)
    .then(function(r) { return r.json(); })
    .then(updateApps)
    .catch(function() {});
}

function appAction(appId, action, body) {
  // Disable this row's buttons immediately for snappy feedback; the poll
  // loop (updateApps) then keeps them disabled until the app leaves its
  // transient state. On failure we re-enable right away.
  var row = document.querySelector('tr[data-app-id="' + appId + '"]');
  if (row) setRowBusy(row, true);

  var opts = {method: 'POST', credentials: 'same-origin'};
  if (body) {
    opts.headers = {'Content-Type': 'application/json'};
    opts.body = JSON.stringify(body);
  }
  return fetch(action + '/' + appId, opts)
    .then(function(r) {
      return r.json().then(function(d) { return {ok: r.ok, data: d}; },
                          function() { return {ok: r.ok, data: {}}; });
    })
    .then(function(res) {
      if (!res.ok || (res.data && res.data.error)) {
        alert((res.data && res.data.error) || 'Request failed');
        if (row) setRowBusy(row, false);
      }
      refreshApps();
    })
    .catch(function() {
      alert('Request failed');
      if (row) setRowBusy(row, false);
      refreshApps();
    });
}

function reloadAndUpdate(appId) {
  appAction(appId, 'reload_app', {update: true});
}

function updateApps(apps) {
  // /api/apps now returns a list of {app_id, name, status, error_message}.
  var byId = {};
  apps.forEach(function(a) { byId[a.app_id] = a; });
  document.querySelectorAll('tr[data-app-id]').forEach(function(row) {
    var appId = row.getAttribute('data-app-id');
    var info = byId[appId];
    if (!info) {
      row.style.display = 'none';
      return;
    }
    row.style.display = '';
    var statusEl = row.querySelector('.app-status');
    statusEl.className = 'app-status status-' + info.status;
    statusEl.textContent = info.status;
    // Keep the action buttons disabled while an operation is in flight
    // (the app is in a transient state), re-enabling once it settles.
    setRowBusy(row, !!TRANSIENT_STATUSES[info.status]);
  });
}

if (config.apiAppsUrl) {
  refreshApps();
  setInterval(refreshApps, 3000);
}
