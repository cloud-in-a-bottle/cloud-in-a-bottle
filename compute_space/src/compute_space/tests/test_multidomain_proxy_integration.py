"""Phase 1 integration test: drive the real SubdomainProxyMiddleware end to end.

A stub HTTP backend stands in for an app container (the proxy just connects to
``http://127.0.0.1:<local_port>/``).  We assert that the *same* deployed app is
reachable under two configured domains at once, and that each request carries the
scheme of the domain it arrived on — https on the TLS domain, http on the mDNS
`.local` domain — proving the per-request scheme split works through the actual
ASGI proxy hop, no podman required.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from typing import cast
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import httpx
import pytest
from litestar import Litestar
from litestar import get

from compute_space.config import Config
from compute_space.core.app_id import new_app_id
from compute_space.core.domains import Domain
from compute_space.core.domains import DomainRecord
from compute_space.core.domains import seed_domains
from compute_space.db.connection import init_db
from compute_space.tests._litestar_helpers import auth_cookie
from compute_space.tests._litestar_helpers import ws_cookie_header
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db
from compute_space.web.middleware.subdomain_proxy import SubdomainProxyMiddleware

PRIMARY = Domain(name="host.example.com", tls=True)
LOCAL = Domain(name="myhost.local", tls=False, mdns=True)


class _EchoBackend(BaseHTTPRequestHandler):
    """Reflects the forwarding headers the router set back to the caller."""

    def do_GET(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        cast(_RecordingBackend, self.server).requests.append((self.command, self.path, body))
        response_body = b"backend-error" if self.path == "/backend-error" else b"backend-ok"
        self.send_response(418 if self.path == "/backend-error" else 200)
        self.send_header("X-Backend-Saw-Proto", self.headers.get("X-Forwarded-Proto", ""))
        self.send_header("X-Backend-Saw-Host", self.headers.get("X-Forwarded-Host", ""))
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response_body)

    do_POST = do_GET
    do_PUT = do_GET
    do_PATCH = do_GET
    do_DELETE = do_GET
    do_OPTIONS = do_GET
    do_HEAD = do_GET

    def log_message(self, *args: Any) -> None:  # silence stderr spam
        pass


class _RecordingBackend(ThreadingHTTPServer):
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, bytes]] = []
        super().__init__(("127.0.0.1", 0), _EchoBackend)


@pytest.fixture
def backend() -> Iterator[_RecordingBackend]:
    srv = _RecordingBackend()
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join()


@pytest.fixture
def backend_port(backend: _RecordingBackend) -> int:
    return backend.server_address[1]


@get("/health", sync_to_thread=False)
def _router_health() -> str:
    return "router-ok"


def _seed_app(db_path: str, name: str, local_port: int, public_paths: list[str]) -> None:
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            """INSERT INTO apps
                 (app_id, name, version, repo_path, local_port, status, public_paths)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (new_app_id(), name, "1.0.0", f"/tmp/{name}", local_port, "running", json.dumps(public_paths)),
        )
        db.commit()
    finally:
        db.close()


@pytest.fixture
def proxy_config(tmp_path: Path, backend_port: int) -> Config:
    """Active config with two domains + seeded apps.

    `myapp` makes "/" public (so proxy tests don't need auth); `privapp` has no public
    paths (so unauthenticated requests trigger the login redirect)."""
    cfg = _make_test_config(tmp_path, seed_primary=False)  # this test seeds the full set itself
    init_db(cfg.db_path)
    with closing(open_db(cfg)) as db:
        seed_domains(db, PRIMARY, [DomainRecord(LOCAL.name, LOCAL.tls, LOCAL.mdns)])
    _seed_app(cfg.db_path, "myapp", backend_port, public_paths=["/"])
    # App ports are unique in the DB; this app is only used for auth/interception tests.
    _seed_app(cfg.db_path, "privapp", backend_port + 1, public_paths=[])
    return cfg


@pytest.fixture
def wrapped_app(proxy_config: Config) -> Any:
    return SubdomainProxyMiddleware(Litestar(route_handlers=[_router_health], openapi_config=None))


def _client(wrapped_app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped_app), base_url="http://unused")


@pytest.mark.asyncio
async def test_app_reachable_over_https_on_tls_domain(wrapped_app: Any) -> None:
    async with _client(wrapped_app) as c:
        r = await c.get("http://myapp.host.example.com/")
    assert r.status_code == 200
    assert r.text == "backend-ok"
    assert r.headers["X-Backend-Saw-Proto"] == "https"
    assert r.headers["X-Backend-Saw-Host"] == "myapp.host.example.com"


@pytest.mark.asyncio
async def test_same_app_reachable_over_http_on_local_domain(wrapped_app: Any) -> None:
    async with _client(wrapped_app) as c:
        r = await c.get("http://myapp.myhost.local/")
    assert r.status_code == 200
    assert r.text == "backend-ok"
    # the crux: same app, arriving on `.local`, is proxied as plain http
    assert r.headers["X-Backend-Saw-Proto"] == "http"
    assert r.headers["X-Backend-Saw-Host"] == "myapp.myhost.local"


@pytest.mark.asyncio
async def test_unknown_app_subdomain_404s_on_local_domain(wrapped_app: Any) -> None:
    async with _client(wrapped_app) as c:
        r = await c.get("http://nope.myhost.local/")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_router_reachable_on_both_bare_domains(wrapped_app: Any) -> None:
    async with _client(wrapped_app) as c:
        r_pub = await c.get("http://host.example.com/health")
        r_local = await c.get("http://myhost.local/health")
    assert r_pub.status_code == 200 and r_pub.text == "router-ok"
    assert r_local.status_code == 200 and r_local.text == "router-ok"


@pytest.mark.asyncio
async def test_router_reachable_on_internal_gateway_hosts(wrapped_app: Any) -> None:
    # App→router service calls arrive on the container→host gateway (OPENHOST_ROUTER_URL), not a
    # configured domain; the proxy must defer to the router rather than 404.
    async with _client(wrapped_app) as c:
        for host in ("host.containers.internal:8080", "host.docker.internal:8080", "127.0.0.1:8080"):
            r = await c.get(f"http://{host}/health")
            assert r.status_code == 200 and r.text == "router-ok", host


@pytest.mark.asyncio
async def test_unknown_external_host_still_404s(wrapped_app: Any) -> None:
    # The internal-host allowance must not reopen serving arbitrary unmatched hosts.
    async with _client(wrapped_app) as c:
        r = await c.get("http://evil.example.org/health")
    assert r.status_code == 404


# --- Phase 2: unauthenticated login redirect stays on the ARRIVING domain ----------


@pytest.mark.asyncio
async def test_unauth_on_local_redirects_to_local_login_over_http(wrapped_app: Any) -> None:
    async with _client(wrapped_app) as c:
        r = await c.get("http://privapp.myhost.local/secret")  # httpx doesn't auto-follow
    assert r.status_code == 302
    # bounced to the .local login over http, NOT the public/canonical domain
    assert r.headers["location"] == ("http://myhost.local/login?next=http%3A%2F%2Fprivapp.myhost.local%2Fsecret")


@pytest.mark.asyncio
async def test_unauth_on_public_redirects_to_public_login_over_https(wrapped_app: Any) -> None:
    async with _client(wrapped_app) as c:
        r = await c.get("http://privapp.host.example.com/secret")
    assert r.status_code == 302
    assert r.headers["location"] == (
        "https://host.example.com/login?next=https%3A%2F%2Fprivapp.host.example.com%2Fsecret"
    )


# Startup interception uses the real DB, owner session verification, and HTTP proxy.


def _set_status(cfg: Config, status: str, name: str = "myapp") -> None:
    with closing(open_db(cfg)) as db:
        db.execute("UPDATE apps SET status = ? WHERE name = ?", (status, name))
        db.commit()


class _PageElements(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))


def _assert_startup(r: httpx.Response, *, html: bool) -> None:
    assert r.status_code == 503
    assert r.headers["retry-after"] == "3"
    assert r.headers["cache-control"] == "no-store"
    assert "location" not in r.headers
    assert "refresh" not in r.headers
    assert r.headers["content-type"].startswith("text/html" if html else "text/plain")
    if html:
        assert "Your app is coming up" in r.text
        page = _PageElements(r.text)
        assert any("layout--narrow" in (attrs.get("class") or "").split() for _, attrs in page.elements)
        assert any(tag == "main" and attrs.get("data-retry-seconds") == "3" for tag, attrs in page.elements)
        ids = {attrs.get("id") for _, attrs in page.elements}
        assert {"startup-retry", "startup-pause"} <= ids
        assert any(
            tag == "script" and urlsplit(attrs.get("src") or "").path == "/static/js/app-starting.js"
            for tag, attrs in page.elements
        )
    else:
        assert "<script" not in r.text
        assert "app-starting.js" not in r.text
        if r.request.method == "HEAD":
            assert r.content == b""
        else:
            assert "coming up" in r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["building", "starting"])
@pytest.mark.parametrize("authority,scheme", [("host.example.com:8443", "https"), ("myhost.local:8080", "http")])
async def test_startup_at_deep_app_url_then_running_proxies_same_url(
    wrapped_app: Any, proxy_config: Config, backend: _RecordingBackend, status: str, authority: str, scheme: str
) -> None:
    _set_status(proxy_config, status)
    with closing(open_db(proxy_config)) as db:
        db.execute("UPDATE apps SET public_paths = ? WHERE name = 'myapp'", (json.dumps(["/nested"]),))
        db.commit()
    url = f"{scheme}://myapp.{authority}/nested/a%3Ab?tab=logs&next=%2Fdeep"
    async with _client(wrapped_app) as c:
        r = await c.get(url, headers={"Accept": "text/html", "Sec-Fetch-Dest": "document"})
        _assert_startup(r, html=True)
        assert str(r.url) == url
        assert not r.history
        assert backend.requests == []
        page = _PageElements(r.text)
        links = [attrs.get("href") or "" for tag, attrs in page.elements if tag == "a"]
        assert not any(urlsplit(link).path.startswith(("/app_detail", "/dashboard")) for link in links)
        assets = [
            attrs.get("src") or attrs.get("href") or "" for tag, attrs in page.elements if tag in ("script", "link")
        ]
        router_assets = [asset for asset in assets if urlsplit(asset).path.startswith("/static/")]
        assert {urlsplit(asset).path for asset in router_assets} >= {
            "/static/css/tokens.css",
            "/static/css/components.css",
            "/static/css/app-starting.css",
            "/static/js/app-starting.js",
        }
        for asset in router_assets:
            assert asset.startswith(f"{scheme}://{authority}/static/"), asset
        assert all(urlsplit(asset).scheme in ("http", "https") for asset in assets)

        private = await c.get(f"{scheme}://myapp.{authority}/private", headers={"Accept": "text/html"})
        assert private.status_code == 302
        assert urlsplit(private.headers["location"]).path == "/login"
        assert backend.requests == []

        _set_status(proxy_config, "running")
        ready = await c.get(url, headers={"Accept": "text/html"})
    assert ready.status_code == 200
    assert ready.text == "backend-ok"
    assert ready.headers["X-Backend-Saw-Host"] == f"myapp.{authority}"
    assert ready.headers["X-Backend-Saw-Proto"] == scheme
    assert backend.requests == [("GET", "/nested/a%3Ab?tab=logs&next=%2Fdeep", b"")]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["building", "starting"])
@pytest.mark.parametrize("authority,scheme", [("host.example.com:8443", "https"), ("myhost.local:8080", "http")])
async def test_private_startup_auth_precedes_interception_and_owner_gets_details(
    wrapped_app: Any, proxy_config: Config, backend: _RecordingBackend, status: str, authority: str, scheme: str
) -> None:
    _set_status(proxy_config, status, "privapp")
    url = f"{scheme}://privapp.{authority}/secret/deep?view=logs&next=%2Fprivate"
    async with _client(wrapped_app) as c:
        r = await c.get(url, headers={"Accept": "text/html"})
        assert r.status_code == 302
        login = urlsplit(r.headers["location"])
        assert (login.scheme, login.netloc, login.path) == (scheme, authority, "/login")
        assert parse_qs(login.query) == {"next": [url]}
        assert "coming up" not in r.text
        assert backend.requests == []

        c.cookies.update(auth_cookie(proxy_config))
        owner = await c.get(url, headers={"Accept": "text/html"})
    _assert_startup(owner, html=True)
    links = [attrs.get("href") for tag, attrs in _PageElements(owner.text).elements if tag == "a"]
    assert links == [f"{scheme}://{authority}/app_detail/privapp"]
    assert backend.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["building", "starting"])
@pytest.mark.parametrize(
    "method,accept,destination,html",
    [
        ("GET", "text/html", None, True),
        ("GET", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "document", True),
        ("GET", "text/html;q=0.5,application/json;q=0.1", "iframe", True),
        ("GET", "application/json", None, False),
        ("GET", "*/*", None, False),
        ("GET", None, None, False),
        ("GET", "text/html;q=0", None, False),
        ("GET", "text/html;q=inf", None, False),
        ("GET", "text/html;q=1e309", None, False),
        ("GET", "text/html;q=garbage", None, False),
        ("GET", "text/html;q=NaN", None, False),
        ("GET", "text/html;q=", None, False),
        ("GET", "text/html;q=2", None, False),
        ("GET", "text/html;q=-1", None, False),
        ("GET", "text/html;q=1e-1", None, False),
        ("GET", "text/html;q=0,*/*;q=1", "document", False),
        ("GET", "text/html;charset=utf-8;q=0,text/html;q=1", "document", False),
        ("GET", "text/html;charset=iso-8859-1;q=0,text/html;q=1", "document", True),
        ("GET", "TEXT/HTML;CHARSET=UTF-8", "document", True),
        ("GET", "application/json,text/html;q=0.5", None, False),
        ("GET", "text/html", "empty", False),
        ("GET", "text/html", "script", False),
        ("POST", "text/html", "document", False),
        ("POST", "text/html;q=inf", "document", False),
        ("PUT", "text/html", None, False),
        ("PATCH", "text/html", None, False),
        ("DELETE", "text/html", None, False),
        ("OPTIONS", "text/html", None, False),
        ("HEAD", "text/html", "document", False),
        ("HEAD", "text/html;q=inf", "document", False),
    ],
)
async def test_startup_only_retries_html_get_navigation(
    wrapped_app: Any,
    proxy_config: Config,
    backend: _RecordingBackend,
    status: str,
    method: str,
    accept: str | None,
    destination: str | None,
    html: bool,
) -> None:
    _set_status(proxy_config, status)
    bodies: list[bytes] = []

    async def observe_response(scope: Any, receive: Any, send: Any) -> None:
        async def record(event: Any) -> None:
            if event["type"] == "http.response.body":
                bodies.append(event.get("body", b""))
            await send(event)

        await wrapped_app(scope, receive, record)

    async with _client(observe_response) as c:
        c.headers.pop("Accept", None)
        headers = {}
        if accept is not None:
            headers["Accept"] = accept
        if destination is not None:
            headers["Sec-Fetch-Dest"] = destination
        r = await c.request(
            method,
            "http://myapp.myhost.local/deep/action?keep=this",
            headers=headers,
            content=b"do-not-replay" if method in ("POST", "PUT", "PATCH", "DELETE") else None,
        )
    _assert_startup(r, html=html)
    if method == "HEAD":
        # ASGITransport discards HEAD bodies itself; inspect actual emitted bytes too.
        assert b"".join(bodies) == b""
    assert backend.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "error", "stopped", "removing"])
@pytest.mark.parametrize(
    "path,expected_status,body", [("/deep", 200, "backend-ok"), ("/backend-error", 418, "backend-error")]
)
async def test_nonstartup_states_preserve_backend_success_and_errors(
    wrapped_app: Any,
    proxy_config: Config,
    backend: _RecordingBackend,
    status: str,
    path: str,
    expected_status: int,
    body: str,
) -> None:
    _set_status(proxy_config, status)
    async with _client(wrapped_app) as c:
        r = await c.get(f"http://myapp.myhost.local{path}", headers={"Accept": "text/html"})
    assert r.status_code == expected_status
    assert r.text == body
    assert "retry-after" not in r.headers
    assert "location" not in r.headers
    assert backend.requests == [("GET", path, b"")]


@pytest.mark.asyncio
async def test_running_transport_failure_is_not_disguised_as_startup(wrapped_app: Any, proxy_config: Config) -> None:
    # Reserve a real, non-listening port so the connect fails without racing another listener.
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        with closing(open_db(proxy_config)) as db:
            db.execute("UPDATE apps SET local_port = ? WHERE name = 'myapp'", (unavailable.getsockname()[1],))
            db.commit()
        async with _client(wrapped_app) as c:
            r = await c.get("http://myapp.myhost.local/deep?keep=this", headers={"Accept": "text/html"})
    assert r.status_code == 502
    assert r.text == "App is not responding"
    assert r.headers["content-type"].startswith("text/plain")
    assert "retry-after" not in r.headers
    assert "location" not in r.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["building", "starting"])
@pytest.mark.parametrize(
    "name,owner,code", [("myapp", False, 1013), ("privapp", False, 4401), ("privapp", True, 1013)]
)
async def test_startup_websocket_closes_after_auth_without_backend_handshake(
    wrapped_app: Any, proxy_config: Config, backend: _RecordingBackend, status: str, name: str, owner: bool, code: int
) -> None:
    _set_status(proxy_config, status, name)
    headers = [(b"host", f"{name}.myhost.local:8080".encode())]
    if owner:
        headers.extend(
            (key.encode(), value.encode()) for key, value in ws_cookie_header(auth_cookie(proxy_config)).items()
        )
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": "/deep/socket",
        "raw_path": b"/deep/socket",
        "query_string": b"keep=this",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("myhost.local", 8080),
        "subprotocols": [],
        "state": {},
    }
    events: list[dict[str, Any]] = []

    async def receive() -> dict[str, str]:
        return {"type": "websocket.connect"}

    async def send(event: dict[str, Any]) -> None:
        events.append(event)

    await wrapped_app(scope, receive, send)
    # A close before acceptance becomes HTTP 403, not a client-visible 1013 frame.
    # Private unauthenticated sockets should still reject the handshake outright.
    expected_types = ["websocket.accept", "websocket.close"] if code == 1013 else ["websocket.close"]
    assert [event["type"] for event in events] == expected_types
    assert events[-1]["code"] == code
    assert backend.requests == []
