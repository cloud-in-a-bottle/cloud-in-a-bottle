(function() {
  var page = document.querySelector('.app-starting');
  var hint = document.getElementById('startup-hint');
  hint.textContent = "This page will open your app automatically when it's ready.";
  // Reload the original GET, preserving its path, query, and fragment. Probing
  // the app URL first could consume a one-time link before the real navigation.
  window.setTimeout(function() { window.location.reload(); }, Number(page.dataset.retrySeconds) * 1000);
})();
