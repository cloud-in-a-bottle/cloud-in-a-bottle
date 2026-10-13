// ─── Domain root ───
// Owner-facing UI over /api/settings/root-app: choose what the bare domain serves (the dashboard, which is the
// default, nothing, or an app) and, when it isn't the dashboard, the subdomain the dashboard lives on.

var ROOT_APP_URL = '/api/settings/root-app';
var APP_VALUE_PREFIX = 'app:';

function renderRootApp(data) {
  var select = document.getElementById('root-kind-select');
  var options = [
    dom.el('option', {value: 'dashboard', text: 'Dashboard and API (default)'}),
    dom.el('option', {value: 'nothing', text: 'Nothing'}),
  ];
  data.apps.forEach(function(app) {
    options.push(dom.el('option', {value: APP_VALUE_PREFIX + app.app_id, text: 'App: ' + app.name}));
  });
  dom.replace(select, options);
  select.value = data.kind === 'app' ? APP_VALUE_PREFIX + data.app_id : data.kind;
  select.disabled = false;
  document.getElementById('router-subdomain-input').value = data.router_subdomain;
  document.getElementById('root-app-save-btn').disabled = false;
  // The dashboard's current subdomain, or '' while it's at the bare domain.
  select.dataset.savedRouterLabel = data.kind === 'dashboard' ? '' : data.router_subdomain;
  updateRootAppForm();
}

function updateRootAppForm() {
  var dashboardAtRoot = document.getElementById('root-kind-select').value === 'dashboard';
  document.getElementById('router-subdomain-field').hidden = dashboardAtRoot;
  document.getElementById('root-app-warning').hidden = dashboardAtRoot;
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

// The dashboard moved, so the instance is restarting. Wait for the router at its new address to come back as a
// new process, then go there.
function waitForMovedDashboard(routerUrl, previousGeneration) {
  var target = routerUrl + '/settings';
  var deadline = Date.now() + 180000;
  showRootAppMsg('Moving the dashboard to ' + routerUrl + '. Waiting for the instance to restart…', false);

  function poll() {
    fetch(routerUrl + '/health', {cache: 'no-store'})
      .then(function(r) { return r.ok ? r.json() : null; })
      .then(function(health) {
        if (health && health.generation && health.generation !== previousGeneration) {
          window.location.assign(target);
          return;
        }
        retry();
      })
      .catch(retry);
  }

  function retry() {
    if (Date.now() >= deadline) {
      showRootAppMsg('The restart did not complete. Restart the instance manually, then open ' + target, true);
      return;
    }
    setTimeout(poll, 500);
  }

  poll();
}

function saveRootApp() {
  var value = document.getElementById('root-kind-select').value;
  var isApp = value.indexOf(APP_VALUE_PREFIX) === 0;
  var body = {
    kind: isApp ? 'app' : value,
    app_id: isApp ? value.slice(APP_VALUE_PREFIX.length) : null,
    router_subdomain: document.getElementById('router-subdomain-input').value.trim(),
  };
  var newRouterLabel = body.kind === 'dashboard' ? '' : body.router_subdomain;
  if (newRouterLabel !== document.getElementById('root-kind-select').dataset.savedRouterLabel) {
    var where = newRouterLabel ? 'the ' + newRouterLabel + ' subdomain' : 'the bare domain';
    if (!confirm('Move the dashboard to ' + where + '? This instance and its running apps will restart.')) return;
  }
  var btn = document.getElementById('root-app-save-btn');
  btn.disabled = true;
  fetch(ROOT_APP_URL, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  })
    .then(readJsonResponse)
    .then(function(res) {
      if (!res.ok) {
        showRootAppMsg(responseErrorMessage(res.data, 'Failed to save the domain root.'), true);
        btn.disabled = false;
        return;
      }
      if (res.data.restarting) {
        waitForMovedDashboard(res.data.router_url, res.data.generation);
        return;
      }
      renderRootApp(res.data);
      showRootAppMsg('Saved.', false);
      btn.disabled = false;
    });
}

loadRootApp();
