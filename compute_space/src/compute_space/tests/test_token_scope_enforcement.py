"""Guard-level enforcement tests for API-token scopes.

Seeds real ``api_tokens`` rows with specific scopes and drives real routes via
TestClient, asserting a token is allowed on in-scope routes and rejected on
out-of-scope ones. The `owner` super-scope passes everywhere; a session owner
is never scope-restricted.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from litestar.testing import TestClient

from compute_space.config import set_active_config
from compute_space.core.auth.scopes import APPS_READ
from compute_space.core.auth.scopes import OWNER
from compute_space.core.auth.scopes import TOKENS_MANAGE
from compute_space.db.connection import init_db
from compute_space.db.versioned import apply_migrations
from compute_space.web.routes.api.apps import api_apps
from compute_space.web.routes.api.system import api_tokens_list

from ._litestar_helpers import auth_cookie
from ._litestar_helpers import make_test_app
from .conftest import _make_test_config


def _seed_token(db_path: str, name: str, scopes_json: str, raw: str = "raw-secret") -> str:
    """Insert an api_tokens row (never-expiring) with the given scopes JSON.

    Returns the raw bearer token to send in the Authorization header.
    """
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO api_tokens (name, token_hash, expires_at, scopes) VALUES (?, ?, '', ?)",
            (name, token_hash, scopes_json),
        )
        conn.commit()
    finally:
        conn.close()
    return raw


@pytest.fixture
def cfg(tmp_path: Path) -> Iterator[Any]:
    config = _make_test_config(tmp_path, zone_domain="alice-zone.example.com")
    init_db(config.db_path)
    set_active_config(config)
    yield config


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_apps_read_token_allowed_on_apps_denied_on_tokens(cfg: Any) -> None:
    token = _seed_token(cfg.db_path, "reader", '["apps:read"]', raw="apps-read-tok")
    app = make_test_app(api_apps, api_tokens_list)
    with TestClient(app=app) as client:
        # in-scope: apps:read grants GET /api/apps
        assert client.get("/api/apps", headers=_bearer(token)).status_code == 200
        # out-of-scope: listing tokens needs tokens:manage
        assert client.get("/api/tokens", headers=_bearer(token)).status_code == 401


def test_tokens_manage_token_allowed_on_tokens_denied_on_apps(cfg: Any) -> None:
    token = _seed_token(cfg.db_path, "tok-mgr", f'["{TOKENS_MANAGE}"]', raw="tok-mgr-tok")
    app = make_test_app(api_apps, api_tokens_list)
    with TestClient(app=app) as client:
        assert client.get("/api/tokens", headers=_bearer(token)).status_code == 200
        assert client.get("/api/apps", headers=_bearer(token)).status_code == 401


def test_owner_scope_passes_everywhere(cfg: Any) -> None:
    token = _seed_token(cfg.db_path, "full", f'["{OWNER}"]', raw="owner-tok")
    app = make_test_app(api_apps, api_tokens_list)
    with TestClient(app=app) as client:
        assert client.get("/api/apps", headers=_bearer(token)).status_code == 200
        assert client.get("/api/tokens", headers=_bearer(token)).status_code == 200


def test_empty_scopes_token_denied_everywhere(cfg: Any) -> None:
    # A token stripped of all scopes (explicit empty array) has no access.
    token = _seed_token(cfg.db_path, "empty", "[]", raw="empty-tok")
    app = make_test_app(api_apps, api_tokens_list)
    with TestClient(app=app) as client:
        assert client.get("/api/apps", headers=_bearer(token)).status_code == 401
        assert client.get("/api/tokens", headers=_bearer(token)).status_code == 401


def test_session_owner_is_never_scope_restricted(cfg: Any) -> None:
    # A human session owner passes every scoped route regardless of scopes.
    cookie = auth_cookie(cfg, username="owner")
    app = make_test_app(api_apps, api_tokens_list)
    with TestClient(app=app) as client:
        assert client.get("/api/apps", cookies=cookie).status_code == 200
        assert client.get("/api/tokens", cookies=cookie).status_code == 200


def test_unknown_bearer_rejected(cfg: Any) -> None:
    _seed_token(cfg.db_path, "reader", f'["{APPS_READ}"]', raw="real-tok")
    app = make_test_app(api_apps, api_tokens_list)
    with TestClient(app=app) as client:
        assert client.get("/api/apps", headers=_bearer("bogus")).status_code == 401


def test_v13_migration_backfills_existing_tokens_to_owner(tmp_path: Path) -> None:
    """A token that predates scopes must become explicit ["owner"] (not empty,
    not a silent default) so its full-access behaviour is preserved."""
    db_path = str(tmp_path / "pre_v13.db")
    conn = sqlite3.connect(db_path)
    try:
        # Minimal pre-v13 shape: api_tokens without a scopes column, stamped v12.
        conn.executescript(
            """
            CREATE TABLE api_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE schema_version (id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL);
            INSERT INTO schema_version (id, version) VALUES (1, 12);
            INSERT INTO api_tokens (name, token_hash, expires_at) VALUES ('legacy', 'deadbeef', '');
            """
        )
        conn.commit()
    finally:
        conn.close()

    apply_migrations(db_path)

    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT scopes FROM api_tokens WHERE name = 'legacy'").fetchone()
    finally:
        conn.close()
    assert row["scopes"] == '["owner"]'
