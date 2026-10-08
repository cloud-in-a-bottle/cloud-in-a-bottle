// ─── Domain root ───
// Owner-facing UI over /api/settings/root-app: choose the app served at the bare domain, or the
// dashboard (the default).

var ROOT_APP_URL = '/api/settings/root-app';

function renderRootApp(data) {
  var select = document.getElementById('root-app-select');
  var options = [dom.el('option', {value: '', text: 'Dashboard (default)'})];
  data.apps.forEach(function(app) {
    options.push(dom.el('option', {value: app.app_id, text: app.name}));
  });
  dom.replace(select, options);
  select.value = data.app_id || '';
  select.disabled = false;
  document.getElementById('root-app-save-btn').disabled = false;
}

function showRootAppMsg(text, isError) {
  var msg = document.getElementById('root-app-msg');
  msg.textContent = text;
  msg.className = isError ? 'notice notice--error' : 'msg';
  msg.hidden = false;
}

function loadRootApp() {
  fetch(ROOT_APP_URL, {credentials: 'same-origin', cache: 'no-store'})
    .then(readJsonResponse)
    .then(function(res) {
      if (!res.ok) { showRootAppMsg(responseErrorMessage(res.data, 'Failed to load the domain root.'), true); return; }
      renderRootApp(res.data);
    });
}

function saveRootApp() {
  var select = document.getElementById('root-app-select');
  var appId = select.value || null;
  if (appId !== null) {
    var name = select.options[select.selectedIndex].textContent;
    if (!confirm('Serve ' + name + ' at the bare domain? The dashboard will only be at the bottle. subdomain.')) {
      return;
    }
  }
  var btn = document.getElementById('root-app-save-btn');
  btn.disabled = true;
  fetch(ROOT_APP_URL, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({app_id: appId}),
  })
    .then(readJsonResponse)
    .then(function(res) {
      if (!res.ok) { showRootAppMsg(responseErrorMessage(res.data, 'Failed to save the domain root.'), true); return; }
      // This page may be on the bare domain, which now serves the app: move to the router subdomain.
      if (res.data.app_id !== null && window.location.origin !== res.data.router_url) {
        window.location.assign(res.data.router_url + '/settings');
        return;
      }
      renderRootApp(res.data);
      showRootAppMsg('Saved.', false);
    })
    .finally(function() { btn.disabled = false; });
}

loadRootApp();
