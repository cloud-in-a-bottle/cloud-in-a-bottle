// ─── API Tokens ───

var TOKENS_URL = '/api/tokens';

function deleteTokenButton(t) {
  var label = 'Delete token ' + t.name;
  return dom.el('button', {class: 'icon-btn', type: 'button', title: label, 'aria-label': label,
                           onclick: function() { deleteToken(t.id); }},
    dom.el('img', {class: 'icon', src: '/static/img/icons/trash.svg',
                   width: '14', height: '14', alt: '', 'aria-hidden': 'true'}));
}

function loadTokens() {
  fetch(TOKENS_URL, {credentials: 'same-origin'})
    .then(function(r) { return r.json(); })
    .then(function(tokens) {
      var tbody = document.getElementById('tokens-body');
      var wrap = document.getElementById('tokens-table-wrap');
      var noTokens = document.getElementById('no-tokens');
      if (!tokens.length) { wrap.hidden = true; noTokens.hidden = false; return; }
      wrap.hidden = false;
      noTokens.hidden = true;
      dom.replace(tbody, tokens.map(function(t) {
        return dom.el('tr', {class: t.expired ? 'is-expired' : null}, [
          dom.el('td', {text: t.name}),
          dom.el('td', {text: t.created_at}),
          dom.el('td', null, t.expires_at
            ? (t.expired
                ? dom.el('span', {class: 'status-text status-text--error', text: 'Expired', title: t.expires_at})
                : t.expires_at)
            : dom.el('span', {class: 'status-text status-text--muted', text: 'Never'})),
          dom.el('td', {class: 'col-actions'}, deleteTokenButton(t)),
        ]);
      }));
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
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) { alert(data.error); return; }
      document.getElementById('token-value').textContent = data.token;
      document.getElementById('token-created').hidden = false;
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
