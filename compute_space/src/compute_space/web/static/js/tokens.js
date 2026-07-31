// ─── API Tokens ───

var TOKENS_URL = '/api/tokens';

// Scope vocabulary, mirroring compute_space/core/auth/scopes.py. `ownerEquiv`
// marks privilege-escalation scopes that trigger the warning. The server is the
// source of truth and re-validates; this list only drives the UI.
var SCOPES = [
  {name: 'owner', desc: 'Full access (all scopes)', ownerEquiv: true},
  {name: 'apps:read', desc: 'List apps, status, diagnostics', ownerEquiv: false},
  {name: 'apps:logs', desc: 'Read app logs (may contain user data)', ownerEquiv: false},
  {name: 'apps:manage', desc: 'Deploy, reload, stop, start, rename apps', ownerEquiv: false},
  {name: 'apps:delete', desc: 'Remove apps', ownerEquiv: false},
  {name: 'system:read', desc: 'Version, ports, storage/ssh status, logs', ownerEquiv: false},
  {name: 'settings:read', desc: 'Read settings, owner username, remote', ownerEquiv: false},
  {name: 'system:admin', desc: 'Toggle SSH, restart router, drop cache', ownerEquiv: true},
  {name: 'settings:write', desc: 'Update settings, change owner password', ownerEquiv: true},
  {name: 'storage:admin', desc: 'Configure archive backend / S3 creds', ownerEquiv: true},
  {name: 'tokens:manage', desc: 'Create/list/delete API tokens', ownerEquiv: true},
  {name: 'permissions:manage', desc: 'Grant/revoke app service permissions (all secrets)', ownerEquiv: true},
  {name: 'identity:approve', desc: 'Sign federated identity tokens as owner', ownerEquiv: true},
];

function renderScopeCheckboxes() {
  var container = document.getElementById('token-scopes');
  container.innerHTML = SCOPES.map(function(s) {
    var warn = s.ownerEquiv ? ' <span style="color:#8a4b00;">(owner-equivalent)</span>' : '';
    return '<label style="display:block; margin: 0.15em 0;">'
      + '<input type="checkbox" class="token-scope" value="' + s.name + '" onchange="onScopeChange()"> '
      + '<code>' + s.name + '</code> — ' + s.desc + warn + '</label>';
  }).join('');
}

function selectedScopes() {
  var boxes = document.querySelectorAll('.token-scope');
  var out = [];
  for (var i = 0; i < boxes.length; i++) {
    if (boxes[i].checked) out.push(boxes[i].value);
  }
  return out;
}

// Checking `owner` means "everything", so disable the rest to avoid confusion.
// Show the privilege-escalation warning whenever any owner-equivalent scope
// (including `owner`) is selected.
function onScopeChange() {
  var boxes = document.querySelectorAll('.token-scope');
  var ownerChecked = false;
  for (var i = 0; i < boxes.length; i++) {
    if (boxes[i].value === 'owner' && boxes[i].checked) ownerChecked = true;
  }
  var anyOwnerEquiv = false;
  for (var j = 0; j < boxes.length; j++) {
    var meta = SCOPES.filter(function(s) { return s.name === boxes[j].value; })[0];
    if (boxes[j].value !== 'owner') {
      boxes[j].disabled = ownerChecked;
      if (ownerChecked) boxes[j].checked = false;
    }
    if (boxes[j].checked && meta && meta.ownerEquiv) anyOwnerEquiv = true;
  }
  document.getElementById('token-scope-warning').style.display = anyOwnerEquiv ? '' : 'none';
}

function loadTokens() {
  fetch(TOKENS_URL, {credentials: 'same-origin'})
    .then(function(r) { return r.json(); })
    .then(function(tokens) {
      var tbody = document.getElementById('tokens-body');
      var table = document.getElementById('tokens-table');
      var noTokens = document.getElementById('no-tokens');
      if (!tokens.length) { table.style.display = 'none'; noTokens.style.display = ''; return; }
      table.style.display = ''; noTokens.style.display = 'none';
      tbody.innerHTML = tokens.map(function(t) {
        var style = t.expired ? ' style="color:#888;text-decoration:line-through;"' : '';
        var expiresDisplay = t.expires_at ? t.expires_at : 'Never';
        var scopes = (t.scopes || []).map(function(s) { return '<code>' + s + '</code>'; }).join(' ') || '—';
        return '<tr><td' + style + '>' + t.name + '</td>'
          + '<td>' + scopes + '</td>'
          + '<td>' + t.created_at + '</td>'
          + '<td' + (t.expired ? ' style="color:#c00;"' : '') + '>' + expiresDisplay + '</td>'
          + '<td><button class="btn btn-danger" onclick="deleteToken(' + t.id + ')">Delete</button></td></tr>';
      }).join('');
    });
}

function createToken() {
  var scopes = selectedScopes();
  if (!scopes.length) {
    alert('Select at least one scope for this token.');
    return;
  }
  var body = {name: document.getElementById('token-name').value, scopes: scopes};
  if (document.getElementById('token-no-expiry').checked) {
    body.expiry_hours = 'never';
  } else {
    body.expiry_hours = document.getElementById('token-expiry').value;
  }
  fetch(TOKENS_URL, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) { alert(data.error); return; }
      document.getElementById('token-value').textContent = data.token;
      document.getElementById('token-created').style.display = '';
      document.getElementById('token-name').value = '';
      loadTokens();
    });
}

function deleteToken(id) {
  if (!confirm('Delete this token? Any agents using it will lose access.')) return;
  fetch(TOKENS_URL + '/' + id, {method: 'DELETE', credentials: 'same-origin'})
    .then(function() { loadTokens(); });
}

renderScopeCheckboxes();
loadTokens();
document.getElementById('token-name').value = 'token-' + Math.random().toString(36).slice(2, 8);
