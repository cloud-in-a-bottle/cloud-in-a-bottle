// ─── Domains ───
// Owner-facing UI over /api/domains: list the hostnames this instance answers on,
// add a secondary public domain (auto-acquires a TLS cert) or a local .local name,
// and remove non-primary domains.

var DOMAINS_URL = '/api/domains';

var CERT_STATUS = {
  active: {label: 'Active', variant: 'ok'},
  acquiring: {label: 'Acquiring', variant: 'warn'},
  error: {label: 'Error', variant: 'error'},
};

function domainCertCell(d) {
  if (!d.tls) return dom.el('span', {class: 'muted', text: '—'});
  var spec = CERT_STATUS[d.cert_status] || {label: 'None', variant: 'muted'};
  return dom.el('span', {
    class: 'status-text status-text--' + spec.variant,
    text: spec.label,
    title: d.error_message || null,
  });
}

function removeDomainButton(name) {
  var label = 'Remove ' + name;
  return dom.el('button', {class: 'icon-btn', type: 'button', title: label, 'aria-label': label,
                           onclick: function() { removeDomain(name); }},
    dom.el('img', {class: 'icon', src: '/static/img/icons/trash.svg',
                   width: '14', height: '14', alt: '', 'aria-hidden': 'true'}));
}

// Repaint the table from a domain list (as returned by GET/POST/DELETE /api/domains).
function renderDomains(domains) {
  var tbody = document.getElementById('domains-body');
  var wrap = document.getElementById('domains-table-wrap');
  var none = document.getElementById('no-domains');
  if (!domains.length) {
    wrap.hidden = true;
    none.hidden = false;
    none.textContent = 'No domains.';
    return;
  }
  wrap.hidden = false;
  none.hidden = true;
  var anyAcquiring = false;
  dom.replace(tbody, domains.map(function(d) {
    if (d.tls && d.cert_status === 'acquiring') anyAcquiring = true;
    return dom.el('tr', null, [
      dom.el('td', null, [
        d.name,
        d.is_primary ? dom.el('span', {class: 'muted', text: ' (primary)'}) : null,
      ]),
      dom.el('td', {text: d.scheme}),
      dom.el('td', {text: d.tls ? 'Public' : 'Local'}),
      dom.el('td', null, domainCertCell(d)),
      dom.el('td', {class: 'col-actions'}, d.is_primary
        ? dom.el('span', {class: 'muted', text: '—'})
        : removeDomainButton(d.name)),
    ]);
  }));
  // A cert acquisition (DNS-01) runs in the background; poll until it settles.
  if (anyAcquiring) setTimeout(loadDomains, 4000);
}

function loadDomains() {
  // no-store so a poll reflects the current set, not a cached copy.
  fetch(DOMAINS_URL, {credentials: 'same-origin', cache: 'no-store'})
    .then(function(r) { return r.json(); })
    .then(function(data) { renderDomains((data && data.domains) || []); });
}

function addDomain() {
  var name = document.getElementById('domain-name').value.trim();
  var msg = document.getElementById('domain-msg');
  if (!name) { return; }
  var type = document.getElementById('domain-type').value;
  var body = {name: name, tls: type === 'public', mdns: type === 'mdns'};
  fetch(DOMAINS_URL, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  })
    .then(readJsonResponse)
    .then(function(res) {
      if (!res.ok) { alert(responseErrorMessage(res.data, 'Failed to add domain.')); return; }
      var data = res.data;
      document.getElementById('domain-name').value = '';
      document.getElementById('add-domain-btn').disabled = true;
      if (body.tls) {
        msg.textContent = 'Added. Acquiring a TLS certificate in the background — '
          + 'its DNS must be delegated to this instance for acquisition to succeed.';
        msg.className = 'notice';
        msg.hidden = false;
      }
      // Repaint from the POST response (the full list) — no follow-up GET to race the Caddy restart.
      renderDomains((data && data.domains) || []);
    });
}

function removeDomain(name) {
  if (!confirm('Remove ' + name + '? This instance will stop answering on it.')) { return; }
  fetch(DOMAINS_URL + '/' + encodeURIComponent(name), {method: 'DELETE', credentials: 'same-origin'})
    .then(readJsonResponse)
    .then(function(res) {
      if (!res.ok) { alert(responseErrorMessage(res.data, 'Failed to remove domain.')); return; }
      renderDomains((res.data && res.data.domains) || []);
    });
}

document.getElementById('domain-name').addEventListener('input', function(e) {
  document.getElementById('add-domain-btn').disabled = e.target.value.trim() === '';
});

loadDomains();
