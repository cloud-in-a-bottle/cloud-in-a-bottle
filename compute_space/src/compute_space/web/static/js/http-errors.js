function readJsonResponse(response) {
  return response.json().then(
    function(data) { return {ok: response.ok, status: response.status, data: data}; },
    function() { return {ok: response.ok, status: response.status, data: null}; }
  );
}

function responseErrorMessage(data, fallback) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return fallback;
  var extra = data.extra;
  var output = extra && typeof extra === 'object' && !Array.isArray(extra) ? extra.output : null;
  if (data.status_code >= 500 && output) return output;
  return data.detail || output || fallback;
}
