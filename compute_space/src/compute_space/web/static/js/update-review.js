// Full-page review of the settings an update changes. The diff is produced by
// the reload gate (POST /reload_app) and stashed in sessionStorage by
// app-detail.js under 'openhost.updateReview.<appId>'; this page renders it and,
// on approval, re-issues the reload with approve_new_permissions.

var config = JSON.parse(document.getElementById('page-config').textContent);
var storageKey = 'openhost.updateReview.' + config.appId;

function esc(s) {
  return (s == null ? '' : String(s))
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function show(id) { document.getElementById(id).style.display = ''; }
function hide(id) { document.getElementById(id).style.display = 'none'; }

function loadReview() {
  var raw = null;
  try { raw = sessionStorage.getItem(storageKey); } catch (e) { raw = null; }
  if (!raw) { show('no-review'); return null; }
  try { return JSON.parse(raw); } catch (e) { show('no-review'); return null; }
}

function renderSettings(changes) {
  if (!changes.length) return;
  var order = [];
  var byGroup = {};
  changes.forEach(function(c) {
    if (!byGroup[c.group]) { byGroup[c.group] = []; order.push(c.group); }
    byGroup[c.group].push(c);
  });
  var rows = '<tr><th>Setting</th><th>Current</th><th>After update</th></tr>';
  order.forEach(function(group) {
    rows += '<tr><th colspan="3" style="background:#f0f3f8;">' + esc(group) + '</th></tr>';
    byGroup[group].forEach(function(c) {
      rows += '<tr><td>' + esc(c.label) + '</td>'
        + '<td><code>' + esc(c.old) + '</code></td>'
        + '<td><code>' + esc(c.new) + '</code></td></tr>';
    });
  });
  document.getElementById('settings-table').innerHTML = rows;
  show('settings-section');
}

function renderPermissions(perms) {
  if (!perms.length) return;
  var rows = '<tr><th>Service</th><th>Grant</th></tr>';
  perms.forEach(function(p) {
    var svc = p.shortname ? (esc(p.shortname) + ' <span class="muted">(' + esc(p.service_url) + ')</span>') : esc(p.service_url);
    rows += '<tr><td>' + svc + '</td>'
      + '<td><code>' + esc(JSON.stringify(p.grant)) + '</code></td></tr>';
  });
  document.getElementById('permissions-table').innerHTML = rows;
  show('permissions-section');
}

function approveUpdate() {
  var btn = document.getElementById('approve-btn');
  var msg = document.getElementById('review-msg');
  btn.disabled = true;
  msg.textContent = 'Updating & reloading…';
  fetch(config.reloadAppUrl, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({update: true, approve_new_permissions: true}),
  })
    .then(function(r) { return r.json().then(function(d) { return {ok: r.ok, data: d}; }); })
    .then(function(res) {
      if (!res.ok || (res.data && res.data.error)) {
        var err = (res.data && res.data.error) || 'Update failed';
        document.getElementById('review-error').textContent = err;
        show('review-error');
        msg.textContent = '';
        btn.disabled = false;
        return;
      }
      clearReview();
      window.location.href = config.appDetailUrl;
    })
    .catch(function() {
      document.getElementById('review-error').textContent = 'Update failed';
      show('review-error');
      msg.textContent = '';
      btn.disabled = false;
    });
}

function clearReview() {
  try { sessionStorage.removeItem(storageKey); } catch (e) { /* ignore */ }
}

function cancelReview() {
  clearReview();
  window.location.href = config.appDetailUrl;
}

(function() {
  var review = loadReview();
  if (!review) return;
  renderSettings(review.settings_changed || []);
  renderPermissions(review.permissions_required || []);
  show('review-body');
})();
