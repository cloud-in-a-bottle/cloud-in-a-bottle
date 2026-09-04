"""v12: move the provisioning-time TLS pair into the per-domain certificate layout.

The domains table is available by this point because v7 captured it for upgraded
instances.  Keep this migration self-contained and stdlib-only: system migrations
run before the updated router dependencies are installed.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

from openhost_system_agent.migrations.base import SystemMigration

_DEFAULT_DATA_DIR = "/home/host/.openhost/local_compute_space/persistent_data/openhost"
DATA_DIR_ENV = "OPENHOST_DATA_DIR"

_LEGACY_CERT_NAME = "openhost-tls-cert.pem"
_LEGACY_KEY_NAME = "openhost-tls-key.pem"
_PAIR_READY_MARKER = ".v12-uniform-certificate-pair-ready"
_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$")


def _certificate_owner(db_path: Path) -> str | None:
    """Return the normalized domain that owns the legacy pair without modifying the DB."""
    if not db_path.is_file():
        return None
    try:
        uri = db_path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=30)) as db:
            legacy_owner = db.execute("SELECT value FROM settings WHERE key = 'legacy_cert_domain'").fetchone()
            primary_rows = db.execute("SELECT name FROM domains WHERE is_primary = 1").fetchall()
    except (OSError, sqlite3.Error):
        return None
    if legacy_owner is not None:
        raw_name = legacy_owner[0]
    elif len(primary_rows) == 1:
        raw_name = primary_rows[0][0]
    else:
        return None
    name = str(raw_name).strip().split(":", 1)[0].lower()
    return name if _DOMAIN_RE.fullmatch(name) else None


def _regular_file(name: str, directory_fd: int, directory: Path) -> os.stat_result | None:
    try:
        file_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(file_stat.st_mode):
        raise RuntimeError(f"refusing to migrate non-regular TLS file: {directory / name}")
    return file_stat


def _copy_into_directory(
    source_name: str,
    source_directory_fd: int,
    destination_name: str,
    mode: int,
    destination_directory_fd: int,
) -> None:
    """Copy a regular source to an atomic destination without following host-controlled symlinks."""
    source_fd = os.open(source_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_directory_fd)
    temporary_name = f".{destination_name}.v12.{secrets.token_hex(8)}.tmp"
    temporary_fd: int | None = None
    try:
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise RuntimeError(f"refusing to migrate non-regular TLS file: {source_name}")
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=destination_directory_fd,
        )
        with os.fdopen(source_fd, "rb") as source_file, os.fdopen(temporary_fd, "wb") as temporary_file:
            source_fd = -1
            temporary_fd = None
            shutil.copyfileobj(source_file, temporary_file)
            temporary_file.flush()
            os.fchown(temporary_file.fileno(), source_stat.st_uid, source_stat.st_gid)
            os.fchmod(temporary_file.fileno(), mode)
            os.fsync(temporary_file.fileno())
        os.replace(
            temporary_name,
            destination_name,
            src_dir_fd=destination_directory_fd,
            dst_dir_fd=destination_directory_fd,
        )
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if temporary_fd is not None:
            os.close(temporary_fd)
        try:
            os.unlink(temporary_name, dir_fd=destination_directory_fd)
        except FileNotFoundError:
            pass


def _is_regular_at(name: str, directory_fd: int) -> bool:
    try:
        file_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(file_stat.st_mode)


def _write_pair_ready_marker(owner_domain: str, directory_fd: int) -> None:
    temporary_name = f".{_PAIR_READY_MARKER}.{secrets.token_hex(8)}.tmp"
    marker_fd: int | None = None
    try:
        marker_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        os.write(marker_fd, owner_domain.encode())
        os.fsync(marker_fd)
        os.close(marker_fd)
        marker_fd = None
        os.replace(temporary_name, _PAIR_READY_MARKER, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    finally:
        if marker_fd is not None:
            os.close(marker_fd)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _pair_ready_marker_matches(owner_domain: str, directory_fd: int) -> bool:
    try:
        marker_fd = os.open(_PAIR_READY_MARKER, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    except FileNotFoundError:
        return False
    try:
        marker_stat = os.fstat(marker_fd)
        if not stat.S_ISREG(marker_stat.st_mode):
            return False
        return os.read(marker_fd, 1024).decode(errors="replace") == owner_domain
    finally:
        os.close(marker_fd)


def migrate(db_path: str | None = None, data_dir: str | None = None) -> None:
    """Move any remaining legacy cert/key files to their owner's named paths.

    Install each destination atomically, but retain the complete legacy pair until
    both destinations exist.  An interruption therefore leaves one complete pair
    available to the updater and is repaired by the next run.
    """
    root = Path(data_dir or os.environ.get(DATA_DIR_ENV, _DEFAULT_DATA_DIR))
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    try:
        candidates = [
            (_LEGACY_CERT_NAME, ".pem", 0o644),
            (_LEGACY_KEY_NAME, ".key", 0o600),
        ]
        pending = [
            (source_name, suffix, mode, file_stat)
            for source_name, suffix, mode in candidates
            if (file_stat := _regular_file(source_name, root_fd, root)) is not None
        ]
        if not pending:
            return

        owner_domain = _certificate_owner(Path(db_path) if db_path is not None else root / "router.db")
        if owner_domain is None:
            raise RuntimeError("cannot migrate legacy TLS files without a valid certificate owner in router.db")

        owner = pending[0][3]
        if len(pending) != len(candidates):
            try:
                directory_fd = os.open("certs", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            except FileNotFoundError as exc:
                raise RuntimeError("cannot migrate an incomplete legacy TLS pair") from exc
            try:
                complete = all(_is_regular_at(f"{owner_domain}{suffix}", directory_fd) for suffix in (".pem", ".key"))
                if not complete or not _pair_ready_marker_matches(owner_domain, directory_fd):
                    raise RuntimeError("cannot migrate an incomplete legacy TLS pair")

                for source_name, _suffix, _mode, _source_stat in pending:
                    os.unlink(source_name, dir_fd=root_fd)
                os.fsync(root_fd)
                os.unlink(_PAIR_READY_MARKER, dir_fd=directory_fd)
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return

        try:
            os.mkdir("certs", mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        directory_fd = os.open("certs", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        try:
            directory_stat = os.fstat(directory_fd)
            if (directory_stat.st_uid, directory_stat.st_gid) != (owner.st_uid, owner.st_gid):
                os.fchown(directory_fd, owner.st_uid, owner.st_gid)
            os.fchmod(directory_fd, 0o700)

            for source_name, suffix, mode, _source_stat in pending:
                _copy_into_directory(source_name, root_fd, f"{owner_domain}{suffix}", mode, directory_fd)
            if not all(_is_regular_at(f"{owner_domain}{suffix}", directory_fd) for suffix in (".pem", ".key")):
                raise RuntimeError("cannot remove legacy TLS files until the named certificate pair is complete")
            _write_pair_ready_marker(owner_domain, directory_fd)
            os.fsync(directory_fd)
            os.fsync(root_fd)

            for source_name, _suffix, _mode, _source_stat in pending:
                os.unlink(source_name, dir_fd=root_fd)
            os.fsync(root_fd)
            os.unlink(_PAIR_READY_MARKER, dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.close(root_fd)


class Migration0012UniformCertificatePaths(SystemMigration):
    version = 12

    def up(self) -> None:
        migrate()
