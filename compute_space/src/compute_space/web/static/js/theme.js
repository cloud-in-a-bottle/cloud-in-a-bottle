// ─── Appearance ───

async function saveTheme() {
  var msg = document.getElementById('theme-msg');
  msg.hidden = true;
  try {
    var resp = await fetch('/api/settings/theme', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        style: document.getElementById('theme-style').value,
        app_list: document.getElementById('theme-app-list').value,
      }),
    });
    var result = await readJsonResponse(resp);
    if (!result.ok) throw new Error(responseErrorMessage(result.data, 'failed to save'));
    // The same attributes layout.html renders, so the page restyles without a reload.
    document.documentElement.dataset.style = result.data.style;
    document.documentElement.dataset.appList = result.data.app_list;
    msg.textContent = 'Saved.';
    msg.className = 'msg msg--ok';
  } catch (e) {
    msg.textContent = 'Failed to save appearance: ' + e.message;
    msg.className = 'msg msg--error';
  }
  msg.hidden = false;
}
