"""Integration tests for the installer dispatch inside the v2 service proxy.

Tests the request-routing surface: shortname dispatch, version specifier
handling, permission check, endpoint dispatch (install / status / logs),
and the ``{installed_by != caller}`` visibility scoping.  The actual
install side-effect (clone + build + run) is patched out — that's
exercised in the end-to-end harness, not here.
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
from compute_space.core.auth.permissions_v2 import grant_permission_v2
from compute_space.core.installer import INSTALLER_SERVICE_URL
from compute_space.core.installer import InstallResult
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.tests.conftest import _make_test_config
from compute_space.web.routes.services_v2 import services_v2_routes

CALLER_APP = "openhost-catalog"
CALLER_TOKEN = "test-installer-caller-token"
# Pinned so /status / /logs scoping checks compare against the same value
# the installer wrote into ``installed_by``.
CALLER_APP_ID = "TestCaller01"

# Manifest the caller declares.  Crucially includes a
# [[services.v2.consumes]] entry pointing at the installer service so
# /api/services/v2/call/installer/... resolves.
CALLER_MANIFEST = """
[app]
name = "openhost-catalog"
version = "0.2.0"

[runtime.container]
image = "Dockerfile"
port = 8080

[[services.v2.consumes]]
service = "github.com/imbue-openhost/openhost/services/installer"
shortname = "installer"
version = ">=0.1.0"
grants = [{capability = "install", repo_url_prefix = "*"}]
"""


def _make_app() -> Litestar:
    """Build a Litestar app exposing services_v2_routes.  Auth happens inside
    the ``require_app_auth`` guard (which calls ``authenticate(connection)``
    on demand), so no outer middleware is needed for bearer-token auth."""
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
    """Insert the caller's apps row (with manifest_raw) + app_tokens row
    so require_app_auth can resolve the bearer to the caller's app_id
    AND lookup_shortname can find the installer shortname declaration."""
    db = sqlite3.connect(db_path)
    try:
        db.row_factory = sqlite3.Row
        db.execute(
            """INSERT INTO apps
                 (app_id, name, version, repo_path, local_port, status, installed_by, manifest_raw)
               VALUES (?, ?, ?, ?, ?, ?, NULL, ?)""",
            (app_id, app_name, "0.0.0", f"/tmp/{app_name}", 19500, "running", manifest_raw),
        )
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        db.execute(
            "INSERT INTO app_tokens (app_id, token_hash) VALUES (?, ?)",
            (app_id, token_hash),
        )
        db.commit()
    finally:
        db.close()


def _grant_installer(db_path: str, app_id: str, repo_url_prefix: str = "*") -> None:
    """Add a permissions_v2 row letting ``app_id`` use the installer service."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        with mock.patch("compute_space.core.auth.permissions_v2.get_db", return_value=conn):
            grant_permission_v2(
                app_id,
                INSTALLER_SERVICE_URL,
                {"capability": "install", "repo_url_prefix": repo_url_prefix},
            )
    finally:
        conn.close()


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    return _make_test_config(tmp_path, port=20500)


@pytest.fixture
def client(cfg: Any) -> Iterator[TestClient[Litestar]]:
    init_db(cfg.db_path)
    _seed_caller(cfg.db_path)
    with TestClient(app=_make_app()) as c:
        yield c


def _url(endpoint: str) -> str:
    """Build the v2 shortname-call URL for the installer."""
    return "/api/services/v2/call/installer/" + endpoint.lstrip("/")


def _headers(token: str = CALLER_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# --- /install --------------------------------------------------------------


def test_install_without_grant_returns_permission_required(client: TestClient[Litestar]) -> None:
    resp = client.post(
        _url("install"),
        headers=_headers(),
        content=json.dumps({"repo_url": "https://github.com/cloud-in-a-bottle/app-catalog"}),
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["extra"]["code"] == "permission_required"
    assert body["extra"]["required_grant"]["grant"]["capability"] == "install"
    # The proposed grant must come from the consumer's manifest-declared
    # prefix ("*" in CALLER_MANIFEST), not the verbatim requested URL —
    # otherwise the owner gets a fresh approval prompt per repo.
    assert body["extra"]["required_grant"]["grant"]["repo_url_prefix"] == "*"


def test_install_with_non_matching_grant_denied(client: TestClient[Litestar], cfg: Any) -> None:
    _grant_installer(cfg.db_path, CALLER_APP_ID, repo_url_prefix="https://github.com/cloud-in-a-bottle/")
    resp = client.post(
        _url("install"),
        headers=_headers(),
        content=json.dumps({"repo_url": "https://github.com/evil/badapp"}),
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["extra"]["code"] == "permission_required"


# Caller whose manifest declares the GitHub-org prefix the catalog actually ships
# with.  Used by test_proposed_grant_uses_manifest_declared_prefix to exercise
# the org-scoped (rather than wildcard) path.
NARROW_CALLER_APP = "narrow-catalog"
NARROW_CALLER_TOKEN = "narrow-test-token"
NARROW_CALLER_MANIFEST = """
[app]
name = "narrow-catalog"
version = "0.2.0"

[runtime.container]
image = "Dockerfile"
port = 8080

[[services.v2.consumes]]
service = "github.com/imbue-openhost/openhost/services/installer"
shortname = "installer"
version = ">=0.1.0"
grants = [{capability = "install", repo_url_prefix = "https://github.com/cloud-in-a-bottle/"}]
"""


def test_proposed_grant_uses_manifest_declared_prefix(cfg: Any) -> None:
    """When the consumer's manifest declares a broad prefix, a denial for an
    URL that matches that prefix must propose the broad grant — not a fresh
    per-URL grant — so a single approval covers every install."""
    init_db(cfg.db_path)
    _seed_caller(
        cfg.db_path,
        app_name=NARROW_CALLER_APP,
        token=NARROW_CALLER_TOKEN,
        manifest_raw=NARROW_CALLER_MANIFEST,
    )
    with TestClient(app=_make_app()) as client:
        resp = client.post(
            _url("install"),
            headers={"Authorization": f"Bearer {NARROW_CALLER_TOKEN}", "Content-Type": "application/json"},
            content=json.dumps({"repo_url": "https://github.com/cloud-in-a-bottle/bottled-gemini"}),
        )
    assert resp.status_code == 403
    body = resp.json()
    assert body["extra"]["required_grant"]["grant"] == {
        "capability": "install",
        "repo_url_prefix": "https://github.com/cloud-in-a-bottle/",
    }


def test_proposed_grant_falls_back_when_manifest_prefix_doesnt_match(cfg: Any) -> None:
    """If the consumer's manifest only declares a narrow prefix that does NOT
    cover the requested URL, fall back to a per-URL grant rather than
    suggesting a grant that wouldn't help."""
    init_db(cfg.db_path)
    _seed_caller(
        cfg.db_path,
        app_name=NARROW_CALLER_APP,
        token=NARROW_CALLER_TOKEN,
        manifest_raw=NARROW_CALLER_MANIFEST,
    )
    with TestClient(app=_make_app()) as client:
        resp = client.post(
            _url("install"),
            headers={"Authorization": f"Bearer {NARROW_CALLER_TOKEN}", "Content-Type": "application/json"},
            content=json.dumps({"repo_url": "https://gitlab.com/something/else"}),
        )
    assert resp.status_code == 403
    body = resp.json()
    assert body["extra"]["required_grant"]["grant"]["repo_url_prefix"] == "https://gitlab.com/something/else"


def test_install_with_matching_grant_succeeds(client: TestClient[Litestar], cfg: Any) -> None:
    _grant_installer(cfg.db_path, CALLER_APP_ID, repo_url_prefix="*")

    async def fake_install(repo_url, config, db, *, app_name=None, installed_by=None):  # type: ignore[no-untyped-def]
        return InstallResult(app_name="newapp", status="building")

    with mock.patch("compute_space.web.routes.services_v2.install_from_repo_url", side_effect=fake_install):
        resp = client.post(
            _url("install"),
            headers=_headers(),
            content=json.dumps({"repo_url": "https://github.com/anyone/anything"}),
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"ok": True, "app_name": "newapp", "status": "building"}


def test_install_propagates_install_error_status_code(client: TestClient[Litestar], cfg: Any) -> None:
    _grant_installer(cfg.db_path, CALLER_APP_ID, repo_url_prefix="*")

    async def fake_install(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("manifest invalid")

    with mock.patch("compute_space.web.routes.services_v2.install_from_repo_url", side_effect=fake_install):
        resp = client.post(
            _url("install"),
            headers=_headers(),
            content=json.dumps({"repo_url": "https://github.com/foo/bar"}),
        )
    assert resp.status_code == 400
    body = resp.json()
    assert body["extra"]["code"] == "install_failed"
    assert body["extra"]["output"] == "manifest invalid"


def test_install_rejects_missing_repo_url(client: TestClient[Litestar], cfg: Any) -> None:
    _grant_installer(cfg.db_path, CALLER_APP_ID, repo_url_prefix="*")
    resp = client.post(
        _url("install"),
        headers=_headers(),
        content=json.dumps({"app_name": "no-url-supplied"}),
    )
    assert resp.status_code == 400


def test_install_rejects_non_json_body(client: TestClient[Litestar], cfg: Any) -> None:
    _grant_installer(cfg.db_path, CALLER_APP_ID, repo_url_prefix="*")
    resp = client.post(_url("install"), headers=_headers(), content="not-json")
    assert resp.status_code == 400


# --- /status/<name> --------------------------------------------------------


def test_status_returns_404_for_unknown_app(client: TestClient[Litestar], cfg: Any) -> None:
    _grant_installer(cfg.db_path, CALLER_APP_ID)
    resp = client.get(_url("status/nope"), headers=_headers())
    assert resp.status_code == 404


def test_status_returns_403_for_app_not_installed_by_caller(client: TestClient[Litestar], cfg: Any) -> None:
    _grant_installer(cfg.db_path, CALLER_APP_ID)
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status, installed_by)
               VALUES (?, ?, ?, ?, ?, ?, NULL)""",
            (new_app_id(), "dashboard-installed-app", "1.0.0", "/tmp/dash", 19501, "running"),
        )
        db.commit()
    finally:
        db.close()
    resp = client.get(_url("status/dashboard-installed-app"), headers=_headers())
    assert resp.status_code == 403


def test_status_returns_app_state_for_caller_installs(client: TestClient[Litestar], cfg: Any) -> None:
    _grant_installer(cfg.db_path, CALLER_APP_ID)
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status, installed_by, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (new_app_id(), "catalog-installed-app", "1.0.0", "/tmp/c", 19502, "running", CALLER_APP_ID, None),
        )
        db.commit()
    finally:
        db.close()
    resp = client.get(_url("status/catalog-installed-app"), headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "running", "error": None}


# --- Shortname not declared ------------------------------------------------


def test_unknown_shortname_returns_404(client: TestClient[Litestar], cfg: Any) -> None:
    """Calls to /api/services/v2/call/<other>/... 404 because the caller's
    manifest only declares the installer shortname."""
    _grant_installer(cfg.db_path, CALLER_APP_ID, repo_url_prefix="*")
    resp = client.post(
        "/api/services/v2/call/not-installed/anything",
        headers=_headers(),
        content=json.dumps({}),
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["extra"]["code"] == "shortname_not_declared"


# --- Unknown installer sub-endpoint ----------------------------------------


def test_unknown_installer_subpath_returns_404(client: TestClient[Litestar], cfg: Any) -> None:
    _grant_installer(cfg.db_path, CALLER_APP_ID)
    resp = client.post(_url("wat"), headers=_headers(), content=json.dumps({}))
    assert resp.status_code == 404
