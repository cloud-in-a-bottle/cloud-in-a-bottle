"""Tests for the auto-deploy-default-apps hook."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from compute_space.config import DefaultConfig
from compute_space.core import default_apps as da
from compute_space.core.app_id import new_app_id
from compute_space.core.manifest import parse_manifest
from compute_space.db.schema import schema_path


def _make_cfg(tmp_path: Path, *, apps_dir: Path, default_apps: list[str]) -> DefaultConfig:
    cfg = DefaultConfig(
        host="127.0.0.1",
        data_root_dir=str(tmp_path),
        apps_dir_override=str(apps_dir),
        zone_domain="testzone.local",
        tls_enabled=False,
        start_caddy=False,
        default_apps=default_apps,
    )
    cfg.make_all_dirs()
    return cfg


def _seed_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        with open(schema_path()) as f:
            conn.executescript(f.read())
        conn.commit()
    finally:
        conn.close()


def _make_app_dir(apps_dir: Path, dir_name: str, *, manifest_name: str) -> None:
    app_dir = apps_dir / dir_name
    app_dir.mkdir(parents=True)
    (app_dir / "openhost.toml").write_text(
        f'[app]\nname = "{manifest_name}"\nversion = "0.1"\n[runtime.container]\nimage = "Dockerfile"\nport = 8080\n'
    )
    (app_dir / "Dockerfile").write_text("FROM alpine\n")


@pytest.fixture
def cfg_with_apps(tmp_path: Path):
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    _make_app_dir(apps_dir, "secrets_v2", manifest_name="secrets-v2")
    _make_app_dir(apps_dir, "file_browser", manifest_name="file-browser")
    cfg = _make_cfg(tmp_path, apps_dir=apps_dir, default_apps=["secrets_v2", "file_browser"])
    _seed_db(cfg.db_path)
    return cfg


def _patch_insert_and_deploy(monkeypatch, *, fail_for: set[str] | None = None):
    fail_for = fail_for or set()

    def fake(manifest, repo_path, config, db, **kwargs):
        if manifest.name in fail_for:
            raise RuntimeError(f"simulated failure for {manifest.name}")
        next_port = db.execute("SELECT COALESCE(MAX(local_port), 18999) + 1 FROM apps").fetchone()[0]
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status) "
            "VALUES (?, ?, ?, ?, ?, 'building')",
            (new_app_id(), manifest.name, manifest.version or "0.1", repo_path, next_port),
        )
        db.commit()
        return manifest.name

    monkeypatch.setattr(da, "insert_and_deploy", fake)


def test_deploy_default_apps_installs_each(cfg_with_apps, monkeypatch):
    _patch_insert_and_deploy(monkeypatch)
    db = sqlite3.connect(cfg_with_apps.db_path)
    try:
        result = da.deploy_default_apps(cfg_with_apps, db)
    finally:
        db.close()

    assert sum(1 for o in result if o.status == "ok") == 2
    assert sum(1 for o in result if o.status == "failed") == 0
    with open(cfg_with_apps.default_apps_sentinel_path) as f:
        sentinel = json.load(f)
    assert all(entry["status"] == "ok" for entry in sentinel.values())


def test_redeploy_short_circuits_on_terminal_sentinel(cfg_with_apps, monkeypatch):
    _patch_insert_and_deploy(monkeypatch)
    db = sqlite3.connect(cfg_with_apps.db_path)
    try:
        da.deploy_default_apps(cfg_with_apps, db)

        def must_not_run(*args, **kwargs):
            raise AssertionError("re-walked apps_dir on terminal-sentinel app")

        monkeypatch.setattr(da, "_install_one", must_not_run)

        result = da.deploy_default_apps(cfg_with_apps, db)
    finally:
        db.close()

    assert all(o.status == "ok" for o in result)


def test_retries_until_max_attempts(cfg_with_apps, monkeypatch):
    _patch_insert_and_deploy(monkeypatch, fail_for={"secrets-v2", "file-browser"})

    for i in range(da.MAX_RETRY_ATTEMPTS):
        db = sqlite3.connect(cfg_with_apps.db_path)
        try:
            result = da.deploy_default_apps(cfg_with_apps, db)
        finally:
            db.close()
        for o in result:
            assert o.status == "failed"
            assert o.attempts == i + 1

    db = sqlite3.connect(cfg_with_apps.db_path)
    try:
        result_after = da.deploy_default_apps(cfg_with_apps, db)
    finally:
        db.close()
    for o in result_after:
        assert o.attempts == da.MAX_RETRY_ATTEMPTS


def test_malformed_sentinel_is_ignored(cfg_with_apps, monkeypatch):
    """Non-dict sentinel entries (or non-UTF-8 / non-JSON content) must
    not raise — they're treated as if the sentinel were absent."""
    _patch_insert_and_deploy(monkeypatch)
    os.makedirs(os.path.dirname(cfg_with_apps.default_apps_sentinel_path), exist_ok=True)
    with open(cfg_with_apps.default_apps_sentinel_path, "w") as f:
        json.dump({"secrets_v2": "not-a-dict", "file_browser": {"status": "failed"}}, f)

    db = sqlite3.connect(cfg_with_apps.db_path)
    try:
        result = da.deploy_default_apps(cfg_with_apps, db)
    finally:
        db.close()
    by_name = {o.name: o.status for o in result}
    assert by_name["secrets_v2"] == "ok"


def test_empty_default_apps_is_no_op(tmp_path: Path):
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    cfg = _make_cfg(tmp_path, apps_dir=apps_dir, default_apps=[])
    _seed_db(cfg.db_path)

    db = sqlite3.connect(cfg.db_path)
    try:
        result = da.deploy_default_apps(cfg, db)
    finally:
        db.close()

    assert result == []
    assert not os.path.isfile(cfg.default_apps_sentinel_path)


# --- Remote URL support ---


def test_is_remote_url_classification():
    assert da._is_remote_url("https://github.com/foo/bar") is True
    assert da._is_remote_url("http://example.com/repo") is True
    assert da._is_remote_url("file:///tmp/foo") is True
    assert da._is_remote_url("git+ssh://git@host/repo.git") is True
    # Bare dirnames are NOT remote URLs.
    assert da._is_remote_url("secrets_v2") is False
    assert da._is_remote_url("file_browser") is False
    assert da._is_remote_url("openhost-catalog") is False


def test_remote_url_entry_dispatches_to_clone_path(cfg_with_apps, monkeypatch):
    """A URL-form default_apps entry routes through clone_and_read_manifest,
    not the local copytree path.  We patch the clone function to keep the
    test hermetic."""
    _patch_insert_and_deploy(monkeypatch)

    # Make a fake "cloned" tree at a fresh tempdir so move_clone_to_app_temp_dir
    # has something to operate on.
    src_app_dir = Path(cfg_with_apps.apps_dir) / "secrets_v2"
    fake_clone_parent = tempfile.mkdtemp(prefix="openhost-clone-")
    fake_clone_dir = os.path.join(fake_clone_parent, "repo")
    shutil.copytree(src_app_dir, fake_clone_dir)
    fake_manifest = parse_manifest(fake_clone_dir)

    calls: list[str] = []

    async def fake_clone(repo_url, github_token=None):  # noqa: ANN001
        calls.append(repo_url)
        return fake_manifest, fake_clone_dir, None

    monkeypatch.setattr(da, "clone_and_read_manifest", fake_clone)

    # Replace the config's default_apps with a single remote URL entry.
    cfg = cfg_with_apps.evolve(default_apps=["https://github.com/imbue-openhost/openhost-catalog"])

    db = sqlite3.connect(cfg.db_path)
    try:
        result = da.deploy_default_apps(cfg, db)
    finally:
        db.close()

    assert calls == ["https://github.com/imbue-openhost/openhost-catalog"]
    assert len(result) == 1
    assert result[0].status == "ok"
    assert result[0].name == "https://github.com/imbue-openhost/openhost-catalog"

    # The sentinel keys on the spec string (the URL), not the manifest name.
    with open(cfg.default_apps_sentinel_path) as f:
        sentinel = json.load(f)
    assert "https://github.com/imbue-openhost/openhost-catalog" in sentinel


def test_email_apps_deployed_only_when_email_enabled(cfg_with_apps, monkeypatch):
    """The mailbox + webmail apps are appended to the deployed set only when
    email is enabled; with email off they are not attempted."""
    _patch_insert_and_deploy(monkeypatch)

    src_app_dir = Path(cfg_with_apps.apps_dir) / "secrets_v2"

    cloned: list[str] = []

    async def fake_clone(repo_url, github_token=None):  # noqa: ANN001
        cloned.append(repo_url)
        # Each URL needs its own fake clone tree with a unique manifest name so
        # they don't collide as "already installed".
        parent = tempfile.mkdtemp(prefix="openhost-clone-")
        clone_dir = os.path.join(parent, "repo")
        shutil.copytree(src_app_dir, clone_dir)
        name = repo_url.rstrip("/").rsplit("/", 1)[-1]
        (Path(clone_dir) / "openhost.toml").write_text(
            f'[app]\nname = "{name}"\nversion = "0.1"\n[runtime.container]\nimage = "Dockerfile"\nport = 8080\n'
        )
        return parse_manifest(clone_dir), clone_dir, None

    monkeypatch.setattr(da, "clone_and_read_manifest", fake_clone)

    email_kwargs = dict(
        email_enabled=True,
        email_proxy_base_url="https://frontend.example.com",
        email_keycloak_issuer_url="https://kc.example.com/realms/openhost-customers",
        email_keycloak_client_id="instance-x",
        email_keycloak_client_secret="s3cr3t",
        email_inbound_mx_host="inbound-smtp.us-west-2.amazonaws.com",
        public_ip="203.0.113.10",  # required for the default direct inbound mode
    )

    # Email OFF: only the base default app is attempted.
    cfg_off = cfg_with_apps.evolve(default_apps=["https://github.com/imbue-openhost/openhost-catalog"])
    db = sqlite3.connect(cfg_off.db_path)
    try:
        da.deploy_default_apps(cfg_off, db)
    finally:
        db.close()
    assert cloned == ["https://github.com/imbue-openhost/openhost-catalog"]

    # Email ON (fresh config + db): the two email apps are appended and attempted.
    cloned.clear()
    cfg_on = cfg_with_apps.evolve(
        default_apps=["https://github.com/imbue-openhost/openhost-catalog"],
        **email_kwargs,
    )
    db = sqlite3.connect(cfg_on.db_path)
    try:
        result = da.deploy_default_apps(cfg_on, db)
    finally:
        db.close()
    assert "https://github.com/imbue-openhost/openhost-stalwart-email-server" in cloned
    assert "https://github.com/imbue-openhost/openhost-bulwark-email-client" in cloned
    assert {o.name for o in result if o.status == "ok"} >= {
        "https://github.com/imbue-openhost/openhost-stalwart-email-server",
        "https://github.com/imbue-openhost/openhost-bulwark-email-client",
    }


def test_remote_url_clone_failure_is_retried(cfg_with_apps, monkeypatch):
    """Clone failures get the same retry-budget treatment as local failures."""
    _patch_insert_and_deploy(monkeypatch)

    async def always_fail(repo_url, github_token=None):  # noqa: ANN001
        return None, None, "fake network error"

    monkeypatch.setattr(da, "clone_and_read_manifest", always_fail)

    cfg = cfg_with_apps.evolve(default_apps=["https://github.com/foo/bar"])

    for i in range(da.MAX_RETRY_ATTEMPTS):
        db = sqlite3.connect(cfg.db_path)
        try:
            result = da.deploy_default_apps(cfg, db)
        finally:
            db.close()
        assert len(result) == 1
        assert result[0].status == "failed"
        assert result[0].attempts == i + 1
        assert "fake network error" in (result[0].error or "")


def test_remote_install_works_from_running_event_loop(cfg_with_apps, monkeypatch):
    """deploy_default_apps is called from /setup (an async Quart handler).
    Plain asyncio.run() would raise; the in-module thread wrapper must
    isolate the clone's event loop from the caller's."""
    _patch_insert_and_deploy(monkeypatch)

    src_app_dir = Path(cfg_with_apps.apps_dir) / "secrets_v2"
    fake_clone_parent = tempfile.mkdtemp(prefix="openhost-clone-")
    fake_clone_dir = os.path.join(fake_clone_parent, "repo")
    shutil.copytree(src_app_dir, fake_clone_dir)
    fake_manifest = parse_manifest(fake_clone_dir)

    async def fake_clone(repo_url, github_token=None):  # noqa: ANN001
        return fake_manifest, fake_clone_dir, None

    monkeypatch.setattr(da, "clone_and_read_manifest", fake_clone)

    cfg = cfg_with_apps.evolve(default_apps=["https://github.com/foo/bar"])

    async def runner():
        db = sqlite3.connect(cfg.db_path)
        try:
            return da.deploy_default_apps(cfg, db)
        finally:
            db.close()

    result = asyncio.run(runner())
    assert len(result) == 1
    assert result[0].status == "ok", result[0].error


def test_backup_in_default_factory():
    """openhost-backup must be in the shipped DefaultConfig.default_apps
    so every new instance auto-installs it at /setup completion.

    Regression guard against accidental removal during config refactors.
    """
    cfg = DefaultConfig(
        host="127.0.0.1",
        data_root_dir="/tmp/fake",
        zone_domain="test.local",
        tls_enabled=False,
        start_caddy=False,
    )
    backup_entries = [s for s in cfg.default_apps if "openhost-backup" in s.lower()]
    assert backup_entries, f"openhost-backup not in default_apps: {cfg.default_apps}"


def test_catalog_in_default_factory():
    """openhost-catalog must be in the shipped DefaultConfig.default_apps
    so every new instance auto-installs it at /setup completion.

    Regression guard against accidental removal during config refactors.
    """
    cfg = DefaultConfig(
        host="127.0.0.1",
        data_root_dir="/tmp/fake",
        zone_domain="test.local",
        tls_enabled=False,
        start_caddy=False,
    )
    catalog_entries = [s for s in cfg.default_apps if "openhost-catalog" in s.lower()]
    assert catalog_entries, f"openhost-catalog not in default_apps: {cfg.default_apps}"


def test_filestash_in_default_factory():
    """openhost-filestash must be in the shipped DefaultConfig.default_apps."""
    cfg = DefaultConfig(
        host="127.0.0.1",
        data_root_dir="/tmp/fake",
        zone_domain="test.local",
        tls_enabled=False,
        start_caddy=False,
    )
    filestash_entries = [s for s in cfg.default_apps if "openhost-filestash" in s.lower()]
    assert filestash_entries, f"openhost-filestash not in default_apps: {cfg.default_apps}"


def test_oauth_provider_in_default_factory():
    """The vendored oauth_provider app must be in the shipped
    DefaultConfig.default_apps so every new instance auto-installs the OAuth
    cross-app service provider at /setup completion.

    Regression guard against accidental removal during config refactors.
    """
    cfg = DefaultConfig(
        host="127.0.0.1",
        data_root_dir="/tmp/fake",
        zone_domain="test.local",
        tls_enabled=False,
        start_caddy=False,
    )
    assert "oauth_provider" in cfg.default_apps, f"oauth_provider not in default_apps: {cfg.default_apps}"


def test_mixed_local_and_remote_entries(cfg_with_apps, monkeypatch):
    """A single deploy can contain both vendored and remote entries."""
    _patch_insert_and_deploy(monkeypatch)

    src_app_dir = Path(cfg_with_apps.apps_dir) / "file_browser"
    fake_clone_parent = tempfile.mkdtemp(prefix="openhost-clone-")
    fake_clone_dir = os.path.join(fake_clone_parent, "repo")
    shutil.copytree(src_app_dir, fake_clone_dir)
    # Rename the manifest so it doesn't collide with the vendored file_browser
    (Path(fake_clone_dir) / "openhost.toml").write_text(
        '[app]\nname = "from-remote"\nversion = "0.1"\n[runtime.container]\nimage = "Dockerfile"\nport = 8080\n'
    )
    fake_manifest = parse_manifest(fake_clone_dir)

    async def fake_clone(repo_url, github_token=None):  # noqa: ANN001
        return fake_manifest, fake_clone_dir, None

    monkeypatch.setattr(da, "clone_and_read_manifest", fake_clone)

    cfg = cfg_with_apps.evolve(
        default_apps=["secrets_v2", "https://github.com/example/from-remote"],
    )

    db = sqlite3.connect(cfg.db_path)
    try:
        result = da.deploy_default_apps(cfg, db)
    finally:
        db.close()

    by_name = {o.name: o.status for o in result}
    assert by_name == {
        "secrets_v2": "ok",
        "https://github.com/example/from-remote": "ok",
    }


def test_remote_clone_timeout_returns_failed(cfg_with_apps, monkeypatch):
    """A hung clone must not block /setup indefinitely; the worker thread
    times out and the entry is recorded as failed (and retried on next boot)."""
    _patch_insert_and_deploy(monkeypatch)

    async def hang_forever(repo_url, github_token=None):  # noqa: ANN001
        await asyncio.sleep(60)  # Far longer than the patched timeout.
        return None, None, None  # never reached

    monkeypatch.setattr(da, "clone_and_read_manifest", hang_forever)
    # Shrink the timeout so the test doesn't take 3 minutes.
    monkeypatch.setattr(da, "REMOTE_CLONE_TIMEOUT_SECONDS", 0.2)

    cfg = cfg_with_apps.evolve(default_apps=["https://github.com/foo/bar"])

    db = sqlite3.connect(cfg.db_path)
    try:
        result = da.deploy_default_apps(cfg, db)
    finally:
        db.close()

    assert len(result) == 1
    assert result[0].status == "failed"
    assert "timeout" in (result[0].error or "").lower()
