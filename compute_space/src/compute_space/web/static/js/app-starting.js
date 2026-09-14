(function() {
  var page = document.querySelector('.app-starting');
  var hint = document.getElementById('startup-hint');
  var retry = document.getElementById('startup-retry');
  var pause = document.getElementById('startup-pause');
  var paused = false;
  var timer;

  function scheduleRetry() {
    hint.textContent = "This page will open your app automatically when it's ready.";
    // Reload the original GET, preserving its path, query, and fragment. Probing
    // the app URL first could consume a one-time link before the real navigation.
    timer = window.setTimeout(function() { window.location.reload(); }, Number(page.dataset.retrySeconds) * 1000);
  }

  retry.hidden = false;
  pause.hidden = false;
  retry.addEventListener('click', function() { window.location.reload(); });
  pause.addEventListener('click', function() {
    paused = !paused;
    window.clearTimeout(timer);
    pause.textContent = paused ? 'Resume automatic retry' : 'Pause automatic retry';
    if (paused) hint.textContent = 'Automatic retry paused. Choose Try now when you want to check again.';
    else scheduleRetry();
  });
  scheduleRetry();
})();
