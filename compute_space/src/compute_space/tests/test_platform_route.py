"""Integration tests for the platform-service dispatch inside the v2 proxy.

Drives /api/services/v2/call/platform/... via TestClient with a seeded caller
app + app-token, granting platform capabilities directly in permissions_v2.
The actual install side-effect is patched out (exercised in the live harness).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.testing import TestClient

from compute_space.config import provide_config
from compute_space.core.app_id import new_app_id
from compute_space.core.auth.permissions_v2 import Grant
from compute_space.core.auth.permissions_v2 import grant_permission_v2
from compute_space.core.installer import InstallResult
from compute_space.core.platform_service import PLATFORM_SERVICE_URL
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.tests.conftest import _make_test_config
from compute_space.web.routes.services_v2 import services_v2_routes

CALLER_APP = "deployer-mind"
CALLER_TOKEN = "platform-caller-token"
CALLER_APP_ID = "PlatCaller01"

CALLER_MANIFEST = """
[app]
name = "deployer-mind"
version = "0.1.0"

[runtime.container]
image = "Dockerfile"
port = 8080

[[services.v2.consumes]]
service = "github.com/imbue-openhost/openhost/services/platform"
shortname = "platform"
version = ">=0.1.0"
grants = []
"""


def _make_app() -> Litestar:
    return Litestar(
        route_handlers=[services_v2_routes],
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        openapi_config=None,
    )


def _seed_caller(
    db_path: str,
    app_name: str = CALLER_APP,
    app_id: str = CALLER_APP_ID,
    token: str = CALLER_TOKEN,
    manifest_raw: str = CALLER_MANIFEST,
) -> None:
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            """INSERT INTO apps
                 (app_id, name, version, repo_path, local_port, status, installed_by, manifest_raw)
               VALUES (?, ?, ?, ?, ?, ?, NULL, ?)""",
            (app_id, app_name, "0.0.0", f"/tmp/{app_name}", 19600, "running", manifest_raw),
        )
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        db.execute("INSERT INTO app_tokens (app_id, token_hash) VALUES (?, ?)", (app_id, token_hash))
        db.commit()
    finally:
        db.close()


def _grant(db_path: str, consumer: str, grant: Grant, service: str = PLATFORM_SERVICE_URL) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        with mock.patch("compute_space.core.auth.permissions_v2.get_db", return_value=conn):
            grant_permission_v2(consumer, service, grant)
    finally:
        conn.close()


def _insert_app(
    db_path: str,
    *,
    name: str,
    app_id: str | None = None,
    installed_by: str | None = None,
    status: str = "running",
    port: int = 19700,
) -> str:
    app_id = app_id or new_app_id()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status, installed_by)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (app_id, name, "1.0.0", f"/tmp/{name}", port, status, installed_by),
        )
        conn.commit()
    finally:
        conn.close()
    return app_id


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    return _make_test_config(tmp_path, port=20600)


@pytest.fixture
def client(cfg: Any) -> Iterator[TestClient[Litestar]]:
    init_db(cfg.db_path)
    _seed_caller(cfg.db_path)
    with TestClient(app=_make_app()) as c:
        yield c


def _url(endpoint: str) -> str:
    return "/api/services/v2/call/platform/" + endpoint.lstrip("/")


def _headers(token: str = CALLER_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ── deploy ───────────────────────────────────────────────────────────────────


def test_deploy_without_grant_denied(client: TestClient[Litestar]) -> None:
    resp = client.post(_url("deploy"), headers=_headers(), content=json.dumps({"repo_url": "https://github.com/a/b"}))
    assert resp.status_code == 403
    assert resp.json()["error"] == "permission_required"


def test_deploy_with_matching_grant_succeeds_and_stamps_installed_by(
    client: TestClient[Litestar], cfg: Any
) -> None:
    _grant(cfg.db_path, CALLER_APP_ID, {"capability": "deploy", "repo_url_prefix": "*"})
    captured: dict[str, Any] = {}

    async def fake_install(repo_url, config, db, *, app_name=None, installed_by=None):  # type: ignore[no-untyped-def]
        captured["installed_by"] = installed_by
        # Simulate the app row the installer would have created.
        _insert_app(cfg.db_path, name="newapp", installed_by=installed_by)
        return InstallResult(app_name="newapp", status="building")

    with mock.patch("compute_space.web.routes.platform_dispatch.install_from_repo_url", side_effect=fake_install):
        resp = client.post(
            _url("deploy"), headers=_headers(), content=json.dumps({"repo_url": "https://github.com/x/y"})
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["app_name"] == "newapp" and body["app_id"]
    # Provenance stamped with the caller so manage/delegate can key on it.
    assert captured["installed_by"] == CALLER_APP_ID


def test_deploy_non_matching_prefix_denied(client: TestClient[Litestar], cfg: Any) -> None:
    _grant(cfg.db_path, CALLER_APP_ID, {"capability": "deploy", "repo_url_prefix": "https://github.com/acme/"})
    resp = client.post(
        _url("deploy"), headers=_headers(), content=json.dumps({"repo_url": "https://github.com/evil/x"})
    )
    assert resp.status_code == 403


# ── manage: list / status / stop / start / remove ────────────────────────────


def test_list_apps_requires_manage_grant(client: TestClient[Litestar]) -> None:
    resp = client.get(_url("apps"), headers=_headers())
    assert resp.status_code == 403


def test_list_apps_own_only_filters_by_installed_by(client: TestClient[Litestar], cfg: Any) -> None:
    _grant(cfg.db_path, CALLER_APP_ID, {"capability": "manage_apps", "target": "own"})
    mine = _insert_app(cfg.db_path, name="mine", installed_by=CALLER_APP_ID, port=19701)
    _insert_app(cfg.db_path, name="theirs", installed_by="someone-else", port=19702)
    resp = client.get(_url("apps"), headers=_headers())
    assert resp.status_code == 200
    names = {a["name"] for a in resp.json()["apps"]}
    assert "mine" in names and "theirs" not in names
    ids = {a["app_id"] for a in resp.json()["apps"]}
    assert mine in ids


def test_list_apps_all_sees_everything(client: TestClient[Litestar], cfg: Any) -> None:
    _grant(cfg.db_path, CALLER_APP_ID, {"capability": "manage_apps", "target": "all"})
    _insert_app(cfg.db_path, name="mine", installed_by=CALLER_APP_ID, port=19703)
    _insert_app(cfg.db_path, name="theirs", installed_by="someone-else", port=19704)
    resp = client.get(_url("apps"), headers=_headers())
    names = {a["name"] for a in resp.json()["apps"]}
    assert {"mine", "theirs"} <= names


def test_status_own_app_allowed(client: TestClient[Litestar], cfg: Any) -> None:
    _grant(cfg.db_path, CALLER_APP_ID, {"capability": "manage_apps", "target": "own"})
    aid = _insert_app(cfg.db_path, name="mine", installed_by=CALLER_APP_ID, port=19705)
    resp = client.get(_url(f"apps/{aid}/status"), headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"


def test_status_others_app_denied_under_own_scope(client: TestClient[Litestar], cfg: Any) -> None:
    _grant(cfg.db_path, CALLER_APP_ID, {"capability": "manage_apps", "target": "own"})
    aid = _insert_app(cfg.db_path, name="theirs", installed_by="other", port=19706)
    resp = client.get(_url(f"apps/{aid}/status"), headers=_headers())
    assert resp.status_code == 403


def test_specific_app_id_grant_allows_only_that_app(client: TestClient[Litestar], cfg: Any) -> None:
    target = _insert_app(cfg.db_path, name="target", installed_by="other", port=19707)
    other = _insert_app(cfg.db_path, name="other-app", installed_by="other", port=19708)
    _grant(cfg.db_path, CALLER_APP_ID, {"capability": "manage_apps", "target": target})
    assert client.get(_url(f"apps/{target}/status"), headers=_headers()).status_code == 200
    assert client.get(_url(f"apps/{other}/status"), headers=_headers()).status_code == 403


def test_stop_own_app(client: TestClient[Litestar], cfg: Any) -> None:
    _grant(cfg.db_path, CALLER_APP_ID, {"capability": "manage_apps", "target": "own"})
    aid = _insert_app(cfg.db_path, name="mine", installed_by=CALLER_APP_ID, port=19709)
    with (
        mock.patch("compute_space.web.routes.platform_dispatch.stop_app_process"),
        mock.patch("compute_space.web.routes.platform_dispatch.stop_container"),
    ):
        resp = client.post(_url(f"apps/{aid}/stop"), headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "stopped"
    conn = sqlite3.connect(cfg.db_path)
    try:
        status = conn.execute("SELECT status FROM apps WHERE app_id = ?", (aid,)).fetchone()[0]
    finally:
        conn.close()
    assert status == "stopped"


def test_remove_own_app_claims_and_spawns(client: TestClient[Litestar], cfg: Any) -> None:
    _grant(cfg.db_path, CALLER_APP_ID, {"capability": "manage_apps", "target": "own"})
    aid = _insert_app(cfg.db_path, name="mine", installed_by=CALLER_APP_ID, port=19710)
    with mock.patch("compute_space.web.routes.platform_dispatch.Thread") as thread_cls:
        resp = client.post(_url(f"apps/{aid}/remove"), headers=_headers(), content=json.dumps({"keep_data": True}))
    assert resp.status_code == 200
    assert resp.json()["status"] == "removing"
    thread_cls.assert_called_once()
    conn = sqlite3.connect(cfg.db_path)
    try:
        status = conn.execute("SELECT status FROM apps WHERE app_id = ?", (aid,)).fetchone()[0]
    finally:
        conn.close()
    assert status == "removing"


def test_manage_missing_app_under_own_scope_404(client: TestClient[Litestar], cfg: Any) -> None:
    _grant(cfg.db_path, CALLER_APP_ID, {"capability": "manage_apps", "target": "own"})
    resp = client.get(_url("apps/abcdefghijkm/status"), headers=_headers())
    # own/all callers may learn of a missing app (404); narrow app_id callers get 403.
    assert resp.status_code == 404


def test_manage_missing_app_under_specific_scope_403(client: TestClient[Litestar], cfg: Any) -> None:
    _grant(cfg.db_path, CALLER_APP_ID, {"capability": "manage_apps", "target": "some-other-id"})
    resp = client.get(_url("apps/abcdefghijkm/status"), headers=_headers())
    assert resp.status_code == 403


# ── system_read ──────────────────────────────────────────────────────────────


def test_system_requires_grant(client: TestClient[Litestar]) -> None:
    assert client.get(_url("system"), headers=_headers()).status_code == 403


def test_system_with_grant(client: TestClient[Litestar], cfg: Any) -> None:
    _grant(cfg.db_path, CALLER_APP_ID, {"capability": "system_read"})
    with mock.patch(
        "compute_space.web.routes.platform_dispatch.storage_status", return_value={"disk": "ok"}
    ):
        resp = client.get(_url("system"), headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["storage"] == {"disk": "ok"}


# ── delegate (non-escalating) ────────────────────────────────────────────────

SVC = "github.com/imbue-openhost/openhost/services/secrets"


def test_delegate_denied_without_capability(client: TestClient[Litestar], cfg: Any) -> None:
    target = _insert_app(cfg.db_path, name="child", installed_by=CALLER_APP_ID, port=19711)
    # caller holds the secrets grant but NOT delegate_permissions
    _grant(cfg.db_path, CALLER_APP_ID, {"key": "DB_URL"}, service=SVC)
    resp = client.post(
        _url("delegate"),
        headers=_headers(),
        content=json.dumps({"app_id": target, "service": SVC, "grant": {"key": "DB_URL"}}),
    )
    assert resp.status_code == 403


def test_delegate_denied_when_caller_lacks_grant(client: TestClient[Litestar], cfg: Any) -> None:
    target = _insert_app(cfg.db_path, name="child", installed_by=CALLER_APP_ID, port=19712)
    _grant(cfg.db_path, CALLER_APP_ID, {"capability": "delegate_permissions"})
    # caller does NOT hold {"key":"SECRET"} for SVC -> escalation attempt
    resp = client.post(
        _url("delegate"),
        headers=_headers(),
        content=json.dumps({"app_id": target, "service": SVC, "grant": {"key": "SECRET"}}),
    )
    assert resp.status_code == 403


def test_delegate_denied_to_app_not_deployed_by_caller(client: TestClient[Litestar], cfg: Any) -> None:
    target = _insert_app(cfg.db_path, name="not-mine", installed_by="someone-else", port=19713)
    _grant(cfg.db_path, CALLER_APP_ID, {"capability": "delegate_permissions"})
    _grant(cfg.db_path, CALLER_APP_ID, {"key": "DB_URL"}, service=SVC)
    resp = client.post(
        _url("delegate"),
        headers=_headers(),
        content=json.dumps({"app_id": target, "service": SVC, "grant": {"key": "DB_URL"}}),
    )
    assert resp.status_code == 403


def test_delegate_success_writes_grant_to_child(client: TestClient[Litestar], cfg: Any) -> None:
    target = _insert_app(cfg.db_path, name="child", installed_by=CALLER_APP_ID, port=19714)
    _grant(cfg.db_path, CALLER_APP_ID, {"capability": "delegate_permissions"})
    _grant(cfg.db_path, CALLER_APP_ID, {"key": "DB_URL"}, service=SVC)
    resp = client.post(
        _url("delegate"),
        headers=_headers(),
        content=json.dumps({"app_id": target, "service": SVC, "grant": {"key": "DB_URL"}}),
    )
    assert resp.status_code == 200, resp.text
    # The child now holds exactly the delegated grant for SVC.
    conn = sqlite3.connect(cfg.db_path)
    try:
        row = conn.execute(
            "SELECT grant_payload FROM permissions_v2 WHERE consumer_app_id = ? AND service_url = ?",
            (target, SVC),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert json.loads(row[0]) == {"key": "DB_URL"}


# ── misc ─────────────────────────────────────────────────────────────────────


def test_unknown_platform_subpath_404(client: TestClient[Litestar], cfg: Any) -> None:
    _grant(cfg.db_path, CALLER_APP_ID, {"capability": "system_read"})
    resp = client.post(_url("wat"), headers=_headers(), content=json.dumps({}))
    assert resp.status_code == 404


def test_platform_call_requires_app_auth(client: TestClient[Litestar]) -> None:
    resp = client.get(_url("apps"), headers={"Authorization": "Bearer bogus"})
    assert resp.status_code == 401


def test_version_mismatch_returns_503(cfg: Any) -> None:
    manifest = CALLER_MANIFEST.replace('version = ">=0.1.0"', 'version = ">=9.0.0"')
    init_db(cfg.db_path)
    _seed_caller(cfg.db_path, manifest_raw=manifest)
    _grant(cfg.db_path, CALLER_APP_ID, {"capability": "system_read"})
    with TestClient(app=_make_app()) as client:
        resp = client.get(_url("system"), headers=_headers())
    assert resp.status_code == 503
