# Detached stdlib-only mini web server that covers the compute_space downtime
# window: it retry-binds 80/443 once Caddy releases them, terminates TLS, serves
# the update page, then releases the ports once the new compute_space is back.

from __future__ import annotations

import json
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlparse

from loguru import logger

from openhost_system_agent.updater import progress
from openhost_system_agent.updater.paths import ready_marker_path
from openhost_system_agent.updater.paths import token_path
from openhost_system_agent.updater.paths import updater_dir

# Matches compute_space's default port (config.py DefaultConfig.port).
_COMPUTE_SPACE_PORT = 8080

# Hard ceiling on how long the updater holds 80/443, so it can never permanently
# squat the ports and lock out a compute_space that came back unexpectedly.
_MAX_LIFETIME_SECONDS = 30 * 60

_BIND_WAIT_SECONDS = 60
_BIND_RETRY_INTERVAL = 0.02
_READY_POLL_INTERVAL = 0.1


def _read_token_file() -> str | None:
    try:
        return token_path().read_text().strip() or None
    except OSError:
        return None


def _tail_progress() -> list[dict[str, object]]:
    return progress.read_entries()


def _is_terminal(entries: list[dict[str, object]]) -> bool:
    return progress.is_terminal(entries)


# Both compute_space (Jinja) and this updater render the update page from the same
# source files (shared CSS, body fragment, polling JS) to avoid drift.
# server.py -> updater -> openhost_system_agent -> src -> openhost_system_agent
# -> <repo root>, i.e. parents[4].
_REPO_ROOT = Path(__file__).resolve().parents[4]
_WEB_DIR = _REPO_ROOT / "compute_space" / "src" / "compute_space" / "web"
_CSS_PATH = _WEB_DIR / "static" / "css" / "update-progress.css"
_JS_PATH = _WEB_DIR / "static" / "js" / "update-progress.js"
_BODY_PATH = _WEB_DIR / "templates" / "_update_progress_body.html"

_FALLBACK_CSS = "body{font-family:-apple-system,system-ui,sans-serif;max-width:640px;margin:3em auto;color:#222}"
_FALLBACK_BODY = (
    "<h1>Updating this instance\u2026</h1><ul id='log'></ul><p>This instance is updating and will be back shortly.</p>"
)
_FALLBACK_JS = (
    "function p(){fetch('/updates',{cache:'no-store'}).then(function(r){return r.ok?r.json():null})"
    ".then(function(d){if(d&&d.terminal){fetch('/health').then(function(h){if(h.ok)location.href='/settings'})}"
    "setTimeout(p,1500)}).catch(function(){setTimeout(p,1500)})}p();"
)


def _read(path: Path, fallback: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return fallback


def _page() -> bytes:
    css = _read(_CSS_PATH, _FALLBACK_CSS)
    body = _read(_BODY_PATH, _FALLBACK_BODY)
    js = _read(_JS_PATH, _FALLBACK_JS)
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='robots' content='noindex'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Updating\u2026</title><style>" + css + "</style></head>"
        "<body>" + body + "<script>" + js + "</script></body></html>"
    )
    return html.encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # noqa: D401
        pass

    server_version = "OpenHostUpdater/1"

    @property
    def _expected_token(self) -> str | None:
        return _read_token_file()

    def _query_token(self) -> str | None:
        qs = parse_qs(urlparse(self.path).query)
        vals = qs.get("token")
        return vals[0] if vals else None

    def _authed(self) -> bool:
        expected = self._expected_token
        supplied = self._query_token()
        return bool(expected) and supplied == expected

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/updates":
            self._serve_updates()
            return
        if path == "/health":
            # Answer 503 while the updater owns the port: the real dashboard is
            # NOT up, and the page probes /health to decide when to redirect.
            self._respond(503, "text/plain; charset=utf-8", b"updating")
            return
        self._respond(200, "text/html; charset=utf-8", _page())

    def _serve_updates(self) -> None:
        if not self._authed():
            self._respond(403, "application/json", b'{"error":"forbidden"}')
            return
        entries = _tail_progress()
        payload = json.dumps({"entries": entries, "terminal": _is_terminal(entries)}).encode("utf-8")
        self._respond(200, "application/json", payload)

    def _respond(self, status: int, content_type: str, body: bytes) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass


def _compute_space_ready() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", _COMPUTE_SPACE_PORT), timeout=0.5):
            return True
    except OSError:
        return False


def _make_ssl_context(cert_path: Path, key_path: Path) -> ssl.SSLContext | None:
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        return ctx
    except (OSError, ssl.SSLError):
        return None


def _try_bind(host: str, port: int) -> socket.socket | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(128)
        return sock
    except OSError:
        # Close on failure so the many retry-bind attempts don't leak an fd each.
        sock.close()
        return None


class _BoundServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, sock: socket.socket, handler: type[BaseHTTPRequestHandler]) -> None:
        super().__init__(sock.getsockname(), handler, bind_and_activate=False)
        self.socket = sock


def _serve_on(sock: socket.socket, ssl_ctx: ssl.SSLContext | None) -> ThreadingHTTPServer:
    httpd = _BoundServer(sock, _Handler)
    if ssl_ctx is not None:
        httpd.socket = ssl_ctx.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def run(cert_path: Path, key_path: Path) -> None:
    """Cover the compute_space downtime window, then release the ports."""
    logger.info(f"updater run() starting; ssl_cert={cert_path} exists={cert_path.exists()}")
    ssl_ctx = _make_ssl_context(cert_path, key_path)
    logger.info(f"updater ssl_ctx built: {ssl_ctx is not None}")

    https_sock, http_sock = _acquire_ports_during_downtime(ssl_ctx)
    logger.info(f"updater acquire returned https={https_sock is not None} http={http_sock is not None}")

    servers: list[ThreadingHTTPServer] = []
    if https_sock is not None:
        servers.append(_serve_on(https_sock, ssl_ctx))
    if http_sock is not None:
        servers.append(_serve_on(http_sock, None))

    if not servers:
        logger.warning("updater got no ports; nothing to cover, exiting")
        return
    logger.info(f"updater serving on {len(servers)} port(s); holding until compute_space is back")

    try:
        deadline = time.monotonic() + _MAX_LIFETIME_SECONDS
        while time.monotonic() < deadline:
            if _compute_space_ready():
                break
            time.sleep(_READY_POLL_INTERVAL)
    finally:
        # Close listening sockets FIRST so :443/:80 are freed for the new Caddy
        # before the slower per-connection server teardown.
        for httpd in servers:
            try:
                httpd.socket.close()
            except OSError:
                pass
        for httpd in servers:
            try:
                httpd.shutdown()
                httpd.server_close()
            except OSError:
                pass
        _clear_ready_marker()


def _acquire_ports_during_downtime(
    ssl_ctx: ssl.SSLContext | None,
) -> tuple[socket.socket | None, socket.socket | None]:
    """Retry-bind 80/443 until the restart frees them from Caddy.

    The bind-wait window only starts counting once compute_space first goes
    offline, so a still-up compute_space at launch is NOT mistaken for "already
    recovered" (bounded by an absolute ceiling so a never-firing restart can't
    hang us forever).
    """
    https_sock: socket.socket | None = None
    http_sock: socket.socket | None = None

    _touch_ready_marker()

    absolute_deadline = time.monotonic() + _MAX_LIFETIME_SECONDS
    downtime_seen = False
    bind_deadline: float | None = None

    while time.monotonic() < absolute_deadline:
        if https_sock is None and ssl_ctx is not None:
            https_sock = _try_bind("0.0.0.0", 443)
        if http_sock is None:
            http_sock = _try_bind("0.0.0.0", 80)

        if https_sock is not None or (ssl_ctx is None and http_sock is not None):
            return https_sock, http_sock

        ready = _compute_space_ready()
        if not ready and not downtime_seen:
            # Restart took compute_space offline: downtime window has begun.
            downtime_seen = True
            bind_deadline = time.monotonic() + _BIND_WAIT_SECONDS
            logger.info("updater: compute_space went down; downtime window started, racing for 443/80")
        elif downtime_seen and ready:
            # compute_space came back before we grabbed a port; close any partial
            # bind so we don't squat a port.
            _close(https_sock, http_sock)
            return None, None
        elif bind_deadline is not None and time.monotonic() > bind_deadline:
            _close(https_sock, http_sock)
            return None, None

        # Tight retry to grab 443 the instant the dying Caddy releases it, before
        # the freshly-started Caddy can rebind it.
        time.sleep(_BIND_RETRY_INTERVAL)

    return https_sock, http_sock


def _touch_ready_marker() -> None:
    try:
        updater_dir().mkdir(parents=True, exist_ok=True)
        ready_marker_path().write_text("")
    except OSError:
        pass


def _clear_ready_marker() -> None:
    try:
        ready_marker_path().unlink(missing_ok=True)
    except OSError:
        pass


def _close(*socks: socket.socket | None) -> None:
    for s in socks:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass
