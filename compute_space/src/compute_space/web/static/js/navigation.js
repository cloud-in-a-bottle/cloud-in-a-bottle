// Config and query-string destinations must never execute javascript: URLs.
function navigateToHttpUrl(value) {
  var url;
  try {
    url = new URL(value, window.location.href);
  } catch (e) {
    return false;
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return false;
  window.location.href = url.href;
  return true;
}
