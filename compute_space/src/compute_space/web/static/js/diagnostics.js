var config = JSON.parse(document.getElementById('page-config').textContent);

var latest = null;

function formatBytes(bytes) {
  if (bytes == null) return '';
  if (bytes >= 1099511627776) return (bytes / 1099511627776).toFixed(1) + ' TiB';
  if (bytes >= 1073741824) return (bytes / 1073741824).toFixed(1) + ' GiB';
  if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + ' MiB';
  if (bytes >= 1024) return (bytes / 1024).toFixed(1) + ' KiB';
  return bytes + ' B';
}

function gitText(git) {
  if (!git || !git.sha) return '(not a git checkout)';
  var branch = git.branch || '(detached HEAD)';
  var dirty = git.dirty ? ' (dirty)' : '';
  return branch + ' @ ' + (git.short_sha || git.sha) + dirty;
}

function renderSummary(data) {
  var sys = data.system || {};
  var rt = data.container_runtime || {};
  var disk = (data.storage && data.storage.disk) || {};
  var rows = [];
  function row(label, value, cls) {
    rows.push(dom.infoRow(label, value, cls));
  }
  function code(text) {
    return dom.el('code', {text: text});
  }
  row('Generated at', data.generated_at);
  row('Zone domain', data.zone_domain);
  row('Cloud in a Bottle version', gitText(data.openhost));
  if (data.openhost && data.openhost.remote_url) {
    row('Cloud in a Bottle remote', code(data.openhost.remote_url));
  }
  row('Host', sys.hostname + ' \u2014 ' + sys.platform);
  row('Kernel', sys.system + ' ' + sys.release + ' (' + sys.machine + ')');
  row('CPU count', sys.cpu_count);
  row('Boot time', sys.boot_time);
  row('Python', sys.python_implementation + ' ' + sys.python_version);
  var rtText = rt.available
    ? ('podman ' + (rt.version || '?') + (rt.rootless === true ? ', rootless' : (rt.rootless === false ? ', ROOTFUL' : '')))
    : ('unavailable' + (rt.error ? ' (' + rt.error + ')' : ''));
  row('Container runtime', rtText, (rt.available && rt.rootless !== false) ? null : 'status-error');
  if (disk.total_bytes != null) {
    row('Disk', formatBytes(disk.free_bytes) + ' free / ' + formatBytes(disk.total_bytes));
  }
  var deps = data.dependencies || {};
  var depNames = Object.keys(deps).sort();
  if (depNames.length) {
    var depCells = [];
    depNames.forEach(function (n, i) {
      if (i) depCells.push(' \u00b7 ');
      depCells.push(code(n), ' ' + deps[n]);
    });
    row('Key dependencies', depCells);
  }

  var rp = data.resource_pressure || {};
  if (rp.memory_total_bytes != null) {
    var memText = formatBytes(rp.memory_total_bytes - (rp.memory_available_bytes || 0))
      + ' / ' + formatBytes(rp.memory_total_bytes)
      + (rp.memory_used_percent != null ? ' (' + rp.memory_used_percent + '%)' : '');
    row('Memory used', memText,
        (rp.memory_used_percent != null && rp.memory_used_percent >= 90) ? 'status-error' : null);
  }
  if (rp.load_avg_1m != null) {
    var loadText = rp.load_avg_1m + ' / ' + rp.load_avg_5m + ' / ' + rp.load_avg_15m
      + (rp.cpu_count != null ? '  (over ' + rp.cpu_count + ' CPUs)' : '');
    row('Load avg (1/5/15m)', loadText,
        (rp.cpu_count && rp.load_avg_1m > rp.cpu_count) ? 'status-error' : null);
  }

  dom.replace(document.getElementById('summary-body'), rows);
}

function healthCell(h) {
  if (!h || !h.checked) return dom.badge('n/a');
  if (h.healthy) return dom.badge('OK' + (h.status_code ? ' ' + h.status_code : ''), 'ok');
  var detail = h.status_code ? String(h.status_code) : (h.error || 'unreachable');
  return dom.badge('Fail', 'error', detail);
}

// An app that isn't running has no reading to show, as opposed to a reading of zero.
function notRunning() {
  return dom.el('span', {class: 'muted', text: '\u2014'});
}

function cpuCell(r) {
  if (!r || !r.running) return notRunning();
  return r.cpu_percent != null ? r.cpu_percent + '%' : '?';
}

function memCell(r) {
  if (!r || !r.running) return notRunning();
  var mem = (r.memory_usage_bytes != null) ? formatBytes(r.memory_usage_bytes) : '?';
  if (r.memory_percent != null) mem += ' (' + r.memory_percent + '%)';
  return mem;
}

// ─── Installed Apps table (sortable) ───
// The apps arrive in the combined diagnostics payload; we keep the last-seen
// list so a header click can re-sort it in place without re-fetching.

var appsData = [];
var appSort = {key: 'name', dir: 1};

function cmp(x, y) { return x < y ? -1 : (x > y ? 1 : 0); }

// The header's data-sort-key is a dotted path into the app (e.g. "resources.cpu_percent"
// or "health.healthy"), resolved generically to a scalar. Only columns with a scalar
// value are marked sortable; Git (an object with no natural order) is left non-sortable.
function appSortValue(a, path) {
  return path.split('.').reduce(function(o, k) { return o == null ? null : o[k]; }, a);
}

function sortApps() {
  // Sort by the active column, then break ties by name so equal rows stay stable.
  appsData.sort(function(a, b) {
    return cmp(appSortValue(a, appSort.key), appSortValue(b, appSort.key)) * appSort.dir
      || cmp(a.name, b.name);
  });
}

function renderApps(data) {
  appsData = (data.apps || []).slice();
  sortApps();
  renderAppRows();
}

function renderAppRows() {
  var body = document.getElementById('apps-body');
  if (!appsData.length) {
    dom.replace(body, dom.el('tr', null, dom.el('td', {colspan: '7', class: 'muted', text: 'No apps installed.'})));
    return;
  }
  dom.replace(body, appsData.map(function(a) {
    var statusVariant = a.status === 'running' ? 'ok'
      : a.status === 'error' ? 'error'
      : (a.status === 'building' || a.status === 'starting' || a.status === 'removing') ? 'warn' : '';
    return dom.el('tr', null, [
      dom.el('td', {text: a.name}),
      dom.el('td', {text: a.version || ''}),
      dom.el('td', null, dom.badge(a.status, statusVariant)),
      dom.el('td', null, healthCell(a.health)),
      dom.el('td', null, cpuCell(a.resources)),
      dom.el('td', null, memCell(a.resources)),
      dom.el('td', {text: gitText(a.git)}),
    ]);
  }));
}

function updateSortIndicators() {
  var ths = document.querySelectorAll('#apps-table th.sortable');
  Array.prototype.forEach.call(ths, function(th) {
    var key = th.getAttribute('data-sort-key');
    var active = appSort.key === key;
    var arrow = active ? (appSort.dir > 0 ? ' ▲' : ' ▼') : '';
    th.textContent = th.getAttribute('data-label') + arrow;
    th.setAttribute('aria-sort', active ? (appSort.dir > 0 ? 'ascending' : 'descending') : 'none');
  });
}

function wireAppSorting() {
  var ths = document.querySelectorAll('#apps-table th.sortable');
  Array.prototype.forEach.call(ths, function(th) {
    th.setAttribute('data-label', th.textContent);
    th.addEventListener('click', function() {
      var key = th.getAttribute('data-sort-key');
      if (appSort.key === key) {
        appSort.dir = -appSort.dir;
      } else {
        appSort.key = key;
        appSort.dir = 1;
      }
      sortApps();
      renderAppRows();
      updateSortIndicators();
    });
  });
  updateSortIndicators();
}

function renderReachability(data) {
  var targets = data.reachability || [];
  var body = document.getElementById('reachability-body');
  if (!targets.length) {
    dom.replace(body, dom.el('tr', null, dom.el('td', {colspan: '4', class: 'muted', text: 'No reachability data.'})));
    return;
  }
  dom.replace(body, targets.map(function(t) {
    var detail = t.reachable ? t.status_code : t.error;
    return dom.el('tr', null, [
      dom.el('td', {text: t.label}),
      dom.el('td', null, dom.el('code', {text: t.url})),
      dom.el('td', null, dom.badge(t.reachable ? 'Yes' : 'No',
                                   t.reachable ? 'ok' : 'error',
                                   detail ? String(detail) : null)),
      dom.el('td', {text: t.latency_ms != null ? t.latency_ms + ' ms' : ''}),
    ]);
  }));
}

function loadDiagnostics() {
  document.getElementById('copy-status').textContent = '';
  fetch(config.diagnosticsUrl, {credentials: 'same-origin'})
    .then(function(r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function(data) {
      latest = data;
      renderSummary(data);
      renderReachability(data);
      renderApps(data);
      document.getElementById('diag-json').textContent = JSON.stringify(data, null, 2);
    })
    .catch(function(e) {
      document.getElementById('diag-json').textContent = 'Failed to load diagnostics: ' + e.message;
    });
}

document.getElementById('copy-btn').addEventListener('click', function() {
  if (!latest) return;
  var text = JSON.stringify(latest, null, 2);
  var status = document.getElementById('copy-status');
  var done = function() { status.textContent = 'Copied.'; };
  var fail = function() { status.textContent = 'Copy failed — select the JSON below manually.'; };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, fail);
  } else {
    // Fallback for non-secure contexts (plain-HTTP dev): copy via a temp textarea.
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy') ? done() : fail(); } catch (e) { fail(); }
    document.body.removeChild(ta);
  }
});

document.getElementById('refresh-btn').addEventListener('click', loadDiagnostics);

wireAppSorting();
loadDiagnostics();
