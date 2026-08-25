from __future__ import annotations

import os
from pathlib import Path

# Matches compute_space's Config.openhost_data_path in production. The env var
# points both sides at the same directory when the data dir is non-default:
# compute_space sets it for itself at boot and forwards it on every agent call
# (`sudo env`) and into the detached updater (`systemd-run --setenv`).
_DEFAULT_DATA_DIR = "/home/host/.openhost/local_compute_space/persistent_data/openhost"
# OpenHost -> Cloud in a Bottle rename: the data-dir override is read under the
# new BOTTLE_ name (preferred) and the legacy OPENHOST_ name (kept for compat).
# Order matters — BOTTLE_ wins. Writers set BOTH names so a reader on either side
# of a version-skewed self-update still finds it.
DATA_DIR_ENV = "OPENHOST_DATA_DIR"
DATA_DIR_ENV_NEW = "BOTTLE_DATA_DIR"
DATA_DIR_ENV_NAMES = (DATA_DIR_ENV_NEW, DATA_DIR_ENV)


def data_dir_env_value() -> str | None:
    """The configured data-dir override from the environment, or None if unset.

    Prefers ``BOTTLE_DATA_DIR`` over the legacy ``OPENHOST_DATA_DIR``.
    """
    for name in DATA_DIR_ENV_NAMES:
        value = os.environ.get(name)
        if value:
            return value
    return None


def data_dir_setenv_pairs(value: str) -> list[str]:
    """``NAME=value`` assignments for every data-dir env name, for ``env`` /
    ``systemd-run --setenv`` so both the new and legacy names are exported."""
    return [f"{name}={value}" for name in DATA_DIR_ENV_NAMES]


def data_dir() -> Path:
    value = data_dir_env_value()
    return Path(value) if value else Path(_DEFAULT_DATA_DIR)


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
