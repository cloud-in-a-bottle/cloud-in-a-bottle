var config = JSON.parse(document.getElementById('page-config').textContent);
const initialRepo = config.initialRepo;
const nextUrl = config.nextUrl;
let cloneDir = null;
let repoUrl = null;

if (initialRepo) {
  document.getElementById('repo-url').value = initialRepo;
  cloneApp(initialRepo);
}

function showError(msg) {
  const el = document.getElementById('error');
  el.textContent = msg;
  el.style.display = '';
}

function clearError() {
  document.getElementById('error').style.display = 'none';
}

function show(id) { document.getElementById(id).style.display = ''; }
function hide(id) { document.getElementById(id).style.display = 'none'; }

async function cloneApp(url) {
  clearError();
  repoUrl = url || document.getElementById('repo-url').value.trim();
  if (!repoUrl) { showError('No repository URL provided'); return; }

  hide('clone-form');
  hide('confirm-section');
  show('cloning-status');

  let resp;
  try {
    resp = await fetch('/api/clone_and_get_app_info', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({repo_url: repoUrl}),
    });
  } catch (e) {
    showError('Network error: ' + e.message);
    show('clone-form');
    hide('cloning-status');
    return;
  }

  const data = await resp.json();
  hide('cloning-status');

  if (data.authorize_url) {
    window.location = data.authorize_url;
    return;
  }

  if (data.error) {
    showError(data.error);
    show('clone-form');
    return;
  }

  cloneDir = data.clone_dir;
  renderManifest(data.manifest, repoUrl, data.storage);
  document.getElementById('app-name').value = data.app_name;
  if (data.validation_error) {
    showError(data.validation_error);
  }
  document.getElementById('confirm-header').innerHTML = nextUrl
    ? '<h2>You\'ve received an invite!</h2><p>To accept this invite, you\'ll need to install the app in your compute space. Review the resources and permissions it\'s requesting:</p>'
    : '<h2>Confirm App Deployment</h2><p>Review the resources and permissions this app is requesting:</p>';
  show('confirm-section');
}

function renderManifest(m, url, storage) {
  storage = storage || {};
  let rows = '';
  function section(title, bg) { rows += `<tr><th colspan="2" style="background:${bg};">${title}</th></tr>`; }
  function row(label, val) { rows += `<tr><td>${label}</td><td>${val}</td></tr>`; }

  section('App', '#e8f0fe');
  row('Name', `<strong>${esc(m.name)}</strong> v${esc(m.version)}`);
  if (m.description) row('Description', esc(m.description));
  if (url) row('Source', `<a href="${esc(url)}" target="_blank">${esc(url)}</a>`);

  section('Resources', '#e8f0fe');
  row('Memory', m.memory_mb + ' MB');
  row('CPU', m.cpu_cores + ' cores');
  if (m.gpu) row('GPU', 'Yes');

  if (m.sqlite_dbs.length) {
    section('Data', '#e8f0fe');
    row('SQLite databases', m.sqlite_dbs.map(esc).join(', '));
  }

  const wantsAllData = m.access_all_data;
  const wantsAllAppData = m.access_all_app_data;
  const wantsAllArchive = m.access_all_archive;
  const hasFs = m.app_data || m.app_temp_data || m.access_vm_data || m.access_all_data || m.access_all_app_data || m.access_all_archive || m.app_archive || m.sqlite_dbs.length;
  if (hasFs) {
    section('Filesystem Permissions', '#fff3cd');
    if (wantsAllData) {
      row('Full data access', 'Read/write to ALL apps\u2019 permanent data, temporary data, VM data, and archive data (rw)');
    } else {
      if (wantsAllAppData) {
        row('Full app data access', 'Read/write to ALL apps\u2019 permanent data, temporary data, and VM data (rw)');
      } else {
        if (m.app_data || m.sqlite_dbs.length)
          row('Permanent data', "Read/write to app's own permanent data directory");
        if (m.app_temp_data) row('Temporary data', "Read/write to app's own temporary data directory");
        if (m.access_vm_data) row('VM data', 'Read-only access to VM shared data');
      }
      if (wantsAllArchive) {
        row('Full archive access', 'Read/write to ALL apps\u2019 archive (bulk) data');
      } else if (m.app_archive) {
        row('Archive data', "Read/write to app's own archive (bulk) storage directory");
      }
    }
  }

  // Storage tier summary + durability notice (mirrors the permissions UX).
  if (storage.uses_archive) {
    section('Storage', '#fff3cd');
    if (storage.archive_backend === 's3') {
      row('Archive tier', 'S3-backed (durable object storage)');
    } else if (storage.archive_backend === 'local') {
      row('Archive tier', 'Local disk on this instance (not durable object storage yet)');
    }
  }

  if (m.public_paths.length) {
    section('Public Routes', '#fff3cd');
    row('Accessible without login', m.public_paths.map(p => `<code>${esc(p)}</code>`).join(', '));
  }

  if (m.links && m.links.length) {
    section('Links', '#e8f0fe');
    for (const link of m.links) {
      row(esc(link.name), `<code>${esc(link.path)}</code>`);
    }
  }

  if (m.network_host) {
    section('Host Networking', '#f8d7da');
    row('<strong>WARNING</strong>',
      '<strong>This app uses host networking, which disables all network isolation. ' +
      'It can access any port on the host, including other apps. ' +
      'Only install if you trust this app completely.</strong>');
  }

  if (m.capabilities.length || m.devices.length || m.shm_mb) {
    section('Container Permissions', '#fff3cd');
    if (m.capabilities.length) row('Linux capabilities', m.capabilities.join(', '));
    if (m.devices.length) row('Devices', m.devices.join(', '));
    if (m.shm_mb) row('Shared memory', m.shm_mb + ' MB');
  }

  if (m.consumes_services_v2 && m.consumes_services_v2.length) {
    section('Service Permissions', '#fff3cd');
    let permIdx = 0;
    for (const svc of m.consumes_services_v2) {
      for (const g of svc.grants) {
        const grantStr = typeof g === 'string' ? g : JSON.stringify(g);
        const id = 'perm-grant-' + permIdx;
        row(
          `<label style="display:flex;align-items:center;gap:0.4em;cursor:pointer">
            <input type="checkbox" checked class="perm-v2-checkbox" id="${id}"
              data-service="${esc(svc.service)}" data-grant='${esc(JSON.stringify(g))}'>
            ${esc(svc.shortname)}
          </label>`,
          `<code>${esc(svc.service)}</code> (${esc(svc.version)})<br>Grant: <code>${esc(grantStr)}</code>`
        );
        permIdx++;
      }
    }

  }

  document.getElementById('manifest-table').innerHTML = rows;

  // Render port mappings section
  renderPortMappings(m.port_mappings || []);
}

let portConflicts = new Set();
let portPollInterval = null;
let portDebounceTimers = {};

function renderPortMappings(mappings) {
  const section = document.getElementById('port-mappings-section');
  const body = document.getElementById('port-mappings-body');
  if (!mappings.length) { section.style.display = 'none'; return; }
  section.style.display = '';
  portConflicts.clear();
  let html = '';
  for (const pm of mappings) {
    html += `<tr data-label="${esc(pm.label)}">
      <td><code>${esc(pm.label)}</code></td>
      <td>${pm.container_port}</td>
      <td>
        <input type="number" class="port-host-input" data-label="${esc(pm.label)}"
               value="${pm.host_port}" min="0" max="65535"
               style="width:6em; font-family:monospace;"
               oninput="onPortInput(this)">
      </td>
      <td class="port-status" data-label="${esc(pm.label)}">
        ${pm.host_port === 0 ? '<span style="color:#888">auto-assign</span>' : ''}
      </td>
    </tr>`;
  }
  body.innerHTML = html;

  // Check availability once on initial render for any pre-set ports
  for (const pm of mappings) {
    if (pm.host_port > 0) {
      checkPortAvailability(pm.label, pm.host_port);
    }
  }

  // Poll every 15s to catch status changes while user reviews
  if (portPollInterval) clearInterval(portPollInterval);
  portPollInterval = setInterval(() => recheckAllPorts(), 15000);
}

function onPortInput(el) {
  const label = el.dataset.label;
  const val = parseInt(el.value, 10);
  const statusEl = document.querySelector(`.port-status[data-label="${label}"]`);

  if (isNaN(val) || val === 0) {
    statusEl.innerHTML = '<span style="color:#888">auto-assign</span>';
    portConflicts.delete(label);
    updateDeployButton();
    return;
  }

  // Debounce: check after 400ms of no typing
  statusEl.innerHTML = '<span style="color:#888">checking…</span>';
  if (portDebounceTimers[label]) clearTimeout(portDebounceTimers[label]);
  portDebounceTimers[label] = setTimeout(() => checkPortAvailability(label, val), 400);
}

function formatUsedBy(usedBy) {
  if (!usedBy) return 'unknown';
  if (usedBy.type === 'main_port') return `${esc(usedBy.app_name)} (main port)`;
  if (usedBy.type === 'port_mapping') return `${esc(usedBy.app_name)} (port '${esc(usedBy.label)}')`;
  return 'host-level service';
}

async function checkPortAvailability(label, port) {
  const statusEl = document.querySelector(`.port-status[data-label="${label}"]`);
  try {
    const resp = await fetch(`/api/check_port?port=${port}`);
    const data = await resp.json();
    if (data.available) {
      statusEl.innerHTML = '<span style="color:green">✓ available</span>';
      portConflicts.delete(label);
    } else {
      statusEl.innerHTML = `<span style="color:red">✗ in use by ${formatUsedBy(data.used_by)}</span>`;
      portConflicts.add(label);
    }
  } catch (e) {
    statusEl.innerHTML = '<span style="color:orange">check failed</span>';
    portConflicts.delete(label);
  }
  updateDeployButton();
}

async function recheckAllPorts() {
  const inputs = document.querySelectorAll('.port-host-input');
  const checks = [];
  for (const input of inputs) {
    const val = parseInt(input.value, 10);
    if (!isNaN(val) && val > 0) {
      checks.push(checkPortAvailability(input.dataset.label, val));
    }
  }
  await Promise.all(checks);
  return portConflicts.size === 0;
}

function updateDeployButton() {
  const btn = document.querySelector('#confirm-section .btn-primary');
  if (btn) btn.disabled = portConflicts.size > 0;
}

async function deployApp() {
  clearError();
  const appName = document.getElementById('app-name').value.trim();
  if (!appName) { showError('App name is required'); return; }

  // Re-check all port assignments before deploying
  const portsOk = await recheckAllPorts();
  if (!portsOk) {
    showError('Some port assignments have conflicts — please fix them before deploying.');
    return;
  }

  if (portPollInterval) clearInterval(portPollInterval);

  hide('confirm-section');
  show('installing-status');

  // Collect typed port overrides keyed by label.
  const portOverrides = {};
  document.querySelectorAll('.port-host-input').forEach(input => {
    const val = parseInt(input.value, 10);
    if (!isNaN(val) && val > 0) {
      portOverrides[input.dataset.label] = val;
    }
  });

  // Collect permissions the owner approved (checked checkboxes).
  const permGrants = [];
  document.querySelectorAll('.perm-v2-checkbox:checked').forEach(cb => {
    permGrants.push({service_url: cb.dataset.service, grant: JSON.parse(cb.dataset.grant)});
  });

  const body = {repo_url: repoUrl, app_name: appName, port_overrides: portOverrides, permissions_v2_grants: permGrants};
  if (cloneDir) body.clone_dir = cloneDir;

  let resp;
  try {
    resp = await fetch('/api/add_app', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
  } catch (e) {
    hide('installing-status');
    showError('Network error: ' + e.message);
    show('confirm-section');
    return;
  }

  const data = await resp.json();

  if (data.authorize_url) {
    window.location = data.authorize_url;
    return;
  }

  if (data.error) {
    hide('installing-status');
    showError(data.error);
    show('confirm-section');
    return;
  }

  const detailUrl = '/app_detail/' + encodeURIComponent(data.app_name)
    + (nextUrl ? '?next=' + encodeURIComponent(nextUrl) : '');
  window.location = detailUrl;
}

function cancelDeploy() {
  if (portPollInterval) clearInterval(portPollInterval);
  clearError();
  cloneDir = null;
  repoUrl = null;
  hide('confirm-section');
  show('clone-form');
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
