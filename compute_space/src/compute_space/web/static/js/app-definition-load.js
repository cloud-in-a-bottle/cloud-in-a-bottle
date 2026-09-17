(function() {
  'use strict';

  var fileInput = document.getElementById('app-definition-file');
  var apps = document.getElementById('app-definition-apps');
  var secrets = document.getElementById('app-definition-secret-keys');
  var missing = document.getElementById('app-definition-missing-keys');
  var consentLabel = document.getElementById('app-definition-consent-label');
  var consentText = document.getElementById('app-definition-consent-text');
  var consent = document.getElementById('app-definition-consent');
  var button = document.getElementById('app-definition-deploy');
  var status = document.getElementById('app-definition-load-status');
  var progress = document.getElementById('app-definition-progress');
  var generation = 0;
  var controller = null;
  var plan = null;
  var content = null;
  var loading = false;
  var rows = [];

  function object(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value);
  }

  function text(value) { return typeof value === 'string' && value.trim().length > 0; }
  function keys(value) {
    return Array.isArray(value) && value.every(function(key) { return typeof key === 'string' && key.length > 0; })
      && new Set(value).size === value.length;
  }

  function keyNames(values) {
    return values.map(function(key) {
      if (!/[\s\p{C}",\\]/u.test(key)) return key;
      // Quote ambiguous names and escape nonprinting Unicode without changing the actual keys.
      return JSON.stringify(key).replace(/[^\x20-\x7e]/g, function(character) {
        return '\\u' + character.charCodeAt(0).toString(16).padStart(4, '0');
      });
    }).join(', ');
  }

  function validPlan(value) {
    return object(value) && value.schema_version === 1 && ['sharing', 'private'].includes(value.mode)
      && !('secret_values' in value) && keys(value.secret_keys) && keys(value.missing_secret_keys)
      && (value.mode === 'private' || value.secret_keys.length === 0)
      && Array.isArray(value.apps) && new Set(value.apps.map(function(app) { return app && app.name; })).size === value.apps.length
      && value.apps.every(function(app) {
        if (!object(app) || !text(app.name) || ['.', '..'].includes(app.name) || !text(app.source_label)
            || !['ready', 'existing', 'unavailable'].includes(app.status) || !keys(app.secret_keys)) return false;
        if (app.status !== 'ready') return true;
        var install = app.install;
        // Only the normal installer's explicit payload is accepted, never its all-permissions/clone shortcuts.
        return object(install) && Object.keys(install).sort().join(',') === 'app_name,permissions_v2_grants,port_overrides,repo_url'
          && install.app_name === app.name && text(install.repo_url) && object(install.port_overrides)
          && Object.values(install.port_overrides).every(function(port) { return Number.isInteger(port) && port >= 0 && port <= 65535; })
          && Array.isArray(install.permissions_v2_grants) && install.permissions_v2_grants.every(function(permission) {
            return object(permission) && text(permission.service_url) && object(permission.grant);
          });
      });
  }

  function needsConsent() { return plan && plan.mode === 'private' && plan.secret_keys.length > 0; }

  function syncButton() {
    button.disabled = loading || !plan || (needsConsent() && !consent.checked)
      || (!plan.secret_keys.length && !plan.apps.some(function(app) { return app.status === 'ready'; }));
  }

  function clear() {
    generation += 1;
    if (controller) controller.abort();
    controller = null;
    plan = null;
    content = null;
    loading = false;
    rows = [];
    apps.replaceChildren();
    apps.hidden = true;
    secrets.textContent = '';
    secrets.hidden = true;
    missing.textContent = '';
    missing.hidden = true;
    consent.checked = false;
    consent.disabled = false;
    consentLabel.hidden = true;
    consentText.textContent = '';
    fileInput.disabled = false;
    button.disabled = true;
    status.textContent = '';
    progress.hidden = true;
  }

  function reset() {
    clear();
    fileInput.value = '';
  }

  function link(parent, label, href) {
    var anchor = document.createElement('a');
    anchor.textContent = label;
    anchor.href = href;
    parent.append(' ', anchor);
  }

  function render() {
    plan.apps.forEach(function(app) {
      var row = document.createElement('li');
      var name = document.createElement('strong');
      name.textContent = app.name;
      var feedback = document.createElement('span');
      feedback.textContent = app.status === 'ready' ? 'Ready to deploy'
        : app.status === 'existing' ? 'Skipped: already exists' : 'Skipped: source unavailable on this system';
      row.append(name, ': ' + app.source_label + '. ', feedback);
      if (app.status === 'existing') link(row, 'App details', '/app_detail/' + encodeURIComponent(app.name));
      var references = document.createElement('div');
      references.className = 'hint';
      references.textContent = app.secret_keys.length + ' secret keys' + (app.secret_keys.length ? ': ' + keyNames(app.secret_keys) : '.');
      row.append(references);
      apps.append(row);
      rows.push(feedback);
    });
    apps.hidden = plan.apps.length === 0;
    secrets.hidden = false;
    secrets.textContent = plan.secret_keys.length + ' secret values in file'
      + (plan.secret_keys.length ? ': ' + keyNames(plan.secret_keys) : '.');
    missing.hidden = plan.missing_secret_keys.length === 0;
    missing.textContent = plan.missing_secret_keys.length ? 'Missing secret references (' + plan.missing_secret_keys.length
      + '): ' + keyNames(plan.missing_secret_keys) + '. Configure these before using the apps.' : '';
    consentLabel.hidden = !needsConsent();
    consentText.textContent = 'Import ' + plan.secret_keys.length + ' secret values (replaces existing values with the same names)';
    syncButton();
    status.textContent = needsConsent() ? 'Confirm secret replacement to load.'
      : button.disabled ? 'Nothing to load.' : 'Ready to load.';
  }

  function post(url, payload) {
    return fetch(url, {
      method: 'POST', credentials: 'same-origin', cache: 'no-store', redirect: 'error',
      headers: {'Accept': 'application/json', 'Content-Type': 'application/json'},
      body: JSON.stringify(payload), signal: controller.signal,
    });
  }

  function json(response) {
    if (response.redirected || (response.headers.get('Content-Type') || '').split(';')[0].trim().toLowerCase() !== 'application/json') {
      throw new Error('Invalid response');
    }
    return response.json();
  }

  fileInput.addEventListener('change', async function() {
    if (loading) return;
    var file = fileInput.files[0];
    clear();
    if (!file) return;
    if (file.size > 1024 * 1024) {
      fileInput.value = '';
      status.textContent = 'Choose a YAML file no larger than 1 MiB.';
      return;
    }
    var requestGeneration = generation;
    controller = new AbortController();
    function current() { return generation === requestGeneration; }
    status.textContent = 'Reading YAML…';
    try {
      var bytes = await file.arrayBuffer();
      file = null;
      if (!current()) return;
      content = new TextDecoder('utf-8', {fatal: true}).decode(bytes);
      new Uint8Array(bytes).fill(0);
      bytes = null;
      var response = await post('/api/app-definitions/parse', {content: content});
      if (!current()) return;
      if (!response.ok) throw new Error('Parse failed');
      var parsed = await json(response);
      if (!current()) return;
      if (!validPlan(parsed)) throw new Error('Invalid plan');
      plan = parsed;
      if (!needsConsent()) content = null;
      render();
    } catch (error) {
      if (current()) {
        reset();
        status.textContent = 'Could not read app definitions. Check the YAML file and your owner login, then choose the file again.';
      }
    } finally {
      if (bytes) new Uint8Array(bytes).fill(0);
      bytes = null;
      file = null;
      if (current()) controller = null;
    }
  });

  consent.addEventListener('change', syncButton);

  button.addEventListener('click', async function() {
    if (button.disabled || loading || !plan) return;
    loading = true;
    syncButton();
    fileInput.disabled = true;
    consent.disabled = true;
    fileInput.value = '';
    var requestGeneration = generation;
    var loadPlan = plan;
    controller = new AbortController();
    function current() { return generation === requestGeneration; }
    var importing = needsConsent();
    var activeRow = null;
    var stopped = false;
    progress.hidden = false;
    try {
      if (importing) {
        status.textContent = 'Importing secret values…';
        var pending = post('/api/app-definitions/import-secrets', {content: content, replace_existing: true});
        content = null;
        var imported = await pending;
        if (!current()) return;
        if (!imported.ok || (await json(imported)).ok !== true) throw new Error('Import failed');
        if (!current()) return;
      }
      importing = false;
      for (var index = 0; index < loadPlan.apps.length; index += 1) {
        if (!current()) return;
        var app = loadPlan.apps[index];
        if (app.status !== 'ready') continue;
        activeRow = rows[index];
        activeRow.textContent = 'Requesting deployment…';
        status.textContent = 'Requesting deployment for ' + app.name + '…';
        var response = await post('/api/add_app', app.install);
        if (!current()) return;
        // The installer may have committed the app before a server error or gateway timeout.
        if (response.status >= 500) throw new Error('Uncertain deployment');
        // Owner login errors are distinct from the installer's GitHub authorization response.
        var data = null;
        try { data = await json(response); } catch (error) {
          if (response.status !== 401 && response.status !== 403) throw error;
        }
        if (!current()) return;
        if (response.status === 401 && object(data) && data.detail === 'GitHub authorization required'
            && object(data.extra) && text(data.extra.authorize_url)) {
          activeRow.textContent = 'GitHub authorization required.';
          link(activeRow, 'Authorize in Add app', '/add_app?repo=' + encodeURIComponent(app.install.repo_url));
        } else if (response.status === 401 || response.status === 403) {
          activeRow.textContent = 'Owner authentication required. Deployment queue stopped.';
          stopped = true;
          break;
        } else if (!response.ok) {
          activeRow.textContent = 'Deployment failed (HTTP ' + response.status + '). Check the app source, name and ports on the dashboard.';
        } else {
          if (!object(data) || data.ok !== true || !text(data.app_name) || ['.', '..'].includes(data.app_name)) {
            throw new Error('Uncertain deployment');
          }
          activeRow.textContent = 'Deployment started.';
          link(activeRow, 'App details', '/app_detail/' + encodeURIComponent(data.app_name));
        }
        activeRow = null;
      }
      status.textContent = stopped ? 'Owner authentication required. Remaining apps were not requested. Check the dashboard.'
        : 'Load requests finished. Check the dashboard for deployment progress.';
    } catch (error) {
      if (!current()) return;
      stopped = true;
      if (importing) {
        status.textContent = 'Secret import failed. Some values may have been saved. No apps were deployed. Check Secrets before loading again.';
      } else {
        if (activeRow) activeRow.textContent = 'Deployment result uncertain. Check the dashboard before trying again.';
        status.textContent = 'Deployment result uncertain. Remaining apps were not requested. Check the dashboard before trying again.';
      }
    } finally {
      if (current()) {
        if (stopped) rows.forEach(function(row, index) {
          if (loadPlan.apps[index].status === 'ready' && row.textContent === 'Ready to deploy') row.textContent = 'Not requested: load stopped.';
        });
        content = null;
        plan = null;
        controller = null;
        loading = false;
        fileInput.disabled = false;
        consent.checked = false;
        consent.disabled = true;
        button.disabled = true;
      }
    }
  });

  window.addEventListener('pagehide', reset);
  window.addEventListener('pageshow', function() {
    reset();
    var showGeneration = generation;
    // Native history restoration may happen after pageshow, including with BFCache.
    setTimeout(function() { if (generation === showGeneration) reset(); }, 0);
  });
  reset();
})();
