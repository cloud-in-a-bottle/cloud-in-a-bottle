from __future__ import annotations

import os
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
_LEGACY_CERT_DOMAIN_KEY = "legacy_cert_domain"


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


def tls_cert_path() -> Path:
    return data_dir() / "openhost-tls-cert.pem"


def tls_key_path() -> Path:
    return data_dir() / "openhost-tls-key.pem"


def primary_tls_paths() -> tuple[Path, Path] | None:
    """Resolve the current primary's stable certificate paths from the router database."""
    db_path = data_dir() / "router.db"
    if not db_path.exists():
        return tls_cert_path(), tls_key_path()
    try:
        with closing(sqlite3.connect(db_path)) as db:
            primary = db.execute("SELECT name, tls FROM domains WHERE is_primary = 1").fetchone()
            if primary is None:
                return tls_cert_path(), tls_key_path()
            name = str(primary[0]).split(":")[0].lower()
            if not bool(primary[1]):
                return None
            owner = db.execute("SELECT value FROM settings WHERE key = ?", (_LEGACY_CERT_DOMAIN_KEY,)).fetchone()
            legacy_owner = str(owner[0]).lower() if owner is not None else name
            if name == legacy_owner:
                return tls_cert_path(), tls_key_path()
            return data_dir() / "certs" / f"{name}.pem", data_dir() / "certs" / f"{name}.key"
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return tls_cert_path(), tls_key_path()
        logger.warning(f"updater could not resolve primary TLS paths: {exc}")
        return None
    except sqlite3.Error as exc:
        logger.warning(f"updater could not resolve primary TLS paths: {exc}")
        return None
