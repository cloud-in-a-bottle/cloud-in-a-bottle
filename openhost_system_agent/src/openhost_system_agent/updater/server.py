# Detached mini web server that covers the compute_space downtime window: it
# retry-binds 80/443 once Caddy releases them, terminates TLS with the on-disk
# certs, serves the update page (token-gated live logs for the owner, a generic
# loading page for everyone else), then releases the ports and exits once the new
# compute_space is back on 127.0.0.1:8080. Stdlib-only so the root-run agent gains
# no dependencies. See run() for the full lifecycle.

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

# The loopback port the new compute_space serves on once it is back up. Matches
# compute_space's default (config.py DefaultConfig.port). If a deployment ever
# customizes this, the updater is only cosmetic, so a mismatch just means it
# holds the ports slightly longer before its own max-lifetime bail.
_COMPUTE_SPACE_PORT = 8080

# Hard ceiling on how long the updater will hold 80/443. A normal update is well
# under this; the cap guarantees the updater can never permanently squat the
# ports and lock out a compute_space that came back in an unexpected way.
_MAX_LIFETIME_SECONDS = 30 * 60

# How long to keep trying to grab 80/443 after we first see compute_space go
# down (the restart has TimeoutStopSec=5, so the old Caddy is gone within a few
# seconds; the new one comes up shortly after).
_BIND_WAIT_SECONDS = 60

# Tight bind-retry so we snatch 443 in the gap between the old Caddy releasing it
# and the new Caddy grabbing it. This race is inherent; a short interval makes us
# win it far more often for fast updates.
_BIND_RETRY_INTERVAL = 0.02

_READY_POLL_INTERVAL = 0.1


def _read_token_file() -> str | None:
    try:
        return token_path().read_text().strip() or None
    except OSError:
        return None


def _tail_progress() -> list[dict[str, object]]:
    """Return all well-formed progress entries currently in the log."""
    return progress.read_entries()


def _is_terminal(entries: list[dict[str, object]]) -> bool:
    return progress.is_terminal(entries)


# The updating page is served by TWO processes: compute_space (Jinja template
# web/templates/updating.html) and — during the restart — this stdlib updater.
# To avoid drift, both render from the SAME source files: the shared CSS, the
# body fragment, and the polling JS. The updater reads them from the repo checkout
# on disk (it runs from the repo root) and inlines them into a self-contained page
# (self-contained because the updater serves the same page for every path and
# can't rely on separate static-asset requests being routed during downtime).
#
# server.py -> updater -> openhost_system_agent -> src -> openhost_system_agent
# -> <repo root>, i.e. parents[4].
_REPO_ROOT = Path(__file__).resolve().parents[4]
_WEB_DIR = _REPO_ROOT / "compute_space" / "src" / "compute_space" / "web"
_CSS_PATH = _WEB_DIR / "static" / "css" / "update-progress.css"
_JS_PATH = _WEB_DIR / "static" / "js" / "update-progress.js"
_BODY_PATH = _WEB_DIR / "templates" / "_update_progress_body.html"

# Minimal fallbacks if the shared assets can't be read (unexpected layout): the
# updater must still serve a usable, styled page rather than nothing.
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
    """The updater's single page, assembled from the shared CSS/body/JS so it is
    byte-for-behaviour identical to the compute_space-served /updating page.

    Served for every non-/updates, non-/health path during downtime. The polling
    JS reads its token from the URL (same as the compute_space page), so no token
    needs to be threaded into the markup here.
    """
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
    # Silence default stderr access logging (this runs headless under systemd).
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
            # The page probes /health to learn when the REAL dashboard is back.
            # While the updater owns the port the dashboard is NOT up, so answer
            # 503 — otherwise the page would think compute_space had returned and
            # redirect to a still-down /settings.
            self._respond(503, "text/plain; charset=utf-8", b"updating")
            return
        # Every other path gets the single updating page. It always renders (the
        # spinner + heading); the page's JS reads the token from the URL and only
        # unlocks the live log list via /updates when it matches.
        self._respond(200, "text/html; charset=utf-8", _page())

    # /updates is the polling endpoint the owner page hits for live progress.
    def _serve_updates(self) -> None:
        if not self._authed():
            # Don't leak progress to unauthenticated callers.
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
    """True once the new compute_space is accepting connections on loopback."""
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
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(128)
        return sock
    except OSError:
        return None


class _BoundServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that adopts an already-bound listening socket.

    We bind the socket ourselves (with retry, racing Caddy for the port) and
    hand it to the server so bind failures are handled in our retry loop, not
    swallowed by the constructor.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, sock: socket.socket, handler: type[BaseHTTPRequestHandler]) -> None:
        # bind_and_activate=False: reuse the socket we already bound/listened on.
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
    """Cover the compute_space downtime window, then release the ports.

    Launched (detached) just before ``systemctl restart openhost``. At launch,
    Caddy still holds 80/443, so binding fails; the updater retry-binds until the
    restart frees them (that bind success IS the "downtime started" signal). It
    then serves the update page until the NEW compute_space is listening on
    127.0.0.1:8080, at which point it releases 80/443 and returns so the new Caddy
    can rebind them.
    """
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
        # Never grabbed a port within the window — the restart either never took
        # the ports down (update aborted) or compute_space came back on its own.
        # Nothing to cover.
        logger.warning("updater got no ports; nothing to cover, exiting")
        return
    logger.info(f"updater serving on {len(servers)} port(s); holding until compute_space is back")

    try:
        # Hold the ports until the new compute_space is listening on loopback,
        # then release so its Caddy can rebind 80/443. Capped so we can never
        # permanently squat the ports. Poll tightly so we let go promptly and the
        # new Caddy's bind-retry window is short.
        deadline = time.monotonic() + _MAX_LIFETIME_SECONDS
        while time.monotonic() < deadline:
            if _compute_space_ready():
                break
            time.sleep(_READY_POLL_INTERVAL)
    finally:
        # Close the listening sockets FIRST so :443/:80 are freed for the new
        # Caddy immediately, before the (slower) per-connection server teardown.
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

    Returns the bound sockets (either may be None). Gives up after
    ``_BIND_WAIT_SECONDS`` — but only once the downtime has plausibly started, so
    a still-up compute_space at launch is NOT mistaken for "already recovered".
    The wait window only begins counting after compute_space first goes offline;
    before that we keep waiting for the restart to take effect (bounded by an
    absolute ceiling so a never-firing restart can't hang us forever).
    """
    https_sock: socket.socket | None = None
    http_sock: socket.socket | None = None

    # Signal the launcher that we're about to enter the bind loop, so the restart
    # (and thus the downtime window) only opens once we're poised to grab the
    # ports rather than still starting Python. Best-effort.
    _touch_ready_marker()

    absolute_deadline = time.monotonic() + _MAX_LIFETIME_SECONDS
    # The bind-wait window starts only once we've seen compute_space go down.
    downtime_seen = False
    bind_deadline: float | None = None

    while time.monotonic() < absolute_deadline:
        if https_sock is None and ssl_ctx is not None:
            https_sock = _try_bind("0.0.0.0", 443)
        if http_sock is None:
            http_sock = _try_bind("0.0.0.0", 80)

        # 443 is what the public HTTPS URL needs; once we hold it (or there is no
        # TLS and we hold 80) we are covering the downtime — stop racing.
        if https_sock is not None or (ssl_ctx is None and http_sock is not None):
            return https_sock, http_sock

        ready = _compute_space_ready()
        if not ready and not downtime_seen:
            # The restart has taken compute_space offline: the downtime window has
            # begun. Start the bounded bind-wait now (Caddy releases the ports
            # within TimeoutStopSec).
            downtime_seen = True
            bind_deadline = time.monotonic() + _BIND_WAIT_SECONDS
            logger.info("updater: compute_space went down; downtime window started, racing for 443/80")
        elif downtime_seen and ready:
            # compute_space came back (and rebound its own Caddy) before we ever
            # grabbed a port — the downtime was shorter than our bind cadence.
            # Nothing left to cover. Close any partial bind (e.g. 80 without 443)
            # so we don't leak a listener / squat a port.
            _close(https_sock, http_sock)
            return None, None
        elif bind_deadline is not None and time.monotonic() > bind_deadline:
            # We saw downtime but never managed to grab the port in time; give up
            # rather than spin. (Caddy may have rebound faster than we retried.)
            _close(https_sock, http_sock)
            return None, None

        # Tight retry so we grab 443 the instant the dying Caddy releases it,
        # before the freshly-started Caddy can rebind it.
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
