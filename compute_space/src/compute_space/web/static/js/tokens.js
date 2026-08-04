// ─── API Tokens ───

var TOKENS_URL = '/api/tokens';
var SCOPES_URL = '/api/token_scopes';

// Scope catalog, fetched from the server (single source of truth in
// core/auth/scopes.py) so this UI can't drift out of sync with the vocabulary.
// Each entry: {name, description, owner_equivalent}.
var SCOPES = [];

function loadScopeCatalog() {
  return fetch(SCOPES_URL, {credentials: 'same-origin'})
    .then(function(r) { return r.json(); })
    .then(function(scopes) {
      SCOPES = scopes;
      renderScopeCheckboxes();
    });
}

function renderScopeCheckboxes() {
  var container = document.getElementById('token-scopes');
  container.innerHTML = SCOPES.map(function(s) {
    var warn = s.owner_equivalent ? ' <span style="color:#8a4b00;">(owner-equivalent)</span>' : '';
    // Escape even though the catalog is server-hardcoded, for defense-in-depth
    // and consistency with loadTokens() (escape every server value we splice in).
    var name = escapeHtml(s.name);
    return '<label style="display:block; margin: 0.15em 0;">'
      + '<input type="checkbox" class="token-scope" value="' + name + '" onchange="onScopeChange()"> '
      + '<code>' + name + '</code> — ' + escapeHtml(s.description) + warn + '</label>';
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

function _scopeMeta(name) {
  return SCOPES.filter(function(s) { return s.name === name; })[0];
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
    var meta = _scopeMeta(boxes[j].value);
    if (boxes[j].value !== 'owner') {
      boxes[j].disabled = ownerChecked;
      if (ownerChecked) boxes[j].checked = false;
    }
    if (boxes[j].checked && meta && meta.owner_equivalent) anyOwnerEquiv = true;
  }
  document.getElementById('token-scope-warning').style.display = anyOwnerEquiv ? '' : 'none';
}

// HTML-escape a value before interpolating it into innerHTML.  Token names are
// caller-controlled (a tokens:manage token can create a token with an arbitrary
// name), so rendering them raw would be stored XSS that runs in the owner's
// browser session — a privilege-escalation path from a narrowly-scoped token to
// full owner.  Applied to every server value we splice into markup.
function escapeHtml(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, function(c) {
    return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
  });
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
        var expiresDisplay = t.expires_at ? escapeHtml(t.expires_at) : 'Never';
        var scopes = (t.scopes || []).map(function(s) { return '<code>' + escapeHtml(s) + '</code>'; }).join(' ') || '—';
        return '<tr><td' + style + '>' + escapeHtml(t.name) + '</td>'
          + '<td>' + scopes + '</td>'
          + '<td>' + escapeHtml(t.created_at) + '</td>'
          + '<td' + (t.expired ? ' style="color:#c00;"' : '') + '>' + expiresDisplay + '</td>'
          + '<td><button class="btn btn-danger" onclick="deleteToken(\'' + escapeHtml(t.token_id) + '\')">Delete</button></td></tr>';
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

function deleteToken(tokenId) {
  if (!confirm('Delete this token? Any agents using it will lose access.')) return;
  fetch(TOKENS_URL + '/' + encodeURIComponent(tokenId), {method: 'DELETE', credentials: 'same-origin'})
    .then(function() { loadTokens(); });
}

loadScopeCatalog();
loadTokens();
document.getElementById('token-name').value = 'token-' + Math.random().toString(36).slice(2, 8);
