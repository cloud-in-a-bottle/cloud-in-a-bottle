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
    el.hidden = false;
  }, 100);
}
function clearError() { document.getElementById('error').hidden = true; }
function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

// Returns the fetched update state ({state, error}) so callers like setRemote
// can react to it, or null when the check itself failed.
async function checkForUpdates() {
  clearError();
  const el = document.getElementById('update-status');
  el.innerHTML = '<p class="msg">Checking for updates&hellip;</p>';

  try {
    const resp = await fetch('/api/settings/update');
    if (!resp.ok) {
      const err = await resp.json();
      const detail = responseErrorMessage(err, '');
      el.innerHTML = '<p class="msg msg--error">Repo is in an invalid state for updating (no .git perhaps?)</p>'
        + (detail ? '<div class="error-inline">' + esc(detail) + '</div>' : '')
        + '<div class="actions"><button onclick="checkForUpdates()" class="btn">Retry</button></div>';
      return null;
    }
    const data = await resp.json();

    const checkAgainBtn = '<button onclick="checkForUpdates()" class="btn">Check again</button>';

    if (data.state === 'UP_TO_DATE') {
      el.innerHTML = '<p class="msg msg--ok">Up to date.</p>'
        + '<div class="actions">' + checkAgainBtn + '</div>';
    } else if (data.state === 'UPDATE_AVAILABLE') {
      const notice = data.error ? '<div class="error-inline">' + esc(data.error) + '</div>' : '';
      el.innerHTML = '<p class="msg">Updates available.</p>'
        + notice
        + '<div class="actions">'
        + '<button onclick="applyUpdate()" class="btn btn--primary">Update &amp; restart</button>'
        + checkAgainBtn + '</div>';
    } else if (data.state === 'ERROR') {
      el.innerHTML = '<p class="msg msg--error">Update check failed.</p>'
        + '<div class="error-inline">' + esc(data.error || 'Unknown error') + '</div>'
        + '<div class="actions">' + checkAgainBtn + '</div>';
    }
    return data;
  } catch (e) {
    showError('Failed to check for updates: ' + e.message);
    el.innerHTML = '<div class="actions"><button onclick="checkForUpdates()" class="btn">Retry</button></div>';
    return null;
  }
}

async function applyUpdate() {
  clearError();
  const el = document.getElementById('update-status');
  el.innerHTML = '<p class="msg">Starting update&hellip;</p>';

  let token;
  try {
    // Kicks off the update in the background and returns a token that lets the
    // dedicated /updating page recognize this tab and stream live progress from
    // the detached updater across the (brief) compute_space restart.
    const resp = await fetch('/api/settings/update', {method: 'POST'});
    if (!resp.ok) {
      const err = await resp.json();
      el.innerHTML = '<p class="msg msg--error">' + esc(responseErrorMessage(err, '')) + '</p>'
        + '<div class="actions"><button onclick="checkForUpdates()" class="btn">Retry</button></div>';
      return;
    }
    const data = await resp.json();
    token = data.token;
  } catch (e) {
    el.innerHTML = '<p class="msg msg--error">Update failed: ' + esc(e.message) + '</p>'
      + '<div class="actions"><button onclick="checkForUpdates()" class="btn">Retry</button></div>';
    return;
  }

  // Navigate to the dedicated update page. It renders live progress and, once
  // the new instance is back, reloads into the dashboard. Carrying the token in
  // the URL is what lets the detached updater show *this* owner the logs.
  window.location.href = '/updating?token=' + encodeURIComponent(token || '');
}

function showRestartOverlay() {
  document.getElementById('restart-overlay').hidden = false;
  document.getElementById('update-status').hidden = true;
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
    msg.className = 'msg msg--error';
    msg.hidden = false;
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
  msg.hidden = true;

  try {
    const resp = await fetch('/api/settings/set-remote', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url}),
    });
    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(responseErrorMessage(err, 'failed to set remote'));
    }
    // Re-baseline from the normalized RemoteInfo so the button stays greyed
    // out until the operator edits again (mirrors loadRemote's url@ref shape).
    const saved = await resp.json();
    savedRemote = (saved.url || '') + (saved.pinned && saved.ref ? '@' + saved.ref : '');
    input.value = savedRemote;
    msg.textContent = 'Remote saved.';
    msg.className = 'msg';
    msg.hidden = false;
    // set-remote only records the pin; moving to it is the update walk's job
    // (checkout+migrate+install+restart, in order). Kick the walk off now when
    // the pin resolves to different code than HEAD — but not on a dirty tree
    // (surfaced as UPDATE_AVAILABLE with a notice), which the walk refuses.
    const status = await checkForUpdates();
    if (status && status.state === 'UPDATE_AVAILABLE' && !status.error) {
      await applyUpdate();
    }
  } catch (e) {
    msg.textContent = e.message;
    msg.className = 'msg msg--error';
    msg.hidden = false;
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
      throw new Error(responseErrorMessage(err, 'failed to change password'));
    }
    msg.textContent = 'Password changed successfully';
    msg.className = 'msg';
    msg.hidden = false;
    msg.className = 'msg msg--ok';
    document.getElementById('current-pw').value = '';
    document.getElementById('new-pw').value = '';
    document.getElementById('confirm-pw').value = '';
  } catch (e) {
    msg.textContent = e.message;
    msg.className = 'msg msg--error';
    msg.hidden = false;
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
      throw new Error(responseErrorMessage(err, 'failed to load'));
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
        msg.className = 'msg msg--error';
        msg.hidden = false;
      } else {
        msg.hidden = true;
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
    msg.className = 'msg msg--error';
    msg.hidden = false;
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
    msg.className = 'msg msg--error';
    msg.hidden = false;
    return;
  }

  btn.disabled = true;
  msg.hidden = true;

  try {
    const resp = await fetch('/api/settings/owner_username', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username}),
    });
    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(responseErrorMessage(err, 'failed to save'));
    }
    const data = await resp.json();
    savedUsername = data.username;
    msg.textContent = 'Saved.';
    msg.className = 'msg msg--ok';
    msg.hidden = false;
    setTimeout(() => { msg.hidden = true; }, 4000);
  } catch (e) {
    msg.textContent = e.message;
    msg.className = 'msg msg--error';
    msg.hidden = false;
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
  msg.className = 'msg';
  msg.textContent = 'Dropping cache…';

  fetch('/api/drop-docker-cache', {method: 'POST', credentials: 'same-origin'})
    .then(readJsonResponse)
    .then(function(res) {
      if (!res.ok) {
        msg.className = 'msg msg--error';
        msg.textContent = 'Drop failed: ' + responseErrorMessage(res.data, 'unknown error');
        return;
      }
      var data = res.data;
      var reclaimed = '';
      if (data.output) {
        var match = data.output.match(/Total reclaimed space:\s*(.+)/i);
        if (match && match[1]) {
          reclaimed = ' Freed ' + match[1] + '.';
        }
      }
      msg.className = 'msg msg--ok';
      msg.textContent = 'Build cache dropped.' + reclaimed;
    })
    .catch(function() {
      msg.className = 'msg msg--error';
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
        btn.className = 'btn btn--danger';
        status.textContent = 'SSH active';
        status.className = 'status-text status-text--error';
      } else {
        btn.textContent = 'Enable SSH';
        btn.className = 'btn';
        status.textContent = 'SSH disabled';
        status.className = 'status-text status-text--muted';
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
      + '<td><span class="status-text status-text--ok">S3 (JuiceFS)</span>'
      + (state.state_message ? ' <span class="error">' + escSettingsHtml(state.state_message) + '</span>' : '')
      + '</td></tr>';
    var bucketLine = escSettingsHtml(state.s3_bucket || '?')
      + (state.s3_prefix ? '/' + escSettingsHtml(state.s3_prefix) : '')
      + (state.s3_region ? ' <span class="hint">(' + escSettingsHtml(state.s3_region) + ')</span>' : '');
    rows += '<tr><th>S3 bucket</th><td><code class="path-value">' + bucketLine + '</code></td></tr>';
    if (state.s3_access_key_id) {
      rows += '<tr><th>Access key</th><td><code>' + escSettingsHtml(state.s3_access_key_id.slice(0, 4)) + '…</code></td></tr>';
    }
    if (state.archive_dir) {
      rows += '<tr><th>Host path</th><td><code class="path-value">' + escSettingsHtml(state.archive_dir) + '</code></td></tr>';
    }
    if (state.meta_db_path) {
      rows += '<tr><th>Metadata DB</th><td><code class="path-value">' + escSettingsHtml(state.meta_db_path) + '</code>'
        + ' <span class="error">(must back up to survive disk loss)</span></td></tr>';
    }
    var dumps = state.meta_dumps;
    var dumpLine;
    if (dumps && dumps.count > 0) {
      dumpLine = '<code class="path-value">' + escSettingsHtml(dumps.latest_at || '?') + '</code> <span class="hint">(' + dumps.count + ' in bucket, hourly cadence)</span>';
    } else if (dumps && dumps.count === 0) {
      dumpLine = '<span class="error">No metadata dumps in bucket yet.</span> <span class="hint">JuiceFS writes one within an hour of mount.</span>';
    } else {
      dumpLine = '<span class="hint">unavailable; could not list <code class="path-value">'
        + escSettingsHtml((state.juicefs_volume_name ? state.juicefs_volume_name + '/' : '') + 'meta/')
        + '</code></span>';
    }
    rows += '<tr><th>Latest meta dump</th><td>' + dumpLine + '</td></tr>';
  } else if (state.backend === 'local') {
    rows += '<tr><th>Backend</th>'
      + '<td><span class="status-text">Local disk (JuiceFS)</span>'
      + (state.state_message ? ' <span class="error">' + escSettingsHtml(state.state_message) + '</span>' : '')
      + '</td></tr>';
    if (state.archive_dir) {
      rows += '<tr><th>Host path</th><td><code class="path-value">' + escSettingsHtml(state.archive_dir) + '</code></td></tr>';
    }
    rows += '<tr><th>Durability</th><td><span class="status-text status-text--warn">Local disk only</span> '
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
      + '<td><span class="status-text status-text--muted">Not configured</span></td></tr>';
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
    configureBtn = '<div class="action-bar"><button class="btn" id="archive-backend-configure-btn">Upgrade to S3 backend…</button></div>';
  } else if (state.backend === 'disabled') {
    configureBtn = '<div class="action-bar"><button class="btn" id="archive-backend-configure-btn">Configure S3 backend…</button></div>';
  } else if (state.backend === 's3') {
    configureBtn = '<div class="action-bar"><button class="btn" id="archive-backend-configure-btn">Migrate to a new bucket…</button></div>';
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
    migrateNote = '<p class="notice notice--warn"><strong>This migrates your existing LOCAL archive data into S3.</strong> '
      + 'JuiceFS copies the archive objects into the bucket (verified with <code>--check-all</code>) and re-points the volume; if anything fails the switch is aborted and your local data is left intact (fail-open). '
      + 'After a successful migration the local copy is removed and the switch to S3 is <strong>one-way</strong>.'
      + appsLine + '</p>';
  } else if (state.backend === 's3') {
    migrateNote = '<p class="notice notice--warn"><strong>This migrates your archive from the current bucket (<code class="path-value">'
      + escSettingsHtml(state.s3_bucket || '?') + '</code>) to the NEW bucket below.</strong> '
      + 'JuiceFS copies every archive object to the new bucket (verified with <code>--check-all</code>) and re-points the volume; if anything fails the switch is aborted and your current bucket is left intact (fail-open). '
      + 'After a successful migration the old bucket\u2019s objects (under this zone\u2019s prefix only) are reclaimed.</p>';
  }
  formEl.innerHTML = '<p><strong>Configure S3 archive storage.</strong> JuiceFS will format the bucket and mount it locally; this is a one-time operation.</p>'
    + migrateNote
    + '<p class="notice notice--warn"><strong>Experimental.</strong> Filename-to-S3-chunk mappings live in a SQLite metadata DB on this zone’s local disk, not in the bucket. If the local disk is wiped, the bucket bytes can be recovered only from JuiceFS\'s periodic meta dumps in S3 (replayed via <code>juicefs load</code>).</p>'
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
    + '<p><label class="field--check"><input type="checkbox" id="ab-confirm"><span> I understand the S3 archive backend is experimental'
    + (state.backend === 'local'
        ? ' and that my existing local archive data will be migrated into S3.'
        : state.backend === 's3'
        ? ' and that my archive will be migrated to the new bucket and the old bucket reclaimed.'
        : ' and that this configures S3 for the archive tier.')
    + '</span></label></p>'
    + '<div class="action-bar">'
    + '<button class="btn" id="ab-test-btn">Test connection</button>'
    + '<button class="btn btn--primary" id="ab-submit-btn">Configure</button>'
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
  msg.className = 'hint';
  fetch('/api/storage/archive_backend/test_connection', {
    method: 'POST',
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(_archiveBackendBody()),
  })
    .then(readJsonResponse)
    .then(function(res) {
      msg.className = res.ok ? 'msg msg--ok' : 'msg msg--error';
      if (res.ok) { msg.textContent = 'Bucket reachable'; return; }
      msg.textContent = 'Failed: ' + responseErrorMessage(res.data, '');
    })
    .catch(function(err) {
      msg.className = 'msg msg--error';
      msg.textContent = 'Network error: ' + err;
    });
}

function submitConfigure() {
  var msg = document.getElementById('ab-msg');
  if (!document.getElementById('ab-confirm').checked) {
    msg.className = 'msg msg--error';
    msg.textContent = 'Tick the confirmation checkbox first.';
    return;
  }
  msg.className = 'hint';
  msg.textContent = 'Configuring (may take 10-30s)…';
  document.getElementById('ab-submit-btn').disabled = true;
  fetch('/api/storage/archive_backend/configure', {
    method: 'POST',
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(_archiveBackendBody()),
  })
    .then(readJsonResponse)
    .then(function(res) {
      if (res.ok) {
        loadArchiveBackend();
      } else {
        msg.className = 'msg msg--error';
        msg.textContent = 'Failed: ' + responseErrorMessage(res.data, '');
        document.getElementById('ab-submit-btn').disabled = false;
      }
    })
    .catch(function(err) {
      msg.className = 'msg msg--error';
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
        dom.replace(el, dom.el('p', {class: 'notice notice--error'}, [
          dom.el('strong', {text: 'Archive backend status unavailable.'}), ' ', String(err),
        ]));
      }
    });
}

// --- Connect to Imbue -------------------------------------------------------

async function loadConnectImbueStatus() {
  const section = document.getElementById('connect-imbue-section');
  const state = document.getElementById('connect-imbue-state');
  const btn = document.getElementById('connect-imbue-btn');
  try {
    const resp = await fetch('/api/settings/connect-imbue/status');
    if (!resp.ok) return;  // feature not present; leave the section hidden
    const data = await resp.json();
    if (!data.available) return;  // no Imbue URL configured
    section.hidden = false;
    if (data.connected) {
      state.textContent = 'Connected to Imbue.';
      btn.textContent = 'Reconnect to Imbue';
    } else {
      state.textContent = 'Not connected.';
      btn.textContent = 'Connect to Imbue';
    }
    btn.hidden = false;
  } catch (e) {
    // Non-fatal: the section just stays hidden.
  }
}

async function connectImbue() {
  clearError();
  const btn = document.getElementById('connect-imbue-btn');
  const msg = document.getElementById('connect-imbue-msg');
  btn.disabled = true;
  try {
    const resp = await fetch('/api/settings/connect-imbue/start', { method: 'POST' });
    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(responseErrorMessage(err, 'failed to start'));
    }
    const data = await resp.json();
    // Hand off to Imbue to authorize; it returns to this instance's callback,
    // which stores the credential.
    window.location.href = data.redirect_url;
  } catch (e) {
    btn.disabled = false;
    msg.textContent = 'Could not start connect: ' + e.message;
    msg.className = 'msg msg--error';
    msg.hidden = false;
  }
}

// Surface the ?connect=ok|error result of a completed callback round-trip.
(function showConnectResult() {
  const params = new URLSearchParams(window.location.search);
  const result = params.get('connect');
  if (!result) return;
  const msg = document.getElementById('connect-imbue-msg');
  if (!msg) return;
  if (result === 'ok') {
    msg.textContent = 'Connected to Imbue.';
    msg.className = 'msg msg--ok';
  } else {
    msg.textContent = 'Connecting to Imbue failed. Please try again.';
    msg.className = 'msg msg--error';
  }
  msg.hidden = false;
})();

loadOwnerUsername();
loadRemote();
checkForUpdates();
updateSshStatus();
setInterval(updateSshStatus, 5000);
loadArchiveBackend();
loadConnectImbueStatus();
