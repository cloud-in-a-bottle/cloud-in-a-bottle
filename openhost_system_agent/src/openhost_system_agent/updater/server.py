# Stdlib-only web server that serves the update page on 80/443 while compute_space
# is restarting, then releases the ports once it is back on 127.0.0.1:8080.

from __future__ import annotations

import base64
import json
import socket
import ssl
import threading
import time
from collections.abc import Callable
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

_COMPUTE_SPACE_PORT = 8080
# Must outlast a multi-hop walk: this now covers the whole apply, not a restart.
_MAX_LIFETIME_SECONDS = 60 * 60
_BIND_WAIT_SECONDS = 60
# If the service never goes down the apply that launched us is gone. Exiting keeps
# an idle updater from taking 80/443 during the next unrelated restart.
_DOWNTIME_WAIT_SECONDS = 120
_BIND_RETRY_INTERVAL = 0.02
_RETRY_AFTER_SECONDS = 30
_READY_POLL_INTERVAL = 0.1


def _read_token_file() -> str | None:
    try:
        return token_path().read_text().strip() or None
    except OSError:
        return None


# Render the update page from the same source files as compute_space's /updating
# template so the two never drift.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_WEB_DIR = _REPO_ROOT / "compute_space" / "src" / "compute_space" / "web"
_CSS_PATH = _WEB_DIR / "static" / "css" / "update-progress.css"
_FAVICON_PATH = _WEB_DIR / "static" / "img" / "favicon.svg"
_JS_PATH = _WEB_DIR / "static" / "js" / "update-progress.js"
_BODY_PATH = _WEB_DIR / "templates" / "_update_progress_body.html"

_FALLBACK_CSS = "body{font-family:-apple-system,system-ui,sans-serif;max-width:640px;margin:3em auto;color:#222}"
_FALLBACK_BODY = (
    "<h1>Updating this instance\u2026</h1><ul id='log'></ul><p>This instance is updating and will be back shortly.</p>"
)
# Keep behavior-parity with update-progress.js: authenticate /updates with the
# URL token, and when /updates is not readable (no token / instance back) probe
# /health and reload so no viewer is ever stranded on this page.
_FALLBACK_JS = (
    "var t=new URLSearchParams(location.search).get('token')||'';var e=0;"
    "function r(){fetch('/health',{cache:'no-store'}).then(function(h){if(h.ok)location.reload()})"
    ".catch(function(){})}"
    "function p(){fetch('/updates?token='+encodeURIComponent(t),{cache:'no-store'})"
    ".then(function(x){return x.ok?x.json():(r(),null)})"
    ".then(function(d){if(d){var n=(d.entries||[]).length;e=n?0:e+1;if(e>3)r();"
    "if(d.terminal){fetch('/health').then(function(h){if(h.ok)location.href='/settings'})}}"
    "setTimeout(p,1500)}).catch(function(){r();setTimeout(p,1500)})}p();"
)


def _read(path: Path, fallback: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return fallback


def _build_page() -> bytes:
    css = _read(_CSS_PATH, _FALLBACK_CSS)
    try:
        favicon = _FAVICON_PATH.read_bytes()
    except OSError as exc:
        logger.warning("could not read updater favicon at {}: {}", _FAVICON_PATH, exc)
        favicon = b""
    body = _read(_BODY_PATH, _FALLBACK_BODY)
    js = _read(_JS_PATH, _FALLBACK_JS)
    favicon_link = ""
    if favicon:
        encoded_favicon = base64.b64encode(favicon).decode("ascii")
        favicon_link = (
            "<link rel='icon' type='image/svg+xml' href='data:image/svg+xml;base64," + encoded_favicon + "'>"
        )
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='robots' content='noindex'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        + favicon_link
        + "<title>Updating\u2026</title><style>"
        + css
        + "</style></head>"
        "<body>" + body + "<script>" + js + "</script></body></html>"
    )
    return html.encode("utf-8")


# Rendered once, not per request: these files live in the tree the walk rewrites,
# so a per-request read could serve a file caught mid-write.
_page_snapshot: bytes | None = None


def snapshot_page() -> bytes:
    """Read the page files and cache the rendered HTML for this process's life."""
    global _page_snapshot
    _page_snapshot = _build_page()
    return _page_snapshot


def _page() -> bytes:
    return _page_snapshot if _page_snapshot is not None else snapshot_page()


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
            # 503 while the updater owns the port: the page probes /health to
            # know when the real dashboard is back.
            self._respond(503, "text/plain; charset=utf-8", b"updating")
            return
        # HTML page only for browser document navigations; XHR/fetch (e.g. the
        # dashboard's /api/* calls racing in after the redirect) gets 503 JSON so
        # no caller does JSON.parse("<!doctype ..."). Headerless -> non-navigation.
        if self.headers.get("Sec-Fetch-Dest", "empty") == "document":
            self._respond(200, "text/html; charset=utf-8", _page())
        else:
            self._respond(503, "application/json", b'{"error":"updating"}')

    def do_POST(self) -> None:
        self._respond(503, "application/json", b'{"error":"updating"}')

    def _serve_updates(self) -> None:
        if not self._authed():
            self._respond(403, "application/json", b'{"error":"forbidden"}')
            return
        entries = progress.read_entries()
        payload = json.dumps({"entries": entries, "terminal": progress.is_terminal(entries)}).encode("utf-8")
        self._respond(200, "application/json", payload)

    def _respond(self, status: int, content_type: str, body: bytes) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if status == 503:
                # Every app on the instance answers 503 for the length of the
                # apply, so tell clients when to come back rather than letting
                # them treat it as a hard failure.
                self.send_header("Retry-After", str(_RETRY_AFTER_SECONDS))
            # Don't let the browser reuse this connection: once the updater
            # releases the port, later requests must establish fresh connections
            # to the new compute_space rather than reusing the updater's socket.
            self.send_header("Connection", "close")
            self.close_connection = True
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


def _make_ssl_context(cert_path: Path | None, key_path: Path | None) -> ssl.SSLContext | None:
    if cert_path is None or key_path is None:
        return None
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
        # Adopt the pre-bound socket; close the unbound one the base class made
        # so it isn't leaked, and fill in what server_bind would have set.
        self.socket.close()
        self.socket = sock
        host, port = sock.getsockname()[:2]
        self.server_name = host
        self.server_port = port


def _serve_on(sock: socket.socket, ssl_ctx: ssl.SSLContext | None) -> ThreadingHTTPServer | None:
    """Serve on an already-bound socket; returns None (socket closed) on failure."""
    try:
        httpd = _BoundServer(sock, _Handler)
        if ssl_ctx is not None:
            httpd.socket = ssl_ctx.wrap_socket(httpd.socket, server_side=True)
    except (OSError, ssl.SSLError) as e:
        logger.warning(f"updater failed to start serving on {sock.getsockname()}: {e}")
        _close(sock)
        return None
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def run(resolve_tls_paths: Callable[[], tuple[Path, Path] | None]) -> None:
    # Snapshot the page before touching the ports: the tree is quiet now, and it
    # must not be read once the restart is in flight.
    snapshot_page()

    def load_ssl_context() -> ssl.SSLContext | None:
        tls_paths = resolve_tls_paths()
        return _make_ssl_context(*(tls_paths or (None, None)))

    https_sock, http_sock, ssl_ctx = _acquire_ports_during_downtime(load_ssl_context)

    servers: list[ThreadingHTTPServer] = []
    if https_sock is not None:
        https_server = _serve_on(https_sock, ssl_ctx)
        if https_server is not None:
            servers.append(https_server)
    if http_sock is not None:
        http_server = _serve_on(http_sock, None)
        if http_server is not None:
            servers.append(http_server)

    if not servers:
        _clear_ready_marker()
        return
    logger.info(f"updater serving on {len(servers)} port(s) until compute_space is back")

    try:
        deadline = time.monotonic() + _MAX_LIFETIME_SECONDS
        while time.monotonic() < deadline:
            if _compute_space_ready():
                break
            time.sleep(_READY_POLL_INTERVAL)
    finally:
        # Close the listening sockets first so the ports free before teardown.
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
    load_ssl_context: Callable[[], ssl.SSLContext | None],
) -> tuple[socket.socket | None, socket.socket | None, ssl.SSLContext | None]:
    # Grab 80/443 once the restart frees them. The bind-wait window only starts
    # after compute_space is first seen offline, so a still-up instance isn't
    # mistaken for "recovered".
    https_sock: socket.socket | None = None
    http_sock: socket.socket | None = None
    ssl_ctx: ssl.SSLContext | None = None

    _touch_ready_marker()

    start = time.monotonic()
    absolute_deadline = start + _MAX_LIFETIME_SECONDS
    downtime_seen = False
    bind_deadline: float | None = None

    while time.monotonic() < absolute_deadline:
        if downtime_seen:
            if https_sock is None and ssl_ctx is not None:
                https_sock = _try_bind("0.0.0.0", 443)
            if http_sock is None:
                http_sock = _try_bind("0.0.0.0", 80)

            if https_sock is not None or (ssl_ctx is None and http_sock is not None):
                return https_sock, http_sock, ssl_ctx

        ready = _compute_space_ready()
        if not ready and not downtime_seen:
            downtime_seen = True
            # Once the router is offline, no promotion request can complete and this snapshot is stable.
            ssl_ctx = load_ssl_context()
            bind_deadline = time.monotonic() + _BIND_WAIT_SECONDS
        elif downtime_seen and ready:
            _close(https_sock, http_sock)
            return None, None, ssl_ctx
        elif bind_deadline is not None and time.monotonic() > bind_deadline:
            logger.warning(f"updater never got 80/443 within {_BIND_WAIT_SECONDS}s of the downtime; giving up")
            _close(https_sock, http_sock)
            return None, None, ssl_ctx
        elif not downtime_seen and time.monotonic() - start > _DOWNTIME_WAIT_SECONDS:
            logger.warning(f"compute_space never went down within {_DOWNTIME_WAIT_SECONDS}s; the apply must be gone")
            return None, None, None

        time.sleep(_BIND_RETRY_INTERVAL)

    logger.warning(f"updater hit its {_MAX_LIFETIME_SECONDS}s lifetime while waiting for the ports")
    return https_sock, http_sock, ssl_ctx


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
