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
    var body = await resp.json();
    if (!resp.ok) throw new Error(responseErrorMessage(body, 'failed to save'));
    // The same attributes layout.html renders, so the page restyles without a reload.
    document.documentElement.dataset.style = body.style;
    document.documentElement.dataset.appList = body.app_list;
    msg.textContent = 'Saved.';
    msg.className = 'msg msg--ok';
  } catch (e) {
    msg.textContent = 'Failed to save appearance: ' + e.message;
    msg.className = 'msg msg--error';
  }
  msg.hidden = false;
}
