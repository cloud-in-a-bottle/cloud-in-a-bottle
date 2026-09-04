from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from loguru import logger

# Matches compute_space's Config.openhost_data_path in production. The env var
# points both sides at the same directory when the data dir is non-default:
# compute_space sets it for itself at boot and forwards it on every agent call
# (`sudo env`) and into the detached updater (`systemd-run --setenv`).
_DEFAULT_DATA_DIR = "/home/host/.openhost/local_compute_space/persistent_data/openhost"
DATA_DIR_ENV = "OPENHOST_DATA_DIR"
_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$")


def data_dir() -> Path:
    return Path(os.environ.get(DATA_DIR_ENV, _DEFAULT_DATA_DIR))


def updater_dir() -> Path:
    return data_dir() / "updater"


def progress_log_path() -> Path:
    return updater_dir() / "progress.jsonl"


def token_path() -> Path:
    return updater_dir() / "token"


def write_token(token: str) -> None:
    d = updater_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = token_path()
    path.write_text(token)
    path.chmod(0o600)


def clear_token() -> None:
    token_path().unlink(missing_ok=True)


def ready_marker_path() -> Path:
    return updater_dir() / "serve.ready"


def primary_tls_paths() -> tuple[Path, Path] | None:
    """Resolve a TLS primary's named certificate paths without modifying its database."""
    db_path = data_dir() / "router.db"
    if not db_path.is_file():
        return None
    try:
        uri = db_path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as db:
            primary = db.execute("SELECT name, tls FROM domains WHERE is_primary = 1").fetchone()
            if primary is None:
                return None
            if not bool(primary[1]):
                return None
            name = str(primary[0]).strip().split(":", 1)[0].lower()
            if not _DOMAIN_RE.fullmatch(name):
                return None
            legacy = data_dir() / "openhost-tls-cert.pem", data_dir() / "openhost-tls-key.pem"
            try:
                owner_row = db.execute("SELECT value FROM settings WHERE key = 'legacy_cert_domain'").fetchone()
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower():
                    raise
                owner_row = None
            legacy_owner = str(owner_row[0]).strip().split(":", 1)[0].lower() if owner_row is not None else name
            if legacy_owner == name and all(path.is_file() for path in legacy):
                return legacy
            named = data_dir() / "certs" / f"{name}.pem", data_dir() / "certs" / f"{name}.key"
            return named
    except (OSError, sqlite3.Error) as exc:
        logger.warning(f"updater could not resolve primary TLS paths: {exc}")
        return None
