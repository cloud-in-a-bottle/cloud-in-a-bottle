var config = JSON.parse(document.getElementById('page-config').textContent);

// appColor, COLOR_* and donutHtml/wireDonut come from chart.js; el/replace/clear
// from dom.js. Both are loaded before this file.

// A single full-width <tr> carrying a loading / empty / error message.
function messageRow(colspan, text, cls) {
  return dom.el('tr', null, dom.el('td', {colspan: colspan, class: cls, text: text}));
}

function showMuted(el, text) {
  dom.replace(el, dom.el('span', {class: 'muted', text: text}));
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
  else dom.replace(document.getElementById('ports-body'), messageRow(3, 'Loading\u2026', 'muted'));
  if (sectionRenderers.storage) sectionRenderers.storage();
  else dom.replace(document.getElementById('storage-body'), messageRow(2, 'Loading\u2026', 'muted'));
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
    dom.replace(body, messageRow(3, 'Could not enumerate listening ports.', 'error'));
    return;
  }
  var ports = data.ports || [];
  if (!ports.length) {
    dom.replace(body, messageRow(3, 'No externally exposed ports.', 'muted'));
    return;
  }
  dom.replace(body, ports.map(function(p) {
    var unexpected = p.classification === 'unexpected';
    var cls = unexpected ? 'status-error' : (p.classification === 'secure' ? 'status-running' : null);
    return dom.el('tr', null, [
      dom.el('td', null, dom.el('code', {text: p.port})),
      dom.el('td', null, dom.el('code', {text: p.address})),
      dom.el('td', {class: cls}, unexpected ? dom.el('strong', {text: p.label}) : p.label),
    ]);
  }));
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

// Donut of disk usage: per-app data as coloured slices (largest first), then the
// rest of the used disk — OS / Cloud in a Bottle / build cache — as
// "OS + Cloud in a Bottle" and
// the free space as "Unused", so the ring sums to the whole disk and shows how
// full the box is. Falls back to app-only proportions if disk totals are absent.
function renderStorageDonut(data) {
  var el = document.getElementById('storage-usage-chart');
  if (!el) return;
  var perApp = data.per_app || {};
  var disk = data.disk || {};
  var names = Object.keys(perApp).sort(function(a, b) { return perApp[b] - perApp[a]; });
  if (!names.length && !disk.total_bytes) {
    showMuted(el, 'No app data yet.');
    return;
  }
  var appSum = 0;
  names.forEach(function(n) { appSum += perApp[n]; });
  var segments = names.map(function(name, i) {
    return {name: name, value: perApp[name], valueText: formatBytes(perApp[name]), color: appColor(i)};
  });
  var total = disk.total_bytes;
  var centerText;
  if (total && disk.free_bytes != null) {
    var free = disk.free_bytes;
    var other = Math.max(0, total - free - appSum);
    if (other > 0) segments.push({name: 'OS + Cloud in a Bottle', value: other, valueText: formatBytes(other), color: COLOR_OTHER});
    segments.push({name: 'Unused', value: free, valueText: formatBytes(free), color: COLOR_UNUSED});
    centerText = formatBytes(total - free) + ' / ' + formatBytes(total);
  } else {
    centerText = formatBytes(appSum);
  }
  el.innerHTML = donutHtml('storage-usage-pie', segments, 'Disk', centerText);
  wireDonut('storage-usage-pie');
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

  var freeText = formatBytes(disk.free_bytes || 0) + ' / ' + formatBytes(disk.total_bytes || 0);
  if (hasMinFree) {
    freeText += ' (min ' + formatBytes(data.storage_min_free_bytes) + ' required)';
  }
  var buildCache = (data.build_cache_bytes == null)
    ? dom.el('span', {class: 'muted', text: 'unavailable'})
    : formatBytes(data.build_cache_bytes);
  var rows = [
    dom.infoRow('Disk free', freeText, (hasMinFree && isLow) ? 'status-error' : null),
    dom.infoRow('Cloud in a Bottle data', formatBytes(data.openhost_data_used_bytes || 0)),
    dom.infoRow('App Build Cache', buildCache),
  ];
  if (hasMinFree) {
    var guardText = guardPaused ? 'Paused' : (isLow ? 'Active (low storage)' : 'Active');
    rows.push(dom.infoRow('Storage guard', guardText, (guardPaused || isLow) ? 'status-error' : null));
  }
  dom.replace(document.getElementById('storage-body'), rows);

  // The disk donut lives up in the Resource Usage section alongside the
  // CPU/memory donuts, not in this table.
  renderStorageDonut(data);

  // Guard toggle button (separate row below the table for clarity)
  var guardRow = document.getElementById('storage-guard-row');
  if (hasMinFree && guardPaused) {
    dom.replace(guardRow, guardToggle(false, 'Resume Guard', 'Apps will not be stopped while paused.'));
  } else if (hasMinFree && isLow) {
    dom.replace(guardRow, guardToggle(true, 'Pause Guard', 'Pause to start an app for cleanup.'));
  } else {
    dom.clear(guardRow);
  }
}

function guardToggle(pause, label, hint) {
  return dom.el('div', {class: 'action-bar'}, [
    dom.el('button', {class: 'btn', text: label, onclick: function() { toggleStorageGuard(pause); }}),
    dom.el('span', {class: 'hint', text: hint}),
  ]);
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

// Swap is separate from RAM (a slower, disk-backed overflow), so it gets its own
// donut rather than a slice of the memory ring. "Used" is drawn in the deep
// "System" blue so a busy swap reads as notable; free swap uses the same pale
// "Unused" blue as the other charts.
function swapUsage(pressure) {
  var total = pressure && pressure.swap_total_bytes;
  if (!total) return null;
  var free = pressure.swap_free_bytes != null ? pressure.swap_free_bytes : 0;
  var used = Math.max(0, total - free);
  var segments = [
    {name: 'Used', value: used, valueText: formatBytes(used), color: COLOR_OTHER},
    {name: 'Unused', value: free, valueText: formatBytes(free), color: COLOR_UNUSED},
  ];
  return {segments: segments, centerName: 'Swap', centerText: formatBytes(used) + ' / ' + formatBytes(total)};
}

function renderUsageChart(elId, pieId, usage, emptyMsg) {
  var el = document.getElementById(elId);
  if (!usage.hasApps && usage.segments.length === 0) {
    showMuted(el, emptyMsg);
    return;
  }
  el.innerHTML = donutHtml(pieId, usage.segments, usage.centerName, usage.centerText);
  wireDonut(pieId);
}

function renderSwapChart(pressure) {
  var el = document.getElementById('swap-usage-chart');
  if (!el) return;
  var usage = swapUsage(pressure);
  if (!usage) {
    showMuted(el, 'No swap configured.');
    return;
  }
  el.innerHTML = donutHtml('swap-usage-pie', usage.segments, usage.centerName, usage.centerText);
  wireDonut('swap-usage-pie');
}

function renderResourceUsage(apps, pressure) {
  var cpuCount = pressure ? pressure.cpu_count : null;
  renderUsageChart('cpu-usage-chart', 'cpu-usage-pie', cpuUsage(apps, cpuCount), 'No running apps.');
  renderUsageChart('mem-usage-chart', 'mem-usage-pie', memUsage(apps, pressure), 'No running apps.');
  renderSwapChart(pressure);
}

function updateResourceUsage() {
  fetch(config.diagnosticsUrl, {credentials: 'same-origin'})
    .then(function(r) { return r.json(); })
    .then(function(data) {
      renderResourceUsage(data.apps || [], data.resource_pressure || null);
    })
    .catch(function() {
      ['cpu-usage-chart', 'mem-usage-chart', 'swap-usage-chart'].forEach(function(id) {
        showMuted(document.getElementById(id), 'Unavailable.');
      });
    });
}

// ─── Logs ───

function fetchLogs() {
  var logEl = document.getElementById('cs-logs');
  fetch('/api/compute_space_logs', {credentials: 'same-origin'})
    .then(function(r) {
      if (!r.ok) {
        return r.json().then(function(d) {
          return responseErrorMessage(d, 'Failed to load logs.');
        }, function() {
          return 'Failed to load logs.';
        });
      }
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
updateResourceUsage();

fetchLogs();
setInterval(fetchLogs, 3000);
