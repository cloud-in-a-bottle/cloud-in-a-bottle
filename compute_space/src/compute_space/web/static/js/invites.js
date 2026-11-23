// ─── Invites ───

var INVITES_URL = '/api/invites';

function memberList(invite) {
  if (!invite.members.length) return dom.muted('None', 'span');
  return invite.members.map(function(m) {
    return dom.el('div', null, [
      m.username + ' ',
      dom.iconButton('trash', 'Delete member ' + m.username, function() { deleteMember(m); }),
    ]);
  });
}

function loadInvites() {
  fetch(INVITES_URL, {credentials: 'same-origin'})
    .then(function(r) { return r.json(); })
    .then(function(invites) {
      document.getElementById('invites-table-wrap').hidden = !invites.length;
      document.getElementById('no-invites').hidden = !!invites.length;
      dom.replace(document.getElementById('invites-body'), invites.map(function(inv) {
        var open = inv.uses_left > 0 && !inv.expired;
        return dom.el('tr', {class: open ? null : 'is-expired'}, [
          dom.el('td', {text: inv.name}),
          dom.el('td', {text: inv.apps.join(', ')}),
          dom.el('td', {text: inv.uses_left}),
          dom.el('td', null, inv.expired ? dom.el('span', {class: 'msg msg--error', text: 'Expired'}) : inv.expires_at),
          dom.el('td', null, memberList(inv)),
          dom.el('td', {class: 'col-actions'}, [
            open ? dom.el('button', {class: 'btn', type: 'button', text: 'Close', title: 'Stop new signups',
                                     onclick: function() { closeInvite(inv); }}) : null,
            dom.iconButton('trash', 'Delete invite ' + inv.name + ' and its members', function() { deleteInvite(inv); }),
          ]),
        ]);
      }));
    });
}

function createInvite() {
  var appIds = Array.prototype.map.call(
    document.querySelectorAll('input[name="invite-app"]:checked'), function(el) { return el.value; });
  fetch(INVITES_URL, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      name: document.getElementById('invite-name').value,
      app_ids: appIds,
      max_uses: parseInt(document.getElementById('invite-max-uses').value, 10),
      expiry_hours: parseFloat(document.getElementById('invite-expiry').value),
    }),
  })
    .then(readJsonResponse)
    .then(function(res) {
      if (!res.ok) { alert(responseErrorMessage(res.data, 'Failed to create invite')); return; }
      document.getElementById('invite-url').textContent = res.data.url;
      document.getElementById('invite-created').hidden = false;
      loadInvites();
    });
}

function closeInvite(inv) {
  if (!confirm('Stop new signups through ' + inv.name + '? Its members keep access.')) return;
  fetch(INVITES_URL + '/' + inv.id + '/close', {method: 'POST', credentials: 'same-origin'}).then(loadInvites);
}

function deleteInvite(inv) {
  if (!confirm('Delete ' + inv.name + ' and all ' + inv.members.length + ' of its members?')) return;
  fetch(INVITES_URL + '/' + inv.id, {method: 'DELETE', credentials: 'same-origin'}).then(loadInvites);
}

function deleteMember(m) {
  if (!confirm('Delete member ' + m.username + '?')) return;
  fetch('/api/members/' + m.user_id, {method: 'DELETE', credentials: 'same-origin'}).then(loadInvites);
}

loadInvites();
