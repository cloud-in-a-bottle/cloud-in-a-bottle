"""Tests for the ``/api/storage/archive_backend`` endpoints."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from litestar import Litestar
from litestar.testing import TestClient

import compute_space.web.routes.api.apps as apps_routes
from compute_space.core import archive_backend
from compute_space.core.app_id import new_app_id
from compute_space.core.manifest import AppManifest
from compute_space.db.connection import init_db
from compute_space.tests._litestar_helpers import auth_cookie
from compute_space.tests._litestar_helpers import make_test_app
from compute_space.tests.conftest import _make_test_config
from compute_space.web.routes.api.apps import api_apps_routes
from compute_space.web.routes.api.archive_backend import api_archive_backend_routes


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    cfg = _make_test_config(tmp_path, port=20400)
    init_db(cfg.db_path)
    return cfg


@pytest.fixture
def client(cfg: Any) -> Iterator[TestClient[Litestar]]:
    with TestClient(app=make_test_app(api_archive_backend_routes)) as c:
        yield c


@pytest.fixture
def cookies(cfg: Any) -> dict[str, str]:
    return auth_cookie(cfg)


# --- GET state ------------------------------------------------------------


def test_get_returns_seeded_local_state(client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    resp = client.get("/api/storage/archive_backend", cookies=cookies)
    assert resp.status_code == 200
    body = resp.json()
    assert body["backend"] == "local"
    assert body["s3_bucket"] is None
    assert body["meta_dumps"] is None
    assert "s3_secret_access_key" not in body


def test_get_redacts_secret_when_s3(cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    """In s3 mode the access_key_id is visible (so the dashboard can show
    the AKIA prefix) but the secret is never returned."""
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket='b', "
            "s3_access_key_id='AKIASOMETHING', s3_secret_access_key='hunter2'"
        )
        db.commit()
    finally:
        db.close()

    resp = client.get("/api/storage/archive_backend", cookies=cookies)
    body = resp.json()
    assert body["s3_access_key_id"] == "AKIASOMETHING"
    assert "s3_secret_access_key" not in body


def test_get_surfaces_meta_db_path(client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    """meta_db_path always surfaces (under juicefs/state/) so the operator
    can pre-plan their backup story."""
    resp = client.get("/api/storage/archive_backend", cookies=cookies)
    body = resp.json()
    assert body["meta_db_path"].endswith("/juicefs/state/meta.db")


def test_get_surfaces_meta_dumps_when_s3(cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket='b', "
            "s3_prefix='zone-a', s3_access_key_id='AKIA', s3_secret_access_key='hunter2'"
        )
        db.commit()
    finally:
        db.close()

    summary = archive_backend.MetaDumpSummary(
        count=42,
        latest_at="2026-05-01T18:00:00Z",
        latest_key="zone-a/meta/dump-2026-05-01-180000.json.gz",
    )
    with mock.patch.object(archive_backend, "list_meta_dumps", return_value=summary):
        resp = client.get("/api/storage/archive_backend", cookies=cookies)
    body = resp.json()
    assert body["meta_dumps"]["count"] == 42
    assert body["meta_dumps"]["latest_at"] == "2026-05-01T18:00:00Z"


def test_get_meta_dumps_null_on_disabled(client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    resp = client.get("/api/storage/archive_backend", cookies=cookies)
    body = resp.json()
    assert body["meta_dumps"] is None


def test_get_meta_dumps_null_on_s3_list_failure(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    """``meta_dumps: null`` distinguishes "status unavailable" from "no dumps yet"."""
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket='b', "
            "s3_access_key_id='AKIA', s3_secret_access_key='hunter2'"
        )
        db.commit()
    finally:
        db.close()
    with mock.patch.object(archive_backend, "list_meta_dumps", return_value=None):
        resp = client.get("/api/storage/archive_backend", cookies=cookies)
    body = resp.json()
    assert body["meta_dumps"] is None


def test_get_meta_dumps_lists_by_volume_name(cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    """Regression: dumps live under ``<volume>/meta/``, so the route must pass
    the JuiceFS volume name (not the often-null s3_prefix) to ``list_meta_dumps``."""
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket='b', s3_prefix=NULL, "
            "juicefs_volume_name='openhost', s3_access_key_id='AKIA', s3_secret_access_key='hunter2'"
        )
        db.commit()
    finally:
        db.close()
    spy = mock.MagicMock(return_value=archive_backend.MetaDumpSummary(count=0, latest_at=None, latest_key=None))
    with mock.patch.object(archive_backend, "list_meta_dumps", spy):
        client.get("/api/storage/archive_backend", cookies=cookies)
    # last positional arg is the object prefix JuiceFS actually writes under
    assert spy.call_args.args[-1] == "openhost"


# --- configure route ------------------------------------------------------


def test_configure_requires_creds(client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    resp = client.post(
        "/api/storage/archive_backend/configure",
        json={"s3_bucket": "b"},
        cookies=cookies,
    )
    assert resp.status_code == 400


def test_configure_rejects_invalid_s3_prefix(client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    """Malformed prefix (path traversal, whitespace, NUL, multi-segment,
    uppercase, underscore, too short, leading/trailing dash, dot) is
    rejected at the route layer because it's used directly as the
    JuiceFS volume name (regex ``^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$``)."""
    for bad in (
        "../etc",
        "with space",
        "embedded\x00null",
        "a/b",
        "UPPER",
        "under_score",
        "ab",
        "-leading",
        "trailing-",
        "with.dot",
    ):
        resp = client.post(
            "/api/storage/archive_backend/configure",
            json={
                "s3_bucket": "b",
                "s3_access_key_id": "a",
                "s3_secret_access_key": "s",
                "s3_prefix": bad,
            },
            cookies=cookies,
        )
        body = resp.json()
        assert resp.status_code == 400, (bad, body)
        assert "s3_prefix" in body["error"], (bad, body)


def test_configure_s3_requires_confirm_migrate_s3(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    """When already on S3, configuring a new bucket MIGRATES the data.  Without
    confirm_migrate_s3 it returns 409 explaining the migration."""
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket='oldbucket', "
            "s3_access_key_id='ak', s3_secret_access_key='sk' WHERE id=1"
        )
        db.commit()
    finally:
        db.close()
    resp = client.post(
        "/api/storage/archive_backend/configure",
        json={"s3_bucket": "newbucket", "s3_access_key_id": "a", "s3_secret_access_key": "s"},
        cookies=cookies,
    )
    assert resp.status_code == 409
    assert "confirm_migrate_s3" in resp.json()["error"]
    assert "oldbucket" in resp.json()["error"]


def test_configure_s3_to_s3_happy_path(cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    """With confirm_migrate_s3, the s3->s3 migration proceeds and the response
    reflects the new bucket."""
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket='oldbucket', "
            "s3_access_key_id='ak', s3_secret_access_key='sk', "
            "s3_prefix='oh-zone-1', juicefs_volume_name='oh-zone-1' WHERE id=1"
        )
        db.commit()
    finally:
        db.close()

    with mock.patch.object(archive_backend, "configure_backend") as mock_configure:

        def side_effect(_config: Any, db: sqlite3.Connection, **kwargs: Any) -> None:
            db.execute(
                "UPDATE archive_backend SET s3_bucket=?, s3_access_key_id=?, s3_secret_access_key=? WHERE id=1",
                (kwargs["s3_bucket"], kwargs["s3_access_key_id"], kwargs["s3_secret_access_key"]),
            )
            db.commit()

        mock_configure.side_effect = side_effect

        resp = client.post(
            "/api/storage/archive_backend/configure",
            json={
                "s3_bucket": "newbucket",
                "s3_access_key_id": "newak",
                "s3_secret_access_key": "newsk",
                "confirm_migrate_s3": True,
            },
            cookies=cookies,
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["backend"] == "s3"
    assert body["s3_bucket"] == "newbucket"
    assert "s3_secret_access_key" not in body
    # The new-bucket creds were forwarded to configure_backend as the DESTINATION.
    _, kwargs = mock_configure.call_args
    assert kwargs["s3_bucket"] == "newbucket"
    assert kwargs["s3_access_key_id"] == "newak"


def test_configure_happy_path(client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    """Format + mount + DB UPDATE; response carries the persisted state."""
    with mock.patch.object(archive_backend, "configure_backend") as mock_configure:
        # Side-effect: actually update the DB so read_state returns s3.
        def side_effect(_config: Any, db: sqlite3.Connection, **kwargs: Any) -> None:
            db.execute(
                "UPDATE archive_backend SET backend='s3', s3_bucket=?, "
                "s3_access_key_id=?, s3_secret_access_key=?, s3_prefix=? WHERE id=1",
                (
                    kwargs["s3_bucket"],
                    kwargs["s3_access_key_id"],
                    kwargs["s3_secret_access_key"],
                    kwargs.get("s3_prefix"),
                ),
            )
            db.commit()

        mock_configure.side_effect = side_effect

        resp = client.post(
            "/api/storage/archive_backend/configure",
            json={
                "s3_bucket": "mybucket",
                "s3_access_key_id": "AKIA",
                "s3_secret_access_key": "secret",
                "s3_prefix": "andrew-3",
            },
            cookies=cookies,
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["backend"] == "s3"
    assert body["s3_bucket"] == "mybucket"
    assert "s3_secret_access_key" not in body


# --- test_connection ------------------------------------------------------


def test_test_connection_requires_fields(client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    resp = client.post(
        "/api/storage/archive_backend/test_connection",
        json={"s3_bucket": "b"},
        cookies=cookies,
    )
    assert resp.status_code == 400


def test_test_connection_rejects_invalid_s3_prefix(client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    resp = client.post(
        "/api/storage/archive_backend/test_connection",
        json={
            "s3_bucket": "b",
            "s3_access_key_id": "a",
            "s3_secret_access_key": "s",
            "s3_prefix": "UPPER",
        },
        cookies=cookies,
    )
    assert resp.status_code == 400


def test_test_connection_surfaces_errors(client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    with mock.patch.object(archive_backend, "test_s3_credentials", return_value="bucket not found"):
        resp = client.post(
            "/api/storage/archive_backend/test_connection",
            json={"s3_bucket": "b", "s3_access_key_id": "a", "s3_secret_access_key": "s"},
            cookies=cookies,
        )
    assert resp.status_code == 400
    assert "bucket not found" in resp.json()["error"]


def test_test_connection_succeeds(client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    with mock.patch.object(archive_backend, "test_s3_credentials", return_value=None):
        resp = client.post(
            "/api/storage/archive_backend/test_connection",
            json={"s3_bucket": "b", "s3_access_key_id": "a", "s3_secret_access_key": "s"},
            cookies=cookies,
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# --- manifest predicates --------------------------------------------------


def test_manifest_requires_archive_only_matches_app_archive_true() -> None:
    """``manifest_requires_archive`` (install/reload gates) keys on
    ``app_archive = true`` only; access_all_archive is permissive so it
    doesn't block installs on archive-less zones."""
    assert archive_backend.manifest_requires_archive("[data]\napp_archive = true\n")
    assert not archive_backend.manifest_requires_archive("[data]\naccess_all_archive = true\n")
    assert not archive_backend.manifest_requires_archive("[data]\naccess_all_app_data = true\n")
    assert not archive_backend.manifest_requires_archive("[data]\napp_data = true\n")
    assert not archive_backend.manifest_requires_archive("")
    # Anchor on TOML key=value shape so substring matching can't false-match.
    assert not archive_backend.manifest_requires_archive("[data]\napp_archive = false\napp_data = true\n")
    assert archive_backend.manifest_requires_archive("[data]\napp_archive = true\naccess_all_archive = true\n")


def test_manifest_uses_archive_matches_either_flag() -> None:
    """``manifest_uses_archive`` is broader: app_archive and access_all_archive
    both qualify, since both result in the archive mount being granted to the container."""
    assert archive_backend.manifest_uses_archive("[data]\napp_archive = true\n")
    assert archive_backend.manifest_uses_archive("[data]\naccess_all_archive = true\n")
    assert not archive_backend.manifest_uses_archive("[data]\napp_data = true\n")
    assert not archive_backend.manifest_uses_archive("[data]\napp_archive = false\napp_data = true\n")
    # access_all_app_data alone does NOT imply archive access.
    assert not archive_backend.manifest_uses_archive("[data]\naccess_all_app_data = true\n")
    # access_all_data (backwards-compat alias) DOES imply archive access.
    assert archive_backend.manifest_uses_archive("[data]\naccess_all_data = true\n")


# --- install/reload gates (api/apps endpoints' archive backend checks) ----


def _archive_manifest(name: str, *, app_archive: bool, access_all_archive: bool = False) -> AppManifest:
    return AppManifest(
        name=name,
        version="1.0",
        description="probe",
        runtime_type="serverfull",
        container_image="Dockerfile",
        container_port=8080,
        container_command=None,
        memory_mb=128,
        cpu_cores=0.1,
        gpu=False,
        app_data=True,
        app_archive=app_archive,
        access_all_archive=access_all_archive,
        access_vm_data=False,
        app_temp_data=False,
        public_paths=["/"],
        capabilities=[],
        devices=[],
        consumes_services_v2=[],
        port_mappings=[],
        provides_services_v2=[],
        sqlite_dbs=[],
        health_check="/",
        hidden=False,
        authors=[],
        raw_toml="",
    )


@pytest.fixture
def apps_client(cfg: Any) -> Iterator[TestClient[Litestar]]:
    with TestClient(app=make_test_app(api_apps_routes)) as c:
        yield c


def test_add_app_allows_archive_app_on_default_local_backend(
    apps_client: TestClient[Litestar], cookies: dict[str, str], tmp_path: Path
) -> None:
    """An app with ``app_archive = true`` installs on a fresh zone: the
    default 'local' backend makes the archive tier always available (a
    live JuiceFS file-backed mount), so the archive gate must NOT block
    with the old "configure S3" 400.  With the mount healthy the request
    proceeds past the gate (any later failure is unrelated to the archive
    check)."""
    fake_clone_dir = str(tmp_path / "clone")
    os.makedirs(fake_clone_dir)
    with (
        mock.patch.object(apps_routes, "parse_manifest", return_value=_archive_manifest("probe", app_archive=True)),
        mock.patch.object(apps_routes, "validate_manifest", return_value=None),
        mock.patch.object(apps_routes.archive_backend, "is_archive_dir_healthy", return_value=True),
    ):
        resp = apps_client.post(
            "/api/add_app",
            json={
                "repo_url": "https://example.invalid/repo",
                "app_name": "probe",
                "clone_dir": fake_clone_dir,
            },
            cookies=cookies,
        )
    # The archive gate no longer produces a 400/"configure S3" error.
    if resp.status_code == 400:
        body = resp.json()
        assert "S3" not in body.get("error", "")
        assert "archive" not in body.get("error", "").lower()
    # And it is not blocked as a transient archive-unhealthy 503 either.
    assert resp.status_code != 503


def test_add_app_allows_access_all_archive_when_backend_disabled(
    apps_client: TestClient[Litestar], cookies: dict[str, str], tmp_path: Path
) -> None:
    """``access_all_archive = true`` (without ``app_archive``) does NOT need a
    configured archive backend — the app silently goes without the mount."""
    fake_clone_dir = str(tmp_path / "clone-aad")
    os.makedirs(fake_clone_dir)
    with (
        mock.patch.object(
            apps_routes,
            "parse_manifest",
            return_value=_archive_manifest("seer", app_archive=False, access_all_archive=True),
        ),
        mock.patch.object(apps_routes, "validate_manifest", return_value=None),
        mock.patch.object(apps_routes, "insert_and_deploy", return_value="seer"),
    ):
        resp = apps_client.post(
            "/api/add_app",
            json={
                "repo_url": "https://example.invalid/repo",
                "app_name": "seer",
                "clone_dir": fake_clone_dir,
            },
            cookies=cookies,
        )
    # Must NOT reject with archive-related 400/503.
    assert resp.status_code != 400 or "archive" not in resp.text.lower(), resp.text
    assert resp.status_code != 503 or "archive" not in resp.text.lower(), resp.text


def test_reload_app_refuses_when_archive_unhealthy(
    cfg: Any, apps_client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    """An archive-using app cannot be reloaded while the JuiceFS mount is
    unhealthy; without this guard, provision_data would write to the
    underlying empty mount-point and lose those writes once the mount
    came back."""
    archived_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket='b', s3_access_key_id='a', s3_secret_access_key='s'"
        )
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status, manifest_raw) "
            "VALUES (?, 'archived', '1.0', '/r/archived', 19601, 'running', "
            "'[data]\napp_archive = true\n')",
            (archived_id,),
        )
        db.commit()
    finally:
        db.close()
    resp = apps_client.post(f"/reload_app/{archived_id}", cookies=cookies)
    assert resp.status_code == 503


def test_reload_app_allows_access_all_archive_when_archive_unhealthy(
    cfg: Any, apps_client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    """access_all_archive apps can still reload while the archive is unhealthy
    — they just see the archive when it's available."""
    seer_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket='b', s3_access_key_id='a', s3_secret_access_key='s'"
        )
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status, manifest_raw) "
            "VALUES (?, 'seer', '1.0', '/r/seer', 19603, 'running', "
            "'[data]\naccess_all_archive = true\n')",
            (seer_id,),
        )
        db.commit()
    finally:
        db.close()
    with (
        mock.patch("compute_space.web.routes.api.apps.stop_app_process"),
        mock.patch("compute_space.web.routes.api.apps.reload_app_background"),
    ):
        resp = apps_client.post(f"/reload_app/{seer_id}", cookies=cookies)
    assert resp.status_code != 503 or "archive" not in resp.text.lower()


def test_configure_requires_confirm_when_local_has_data(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    """Upgrading local->S3 while apps have local archive data requires an
    explicit confirm_migrate_local flag; without it -> 409 listing the apps."""
    # Seed a file into the (mounted) local archive for an app.
    app_dir = os.path.join(archive_backend.juicefs_mount_dir(cfg), "nextcloud", "files")
    os.makedirs(app_dir, exist_ok=True)
    with open(os.path.join(app_dir, "x.txt"), "wb") as f:
        f.write(b"data")

    # Without confirm -> 409 (mount reported live so the data is visible).
    with mock.patch.object(archive_backend, "is_mounted", return_value=True):
        resp = client.post(
            "/api/storage/archive_backend/configure",
            json={"s3_bucket": "b", "s3_access_key_id": "a", "s3_secret_access_key": "s"},
            cookies=cookies,
        )
    assert resp.status_code == 409, resp.text
    assert "nextcloud" in resp.json()["error"]

    # With confirm -> proceeds (configure_backend is mocked to flip state).
    with (
        mock.patch.object(archive_backend, "is_mounted", return_value=True),
        mock.patch.object(archive_backend, "configure_backend") as mock_configure,
    ):

        def side_effect(_config: Any, db: sqlite3.Connection, **kwargs: Any) -> None:
            db.execute("UPDATE archive_backend SET backend='s3', s3_bucket=? WHERE id=1", (kwargs["s3_bucket"],))
            db.commit()

        mock_configure.side_effect = side_effect
        resp = client.post(
            "/api/storage/archive_backend/configure",
            json={
                "s3_bucket": "b",
                "s3_access_key_id": "a",
                "s3_secret_access_key": "s",
                "confirm_migrate_local": True,
            },
            cookies=cookies,
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["backend"] == "s3"


def test_get_surfaces_local_archive_apps(cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    """On backend='local', the GET state lists apps with local archive data
    so the dashboard can show whose data an S3 upgrade would migrate."""
    app_dir = os.path.join(archive_backend.juicefs_mount_dir(cfg), "nextcloud", "files")
    os.makedirs(app_dir, exist_ok=True)
    with open(os.path.join(app_dir, "f.txt"), "wb") as f:
        f.write(b"x")
    with mock.patch.object(archive_backend, "is_mounted", return_value=True):
        resp = client.get("/api/storage/archive_backend", cookies=cookies)
    assert resp.status_code == 200
    body = resp.json()
    assert body["backend"] == "local"
    assert body["local_archive_apps"] == ["nextcloud"]
    # The archive tier is always the JuiceFS mountpoint, both backends.
    assert body["archive_dir"] == archive_backend.juicefs_mount_dir(cfg)
