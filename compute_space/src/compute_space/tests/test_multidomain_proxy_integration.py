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
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from litestar import Litestar
from litestar import get

from compute_space.core.app_id import new_app_id
from compute_space.core.domains import Domain
from compute_space.core.domains import DomainRecord
from compute_space.core.domains import seed_domains
from compute_space.db.connection import init_db
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db
from compute_space.web.middleware.subdomain_proxy import SubdomainProxyMiddleware

PRIMARY = Domain(name="host.example.com", tls=True)
LOCAL = Domain(name="myhost.local", tls=False)


class _EchoBackend(BaseHTTPRequestHandler):
    """Reflects the forwarding headers the router set back to the caller."""

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("X-Backend-Saw-Proto", self.headers.get("X-Forwarded-Proto", ""))
        self.send_header("X-Backend-Saw-Host", self.headers.get("X-Forwarded-Host", ""))
        self.end_headers()
        self.wfile.write(b"backend-ok")

    def log_message(self, *args: Any) -> None:  # silence stderr spam
        pass


@pytest.fixture
def backend_port() -> Iterator[int]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _EchoBackend)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()


@get("/health", sync_to_thread=False)
def _router_health() -> str:
    return "router-ok"


def _seed_app(db_path: str, name: str, local_port: int, public_paths: list[str]) -> None:
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            """INSERT INTO apps
                 (app_id, name, version, repo_path, local_port, status, installed_by, public_paths)
               VALUES (?, ?, ?, ?, ?, ?, NULL, ?)""",
            (new_app_id(), name, "1.0.0", f"/tmp/{name}", local_port, "running", json.dumps(public_paths)),
        )
        db.commit()
    finally:
        db.close()


@pytest.fixture
def wrapped_app(tmp_path: Path, backend_port: int) -> Any:
    """Active config with two domains + seeded apps, and the middleware-wrapped router.

    `myapp` makes "/" public (so proxy tests don't need auth); `privapp` has no public
    paths (so unauthenticated requests trigger the login redirect)."""
    cfg = _make_test_config(tmp_path, seed_primary=False)  # this test seeds the full set itself
    init_db(cfg.db_path)
    with closing(open_db(cfg)) as db:
        seed_domains(db, PRIMARY, [DomainRecord(LOCAL.name, LOCAL.tls)])
    _seed_app(cfg.db_path, "myapp", backend_port, public_paths=["/"])
    # distinct port (never actually proxied — requests to it redirect to /login first)
    _seed_app(cfg.db_path, "privapp", backend_port + 1, public_paths=[])
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
