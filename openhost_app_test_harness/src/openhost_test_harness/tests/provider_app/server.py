"""Fixture provider: echoes service-call details back so tests can see what the router sent.

Routes:
    GET  /health   -> {"status": "ok"}
    GET  /         -> {"app": "echo-provider"}
    *    /svc/...  -> {"method", "path", "permissions", "consumer", "body"}
"""

import json
import signal
import sys
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer
from typing import Any


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _json(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _echo(self, body: Any = None) -> None:
        self._json(
            200,
            {
                "method": self.command,
                "path": self.path,
                "permissions": json.loads(self.headers.get("X-OpenHost-Permissions", "null")),
                "consumer": self.headers.get("X-OpenHost-Consumer-Name"),
                "body": body,
            },
        )

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"status": "ok"})
        elif self.path == "/":
            self._json(200, {"app": "echo-provider"})
        elif self.path.startswith("/svc/"):
            self._echo()
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self.path.startswith("/svc/"):
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except ValueError:
            body = raw.decode("utf-8", "replace")
        self._echo(body=body)


if __name__ == "__main__":
    # As a container's PID 1, Python gets no default SIGTERM action, so `podman stop`
    # would wait out its full grace period before SIGKILL. Exit promptly instead.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print("Echo provider listening on :5000", flush=True)
    HTTPServer(("0.0.0.0", 5000), Handler).serve_forever()
