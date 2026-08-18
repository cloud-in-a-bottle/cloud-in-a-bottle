var config = JSON.parse(document.getElementById('page-config').textContent);

// ─── App List ───
//
// Rows are rendered server-side by the app_row macro and only ever carry a
// link to the detail page — every action on an app lives there. This loop just
// keeps each row's status in sync, and rows for apps that have gone away are
// hidden until the next full page load.

function refreshApps() {
  if (!config.apiAppsUrl) return;
  fetch(config.apiAppsUrl)
    .then(function(r) { return r.json(); })
    .then(updateApps)
    .catch(function() {});
}

function updateApps(apps) {
  // /api/apps returns a list of {app_id, name, status, error_message}.
  var byId = {};
  apps.forEach(function(a) { byId[a.app_id] = a; });
  document.querySelectorAll('.app-row[data-app-id]').forEach(function(row) {
    var info = byId[row.getAttribute('data-app-id')];
    if (!info) {
      row.dataset.gone = '1';
      return;
    }
    delete row.dataset.gone;
    // The dot colour is driven off data-status in CSS, so this one assignment
    // both restyles the row and records the state for anything else reading it.
    row.dataset.status = info.status;
    var statusEl = row.querySelector('.app-row__status');
    if (statusEl) statusEl.textContent = info.status;
  });
  applyFilter();
}

// ─── Name filter ───

// Visibility has two independent inputs — whether the app still exists
// (data-gone, set by the poll loop) and whether it matches the filter — so
// both are resolved here rather than each writing `hidden` on its own.
function applyFilter() {
  var input = document.getElementById('app-filter');
  var term = input ? input.value.trim().toLowerCase() : '';
  var matches = 0;
  document.querySelectorAll('.app-row[data-app-name]').forEach(function(row) {
    var hit = !term || row.getAttribute('data-app-name').toLowerCase().indexOf(term) !== -1;
    row.hidden = !hit || row.dataset.gone === '1';
    if (!row.hidden) matches++;
  });
  var empty = document.getElementById('no-matches');
  if (empty) empty.hidden = !term || matches > 0;
}

var filterInput = document.getElementById('app-filter');
if (filterInput) filterInput.addEventListener('input', applyFilter);

if (config.apiAppsUrl) {
  refreshApps();
  setInterval(refreshApps, 3000);
}
