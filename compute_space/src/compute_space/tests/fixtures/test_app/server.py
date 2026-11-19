"""Minimal HTTP server for testing app deployment."""

import json
import os
import signal
import sys
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok"})
        elif self.path == "/":
            self._json(
                200,
                {
                    "app": "test-app",
                    "app_name": os.environ.get("OPENHOST_APP_NAME", ""),
                },
            )
        elif self.path == "/echo-headers":
            headers = {k: v for k, v in self.headers.items()}
            self._json(200, {"headers": headers})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length else ""
        self._json(200, {"method": "POST", "body": body, "path": self.path})

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # suppress logs during tests


if __name__ == "__main__":
    # As a container's PID 1, Python gets no default SIGTERM action, so `podman stop`
    # would wait out its full grace period before SIGKILL. Exit promptly instead.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    server = HTTPServer(("0.0.0.0", 5000), Handler)
    print("Test server listening on :5000", flush=True)
    server.serve_forever()
