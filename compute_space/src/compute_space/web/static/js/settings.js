let pageHidden = false;
window.addEventListener('pagehide', () => { pageHidden = true; });

function showError(msg) {
  // Deferred: navigating away aborts in-flight fetches, whose catch handlers
  // would otherwise flash this banner during page teardown. Timers don't run
  // once the page is gone, and pageHidden covers the bfcache edge.
  setTimeout(() => {
    if (pageHidden) return;
    const el = document.getElementById('error');
    el.textContent = msg;
    el.style.display = '';
  }, 100);
}
function clearError() { document.getElementById('error').style.display = 'none'; }
function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

async function checkForUpdates() {
  clearError();
  const el = document.getElementById('update-status');
  el.innerHTML = '<p>Checking for updates&hellip;</p>';

  try {
    const resp = await fetch('/api/settings/update');
    if (!resp.ok) {
      const err = await resp.json();
      el.innerHTML = '<p class="error">Repo is in an invalid state for updating (no .git perhaps?)</p>'
        + (err.detail ? '<div class="error-inline">' + esc(err.detail) + '</div>' : '')
        + '<button onclick="checkForUpdates()" class="btn" style="margin-top:0.5em;">Retry</button>';
      return;
    }
    const data = await resp.json();

    const checkAgainBtn = '<button onclick="checkForUpdates()" class="btn" style="margin-top:0.5em;">Check again</button>';

    if (data.state === 'UP_TO_DATE') {
      el.innerHTML = '<p style="color:#080;">Up to date.</p>' + checkAgainBtn;
    } else if (data.state === 'UPDATE_AVAILABLE') {
      const notice = data.error ? '<div class="error-inline">' + esc(data.error) + '</div>' : '';
      el.innerHTML = '<p>Updates available.</p>'
        + notice
        + '<button onclick="applyUpdate()" class="btn btn-primary" style="margin-top:0.5em;">Update &amp; restart</button> '
        + checkAgainBtn;
    } else if (data.state === 'ERROR') {
      el.innerHTML = '<p class="error">Update check failed.</p>'
        + '<div class="error-inline">' + esc(data.error || 'Unknown error') + '</div>'
        + checkAgainBtn;
    }
  } catch (e) {
    showError('Failed to check for updates: ' + e.message);
    el.innerHTML = '<button onclick="checkForUpdates()" class="btn">Retry</button>';
  }
}

async function applyUpdate() {
  clearError();
  const el = document.getElementById('update-status');
  el.innerHTML = '<p>Updating&hellip;</p>';

  try {
    const resp = await fetch('/api/settings/update', {method: 'POST'});
    if (!resp.ok) {
      const err = await resp.json();
      el.innerHTML = '<p class="error">' + esc(err.detail || '') + '</p>'
        + '<button onclick="checkForUpdates()" class="btn" style="margin-top:0.5em;">Retry</button>';
      return;
    }
  } catch (e) {
    el.innerHTML = '<p class="error">Update failed: ' + esc(e.message) + '</p>'
      + '<button onclick="checkForUpdates()" class="btn" style="margin-top:0.5em;">Retry</button>';
    return;
  }

  el.innerHTML = '<p>Update applied. Restarting&hellip;</p>';
  try {
    await fetch('/api/settings/restart_compute_space', {method: 'POST'});
  } catch (e) {
    // Expected — server may die before responding
  }
  showRestartOverlay();
}

function showRestartOverlay() {
  document.getElementById('restart-overlay').style.display = '';
  document.getElementById('update-status').style.display = 'none';
  document.getElementById('restart-status').innerHTML = '<strong>Waiting for shutdown&hellip;</strong>';
  pollShutdown();
}

function pollShutdown() {
  setTimeout(async () => {
    try {
      const resp = await fetch('/health', {signal: AbortSignal.timeout(3000)});
      if (resp.ok) { pollShutdown(); return; }
    } catch (e) { /* server is down — move to next phase */ }
    document.getElementById('restart-status').innerHTML = '<strong>Service stopped, waiting for restart&hellip;</strong>';
    pollRestart();
  }, 1000);
}

function pollRestart() {
  setTimeout(async () => {
    try {
      const resp = await fetch('/health', {signal: AbortSignal.timeout(3000)});
      if (resp.ok) { window.location.reload(); return; }
    } catch (e) { /* still down */ }
    pollRestart();
  }, 2500);
}

let savedRemote = '';

async function loadRemote() {
  const input = document.getElementById('remote-url');
  const btn = document.getElementById('set-remote-btn');
  try {
    const resp = await fetch('/api/settings/get-remote');
    if (!resp.ok) throw new Error('failed to load remote');
    const data = await resp.json();
    savedRemote = data.url || '';
    // Only reconstruct the url@ref pin when the instance is actually pinned.
    // When unpinned, data.ref is just the resolved current tag shown elsewhere;
    // appending it here would make re-saving silently pin the host to that tag.
    if (savedRemote && data.pinned && data.ref) {
      savedRemote = savedRemote + '@' + data.ref;
    }
    input.value = savedRemote;
    input.placeholder = 'https://github.com/user/repo@branch';
    input.disabled = false;
    btn.disabled = true;
    input.addEventListener('input', () => {
      btn.disabled = input.value.trim() === savedRemote;
    });
  } catch (e) {
    input.placeholder = '';
    const msg = document.getElementById('remote-msg');
    msg.textContent = 'Failed to load current remote. Reload the page to retry.';
    msg.className = 'error';
    msg.style.display = '';
  }
}

async function setRemote() {
  clearError();
  const input = document.getElementById('remote-url');
  const btn = document.getElementById('set-remote-btn');
  const msg = document.getElementById('remote-msg');
  const url = input.value.trim();
  if (!url) return;

  btn.disabled = true;
  msg.style.display = 'none';

  try {
    const resp = await fetch('/api/settings/set-remote', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url}),
    });
    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.detail || 'failed to set remote');
    }
    try {
      await fetch('/api/settings/restart_compute_space', {method: 'POST'});
    } catch (e) {
      // Expected — server may die before responding
    }
    showRestartOverlay();
  } catch (e) {
    msg.textContent = e.message;
    msg.className = 'error';
    msg.style.display = '';
    btn.disabled = false;
  }
}

async function restartComputeSpace() {
  try {
    await fetch('/api/settings/restart_compute_space', {method: 'POST'});
  } catch (e) {
    // Expected — server may die before responding
  }
  showRestartOverlay();
}

async function changePassword() {
  clearError();
  const msg = document.getElementById('pw-msg');
  const data = {
    current_password: document.getElementById('current-pw').value,
    new_password: document.getElementById('new-pw').value,
    confirm_password: document.getElementById('confirm-pw').value,
  };
  try {
    const resp = await fetch('/api/settings/change_password', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data),
    });
    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.detail || 'failed to change password');
    }
    msg.textContent = 'Password changed successfully';
    msg.className = '';
    msg.style.display = '';
    msg.style.color = '#080';
    document.getElementById('current-pw').value = '';
    document.getElementById('new-pw').value = '';
    document.getElementById('confirm-pw').value = '';
  } catch (e) {
    msg.textContent = e.message;
    msg.className = 'error';
    msg.style.display = '';
    msg.style.color = '';
  }
}

let savedUsername = '';

// Live client-side username validation lives in the shared
// /static/js/username-validation.js module (mirrors the server-side
// validate_owner_username rule). Empty is treated as "no input yet"
// (not an error) so we don't nag before the operator types; the Save
// button is independently gated on emptiness, and the server rejects
// empty values authoritatively.
const usernameError = window.OpenHostUsername.usernameError;

async function loadOwnerUsername() {
  const input = document.getElementById('username-input');
  const btn = document.getElementById('set-username-btn');
  const msg = document.getElementById('username-msg');
  try {
    const resp = await fetch('/api/settings/owner_username');
    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.detail || 'failed to load');
    }
    const data = await resp.json();
    savedUsername = data.username || '';
    input.value = savedUsername;
    input.placeholder = 'e.g. yourname';
    input.disabled = false;
    btn.disabled = true;
    input.addEventListener('input', () => {
      const v = input.value.trim();
      const err = usernameError(v);
      if (err) {
        msg.textContent = err;
        msg.className = 'error';
        msg.style.color = '';
        msg.style.display = '';
      } else {
        msg.style.display = 'none';
      }
      // Save stays disabled when the value matches what's stored,
      // is empty, OR fails client-side validation.  Server-side
      // rejects all of those too; this is the friendlier guard so
      // the operator doesn't round-trip a bound-to-fail request.
      btn.disabled = v === savedUsername || v === '' || err !== '';
    });
  } catch (e) {
    // Surface the failure to the operator rather than leaving the
    // section silently inert.  Both the global error banner and the
    // section-local message catch this so the user sees a problem
    // even if they've scrolled past the top of the page.
    showError('Failed to load owner username: ' + e.message);
    msg.textContent = 'Failed to load. Reload the page to retry.';
    msg.className = 'error';
    msg.style.display = '';
    input.placeholder = 'Failed to load — reload to retry';
  }
}

async function setOwnerUsername() {
  clearError();
  const input = document.getElementById('username-input');
  const btn = document.getElementById('set-username-btn');
  const msg = document.getElementById('username-msg');
  const username = input.value.trim();
  if (!username) return;

  const clientErr = usernameError(username);
  if (clientErr) {
    msg.textContent = clientErr;
    msg.className = 'error';
    msg.style.color = '';
    msg.style.display = '';
    return;
  }

  btn.disabled = true;
  msg.style.display = 'none';

  try {
    const resp = await fetch('/api/settings/owner_username', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username}),
    });
    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.detail || 'failed to save');
    }
    const data = await resp.json();
    savedUsername = data.username;
    msg.textContent = 'Saved.';
    msg.className = '';
    msg.style.color = '#080';
    msg.style.display = '';
    setTimeout(() => { msg.style.display = 'none'; }, 4000);
  } catch (e) {
    msg.textContent = e.message;
    msg.className = 'error';
    msg.style.color = '';
    msg.style.display = '';
    btn.disabled = false;
  }
}

function escSettingsHtml(s) {
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

// ─── Build cache ───

function dropBuildCache() {
  if (!confirm(
    'Drop container build cache?\n\n' +
    'Running containers will not be stopped, but images for stopped apps will be removed and rebuilt on next deploy.'
  )) return;

  var btn = document.getElementById('drop-build-cache-btn');
  var msg = document.getElementById('drop-build-cache-msg');
  btn.disabled = true;
  msg.className = 'muted';
  msg.textContent = 'Dropping cache...';

  fetch('/api/drop-docker-cache', {method: 'POST', credentials: 'same-origin'})
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) {
        msg.className = 'error';
        msg.textContent = 'Drop failed: ' + data.error;
        return;
      }
      var reclaimed = '';
      if (data.output) {
        var match = data.output.match(/Total reclaimed space:\s*(.+)/i);
        if (match && match[1]) {
          reclaimed = ' Freed ' + match[1] + '.';
        }
      }
      msg.className = 'status-running';
      msg.textContent = 'Build cache dropped.' + reclaimed;
    })
    .catch(function() {
      msg.className = 'error';
      msg.textContent = 'Drop failed: request error';
    })
    .then(function() {
      btn.disabled = false;
    });
}

// ─── SSH Toggle ───

function updateSshStatus() {
  fetch('/api/ssh-status', {credentials: 'same-origin'})
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var btn = document.getElementById('ssh-btn');
      var status = document.getElementById('ssh-status');
      btn.disabled = false;
      if (data.ssh_enabled) {
        btn.textContent = 'Disable SSH';
        btn.className = 'btn btn-danger';
        status.textContent = 'SSH active';
        status.className = 'status-error';
      } else {
        btn.textContent = 'Enable SSH';
        btn.className = 'btn';
        status.textContent = 'SSH disabled';
        status.className = 'muted';
      }
    });
}

function toggleSsh() {
  var btn = document.getElementById('ssh-btn');
  btn.disabled = true;
  btn.textContent = '...';
  fetch('/toggle-ssh', {method: 'POST', credentials: 'same-origin'})
    .then(function(r) { return r.json(); })
    .then(function() {
      updateSshStatus();
    });
}

// ─── Archive Backend ───

function renderArchiveBackend(state) {
  var el = document.getElementById('archive-backend-status');
  var rows = '';
  if (state.backend === 's3') {
    rows += '<tr><th>Backend</th>'
      + '<td><span class="status-running">S3 (JuiceFS)</span>'
      + (state.state_message ? ' <span class="error">' + escSettingsHtml(state.state_message) + '</span>' : '')
      + '</td></tr>';
    var bucketLine = escSettingsHtml(state.s3_bucket || '?')
      + (state.s3_prefix ? '/' + escSettingsHtml(state.s3_prefix) : '')
      + (state.s3_region ? ' <span class="hint">(' + escSettingsHtml(state.s3_region) + ')</span>' : '');
    rows += '<tr><th>S3 bucket</th><td><code>' + bucketLine + '</code></td></tr>';
    if (state.s3_access_key_id) {
      rows += '<tr><th>Access key</th><td><code>' + escSettingsHtml(state.s3_access_key_id.slice(0, 4)) + '…</code></td></tr>';
    }
    if (state.archive_dir) {
      rows += '<tr><th>Host path</th><td><code>' + escSettingsHtml(state.archive_dir) + '</code></td></tr>';
    }
    if (state.meta_db_path) {
      rows += '<tr><th>Metadata DB</th><td><code>' + escSettingsHtml(state.meta_db_path) + '</code>'
        + ' <span class="error">(must back up to survive disk loss)</span></td></tr>';
    }
    var dumps = state.meta_dumps;
    var dumpLine;
    if (dumps && dumps.count > 0) {
      dumpLine = '<code>' + escSettingsHtml(dumps.latest_at || '?') + '</code> <span class="hint">(' + dumps.count + ' in bucket, hourly cadence)</span>';
    } else if (dumps && dumps.count === 0) {
      dumpLine = '<span class="error">No metadata dumps in bucket yet.</span> <span class="hint">JuiceFS writes one within an hour of mount.</span>';
    } else {
      dumpLine = '<span class="hint">unavailable; could not list <code>'
        + escSettingsHtml((state.juicefs_volume_name ? state.juicefs_volume_name + '/' : '') + 'meta/')
        + '</code></span>';
    }
    rows += '<tr><th>Latest meta dump</th><td>' + dumpLine + '</td></tr>';
  } else if (state.backend === 'local') {
    rows += '<tr><th>Backend</th>'
      + '<td><span class="status-running">Local disk (JuiceFS)</span>'
      + (state.state_message ? ' <span class="error">' + escSettingsHtml(state.state_message) + '</span>' : '')
      + '</td></tr>';
    if (state.archive_dir) {
      rows += '<tr><th>Host path</th><td><code>' + escSettingsHtml(state.archive_dir) + '</code></td></tr>';
    }
    rows += '<tr><th>Durability</th><td><span class="error">Local disk only.</span> '
      + 'The archive is a JuiceFS volume whose objects live on this instance\u2019s local disk '
      + '(included in backups) but NOT on durable object storage. Configure S3 below for elastic, durable storage.</td></tr>';
    var apps = state.local_archive_apps || [];
    if (apps.length) {
      rows += '<tr><th>Apps with local archive data</th><td>'
        + apps.map(function(a){ return '<code>' + escSettingsHtml(a) + '</code>'; }).join(', ')
        + '<div class="hint">Configuring S3 will migrate these apps\u2019 archive data into the bucket.</div></td></tr>';
    }
  } else {
    // Legacy pre-v12 'disabled' state (no archive tier).
    rows += '<tr><th>Backend</th>'
      + '<td><span class="status-stopped">not configured</span></td></tr>';
  }

  var experimentalNote = '';
  if (state.backend === 's3') {
    experimentalNote = '<p class="hint"><strong class="error">Experimental:</strong> the S3 archive backend is best-effort durable. '
      + 'Filename-to-S3-chunk mappings live in a SQLite metadata DB on this zone’s local disk; '
      + 'recovery after the local disk is wiped requires the latest meta dump in S3 plus a manual <code>juicefs load</code>.</p>';
  }
  // S3 can be configured from the default 'local' backend (data is migrated
  // into the bucket), a legacy 'disabled' zone (fresh format), or an existing
  // 's3' backend (data is migrated to a new bucket/provider).
  var configureBtn = '';
  if (state.backend === 'local') {
    configureBtn = '<div class="control-row"><button class="btn" id="archive-backend-configure-btn">Upgrade to S3 backend…</button></div>';
  } else if (state.backend === 'disabled') {
    configureBtn = '<div class="control-row"><button class="btn" id="archive-backend-configure-btn">Configure S3 backend…</button></div>';
  } else if (state.backend === 's3') {
    configureBtn = '<div class="control-row"><button class="btn" id="archive-backend-configure-btn">Migrate to a new bucket…</button></div>';
  }

  el.innerHTML = '<table id="archive-backend-table" class="form-table"><tbody>' + rows + '</tbody></table>'
    + experimentalNote
    + configureBtn
    + '<div id="archive-backend-form" hidden></div>';
  if (state.backend === 'local' || state.backend === 'disabled' || state.backend === 's3') {
    document.getElementById('archive-backend-configure-btn').onclick = function() { showConfigureForm(state); };
  }
}

function showConfigureForm(state) {
  state = state || {};
  var formEl = document.getElementById('archive-backend-form');
  var migrateNote = '';
  var localApps = (state.local_archive_apps || []);
  if (state.backend === 'local') {
    var appsLine = localApps.length
      ? ' Apps whose archive data will be migrated: ' + localApps.map(function(a){ return '<code>' + escSettingsHtml(a) + '</code>'; }).join(', ') + '.'
      : ' There is no local archive data yet, so nothing will be migrated.';
    migrateNote = '<p class="error"><strong>This migrates your existing LOCAL archive data into S3.</strong> '
      + 'JuiceFS copies the archive objects into the bucket (verified with <code>--check-all</code>) and re-points the volume; if anything fails the switch is aborted and your local data is left intact (fail-open). '
      + 'After a successful migration the local copy is removed and the switch to S3 is <strong>one-way</strong>.'
      + appsLine + '</p>';
  } else if (state.backend === 's3') {
    migrateNote = '<p class="error"><strong>This migrates your archive from the current bucket (<code>'
      + escSettingsHtml(state.s3_bucket || '?') + '</code>) to the NEW bucket below.</strong> '
      + 'JuiceFS copies every archive object to the new bucket (verified with <code>--check-all</code>) and re-points the volume; if anything fails the switch is aborted and your current bucket is left intact (fail-open). '
      + 'After a successful migration the old bucket\u2019s objects (under this zone\u2019s prefix only) are reclaimed.</p>';
  }
  formEl.innerHTML = '<p><strong>Configure S3 archive storage.</strong> JuiceFS will format the bucket and mount it locally; this is a one-time operation.</p>'
    + migrateNote
    + '<p class="error"><strong>Experimental.</strong> Filename-to-S3-chunk mappings live in a SQLite metadata DB on this zone’s local disk, not in the bucket. If the local disk is wiped, the bucket bytes can be recovered only from JuiceFS\'s periodic meta dumps in S3 (replayed via <code>juicefs load</code>).</p>'
    + '<p class="hint">JuiceFS will automatically dump the metadata DB to <code>&lt;bucket&gt;/&lt;prefix&gt;/meta/dump-*.json.gz</code> once an hour. These dumps are the recovery anchor for reattaching a freshly-installed zone to an existing bucket.</p>'
    + '<table class="form-table"><tbody>'
    + '<tr><th><label for="ab-bucket">S3 bucket</label></th><td><input id="ab-bucket" type="text" placeholder="my-openhost-archive"></td></tr>'
    + '<tr><th><label for="ab-region">Region</label></th><td><input id="ab-region" type="text" value="us-east-1"></td></tr>'
    + '<tr><th><label for="ab-endpoint">Endpoint</label></th><td><input id="ab-endpoint" type="text" placeholder="https://..."> <span class="hint">optional, non-AWS</span></td></tr>'
    // On an s3->s3 migration the volume name (object prefix) is fixed by the
    // existing volume and cannot change, so the Prefix input is omitted; it is
    // only meaningful when first choosing a volume name (local/disabled).
    + (state.backend === 's3'
        ? ''
        : '<tr><th><label for="ab-prefix">Prefix</label></th><td><input id="ab-prefix" type="text" placeholder="andrew-3"> <span class="hint">optional single-segment name; lets multiple zones share one bucket &mdash; also used as the JuiceFS volume name</span></td></tr>')
    + '<tr><th><label for="ab-access-key">Access key ID</label></th><td><input id="ab-access-key" type="text"></td></tr>'
    + '<tr><th><label for="ab-secret-key">Secret access key</label></th><td><input id="ab-secret-key" type="password"></td></tr>'
    + '</tbody></table>'
    + '<p><label><input type="checkbox" id="ab-confirm"> I understand the S3 archive backend is experimental'
    + (state.backend === 'local'
        ? ' and that my existing local archive data will be migrated into S3.'
        : state.backend === 's3'
        ? ' and that my archive will be migrated to the new bucket and the old bucket reclaimed.'
        : ' and that this configures S3 for the archive tier.')
    + '</label></p>'
    + '<div class="control-row">'
    + '<button class="btn" id="ab-test-btn">Test connection</button>'
    + '<button class="btn btn-primary" id="ab-submit-btn">Configure</button>'
    + '<button class="btn" id="ab-cancel-btn">Cancel</button>'
    + '<span id="ab-msg" class="hint"></span>'
    + '</div>';
  formEl.hidden = false;
  document.getElementById('ab-cancel-btn').onclick = function() {
    formEl.hidden = true;
    formEl.innerHTML = '';
  };
  document.getElementById('ab-test-btn').onclick = testArchiveConnection;
  document.getElementById('ab-submit-btn').onclick = submitConfigure;
}

function _archiveBackendBody() {
  var prefixEl = document.getElementById('ab-prefix');
  return {
    s3_bucket: document.getElementById('ab-bucket').value,
    s3_region: document.getElementById('ab-region').value,
    s3_endpoint: document.getElementById('ab-endpoint').value,
    // The Prefix input is omitted on an s3->s3 migration (volume name fixed).
    s3_prefix: prefixEl ? prefixEl.value : '',
    s3_access_key_id: document.getElementById('ab-access-key').value,
    s3_secret_access_key: document.getElementById('ab-secret-key').value,
    // Ticking the confirmation checkbox (checked in submitConfigure)
    // acknowledges the one-way local->S3 migration AND the s3->s3 bucket
    // migration; the server ignores whichever flag doesn't apply to the
    // current backend.
    confirm_migrate_local: true,
    confirm_migrate_s3: true,
  };
}

function testArchiveConnection() {
  var msg = document.getElementById('ab-msg');
  msg.textContent = 'Testing…';
  msg.style.color = '';
  fetch('/api/storage/archive_backend/test_connection', {
    method: 'POST',
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(_archiveBackendBody()),
  })
    .then(function(r) { return r.json().then(function(b) { return [r.status, b]; }); })
    .then(function(pair) {
      var ok = pair[0] === 200 && pair[1].ok;
      msg.style.color = ok ? '#16a34a' : '#dc3545';
      msg.textContent = ok ? 'Bucket reachable' : ('Failed: ' + (pair[1].error || ''));
    })
    .catch(function(err) {
      msg.style.color = '#dc3545';
      msg.textContent = 'Network error: ' + err;
    });
}

function submitConfigure() {
  var msg = document.getElementById('ab-msg');
  if (!document.getElementById('ab-confirm').checked) {
    msg.style.color = '#dc3545';
    msg.textContent = 'Tick the confirmation checkbox first.';
    return;
  }
  msg.style.color = '';
  msg.textContent = 'Configuring (may take 10-30s)…';
  document.getElementById('ab-submit-btn').disabled = true;
  fetch('/api/storage/archive_backend/configure', {
    method: 'POST',
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(_archiveBackendBody()),
  })
    .then(function(r) { return r.json().then(function(b) { return [r.status, b]; }); })
    .then(function(pair) {
      if (pair[0] === 200) {
        loadArchiveBackend();
      } else {
        msg.style.color = '#dc3545';
        msg.textContent = 'Failed: ' + (pair[1].error || pair[1]);
        document.getElementById('ab-submit-btn').disabled = false;
      }
    })
    .catch(function(err) {
      msg.style.color = '#dc3545';
      msg.textContent = 'Network error: ' + err;
      document.getElementById('ab-submit-btn').disabled = false;
    });
}

function loadArchiveBackend() {
  return fetch('/api/storage/archive_backend', {credentials: 'same-origin'})
    .then(function(r) {
      if (!r.ok) {
        throw new Error('HTTP ' + r.status);
      }
      return r.json();
    })
    .then(function(data) {
      renderArchiveBackend(data);
      return data;
    })
    .catch(function(err) {
      var el = document.getElementById('archive-backend-status');
      if (el) {
        el.innerHTML = '<p class="error"><strong>Archive backend status unavailable.</strong> '
          + escSettingsHtml(String(err)) + '</p>';
      }
    });
}

loadOwnerUsername();
loadRemote();
checkForUpdates();
updateSshStatus();
setInterval(updateSshStatus, 5000);
loadArchiveBackend();
