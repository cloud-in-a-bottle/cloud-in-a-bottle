"""End-to-end test for the subdomain proxy's response to an unauthenticated request.

This reproduces the router-side half of the "form POST bounced to /login → 405" bug: an unauthenticated
request to a protected app path must not be answered with a 302→/login when the method is unsafe.  A
browser following that redirect re-issues the request as a bodyless GET, so a POST-only app route answers
405 and the form action is silently lost.  The router must return 403 for unsafe methods instead, while
still redirecting navigational GETs so logged-out users reach the login page.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from compute_space.core.app_id import new_app_id
from compute_space.tests.conftest import _make_test_config
from compute_space.web.middleware.subdomain_proxy import SubdomainProxyMiddleware

APP_NAME = "miniflux"
APP_HOST = f"{APP_NAME}.testzone.local"
PROTECTED_PATH = "/feeds/refresh"


@pytest.fixture
def _seeded_db(tmp_path: Path) -> Iterator[None]:
    # _make_test_config initialises the DB and seeds `testzone.local` as the primary domain, which the
    # middleware needs to recognise the zone (Domain.match) and strip it to find the app subdomain.
    cfg = _make_test_config(tmp_path, zone_domain="testzone.local", tls_enabled=True)
    conn = sqlite3.connect(cfg.db_path)
    try:
        # public_paths left at its '[]' default so PROTECTED_PATH requires owner auth.
        conn.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status, installed_by)
               VALUES (?, ?, ?, ?, ?, ?, NULL)""",
            (new_app_id(), APP_NAME, "1.0.0", str(tmp_path / APP_NAME), 19700, "running"),
        )
        conn.commit()
    finally:
        conn.close()
    yield


class _SentinelApp:
    """Wrapped ASGI app that must never be reached: auth must fail before any proxying happens."""

    def __init__(self) -> None:
        self.called = False

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.called = True


async def _drive(method: str) -> tuple[int, dict[str, str], bool]:
    """Run one unauthenticated request through the middleware; return (status, headers, wrapped_app_called)."""
    sentinel = _SentinelApp()
    middleware = SubdomainProxyMiddleware(sentinel)
    scope: dict[str, Any] = {
        "type": "http",
        "method": method,
        "path": PROTECTED_PATH,
        "raw_path": PROTECTED_PATH.encode(),
        "query_string": b"",
        "scheme": "http",
        "server": ("127.0.0.1", 8080),
        "client": ("127.0.0.1", 12345),
        "root_path": "",
        # same-origin form post: host + matching Origin, but NO session cookie -> unauthenticated.
        "headers": [(b"host", APP_HOST.encode()), (b"origin", f"https://{APP_HOST}".encode())],
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await middleware(scope, receive, send)  # type: ignore[arg-type]

    start = next(m for m in sent if m["type"] == "http.response.start")
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return start["status"], headers, sentinel.called


@pytest.mark.asyncio
async def test_unauthenticated_post_gets_403_not_login_redirect(_seeded_db: None) -> None:
    status, headers, wrapped_called = await _drive("POST")
    assert status == 403, "an unsafe-method request must fail closed, not 302→/login (which downgrades to GET → 405)"
    assert "location" not in headers
    assert not wrapped_called, "auth must be enforced before the request is proxied to the app"


@pytest.mark.asyncio
async def test_unauthenticated_get_redirects_to_login(_seeded_db: None) -> None:
    status, headers, wrapped_called = await _drive("GET")
    assert status == 302, "a navigational GET should still send the logged-out user to /login"
    assert "/login?next=" in headers.get("location", "")
    assert not wrapped_called
