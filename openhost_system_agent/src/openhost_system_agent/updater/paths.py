# On-disk paths shared between compute_space (which mints the update token) and
# the detached updater process (which serves the progress page during downtime).
# Centralized here so both sides agree; compute_space points this at its own data
# dir via OPENHOST_DATA_DIR, the root-run agent falls back to the host path.

from __future__ import annotations

import os
from pathlib import Path

# The OpenHost data dir on a provisioned host. compute_space's
# Config.openhost_data_path resolves to this same location in production
# (see compute_space/config.py). The env var lets tests and the (rare)
# non-standard install point both sides at the same directory.
_DEFAULT_DATA_DIR = "/home/host/.openhost/local_compute_space/persistent_data/openhost"
_DATA_DIR_ENV = "OPENHOST_DATA_DIR"


def data_dir() -> Path:
    return Path(os.environ.get(_DATA_DIR_ENV, _DEFAULT_DATA_DIR))


def updater_dir() -> Path:
    """Directory holding the updater's runtime state (token, progress log)."""
    return data_dir() / "updater"


def progress_log_path() -> Path:
    """Append-only JSONL log the updater tails and streams to the browser.

    Written by the apply walk (as root) and read by the updater server. Each
    line is a JSON object; see ``progress.py`` for the schema.
    """
    return updater_dir() / "progress.jsonl"


def token_path() -> Path:
    """File holding the single-use update token minted by compute_space.

    The updater compares the token in an incoming request against this file's
    contents to decide whether to show live logs (owner who clicked) or the
    generic loading page (everyone else). Written 0600 by compute_space; read
    by the root updater.
    """
    return updater_dir() / "token"


def write_token(token: str) -> None:
    """Persist the update token 0600 into the updater dir (run as root).

    Written by the agent (via ``updater set-token``) so it lands in the
    root-managed updater dir regardless of who owns it, and is readable by the
    root updater. Kept 0600 so it isn't world-readable.
    """
    d = updater_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = token_path()
    path.write_text(token)
    path.chmod(0o600)


def clear_token() -> None:
    """Remove the update token if present (run as root). Idempotent."""
    token_path().unlink(missing_ok=True)


def ready_marker_path() -> Path:
    """Marker the updater touches once it is spinning in its bind-retry loop.

    The launcher waits for this before returning (and before the restart fires),
    so the restart's downtime window opens with the updater already poised to
    grab 80/443 — not still importing Python.
    """
    return updater_dir() / "serve.ready"


def tls_cert_path() -> Path:
    """Primary domain's TLS cert on disk (matches Config.tls_cert_path)."""
    return data_dir() / "openhost-tls-cert.pem"


def tls_key_path() -> Path:
    """Primary domain's TLS key on disk (matches Config.tls_key_path)."""
    return data_dir() / "openhost-tls-key.pem"
