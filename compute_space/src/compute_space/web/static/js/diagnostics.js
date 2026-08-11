var config = JSON.parse(document.getElementById('page-config').textContent);

var latest = null;

function escHtml(s) {
  var d = document.createElement('div');
  d.textContent = (s == null) ? '' : String(s);
  return d.innerHTML;
}

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
  var rows = '';
  function row(label, value) {
    rows += '<tr><th class="label-col">' + escHtml(label) + '</th><td>' + value + '</td></tr>';
  }
  row('Generated at', escHtml(data.generated_at));
  row('Zone domain', escHtml(data.zone_domain));
  row('OpenHost version', escHtml(gitText(data.openhost)));
  if (data.openhost && data.openhost.remote_url) {
    row('OpenHost remote', '<code>' + escHtml(data.openhost.remote_url) + '</code>');
  }
  row('Host', escHtml(sys.hostname) + ' — ' + escHtml(sys.platform));
  row('Kernel', escHtml(sys.system) + ' ' + escHtml(sys.release) + ' (' + escHtml(sys.machine) + ')');
  row('CPU count', escHtml(sys.cpu_count));
  row('Boot time', escHtml(sys.boot_time));
  row('Python', escHtml(sys.python_implementation) + ' ' + escHtml(sys.python_version));
  var rtText = rt.available
    ? ('podman ' + escHtml(rt.version || '?') + (rt.rootless === true ? ', rootless' : (rt.rootless === false ? ', ROOTFUL' : '')))
    : ('unavailable' + (rt.error ? ' (' + escHtml(rt.error) + ')' : ''));
  var rtCls = (rt.available && rt.rootless !== false) ? '' : ' class="status-error"';
  rows += '<tr><th class="label-col">Container runtime</th><td' + rtCls + '>' + rtText + '</td></tr>';
  if (disk.total_bytes != null) {
    row('Disk', formatBytes(disk.free_bytes) + ' free / ' + formatBytes(disk.total_bytes));
  }
  var deps = data.dependencies || {};
  var depNames = Object.keys(deps).sort();
  if (depNames.length) {
    var depHtml = depNames.map(function(n) {
      return '<code>' + escHtml(n) + '</code> ' + escHtml(deps[n]);
    }).join(' &middot; ');
    row('Key dependencies', depHtml);
  }

  var rp = data.resource_pressure || {};
  if (rp.memory_total_bytes != null) {
    var memText = formatBytes(rp.memory_total_bytes - (rp.memory_available_bytes || 0))
      + ' / ' + formatBytes(rp.memory_total_bytes)
      + (rp.memory_used_percent != null ? ' (' + rp.memory_used_percent + '%)' : '');
    var memCls = (rp.memory_used_percent != null && rp.memory_used_percent >= 90) ? ' class="status-error"' : '';
    rows += '<tr><th class="label-col">Memory used</th><td' + memCls + '>' + escHtml(memText) + '</td></tr>';
  }
  if (rp.load_avg_1m != null) {
    var loadText = rp.load_avg_1m + ' / ' + rp.load_avg_5m + ' / ' + rp.load_avg_15m
      + (rp.cpu_count != null ? '  (over ' + rp.cpu_count + ' CPUs)' : '');
    var loadCls = (rp.cpu_count && rp.load_avg_1m > rp.cpu_count) ? ' class="status-error"' : '';
    rows += '<tr><th class="label-col">Load avg (1/5/15m)</th><td' + loadCls + '>' + escHtml(loadText) + '</td></tr>';
  }

  document.getElementById('summary-body').innerHTML = rows;
}

function healthCell(h) {
  if (!h || !h.checked) return '<span class="muted">n/a</span>';
  if (h.healthy) return '<span class="status-running">OK' + (h.status_code ? ' (' + escHtml(h.status_code) + ')' : '') + '</span>';
  var detail = h.status_code ? String(h.status_code) : (h.error || 'unreachable');
  return '<span class="status-error">FAIL (' + escHtml(detail) + ')</span>';
}

function resourceCell(r) {
  if (!r || !r.running) return '<span class="muted">not running</span>';
  var cpu = (r.cpu_percent != null) ? (r.cpu_percent + '%') : '?';
  var mem = (r.memory_usage_bytes != null) ? formatBytes(r.memory_usage_bytes) : '?';
  if (r.memory_percent != null) mem += ' (' + r.memory_percent + '%)';
  return escHtml(cpu + ' cpu, ' + mem);
}

function renderApps(data) {
  var apps = data.apps || [];
  var body = document.getElementById('apps-body');
  if (!apps.length) {
    body.innerHTML = '<tr><td colspan="6" class="muted">No apps installed.</td></tr>';
    return;
  }
  body.innerHTML = apps.map(function(a) {
    var statusCls = a.status === 'running' ? 'status-running' : (a.status === 'error' ? 'status-error' : 'status-stopped');
    return '<tr><td>' + escHtml(a.name) + '</td>'
      + '<td>' + escHtml(a.version || '') + '</td>'
      + '<td class="' + statusCls + '">' + escHtml(a.status) + '</td>'
      + '<td>' + healthCell(a.health) + '</td>'
      + '<td>' + resourceCell(a.resources) + '</td>'
      + '<td>' + escHtml(gitText(a.git)) + '</td></tr>';
  }).join('');
}

function renderReachability(data) {
  var targets = data.reachability || [];
  var body = document.getElementById('reachability-body');
  if (!targets.length) {
    body.innerHTML = '<tr><td colspan="4" class="muted">No reachability data.</td></tr>';
    return;
  }
  body.innerHTML = targets.map(function(t) {
    var cls = t.reachable ? 'status-running' : 'status-error';
    var label = t.reachable ? ('yes' + (t.status_code ? ' (' + escHtml(t.status_code) + ')' : '')) : ('no' + (t.error ? ' (' + escHtml(t.error) + ')' : ''));
    var latency = (t.latency_ms != null) ? (t.latency_ms + ' ms') : '';
    return '<tr><td>' + escHtml(t.label) + '</td>'
      + '<td><code>' + escHtml(t.url) + '</code></td>'
      + '<td class="' + cls + '">' + label + '</td>'
      + '<td>' + escHtml(latency) + '</td></tr>';
  }).join('');
}

function setRegionError(bodyId, colspan, msg) {
  document.getElementById(bodyId).innerHTML =
    '<tr><td colspan="' + colspan + '" class="status-error">' + escHtml(msg) + '</td></tr>';
}

function fetchSection(url) {
  return fetch(url, {credentials: 'same-origin'}).then(function(r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  });
}

function updateRawJson() {
  document.getElementById('diag-json').textContent = JSON.stringify(latest, null, 2);
}

// Fetch each section independently and render it the moment it arrives, so the
// fast host/system info paints immediately instead of waiting on the slow
// storage / reachability / per-app probes. The section responses are merged
// into `latest` so Copy / Raw JSON reflect the same combined shape the
// /api/diagnostics endpoint (used by the Download button) returns.
function loadDiagnostics() {
  document.getElementById('copy-status').textContent = '';
  var urls = config.sectionUrls;
  latest = {};

  // System (fast): host/OS/deps/runtime — also scaffolds the summary table.
  fetchSection(urls.system).then(function(sys) {
    Object.keys(sys).forEach(function(k) { latest[k] = sys[k]; });
    renderSummary(latest);
    updateRawJson();
  }).catch(function(e) {
    setRegionError('summary-body', 2, 'Failed to load system info: ' + e.message);
  });

  // Storage (medium): fills the Disk row into the summary once it lands.
  fetchSection(urls.storage).then(function(storage) {
    latest.storage = storage;
    renderSummary(latest);
    updateRawJson();
  }).catch(function() {
    // Non-fatal: the summary still renders without the disk row.
  });

  // Reachability (slow, external hosts).
  fetchSection(urls.reachability).then(function(reach) {
    latest.reachability = reach;
    renderReachability(latest);
    updateRawJson();
  }).catch(function(e) {
    setRegionError('reachability-body', 4, 'Failed to load reachability: ' + e.message);
  });

  // Apps (per-app git/health/stats probes).
  fetchSection(urls.apps).then(function(apps) {
    latest.apps = apps;
    renderApps(latest);
    updateRawJson();
  }).catch(function(e) {
    setRegionError('apps-body', 6, 'Failed to load apps: ' + e.message);
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

loadDiagnostics();
