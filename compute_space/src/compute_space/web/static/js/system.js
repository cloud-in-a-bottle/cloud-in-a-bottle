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

// Golden-angle hue steps keep adjacent app slices distinct at any app count;
// fixed low saturation keeps the palette muted. Shared by every app-colored
// donut so the same app gets a consistent hue slot across charts.
function appColor(i) {
  return 'hsl(' + ((215 + i * 137.5) % 360).toFixed(1) + ', 45%, 62%)';
}

// Neutral greys for the non-app slices of a usage donut.
var COLOR_UNUSED = '#e3e6ea';   // free headroom on the box
var COLOR_OTHER = '#b9c0c9';    // used by the host but not attributed to an app

// Generic donut chart. `segments` is [{name, value, valueText, color}] drawn in
// order from 12 o'clock; hovering a slice shows its name + valueText in the
// center, which otherwise shows defaultName / defaultText. Give each donut a
// unique `id` so multiple can coexist on one page.
function donutHtml(id, segments, defaultName, defaultText) {
  var total = 0;
  segments.forEach(function(s) { total += s.value; });
  if (total <= 0) return '<span class="muted">No data yet.</span>';

  var cx = 100, cy = 100, rOuter = 92, rInner = 58;
  var TAU = 2 * Math.PI;
  var angle = -Math.PI / 2;

  function pt(r, a) {
    return (cx + r * Math.cos(a)).toFixed(2) + ' ' + (cy + r * Math.sin(a)).toFixed(2);
  }

  var slices = segments.map(function(s) {
    var frac = s.value / total;
    // Clamp just under a full turn so a single-slice "circle" still renders as one arc.
    var sweep = Math.min(frac * TAU, TAU - 0.0004);
    var a0 = angle;
    var a1 = a0 + sweep;
    angle += frac * TAU;
    var large = sweep > Math.PI ? 1 : 0;
    var d = 'M ' + pt(rOuter, a0) + ' A ' + rOuter + ' ' + rOuter + ' 0 ' + large + ' 1 ' + pt(rOuter, a1)
      + ' L ' + pt(rInner, a1) + ' A ' + rInner + ' ' + rInner + ' 0 ' + large + ' 0 ' + pt(rInner, a0) + ' Z';
    return '<path class="pie-slice" d="' + d + '" fill="' + s.color + '"'
      + ' data-name="' + escAttr(s.name) + '" data-size="' + escAttr(s.valueText) + '"></path>';
  }).join('');

  return '<svg class="usage-pie" id="' + id + '" viewBox="0 0 200 200" width="200" height="200" role="img"'
    + ' data-default-name="' + escAttr(defaultName) + '" data-default-size="' + escAttr(defaultText) + '">'
    + slices
    + '<text class="pie-center-name" data-role="name" x="100" y="96">' + escHtml(defaultName) + '</text>'
    + '<text class="pie-center-size" data-role="size" x="100" y="114">' + escHtml(defaultText) + '</text>'
    + '</svg>';
}

function wireDonut(id) {
  var svg = document.getElementById(id);
  if (!svg) return;
  var nameEl = svg.querySelector('[data-role="name"]');
  var sizeEl = svg.querySelector('[data-role="size"]');
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

// Donut chart of per-app disk usage. Slices are sorted by
// size, largest first at 12 o'clock.
function perAppPieHtml(perApp) {
  var names = Object.keys(perApp).sort(function(a, b) { return perApp[b] - perApp[a]; });
  var total = 0;
  names.forEach(function(n) { total += perApp[n]; });
  if (!total) return '<span class="muted">No app data yet.</span>';
  var segments = names.map(function(name, i) {
    return {name: name, value: perApp[name], valueText: formatBytes(perApp[name]), color: appColor(i)};
  });
  var defaultName = names.length + (names.length === 1 ? ' app' : ' apps');
  return donutHtml('per-app-pie', segments, defaultName, formatBytes(total));
}

function renderStorageDonut(perApp) {
  var el = document.getElementById('storage-usage-chart');
  if (!el) return;
  if (!Object.keys(perApp).length) {
    el.innerHTML = '<span class="muted">No app data yet.</span>';
    return;
  }
  el.innerHTML = perAppPieHtml(perApp);
  wireDonut('per-app-pie');
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

  if (hasMinFree) {
    var guardText = guardPaused ? 'Paused' : (isLow ? 'Active (low storage)' : 'Active');
    var guardCls = (guardPaused || isLow) ? ' class="status-error"' : '';
    rows += '<tr><th>Storage guard</th><td' + guardCls + '>' + escHtml(guardText) + '</td></tr>';
  }
  document.getElementById('storage-body').innerHTML = rows;

  // The per-app disk donut lives up in the Resource Usage section alongside the
  // CPU/memory donuts, not in this table.
  renderStorageDonut(data.per_app || {});

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

// ─── App Resource Usage (CPU + memory donuts) ───
// Donuts showing how CPU and memory are split across running apps, plus an
// "Unused" slice for the box's remaining headroom so the overall load is
// visible at a glance. Data comes from the combined diagnostics bundle, which
// carries per-app live stats (apps[].resources) plus host totals
// (resource_pressure).

function runningWith(apps, field) {
  return apps.filter(function(a) {
    return a.resources && a.resources.running && a.resources[field] != null;
  }).sort(function(a, b) { return b.resources[field] - a.resources[field]; });
}

// podman reports CPU usage as a percentage where 100% == one full core, so we
// present usage in cores (percent / 100) against the box's core count. That way
// the slices add up to a total the viewer can see (used / N cores) instead of
// bare percentages that look wrong without knowing how many CPUs the box has.
// "Unused" is whatever capacity the app containers aren't accounting for (idle
// + any host overhead we can't attribute per-app).
function cpuUsage(apps, cpuCount) {
  var running = runningWith(apps, 'cpu_percent');
  var appSum = 0;
  running.forEach(function(a) { appSum += a.resources.cpu_percent; });
  var cores = function(pct) { return (pct / 100).toFixed(2) + ' cores'; };
  var segments = running.map(function(a, i) {
    return {name: a.name, value: a.resources.cpu_percent, valueText: cores(a.resources.cpu_percent), color: appColor(i)};
  });
  var centerText;
  if (cpuCount) {
    var capacity = cpuCount * 100;
    var unused = Math.max(0, capacity - appSum);
    segments.push({name: 'Unused', value: unused, valueText: cores(unused) + ' idle', color: COLOR_UNUSED});
    centerText = (appSum / 100).toFixed(1) + ' / ' + cpuCount + ' cores';
  } else {
    centerText = cores(appSum);
  }
  return {segments: segments, centerName: 'CPU', centerText: centerText, hasApps: running.length > 0};
}

// Memory splits into per-app usage, host-but-not-app usage ("System"), and the
// actual free memory ("Unused"), so the donut sums to the box's total RAM and
// honestly shows how loaded it is.
function memUsage(apps, pressure) {
  var running = runningWith(apps, 'memory_usage_bytes');
  var appSum = 0;
  running.forEach(function(a) { appSum += a.resources.memory_usage_bytes; });
  var segments = running.map(function(a, i) {
    var b = a.resources.memory_usage_bytes;
    return {name: a.name, value: b, valueText: formatBytes(b), color: appColor(i)};
  });
  var centerText;
  var total = pressure && pressure.memory_total_bytes;
  if (total) {
    var free = pressure.memory_available_bytes;
    if (free != null) {
      var other = Math.max(0, total - free - appSum);
      if (other > 0) segments.push({name: 'System (non-app)', value: other, valueText: formatBytes(other), color: COLOR_OTHER});
      segments.push({name: 'Unused', value: free, valueText: formatBytes(free), color: COLOR_UNUSED});
      centerText = formatBytes(total - free) + ' / ' + formatBytes(total);
    } else {
      var unused = Math.max(0, total - appSum);
      segments.push({name: 'Unused', value: unused, valueText: formatBytes(unused), color: COLOR_UNUSED});
      centerText = formatBytes(total);
    }
  } else {
    centerText = formatBytes(appSum);
  }
  return {segments: segments, centerName: 'Memory', centerText: centerText, hasApps: running.length > 0};
}

function renderUsageChart(elId, pieId, usage, emptyMsg) {
  var el = document.getElementById(elId);
  if (!usage.hasApps && usage.segments.length === 0) {
    el.innerHTML = '<span class="muted">' + emptyMsg + '</span>';
    return;
  }
  el.innerHTML = donutHtml(pieId, usage.segments, usage.centerName, usage.centerText);
  wireDonut(pieId);
}

function renderResourceUsage(apps, pressure) {
  var cpuCount = pressure ? pressure.cpu_count : null;
  renderUsageChart('cpu-usage-chart', 'cpu-usage-pie', cpuUsage(apps, cpuCount), 'No running apps.');
  renderUsageChart('mem-usage-chart', 'mem-usage-pie', memUsage(apps, pressure), 'No running apps.');
}

function updateResourceUsage() {
  fetch(config.diagnosticsUrl, {credentials: 'same-origin'})
    .then(function(r) { return r.json(); })
    .then(function(data) {
      renderResourceUsage(data.apps || [], data.resource_pressure || null);
    })
    .catch(function() {
      document.getElementById('cpu-usage-chart').innerHTML = '<span class="muted">Unavailable.</span>';
      document.getElementById('mem-usage-chart').innerHTML = '<span class="muted">Unavailable.</span>';
    });
}

// ─── Logs ───

function fetchLogs() {
  var logEl = document.getElementById('cs-logs');
  fetch('/api/compute_space_logs', {credentials: 'same-origin'})
    .then(function(r) { return r.text(); })
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
updateResourceUsage();

fetchLogs();
setInterval(fetchLogs, 3000);
