"""Shared helpers for tests that drive Litestar routes via TestClient.

Kept under a leading-underscore name so pytest doesn't try to collect it.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any

import bcrypt
from litestar import Litestar
from litestar.di import Provide
from litestar.handlers.base import BaseRouteHandler
from litestar.types import ASGIApp
from litestar.types import Receive
from litestar.types import Scope
from litestar.types import Send

from compute_space.config import provide_config
from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import create_session
from compute_space.core.domains import primary_domain_or_none
from compute_space.db import get_db
from compute_space.db import provide_db
from compute_space.web.helpers.zone import ZONE_SCOPE_KEY


def make_http_scope(
    method: str,
    path: str,
    *,
    host: str,
    headers: dict[str, str] | None = None,
    cookie: str | None = None,
    client: tuple[str, int] | None = None,
    extra_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a minimal HTTP ASGI scope for auth / middleware unit tests.

    Callers wrap the result however they need — ``Request(scope)``, ``ASGIConnection(scope)``, or pass
    the raw dict straight into an ASGI middleware.  ``cookie`` (a raw ``name=value`` string) and
    ``headers`` become request headers; ``extra_scope`` merges in extra scope keys (e.g. the
    ``ZONE_SCOPE_KEY`` the proxy middleware would normally stash).
    """
    raw_headers: list[tuple[bytes, bytes]] = [(b"host", host.encode())]
    if cookie is not None:
        raw_headers.append((b"cookie", cookie.encode()))
    for name, value in (headers or {}).items():
        raw_headers.append((name.lower().encode(), value.encode()))
    scope: dict[str, Any] = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "scheme": "http",
        "server": ("127.0.0.1", 8080),
        "root_path": "",
        "headers": raw_headers,
        **(extra_scope or {}),
    }
    if client is not None:
        scope["client"] = client
    return scope


def stash_zone_middleware(app: ASGIApp) -> ASGIApp:
    """Test stand-in for the zone-stashing half of ``SubdomainProxyMiddleware``: put the DB primary
    in the request scope so ``zone_for_request`` resolves in minimal test apps that omit the full
    proxy middleware (the real middleware is required on every request in production)."""

    async def middleware(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            with closing(get_db()) as db:
                scope[ZONE_SCOPE_KEY] = primary_domain_or_none(db)
        await app(scope, receive, send)

    return middleware


def seed_user(db_path: str, username: str = "owner", password: str = "testpass1") -> int:
    """Insert a user row using a real bcrypt hash and return user_id."""
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, pw_hash),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def session_token_for(db_path: str, user_id: int) -> str:
    """Mint a session token bound to ``user_id`` directly against the DB."""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        token = create_session(user_id, conn)
        conn.commit()
        return token
    finally:
        conn.close()


def auth_cookie(cfg: Any, username: str = "owner") -> dict[str, str]:
    """Seed a user + session and return a Cookie dict for the TestClient."""
    user_id = seed_user(cfg.db_path, username=username)
    token = session_token_for(cfg.db_path, user_id)
    return {SESSION_COOKIE_NAME: token}


def make_test_app(*route_handlers: Any) -> Litestar:
    """Build a Litestar app from the given route handlers + standard DI.

    Used by route-level tests that don't want the full ``create_app`` boot path.
    """
    return Litestar(
        route_handlers=list(route_handlers),
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        middleware=[stash_zone_middleware],
        openapi_config=None,
    )


def _allow(_connection: Any, _route_handler: BaseRouteHandler) -> None:
    """Guard that always allows — drop-in for ``require_owner_auth`` when
    tests want to focus on route logic without seeding a session."""
    return None
