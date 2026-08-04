// ─── Domains ───
// Owner-facing UI over /api/domains: list the hostnames this instance answers on,
// add a secondary domain (the server derives mDNS-vs-public from the name), and
// remove non-primary domains.

var DOMAINS_URL = '/api/domains';

// Self-contained HTML/attribute escape (don't depend on settings.js load order).
function dEsc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function(c) {
    return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
  });
}

function domainCertBadge(d) {
  if (!d.tls) { return '<span class="muted">—</span>'; }
  var color = d.cert_status === 'active' ? '#28a745'
    : d.cert_status === 'acquiring' ? '#d08700'
    : d.cert_status === 'error' ? '#c00' : '#888';
  var label = d.cert_status === 'active' ? 'Active'
    : d.cert_status === 'acquiring' ? 'Acquiring…'
    : d.cert_status === 'error' ? 'Error' : 'None';
  var title = d.error_message ? ' title="' + dEsc(d.error_message) + '"' : '';
  return '<span style="color:' + color + ';"' + title + '>' + label + '</span>';
}

// Repaint the table from a domain list (as returned by GET/POST/DELETE /api/domains).
function renderDomains(domains) {
  var tbody = document.getElementById('domains-body');
  var table = document.getElementById('domains-table');
  var none = document.getElementById('no-domains');
  if (!domains.length) {
    table.style.display = 'none';
    none.style.display = '';
    none.textContent = 'No domains.';
    return;
  }
  table.style.display = '';
  none.style.display = 'none';
  var anyAcquiring = false;
  tbody.innerHTML = domains.map(function(d) {
    if (d.tls && d.cert_status === 'acquiring') { anyAcquiring = true; }
    var name = dEsc(d.name) + (d.is_primary ? ' <span class="muted">(primary)</span>' : '');
    var discovery = d.mdns ? 'mDNS (.local)' : 'Public DNS';
    var actions = d.is_primary
      ? '<span class="muted">—</span>'
      : '<button class="btn btn-danger" onclick="removeDomain(\'' + dEsc(d.name) + '\')">Remove</button>';
    return '<tr><td>' + name + '</td>'
      + '<td>' + dEsc(d.scheme) + '</td>'
      + '<td>' + discovery + '</td>'
      + '<td>' + domainCertBadge(d) + '</td>'
      + '<td>' + actions + '</td></tr>';
  }).join('');
  // A cert acquisition (DNS-01) runs in the background; poll until it settles.
  if (anyAcquiring) { setTimeout(loadDomains, 4000); }
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
  if (!name) { alert('Enter a domain name.'); return; }
  fetch(DOMAINS_URL, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: name}),
  })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data && data.error) { alert(data.error); return; }
      document.getElementById('domain-name').value = '';
      // The server derives mDNS-vs-public from the name (which it lowercases); a public one acquires a cert.
      var lower = name.toLowerCase();
      var added = ((data && data.domains) || []).filter(function(d) { return d.name === lower; })[0];
      if (added && added.tls) {
        msg.textContent = 'Added. Acquiring a TLS certificate in the background — '
          + 'its DNS must be delegated to this instance for acquisition to succeed.';
        msg.className = 'hint';
        msg.style.display = '';
      }
      // Repaint from the POST response (the full list) — no follow-up GET to race the Caddy restart.
      renderDomains((data && data.domains) || []);
    });
}

function removeDomain(name) {
  if (!confirm('Remove ' + name + '? This instance will stop answering on it.')) { return; }
  fetch(DOMAINS_URL + '/' + encodeURIComponent(name), {method: 'DELETE', credentials: 'same-origin'})
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data && data.error) { alert(data.error); return; }
      renderDomains((data && data.domains) || []);
    });
}

loadDomains();
