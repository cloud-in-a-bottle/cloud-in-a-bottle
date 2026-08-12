var config = JSON.parse(document.getElementById('page-config').textContent);

function escHtml(s) {
  var d = document.createElement('div');
  d.textContent = (s == null) ? '' : String(s);
  return d.innerHTML;
}

function formatBytes(bytes) {
  if (bytes >= 1099511627776) return (bytes / 1099511627776).toFixed(1) + ' TiB';
  if (bytes >= 1073741824) return (bytes / 1073741824).toFixed(1) + ' GiB';
  if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + ' MiB';
  if (bytes >= 1024) return (bytes / 1024).toFixed(1) + ' KiB';
  return bytes + ' B';
}

// ─── First-paint coordination ───
// All sections fetch immediately on load, but nothing renders until every
// section is ready or the deadline elapses — avoids a staggered flash of
// loading/error states on page open. Sections still pending at the deadline
// show a loading placeholder and fill in when their fetch completes.

var FIRST_PAINT_DEADLINE_MS = 500;
var sectionRenderers = {ports: null, storage: null, logs: null};
var firstPaintDone = false;

function presentSection(section, render) {
  if (firstPaintDone) { render(); return; }
  sectionRenderers[section] = render;
  var allReady = Object.keys(sectionRenderers).every(function(k) { return sectionRenderers[k]; });
  if (allReady) paintAllSections();
}

function paintAllSections() {
  if (firstPaintDone) return;
  firstPaintDone = true;
  if (sectionRenderers.ports) sectionRenderers.ports();
  else document.getElementById('ports-body').innerHTML = '<tr><td colspan="3" class="muted">Loading&hellip;</td></tr>';
  if (sectionRenderers.storage) sectionRenderers.storage();
  else document.getElementById('storage-body').innerHTML = '<tr><td colspan="2" class="muted">Loading&hellip;</td></tr>';
  if (sectionRenderers.logs) sectionRenderers.logs();
  else document.getElementById('cs-logs').textContent = 'Loading...';
}

setTimeout(paintAllSections, FIRST_PAINT_DEADLINE_MS);

// ─── Listening Ports ───

function updateListeningPorts() {
  fetch(config.listeningPortsUrl, {credentials: 'same-origin'})
    .then(function(r) { return r.json(); })
    .then(function(data) {
      presentSection('ports', function() { renderListeningPorts(data); });
    });
}

function renderListeningPorts(data) {
  var body = document.getElementById('ports-body');
  if (!data || data.enumeration_failed) {
    body.innerHTML = '<tr><td colspan="3" class="error">Could not enumerate listening ports.</td></tr>';
    return;
  }
  var ports = data.ports || [];
  if (!ports.length) {
    body.innerHTML = '<tr><td colspan="3" class="muted">No externally exposed ports.</td></tr>';
    return;
  }
  body.innerHTML = ports.map(function(p) {
    var cls = '';
    var label = escHtml(p.label);
    if (p.classification === 'unexpected') {
      cls = ' class="status-error"';
      label = '<strong>' + label + '</strong>';
    } else if (p.classification === 'secure') {
      cls = ' class="status-running"';
    }
    return '<tr><td><code>' + escHtml(p.port) + '</code></td>'
      + '<td><code>' + escHtml(p.address) + '</code></td>'
      + '<td' + cls + '>' + label + '</td></tr>';
  }).join('');
}

// ─── Storage Status ───

function toggleStorageGuard(pause) {
  fetch(config.toggleStorageGuardUrl, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({paused: !!pause}),
  }).then(function() { updateStorageStatus(); });
}

function escAttr(s) {
  return escHtml(s).replace(/"/g, '&quot;');
}

// Donut chart of per-app usage. Hovering a slice shows the app name and size
// in the center; slices are sorted by size, largest first at 12 o'clock.
function perAppPieHtml(perApp) {
  var names = Object.keys(perApp).sort(function(a, b) { return perApp[b] - perApp[a]; });
  var total = 0;
  names.forEach(function(n) { total += perApp[n]; });
  if (!total) return '<span class="muted">No app data yet.</span>';

  var cx = 100, cy = 100, rOuter = 92, rInner = 58;
  var TAU = 2 * Math.PI;
  var angle = -Math.PI / 2;

  function pt(r, a) {
    return (cx + r * Math.cos(a)).toFixed(2) + ' ' + (cy + r * Math.sin(a)).toFixed(2);
  }

  var slices = names.map(function(name, i) {
    var frac = perApp[name] / total;
    // Clamp just under a full turn so a single-app "circle" still renders as one arc.
    var sweep = Math.min(frac * TAU, TAU - 0.0004);
    var a0 = angle;
    var a1 = a0 + sweep;
    angle += frac * TAU;
    var large = sweep > Math.PI ? 1 : 0;
    // Golden-angle hue steps keep adjacent slices distinct at any app count;
    // fixed low saturation keeps the palette muted.
    var hue = (215 + i * 137.5) % 360;
    var d = 'M ' + pt(rOuter, a0) + ' A ' + rOuter + ' ' + rOuter + ' 0 ' + large + ' 1 ' + pt(rOuter, a1)
      + ' L ' + pt(rInner, a1) + ' A ' + rInner + ' ' + rInner + ' 0 ' + large + ' 0 ' + pt(rInner, a0) + ' Z';
    return '<path class="pie-slice" d="' + d + '" fill="hsl(' + hue.toFixed(1) + ', 45%, 62%)"'
      + ' data-name="' + escAttr(name) + '" data-size="' + escAttr(formatBytes(perApp[name])) + '"></path>';
  }).join('');

  var defaultName = names.length + (names.length === 1 ? ' app' : ' apps');
  return '<svg id="per-app-pie" viewBox="0 0 200 200" width="220" height="220" role="img"'
    + ' data-default-name="' + escAttr(defaultName) + '" data-default-size="' + escAttr(formatBytes(total)) + '">'
    + slices
    + '<text id="pie-label-name" class="pie-center-name" x="100" y="96">' + escHtml(defaultName) + '</text>'
    + '<text id="pie-label-size" class="pie-center-size" x="100" y="114">' + escHtml(formatBytes(total)) + '</text>'
    + '</svg>';
}

function wirePerAppPie() {
  var svg = document.getElementById('per-app-pie');
  if (!svg) return;
  var nameEl = document.getElementById('pie-label-name');
  var sizeEl = document.getElementById('pie-label-size');
  function setLabel(name, size) {
    nameEl.textContent = name.length > 18 ? name.slice(0, 17) + '…' : name;
    sizeEl.textContent = size;
  }
  function reset() {
    setLabel(svg.getAttribute('data-default-name'), svg.getAttribute('data-default-size'));
  }
  svg.addEventListener('mouseover', function(e) {
    var t = e.target;
    if (t && t.classList && t.classList.contains('pie-slice')) {
      setLabel(t.getAttribute('data-name'), t.getAttribute('data-size'));
    } else if (t === svg) {
      reset();
    }
  });
  svg.addEventListener('mouseleave', reset);
}

function updateStorageStatus() {
  fetch(config.storageStatusUrl, {credentials: 'same-origin'})
    .then(function(r) { return r.json(); })
    .then(function(data) {
      presentSection('storage', function() { renderStorageStatus(data); });
    });
}

function renderStorageStatus(data) {
  var disk = data.disk || {};
  var hasMinFree = data.storage_min_free_bytes != null;
  var isLow = !!data.storage_low;
  var guardPaused = !!data.guard_paused;

  var rows = '';
  var freeText = formatBytes(disk.free_bytes || 0) + ' / ' + formatBytes(disk.total_bytes || 0);
  if (hasMinFree) {
    freeText += ' (min ' + formatBytes(data.storage_min_free_bytes) + ' required)';
  }
  var freeCls = (hasMinFree && isLow) ? ' class="status-error"' : '';
  rows += '<tr><th>Disk free</th><td' + freeCls + '>' + escHtml(freeText) + '</td></tr>';
  rows += '<tr><th>OpenHost data</th><td>' + escHtml(formatBytes(data.openhost_data_used_bytes || 0)) + '</td></tr>';
  var buildCache = (data.build_cache_bytes == null)
    ? '<span class="muted">unavailable</span>'
    : escHtml(formatBytes(data.build_cache_bytes));
  rows += '<tr><th>App Build Cache</th><td>' + buildCache + '</td></tr>';

  var perApp = data.per_app || {};
  if (Object.keys(perApp).length > 0) {
    rows += '<tr><th>App data</th><td>' + perAppPieHtml(perApp) + '</td></tr>';
  }

  if (hasMinFree) {
    var guardText = guardPaused ? 'Paused' : (isLow ? 'Active (low storage)' : 'Active');
    var guardCls = (guardPaused || isLow) ? ' class="status-error"' : '';
    rows += '<tr><th>Storage guard</th><td' + guardCls + '>' + escHtml(guardText) + '</td></tr>';
  }
  document.getElementById('storage-body').innerHTML = rows;
  wirePerAppPie();

  // Guard toggle button (separate row below the table for clarity)
  var guardRow = document.getElementById('storage-guard-row');
  if (hasMinFree && guardPaused) {
    guardRow.innerHTML = '<div class="control-row"><button class="btn" onclick="toggleStorageGuard(false)">Resume Guard</button>'
      + '<span class="hint">Apps will not be stopped while paused.</span></div>';
  } else if (hasMinFree && isLow) {
    guardRow.innerHTML = '<div class="control-row"><button class="btn" onclick="toggleStorageGuard(true)">Pause Guard</button>'
      + '<span class="hint">Pause to start an app for cleanup.</span></div>';
  } else {
    guardRow.innerHTML = '';
  }
}

// ─── Logs ───

function fetchLogs() {
  var logEl = document.getElementById('cs-logs');
  fetch('/api/compute_space_logs', {credentials: 'same-origin'})
    .then(function(r) {
      if (!r.ok) { return r.json().then(function(d) { return d.detail || 'Failed to load logs.'; }); }
      return r.text();
    })
    .then(function(text) {
      presentSection('logs', function() {
        var sel = window.getSelection();
        if (sel && !sel.isCollapsed && logEl.contains(sel.anchorNode)) return;
        var wasAtBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 50;
        logEl.textContent = text || 'No log output available.';
        if (wasAtBottom) logEl.scrollTop = logEl.scrollHeight;
      });
    });
}

// ─── Init ───
// Ports and storage load once per visit (storage also re-fetches after a
// guard toggle); only the logs tail keeps polling.

updateListeningPorts();
updateStorageStatus();

fetchLogs();
setInterval(fetchLogs, 3000);
