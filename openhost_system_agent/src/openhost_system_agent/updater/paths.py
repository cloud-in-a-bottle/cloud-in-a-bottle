from __future__ import annotations

import os
from pathlib import Path

# Matches compute_space's Config.openhost_data_path in production. The env var
# lets tests and non-standard installs point both sides at the same directory.
_DEFAULT_DATA_DIR = "/home/host/.openhost/local_compute_space/persistent_data/openhost"
_DATA_DIR_ENV = "OPENHOST_DATA_DIR"


def data_dir() -> Path:
    return Path(os.environ.get(_DATA_DIR_ENV, _DEFAULT_DATA_DIR))


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
