from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from openhost_system_agent.migrations.versions import v0012_uniform_certificate_paths as migration
from openhost_system_agent.migrations.versions.v0012_uniform_certificate_paths import (
    Migration0012UniformCertificatePaths,
)


def _domain_db(path: Path, primary: str | None = "Host.Example.com:8443") -> None:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE domains (name TEXT PRIMARY KEY, is_primary INTEGER NOT NULL)")
        db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        if primary is not None:
            db.execute("INSERT INTO domains VALUES (?, 1)", (primary,))


def _legacy_pair(data_dir: Path) -> tuple[Path, Path]:
    cert = data_dir / "openhost-tls-cert.pem"
    key = data_dir / "openhost-tls-key.pem"
    cert.write_text("legacy cert")
    key.write_text("legacy key")
    cert.chmod(0o666)
    key.chmod(0o644)
    return cert, key


def test_moves_pair_to_normalized_primary_paths_with_safe_metadata(tmp_path: Path) -> None:
    db_path = tmp_path / "router.db"
    _domain_db(db_path)
    cert, key = _legacy_pair(tmp_path)
    cert_owner = cert.stat().st_uid, cert.stat().st_gid
    key_owner = key.stat().st_uid, key.stat().st_gid

    migration.migrate(str(db_path), str(tmp_path))

    named_cert = tmp_path / "certs" / "host.example.com.pem"
    named_key = tmp_path / "certs" / "host.example.com.key"
    assert not cert.exists() and not key.exists()
    assert named_cert.read_text() == "legacy cert"
    assert named_key.read_text() == "legacy key"
    assert (named_cert.stat().st_uid, named_cert.stat().st_gid) == cert_owner
    assert (named_key.stat().st_uid, named_key.stat().st_gid) == key_owner
    assert stat.S_IMODE(named_cert.stat().st_mode) == 0o644
    assert stat.S_IMODE(named_key.stat().st_mode) == 0o600
    assert (named_cert.parent.stat().st_uid, named_cert.parent.stat().st_gid) == cert_owner
    assert stat.S_IMODE(named_cert.parent.stat().st_mode) == 0o700


def test_retry_completes_an_interrupted_pair_move(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "router.db"
    _domain_db(db_path, "primary.example.com")
    cert, key = _legacy_pair(tmp_path)
    real_replace = os.replace
    calls = 0

    def fail_second_replace(source: str, destination: str, **kwargs: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated interruption")
        real_replace(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", fail_second_replace)
    with pytest.raises(OSError, match="simulated interruption"):
        migration.migrate(str(db_path), str(tmp_path))

    named_cert = tmp_path / "certs" / "primary.example.com.pem"
    named_key = tmp_path / "certs" / "primary.example.com.key"
    assert named_cert.exists() and cert.exists()
    assert key.exists() and not named_key.exists()

    monkeypatch.setattr(os, "replace", real_replace)
    migration.migrate(str(db_path), str(tmp_path))

    assert named_cert.read_text() == "legacy cert"
    assert named_key.read_text() == "legacy key"
    assert not cert.exists() and not key.exists()


def test_existing_destination_is_replaced_by_legacy_source(tmp_path: Path) -> None:
    db_path = tmp_path / "router.db"
    _domain_db(db_path, "primary.example.com")
    cert, key = _legacy_pair(tmp_path)
    certs_dir = tmp_path / "certs"
    certs_dir.mkdir()
    (certs_dir / "primary.example.com.pem").write_text("partial cert")
    (certs_dir / "primary.example.com.key").write_text("partial key")

    migration.migrate(str(db_path), str(tmp_path))

    assert (certs_dir / "primary.example.com.pem").read_text() == "legacy cert"
    assert (certs_dir / "primary.example.com.key").read_text() == "legacy key"
    assert not cert.exists() and not key.exists()


def test_lone_legacy_file_is_retained_without_a_complete_named_pair(tmp_path: Path) -> None:
    db_path = tmp_path / "router.db"
    _domain_db(db_path, "primary.example.com")
    cert = tmp_path / "openhost-tls-cert.pem"
    cert.write_text("legacy cert")

    with pytest.raises(RuntimeError, match="incomplete legacy TLS pair"):
        migration.migrate(str(db_path), str(tmp_path))

    assert cert.read_text() == "legacy cert"
    assert not (tmp_path / "certs").exists()


def test_retry_finishes_marker_backed_interrupted_legacy_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "router.db"
    _domain_db(db_path, "primary.example.com")
    cert, key = _legacy_pair(tmp_path)
    real_unlink = os.unlink

    def fail_key_cleanup(path: str, *, dir_fd: int | None = None) -> None:
        if path == migration._LEGACY_KEY_NAME:
            raise OSError("simulated cleanup interruption")
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", fail_key_cleanup)
    with pytest.raises(OSError, match="simulated cleanup interruption"):
        migration.migrate(str(db_path), str(tmp_path))

    certs_dir = tmp_path / "certs"
    assert not cert.exists() and key.exists()
    assert (certs_dir / "primary.example.com.pem").read_text() == "legacy cert"
    assert (certs_dir / "primary.example.com.key").read_text() == "legacy key"
    assert (certs_dir / migration._PAIR_READY_MARKER).exists()

    monkeypatch.setattr(os, "unlink", real_unlink)
    migration.migrate(str(db_path), str(tmp_path))

    assert not cert.exists() and not key.exists()
    assert not (certs_dir / migration._PAIR_READY_MARKER).exists()


def test_fresh_install_without_legacy_files_is_noop(tmp_path: Path) -> None:
    db_path = tmp_path / "router.db"
    _domain_db(db_path, "primary.example.com")

    migration.migrate(str(db_path), str(tmp_path))

    assert not (tmp_path / "certs").exists()


def test_uses_forwarded_openhost_data_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _domain_db(tmp_path / "router.db", "primary.example.com")
    cert, key = _legacy_pair(tmp_path)
    monkeypatch.setenv(migration.DATA_DIR_ENV, str(tmp_path))

    migration.migrate()

    assert not cert.exists() and not key.exists()
    assert (tmp_path / "certs" / "primary.example.com.pem").read_text() == "legacy cert"
    assert (tmp_path / "certs" / "primary.example.com.key").read_text() == "legacy key"


@pytest.mark.parametrize("database", ["missing", "corrupt", "missing_table", "missing_primary"])
def test_unavailable_primary_fails_without_touching_legacy_files(tmp_path: Path, database: str) -> None:
    db_path = tmp_path / "router.db"
    if database == "corrupt":
        db_path.write_text("not sqlite")
    elif database == "missing_table":
        with sqlite3.connect(db_path) as db:
            db.execute("CREATE TABLE unrelated (value TEXT)")
    elif database == "missing_primary":
        _domain_db(db_path, None)
    cert, key = _legacy_pair(tmp_path)
    before = [(path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in (cert, key)]

    with pytest.raises(RuntimeError, match="valid certificate owner"):
        migration.migrate(str(db_path), str(tmp_path))

    assert [(path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in (cert, key)] == before
    assert not (tmp_path / "certs").exists()
    if database == "missing":
        assert not db_path.exists()


def test_invalid_primary_fails_without_escaping_certificate_directory(tmp_path: Path) -> None:
    db_path = tmp_path / "router.db"
    _domain_db(db_path, "../../escape.example.com")
    cert, key = _legacy_pair(tmp_path)

    with pytest.raises(RuntimeError, match="valid certificate owner"):
        migration.migrate(str(db_path), str(tmp_path))

    assert cert.exists() and key.exists()
    assert not (tmp_path / "certs").exists()


def test_ambiguous_primary_fails_without_touching_legacy_files(tmp_path: Path) -> None:
    db_path = tmp_path / "router.db"
    _domain_db(db_path, "one.example.com")
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO domains VALUES ('two.example.com', 1)")
    cert, key = _legacy_pair(tmp_path)

    with pytest.raises(RuntimeError, match="valid certificate owner"):
        migration.migrate(str(db_path), str(tmp_path))

    assert cert.exists() and key.exists()
    assert not (tmp_path / "certs").exists()


def test_rejects_symlinked_legacy_source(tmp_path: Path) -> None:
    db_path = tmp_path / "router.db"
    _domain_db(db_path, "primary.example.com")
    secret = tmp_path / "unrelated"
    secret.write_text("do not copy")
    (tmp_path / "openhost-tls-cert.pem").symlink_to(secret)
    (tmp_path / "openhost-tls-key.pem").write_text("legacy key")

    with pytest.raises(RuntimeError, match="non-regular TLS file"):
        migration.migrate(str(db_path), str(tmp_path))

    assert secret.read_text() == "do not copy"
    assert not (tmp_path / "certs").exists()


def test_rejects_symlinked_certificate_directory(tmp_path: Path) -> None:
    db_path = tmp_path / "router.db"
    _domain_db(db_path, "primary.example.com")
    cert, key = _legacy_pair(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "certs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        migration.migrate(str(db_path), str(tmp_path))

    assert cert.exists() and key.exists()
    assert list(outside.iterdir()) == []


def test_destination_symlink_is_replaced_without_touching_its_target(tmp_path: Path) -> None:
    db_path = tmp_path / "router.db"
    _domain_db(db_path, "primary.example.com")
    _legacy_pair(tmp_path)
    certs_dir = tmp_path / "certs"
    certs_dir.mkdir()
    outside = tmp_path / "unrelated"
    outside.write_text("do not overwrite")
    (certs_dir / "primary.example.com.pem").symlink_to(outside)

    migration.migrate(str(db_path), str(tmp_path))

    assert outside.read_text() == "do not overwrite"
    assert not (certs_dir / "primary.example.com.pem").is_symlink()
    assert (certs_dir / "primary.example.com.pem").read_text() == "legacy cert"


def test_pre_migration_promotion_moves_legacy_pair_to_its_persisted_owner(tmp_path: Path) -> None:
    db_path = tmp_path / "router.db"
    _domain_db(db_path, "new.example.com")
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO settings VALUES ('legacy_cert_domain', 'Old.Example.com')")
    cert, key = _legacy_pair(tmp_path)
    certs_dir = tmp_path / "certs"
    certs_dir.mkdir()
    (certs_dir / "new.example.com.pem").write_text("new cert")
    (certs_dir / "new.example.com.key").write_text("new key")

    migration.migrate(str(db_path), str(tmp_path))

    assert not cert.exists() and not key.exists()
    assert (certs_dir / "old.example.com.pem").read_text() == "legacy cert"
    assert (certs_dir / "old.example.com.key").read_text() == "legacy key"
    assert (certs_dir / "new.example.com.pem").read_text() == "new cert"
    assert (certs_dir / "new.example.com.key").read_text() == "new key"


def test_migration_version_is_twelve() -> None:
    assert Migration0012UniformCertificatePaths.version == 12
