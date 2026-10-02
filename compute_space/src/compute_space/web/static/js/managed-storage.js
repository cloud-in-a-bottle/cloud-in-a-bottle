(function (global) {
  'use strict';

  var accessLabels = {read_write: 'Read and write', read_only: 'Read-only', suspended: 'Access paused'};
  var phases = ['reserved', 'bucket_ready', 'token_pending', 'activating', 'ready'];

  function element(tag, text, className) {
    var node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  function number(value) { return typeof value === 'number' && Number.isFinite(value) && value >= 0; }
  function validDate(value) {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    var parsed = new Date(value);
    return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
  }
  function validStatus(data, allocation) {
    if (!data || data.version !== 1 || data.allocation_id !== allocation || phases.indexOf(data.phase) === -1
        || !Object.hasOwn(accessLabels, data.applied_access) || !Object.hasOwn(accessLabels, data.desired_access)
        || !number(data.capacity_bytes) || data.capacity_bytes === 0 || !number(data.reported_at)
        || typeof data.stale !== 'boolean' || typeof data.enforcement_enabled !== 'boolean') return false;
    if (![data.observed_at, data.applied_at].every(function (v) { return v === null || number(v); })) return false;
    var usage = data.usage;
    return usage === null || !!(usage
      && ['operation_microcents', 'read_only_at_microcents', 'suspend_at_microcents']
        .every(function (key) { return number(usage[key]); })
      && ((usage.used_bytes === null && usage.sample_at === null) || (number(usage.used_bytes) && number(usage.sample_at)))
      && (usage.storage_microcents === null || number(usage.storage_microcents))
      && (usage.operations_observed_at == null || number(usage.operations_observed_at))
      && usage.read_only_at_microcents > 0 && usage.suspend_at_microcents > usage.read_only_at_microcents
      && validDate(usage.period_start) && validDate(usage.resets_at) && usage.period_start < usage.resets_at);
  }

  function bytes(value) {
    var units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
    var unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
    return value.toLocaleString(undefined, {maximumFractionDigits: 1}) + ' ' + units[unit];
  }

  function money(value) {
    var dollars = value / 100000000;
    if (dollars > 0 && dollars < 0.01) return '<$0.01';
    return dollars.toLocaleString(undefined, {style: 'currency', currency: 'USD'});
  }

  function timestamp(value) {
    if (value === null) return 'Not yet reported';
    var date = new Date(value * 1000);
    return Number.isFinite(date.getTime()) ? date.toLocaleString(undefined, {timeZone: 'UTC'}) + ' UTC' : 'Unavailable';
  }

  function metric(label, value, maximum, format) {
    var ratio = value / maximum * 100;
    var text = format(value) + ' of ' + format(maximum) + ' (' + ratio.toLocaleString(undefined, {maximumFractionDigits: 1}) + '%)';
    var box = element('div', null, 'managed-storage__metric');
    box.append(element('strong', label), element('p', text));
    var bar = element('div', null, 'meter');
    bar.setAttribute('role', 'progressbar');
    bar.setAttribute('aria-label', label);
    bar.setAttribute('aria-valuemin', '0');
    bar.setAttribute('aria-valuemax', '100');
    bar.setAttribute('aria-valuenow', String(Math.min(100, ratio)));
    bar.setAttribute('aria-valuetext', text);
    var fill = element('div', null, 'meter__fill meter__fill--' + (ratio >= 100 ? 'error' : ratio >= 85 ? 'warn' : 'ok'));
    fill.style.width = Math.min(100, ratio) + '%';
    bar.append(fill);
    box.append(bar);
    return box;
  }

  function create(root, options) {
    options = options || {};
    var fetcher = options.fetch || global.fetch.bind(global);
    var allocation = null, revision = 0, request = null, timer = null, last = null;
    var header = element('div', null, 'managed-storage__header');
    var heading = element('h3', 'Cloud storage allowance');
    heading.id = root.id + '-heading';
    root.setAttribute('role', 'region');
    root.setAttribute('aria-labelledby', heading.id);
    var refresh = element('button', 'Refresh usage', 'btn');
    refresh.type = 'button';
    header.append(heading, refresh);
    var message = element('p', null, 'hint');
    message.setAttribute('role', 'status');
    message.setAttribute('aria-live', 'polite');
    var details = element('div');
    root.replaceChildren(header, message, details);
    root.hidden = true;

    function notice(text, kind) { return element('p', text, 'notice notice--' + kind); }

    function render(data, failed) {
      var oldExplanation = details.querySelector('details');
      var expanded = oldExplanation && oldExplanation.open;
      var explanationFocused = document.activeElement === details.querySelector('summary');
      details.replaceChildren();
      if (data.stale || failed) {
        details.append(notice('Usage or access status may be out of date. These are the last reported values.', 'warn'));
      }
      if (data.phase !== 'ready') {
        details.append(notice('Cloud storage setup is in progress.', 'warn'));
      } else if (data.applied_access === 'read_only') {
        details.append(notice('Cloud storage is reported as read-only. Apps may be unable to save changes to the archive.', 'warn'));
      } else if (data.applied_access === 'suspended') {
        details.append(notice('Cloud storage access is reported as paused. Apps using the archive may be affected.', 'error'));
      }
      if (!data.enforcement_enabled) {
        details.append(notice('Allowances are being monitored. Automatic restrictions are not enabled.', 'warn'));
      } else if (data.desired_access !== data.applied_access) {
        details.append(notice('Permission change pending: ' + accessLabels[data.desired_access] + '.', 'warn'));
      }
      if (data.reason === 'ineligible_or_deleted') {
        details.append(notice('This instance is no longer eligible for managed storage. Check your plan or contact support.', 'warn'));
      } else if (data.reason === 'capacity_limit') {
        details.append(notice('The stored capacity allowance has been reached. A monthly activity reset does not free storage space.', 'warn'));
      }
      var usage = data.usage;
      if (usage === null) {
        details.append(element('p', 'Usage has not been reported for this period yet. Included capacity: ' + bytes(data.capacity_bytes) + '.', 'hint'));
      } else {
        var metrics = element('div', null, 'managed-storage__metrics');
        if (usage.used_bytes === null) {
          var capacity = element('div', null, 'managed-storage__metric');
          capacity.append(element('strong', 'Stored capacity'));
          capacity.append(element('p', 'Storage-size metrics have not been reported yet. Included capacity: ' + bytes(data.capacity_bytes) + '.'));
          metrics.append(capacity);
        } else {
          metrics.append(metric('Stored capacity', usage.used_bytes, data.capacity_bytes, bytes));
        }
        metrics.append(metric('Monthly activity allowance', usage.operation_microcents, usage.read_only_at_microcents, money));
        details.append(metrics);
        if (usage.operation_microcents >= usage.read_only_at_microcents * 0.85 && usage.operation_microcents < usage.read_only_at_microcents) {
          details.append(notice('Approaching the monthly activity limit. At 100%, storage may become read-only.', 'warn'));
        }
        details.append(element('p', 'Activity allowance resets on ' + usage.resets_at + ' (00:00 UTC). Stored capacity does not reset.', 'hint'));
        details.append(element('p', 'Read-only threshold: ' + money(usage.read_only_at_microcents)
          + '. Access-pause threshold: ' + money(usage.suspend_at_microcents) + '.', 'hint'));
        var explanation = element('details');
        explanation.open = !!expanded;
        explanation.append(element('summary', 'How the allowance works'));
        var storageCost = usage.storage_microcents === null ? 'Storage cost is awaiting complete size metrics.'
          : 'Estimated storage cost this period: ' + money(usage.storage_microcents) + '.';
        explanation.append(element('p', 'Reads, writes and listings use the activity allowance. Values are estimated provider costs, not an extra bill. ' + storageCost));
        details.append(explanation);
        if (explanationFocused) explanation.querySelector('summary').focus({preventScroll: true});
      }
      if (usage === null && explanationFocused) refresh.focus({preventScroll: true});
      var facts = element('dl', null, 'managed-storage__facts');
      facts.append(element('dt', 'Reported access'), element('dd', accessLabels[data.applied_access]));
      var activityChecked = usage && usage.operations_observed_at != null ? usage.operations_observed_at : data.observed_at;
      facts.append(element('dt', 'Activity checked'), element('dd', timestamp(activityChecked)));
      if (usage) facts.append(element('dt', 'Storage sampled'), element('dd', timestamp(usage.sample_at)));
      facts.append(element('dt', 'Permissions checked'), element('dd', timestamp(data.applied_at)));
      details.append(facts);
    }

    function schedule() {
      clearTimeout(timer);
      if (allocation) timer = setTimeout(function () {
        if (!document.hidden) load(); else schedule();
      }, options.refreshMs || 60000);
    }

    async function load() {
      if (!allocation || request) return;
      var current = revision;
      var controller = new AbortController();
      request = controller;
      refresh.setAttribute('aria-disabled', 'true');
      root.setAttribute('aria-busy', 'true');
      message.textContent = last ? 'Refreshing usage...' : 'Loading cloud storage usage...';
      var timeout = setTimeout(function () { controller.abort(); }, options.timeoutMs || 15000);
      try {
        var response = await fetcher('/api/storage/managed_usage', {credentials: 'same-origin', cache: 'no-store', signal: controller.signal});
        if (current !== revision) return;
        if (response.status === 401 || response.status === 403) {
          last = null;
          details.replaceChildren();
          message.textContent = 'Sign in as the instance owner to view cloud storage usage.';
          return;
        }
        var body = await response.json();
        if (current !== revision) return;
        if (response.ok && body.managed === false) { setAllocation(null); return; }
        if (!response.ok || body.managed !== true || !validStatus(body.status, allocation)) throw new Error('unavailable');
        last = body.status;
        render(last, false);
        message.textContent = last.stale ? 'Showing the last reported usage.' : 'Cloud storage usage updated.';
      } catch (error) {
        if (current !== revision) return;
        message.textContent = 'Cloud storage usage is unavailable. Try refreshing.';
        if (last) render(last, true);
        else details.replaceChildren(element('p', 'Your usage could not be loaded. This does not tell us whether storage access has changed.', 'hint'));
      } finally {
        clearTimeout(timeout);
        if (current === revision) {
          request = null;
          refresh.setAttribute('aria-disabled', 'false');
          root.setAttribute('aria-busy', 'false');
          schedule();
        }
      }
    }

    function setAllocation(value, expectedRevision) {
      if (expectedRevision !== undefined && expectedRevision !== revision) return;
      value = typeof value === 'string' && /^[a-f0-9]{32}$/.test(value) ? value : null;
      if (value === allocation) return;
      allocation = value;
      revision++;
      if (request) request.abort();
      request = null;
      clearTimeout(timer);
      last = null;
      details.replaceChildren();
      root.hidden = !allocation;
      root.setAttribute('aria-busy', 'false');
      if (allocation) load();
    }

    function visibility() { if (!document.hidden && allocation) load(); }
    function pause() {
      revision++;
      if (request) request.abort();
      request = null;
      clearTimeout(timer);
      refresh.setAttribute('aria-disabled', 'false');
      root.setAttribute('aria-busy', 'false');
      last = null;
      details.replaceChildren();
      message.textContent = 'Cloud storage usage is paused.';
    }
    function resume() { if (allocation) load(); }
    function destroy() {
      pause();
      allocation = null;
      document.removeEventListener('visibilitychange', visibility);
      global.removeEventListener('pagehide', pause);
      global.removeEventListener('pageshow', resume);
      refresh.removeEventListener('click', load);
    }
    refresh.addEventListener('click', load);
    document.addEventListener('visibilitychange', visibility);
    global.addEventListener('pagehide', pause);
    global.addEventListener('pageshow', resume);
    return {setAllocation: setAllocation, refresh: load, destroy: destroy,
      getRevision: function () { return revision; }};
  }

  global.createManagedStorageUsage = create;
  var root = document.getElementById('managed-storage-usage');
  if (root) {
    global.managedStorageUsage = create(root);
    global.managedStorageUsage.setAllocation(root.dataset.allocationId);
  }
})(window);
