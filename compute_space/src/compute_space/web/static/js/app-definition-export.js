(function() {
  'use strict';

  var radios = document.querySelectorAll('input[name="app-definition-mode"]');
  var copyButton = document.getElementById('app-definition-copy');
  var downloadButton = document.getElementById('app-definition-download');
  var preview = document.getElementById('app-definition-preview');
  var output = document.getElementById('app-definition-output');
  var status = document.getElementById('app-definition-status');
  var privateHint = document.getElementById('app-definition-private-hint');
  var mode = 'sharing';
  var payload = null;
  var generation = 0;
  var controller = null;
  var downloadUrl = null;

  function syncRadios() {
    radios.forEach(function(radio) { radio.checked = radio.value === mode; });
  }

  function revokeDownload() {
    if (downloadUrl) URL.revokeObjectURL(downloadUrl);
    downloadUrl = null;
  }

  function clearPayload() {
    generation += 1;
    if (controller) controller.abort();
    controller = null;
    payload = null;
    output.textContent = '';
    status.textContent = '';
    copyButton.disabled = true;
    downloadButton.disabled = true;
    revokeDownload();
  }

  function reset() {
    clearPayload();
    mode = 'sharing';
    syncRadios();
    privateHint.hidden = true;
    preview.open = false;
  }

  async function load() {
    var requestGeneration = generation;
    var requestedMode = mode;
    controller = new AbortController();
    status.textContent = 'Loading…';
    function current() {
      return generation === requestGeneration && mode === requestedMode;
    }
    try {
      var response = await fetch('/api/app-definitions/export', {
        method: 'POST',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: {'Accept': 'application/yaml', 'Content-Type': 'application/json'},
        body: JSON.stringify({mode: requestedMode}),
        signal: controller.signal,
      });
      if (!current()) return;
      if (!response.ok || response.redirected
          || (response.headers.get('Content-Type') || '').split(';')[0].trim().toLowerCase() !== 'application/yaml'
          || response.headers.get('X-App-Definitions-Mode') !== requestedMode
           || response.headers.get('X-App-Definitions-Schema-Version') !== '2') {
        throw new Error('Invalid export');
      }
      // The server validates YAML structure and privacy; keep its text opaque and byte-for-byte intact.
      var text = await response.text();
      if (!current()) return;
      if (!text.trim()) throw new Error('Invalid export');
      payload = text;
      output.textContent = text;
      copyButton.disabled = false;
      downloadButton.disabled = false;
      status.textContent = 'Ready.';
    } catch (error) {
      if (current()) status.textContent = 'Could not load app definitions. Reload to try again.';
    } finally {
      if (current()) controller = null;
    }
  }

  radios.forEach(function(radio) {
    radio.addEventListener('change', function() {
      if (!radio.checked) return;
      clearPayload();
      mode = radio.value;
      syncRadios();
      privateHint.hidden = mode !== 'private';
      load();
    });
  });

  copyButton.addEventListener('click', async function() {
    if (payload === null || copyButton.disabled) return;
    var copyGeneration = generation;
    var copyMode = mode;
    function current() {
      return generation === copyGeneration && mode === copyMode && payload !== null;
    }
    var focused = document.activeElement;
    copyButton.disabled = true;
    var copied = false;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      try { await navigator.clipboard.writeText(payload); copied = true; } catch (error) { copied = false; }
    }
    // A rejected clipboard promise must never copy an old private export via the fallback.
    if (!current()) return;
    if (document.activeElement !== document.body) focused = document.activeElement;
    if (!copied) {
      var textarea = document.createElement('textarea');
      textarea.value = payload;
      textarea.style.position = 'fixed';
      textarea.style.opacity = '0';
      document.body.appendChild(textarea);
      textarea.select();
      try { copied = document.execCommand('copy'); } catch (error) { copied = false; }
      textarea.remove();
    }
    if (!current()) return;
    status.textContent = copied ? 'Copied.' : 'Copy failed. Select the text in Preview to copy it.';
    copyButton.disabled = false;
    if (document.activeElement === document.body && focused) focused.focus({preventScroll: true});
  });

  downloadButton.addEventListener('click', function() {
    if (payload === null) return;
    revokeDownload();
    downloadUrl = URL.createObjectURL(new Blob([payload], {type: 'application/yaml'}));
    var url = downloadUrl;
    var link = document.createElement('a');
    link.href = url;
    link.download = 'app-definitions-' + mode + '.yaml';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function() { if (downloadUrl === url) revokeDownload(); }, 0);
  });

  window.addEventListener('pagehide', reset);
  window.addEventListener('pageshow', function() {
    reset();
    load();
    var showGeneration = generation;
    // History traversal can restore form controls after pageshow, including on BFCache returns.
    setTimeout(function() { if (generation === showGeneration) syncRadios(); }, 0);
  });
  reset();
})();
