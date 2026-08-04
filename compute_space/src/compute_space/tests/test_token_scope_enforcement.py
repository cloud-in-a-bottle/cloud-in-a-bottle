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
from compute_space.core.auth.scopes import SYSTEM_ADMIN
from compute_space.core.auth.scopes import SYSTEM_READ
from compute_space.core.auth.scopes import TOKENS_MANAGE
from compute_space.db.connection import init_db
from compute_space.db.versioned import apply_migrations
from compute_space.web.routes.api.apps import api_apps
from compute_space.web.routes.api.domains import add_domain
from compute_space.web.routes.api.domains import list_domains
from compute_space.web.routes.api.system import api_token_scopes
from compute_space.web.routes.api.system import api_tokens_list
from compute_space.web.routes.api.system import api_tokens_update

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
            "INSERT INTO api_tokens (token_id, name, token_hash, expires_at, scopes) VALUES (?, ?, ?, '', ?)",
            (f"tok_{raw}", name, token_hash, scopes_json),
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


def test_scope_catalog_endpoint(cfg: Any) -> None:
    # The catalog endpoint (single source the CLI/UI render from) needs
    # tokens:manage and returns name/description/owner_equivalent entries.
    token = _seed_token(cfg.db_path, "mgr", f'["{TOKENS_MANAGE}"]', raw="cat-tok")
    reader = _seed_token(cfg.db_path, "rdr", f'["{APPS_READ}"]', raw="rdr-tok")
    app = make_test_app(api_token_scopes)
    with TestClient(app=app) as client:
        r = client.get("/api/token_scopes", headers=_bearer(token))
        assert r.status_code == 200
        catalog = r.json()
        names = {s["name"] for s in catalog}
        assert {"owner", "apps:read", "tokens:manage"} <= names
        owner = next(s for s in catalog if s["name"] == "owner")
        assert owner["owner_equivalent"] is True
        # out of scope for a non-tokens:manage token
        assert client.get("/api/token_scopes", headers=_bearer(reader)).status_code == 401


def _token_scopes(db_path: str, name: str) -> str | None:
    """Read the stored scopes JSON for a seeded token by name."""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT scopes FROM api_tokens WHERE name = ?", (name,)).fetchone()
    finally:
        conn.close()
    return row["scopes"] if row else None


def test_domains_scope_enforcement(cfg: Any) -> None:
    # GET /api/domains is system:read; POST /api/domains is system:admin.  A
    # system:read token may list but not add; an unrelated scope is denied on
    # both.  (The flat scope model means system:admin does not imply
    # system:read, matching ssh_status/toggle_ssh and storage-status/guard.)
    reader = _seed_token(cfg.db_path, "sys-reader", f'["{SYSTEM_READ}"]', raw="sys-read-tok")
    admin = _seed_token(cfg.db_path, "sys-admin", f'["{SYSTEM_ADMIN}"]', raw="sys-admin-tok")
    apps_reader = _seed_token(cfg.db_path, "apps-reader", f'["{APPS_READ}"]', raw="apps-read-tok2")
    app = make_test_app(list_domains, add_domain)
    with TestClient(app=app) as client:
        # system:read: list allowed, add denied.
        assert client.get("/api/domains", headers=_bearer(reader)).status_code == 200
        assert client.post("/api/domains", json={"name": "x.local"}, headers=_bearer(reader)).status_code == 401
        # apps:read: both denied (no system scope).
        assert client.get("/api/domains", headers=_bearer(apps_reader)).status_code == 401
        # system:admin: list denied (admin doesn't imply read in the flat model).
        assert client.get("/api/domains", headers=_bearer(admin)).status_code == 401


def test_patch_tokens_rewrites_scopes(cfg: Any) -> None:
    # A tokens:manage token may edit another token's scopes in place, keyed by
    # the opaque token_id; the new scopes are validated and persisted.
    mgr = _seed_token(cfg.db_path, "tok-mgr", f'["{TOKENS_MANAGE}"]', raw="mgr-tok")
    _seed_token(cfg.db_path, "target", f'["{APPS_READ}"]', raw="target-tok")
    app = make_test_app(api_tokens_update)
    with TestClient(app=app) as client:
        resp = client.patch(
            "/api/tokens/tok_target-tok",
            json={"scopes": [APPS_READ, "apps:logs"]},
            headers=_bearer(mgr),
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
    assert _token_scopes(cfg.db_path, "target") == '["apps:logs", "apps:read"]'


def test_patch_tokens_rejects_unknown_scope(cfg: Any) -> None:
    mgr = _seed_token(cfg.db_path, "tok-mgr", f'["{TOKENS_MANAGE}"]', raw="mgr2-tok")
    _seed_token(cfg.db_path, "target", f'["{APPS_READ}"]', raw="target2-tok")
    app = make_test_app(api_tokens_update)
    with TestClient(app=app) as client:
        resp = client.patch(
            "/api/tokens/tok_target2-tok",
            json={"scopes": ["not:a:scope"]},
            headers=_bearer(mgr),
        )
        assert resp.status_code == 400
    assert _token_scopes(cfg.db_path, "target") == '["apps:read"]'


def test_patch_tokens_rejects_empty_scopes(cfg: Any) -> None:
    # Updating a token to an empty scope list is rejected; the row is untouched.
    mgr = _seed_token(cfg.db_path, "tok-mgr", f'["{TOKENS_MANAGE}"]', raw="mgr3-tok")
    _seed_token(cfg.db_path, "target", f'["{APPS_READ}"]', raw="target-empty-tok")
    app = make_test_app(api_tokens_update)
    with TestClient(app=app) as client:
        resp = client.patch(
            "/api/tokens/tok_target-empty-tok",
            json={"scopes": []},
            headers=_bearer(mgr),
        )
        assert resp.status_code == 400
    assert _token_scopes(cfg.db_path, "target") == '["apps:read"]'


def test_patch_tokens_unknown_id_returns_404(cfg: Any) -> None:
    # Updating a nonexistent token id returns 404 and creates no row.
    mgr = _seed_token(cfg.db_path, "tok-mgr", f'["{TOKENS_MANAGE}"]', raw="mgr404-tok")
    app = make_test_app(api_tokens_update)
    with TestClient(app=app) as client:
        resp = client.patch(
            "/api/tokens/tok_does-not-exist",
            json={"scopes": [APPS_READ]},
            headers=_bearer(mgr),
        )
        assert resp.status_code == 404
    conn = sqlite3.connect(cfg.db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM api_tokens").fetchone()[0]
    finally:
        conn.close()
    assert count == 1  # only the manager token exists


def test_patch_tokens_requires_tokens_manage_scope(cfg: Any) -> None:
    # A token without tokens:manage cannot edit scopes (escalation guard).
    reader = _seed_token(cfg.db_path, "reader", f'["{APPS_READ}"]', raw="reader-tok")
    _seed_token(cfg.db_path, "target", f'["{APPS_READ}"]', raw="target3-tok")
    app = make_test_app(api_tokens_update)
    with TestClient(app=app) as client:
        resp = client.patch(
            "/api/tokens/tok_target3-tok",
            json={"scopes": [OWNER]},
            headers=_bearer(reader),
        )
        assert resp.status_code == 401
    assert _token_scopes(cfg.db_path, "target") == '["apps:read"]'


def test_v14_migration_backfills_existing_tokens(tmp_path: Path) -> None:
    """A token that predates scopes must gain an explicit ["owner"] (not empty,
    not a silent default) and a unique token_id, preserving its full access."""
    db_path = str(tmp_path / "pre_v14.db")
    conn = sqlite3.connect(db_path)
    try:
        # Minimal pre-v14 api_tokens shape (no scopes/token_id), stamped at v13
        # so only the v14 migration runs against it.
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
            INSERT INTO schema_version (id, version) VALUES (1, 13);
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
        row = conn.execute("SELECT token_id, scopes FROM api_tokens WHERE name = 'legacy'").fetchone()
    finally:
        conn.close()
    assert row["scopes"] == '["owner"]'
    assert row["token_id"].startswith("tok_")
