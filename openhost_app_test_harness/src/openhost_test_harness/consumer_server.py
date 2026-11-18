"""Synthetic consumer app server, copied into generated consumer app dirs (see consumer_app.py).

Runs inside the app container with only the python stdlib so image builds need no network.

Routes:
    GET  /health        -> {"status": "ok"}
    POST /call-service  -> {"shortname", "path"?, "payload"?, "method"?} proxied through the
                           router's v2 service-call endpoint using this app's identity;
                           returns {"service_status": ..., "service_body": ...}
"""

import json
import os
import re
import signal
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer
from typing import Any

SHORTNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SERVICE_PATH_RE = re.compile(r"^[A-Za-z0-9_/-]*$")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass

    def _json(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/call-service":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        shortname = body["shortname"]
        path = body.get("path", "")
        payload = body.get("payload")
        method = body.get("method", "POST")
        if not SHORTNAME_RE.match(shortname):
            self._json(400, {"error": "invalid shortname"})
            return
        if not SERVICE_PATH_RE.match(path):
            self._json(400, {"error": "invalid path"})
            return
        url = f"{os.environ['OPENHOST_ROUTER_URL']}/api/services/v2/call/{shortname}/{path}"
        request = urllib.request.Request(
            url,
            data=None if payload is None else json.dumps(payload).encode(),
            method=method,
            headers={
                "Authorization": f"Bearer {os.environ['OPENHOST_APP_TOKEN']}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except (urllib.error.URLError, OSError) as e:
            # Router unreachable — on Linux usually the missing openhost0 /
            # host_containers_internal_ip setup (see harness docs).
            self._json(502, {"error": "router unreachable from app container", "detail": str(e)})
            return
        try:
            service_body = json.loads(raw)
        except ValueError:
            service_body = raw.decode("utf-8", "replace")
        self._json(200, {"service_status": status, "service_body": service_body})


if __name__ == "__main__":
    # As a container's PID 1, Python gets no default SIGTERM action, so `podman stop`
    # would wait out its full grace period before SIGKILL. Exit promptly instead.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print("Harness consumer listening on :5000", flush=True)
    HTTPServer(("0.0.0.0", 5000), Handler).serve_forever()
