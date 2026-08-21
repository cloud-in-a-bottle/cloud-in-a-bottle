// Settings ▸ Services: let the owner choose the default provider app for each
// registered service. Backend: GET /api/services/v2 (providers),
// GET/POST/DELETE /api/services/v2/defaults.

// Escape for both text and double-quoted attribute contexts (service_url and
// app_id are interpolated into data-service="…" / value="…").
function escServiceHtml(s) {
  return (s == null ? '' : String(s))
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Group provider rows by service_url, then collapse multiple versions of the
// same app into one option (defaults are keyed by app_id, not version).
function groupProviders(providers) {
  var byService = {};
  providers.forEach(function(p) {
    var svc = byService[p.service_url] || (byService[p.service_url] = {});
    var app = svc[p.app_id] || (svc[p.app_id] = {app_id: p.app_id, app_name: p.app_name, versions: []});
    if (p.service_version && app.versions.indexOf(p.service_version) === -1) {
      app.versions.push(p.service_version);
    }
  });
  return byService;
}

function renderServices(providers, defaults) {
  var el = document.getElementById('services-status');
  if (!el) return;

  var byService = groupProviders(providers);
  var defaultByService = {};
  defaults.forEach(function(d) { defaultByService[d.service_url] = d.app_id; });

  var serviceUrls = Object.keys(byService).sort();
  if (serviceUrls.length === 0) {
    el.innerHTML = '<p class="muted">No services are registered. Services appear here once an installed '
      + 'app declares one in its manifest.</p>';
    return;
  }

  var rows = '';
  serviceUrls.forEach(function(svc) {
    var apps = byService[svc];
    var current = defaultByService[svc] || '';
    var options = '<option value="">(no default — use highest version)</option>';
    Object.keys(apps).sort().forEach(function(appId) {
      var app = apps[appId];
      var label = app.app_name;
      if (app.versions.length) label += ' (' + app.versions.sort().join(', ') + ')';
      var selected = appId === current ? ' selected' : '';
      options += '<option value="' + escServiceHtml(appId) + '"' + selected + '>' + escServiceHtml(label) + '</option>';
    });
    rows += '<tr>'
      + '<td><code>' + escServiceHtml(svc) + '</code></td>'
      + '<td><select data-service="' + escServiceHtml(svc) + '">' + options + '</select></td>'
      + '<td style="white-space:nowrap;">'
      + '<button class="btn btn-primary" onclick="saveDefaultProvider(this)">Save</button> '
      + '<span class="muted default-msg"></span>'
      + '</td>'
      + '</tr>';
  });

  el.innerHTML = '<table>'
    + '<thead><tr><th>Service</th><th>Default provider</th><th></th></tr></thead>'
    + '<tbody>' + rows + '</tbody></table>';
}

function saveDefaultProvider(btn) {
  var row = btn.closest('tr');
  var select = row.querySelector('select[data-service]');
  var msg = row.querySelector('.default-msg');
  var serviceUrl = select.getAttribute('data-service');
  var appId = select.value;
  btn.disabled = true;
  msg.textContent = 'Saving…';

  var request = appId
    ? fetch('/api/services/v2/defaults', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({service_url: serviceUrl, app_id: appId}),
      })
    : fetch('/api/services/v2/defaults', {
        method: 'DELETE',
        credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({service_url: serviceUrl}),
      });

  request
    .then(function(r) {
      if (!r.ok) return r.text().then(function(t) { throw new Error(t || ('HTTP ' + r.status)); });
      msg.textContent = 'Saved.';
      setTimeout(function() { msg.textContent = ''; }, 2000);
    })
    .catch(function(e) { msg.textContent = 'Failed: ' + e.message; })
    .finally(function() { btn.disabled = false; });
}

function loadServices() {
  var el = document.getElementById('services-status');
  Promise.all([
    fetch('/api/services/v2', {credentials: 'same-origin'}).then(function(r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }),
    fetch('/api/services/v2/defaults', {credentials: 'same-origin'}).then(function(r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }),
  ])
    .then(function(results) { renderServices(results[0], results[1]); })
    .catch(function(err) {
      if (el) el.innerHTML = '<p class="error"><strong>Services unavailable.</strong> ' + escServiceHtml(err) + '</p>';
    });
}

loadServices();
