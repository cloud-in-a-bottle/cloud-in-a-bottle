"""Shared helpers for tests that drive Litestar routes via TestClient.

Kept under a leading-underscore name so pytest doesn't try to collect it.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any

import bcrypt
from litestar import Litestar
from litestar.handlers.base import BaseRouteHandler
from litestar.types import ASGIApp
from litestar.types import Receive
from litestar.types import Scope
from litestar.types import Send

from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import create_session
from compute_space.core.domains import primary_domain_or_none
from compute_space.db import get_db
from compute_space.web.helpers.zone import ZONE_SCOPE_KEY
from compute_space.web.routes.manifest import APP_DEPENDENCIES


def stash_zone_middleware(app: ASGIApp) -> ASGIApp:
    """Test stand-in for the zone-stashing half of ``SubdomainProxyMiddleware``: put the DB primary
    in the request scope so ``zone_for_request`` resolves in minimal test apps that omit the full
    proxy middleware (the real middleware is required on every request in production)."""

    async def middleware(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            try:
                db = get_db()
            except RuntimeError:
                db = None  # app under test never called init_db(); no zone to stash
            if db is not None:
                with closing(db):
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
        dependencies=dict(APP_DEPENDENCIES),
        middleware=[stash_zone_middleware],
        openapi_config=None,
    )


def _allow(_connection: Any, _route_handler: BaseRouteHandler) -> None:
    """Guard that always allows — drop-in for ``require_owner_auth`` when
    tests want to focus on route logic without seeding a session."""
    return None
