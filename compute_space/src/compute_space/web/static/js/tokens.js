// ─── API Tokens ───

var TOKENS_URL = '/api/tokens';

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
        return '<tr><td' + style + '>' + t.name + '</td>'
          + '<td>' + t.created_at + '</td>'
          + '<td' + (t.expired ? ' style="color:#c00;"' : '') + '>' + expiresDisplay + '</td>'
          + '<td><button class="btn btn-danger" onclick="deleteToken(' + t.id + ')">Delete</button></td></tr>';
      }).join('');
    });
}

function createToken() {
  var body = {name: document.getElementById('token-name').value};
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
    .then(readJsonResponse)
    .then(function(res) {
      if (!res.ok) { alert(responseErrorMessage(res.data, 'Failed to create token')); return; }
      var data = res.data;
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

loadTokens();
document.getElementById('token-name').value = 'token-' + Math.random().toString(36).slice(2, 8);
