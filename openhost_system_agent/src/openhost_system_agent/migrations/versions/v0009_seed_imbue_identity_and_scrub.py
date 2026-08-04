"""v9: migrate an OLD instance's per-instance Keycloak credential from ``config.toml`` into the
router DB ``settings`` table, then scrub the now-captured ``cert_api_keycloak_*`` lines.

Before the shared-identity work, an instance's per-instance credential lived in ``config.toml`` as
``cert_api_keycloak_issuer_url`` / ``cert_api_keycloak_client_id`` / ``cert_api_keycloak_client_secret``
(cert-api's auth). The new model stores one shared credential in the DB ``settings`` table
(``imbue_identity_*`` keys), which every Imbue service (cert-api, email, ...) reads. New instances get
it seeded from ``first_boot.toml``; this migration moves an upgraded instance's existing credential
into the same store so the DB is the single source of truth — after which the read-time config
fallback in the router can eventually be removed.

This is the **old-instance (upgrade)** path, mirroring v7 (domains + claim token). Fresh installs never
carry ``cert_api_keycloak_*`` (they seed ``imbue_identity_*`` via first_boot), so this is a no-op
capture for them and just scrubs on a later update. It runs as root during ``openhost update``, before
the router restarts, so it does the capture itself (a scrub-only migration would strip the credential
before the router's runtime resolver could fall back to it).

stdlib only: agent migrations run before ``pixi install``, so this can't import the router's code.
The ``settings`` schema is a frozen snapshot kept byte-compatible with the router's v13 migration so a
``CREATE TABLE IF NOT EXISTS`` here is a no-op after the router (or v7) created it.
"""

from __future__ import annotations

import os
import re
import sqlite3
import tomllib
from contextlib import closing
from pathlib import Path

from openhost_system_agent.migrations.base import SystemMigration

# Router paths (mirror the openhost.service Environment + the data-dir layout).
_DATA_DIR = "/home/host/.openhost/local_compute_space"
CONFIG_TOML_PATH = f"{_DATA_DIR}/config.toml"
DB_PATH = f"{_DATA_DIR}/persistent_data/openhost/router.db"

# Frozen copy of the v13 ``settings`` schema. MUST stay byte-compatible with the router's v13 migration
# so its CREATE-IF-NOT-EXISTS is a no-op after this migration (or v7) has created the table.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# The three config-file assignment lines the credential lives on (whole line, incl. its newline). Once
# captured into the settings table they are DB-sourced, so their config-file copies are scrubbed.
_CAPTURED_LINE_RE = re.compile(
    r"(?m)^[ \t]*cert_api_keycloak_(?:issuer_url|client_id|client_secret)[ \t]*=.*(?:\r?\n|$)"
)

# settings keys — MUST match compute_space.core.identity_store's IMBUE_IDENTITY_* constants.
_ISSUER_KEY = "imbue_identity_issuer_url"
_CLIENT_ID_KEY = "imbue_identity_client_id"
_CLIENT_SECRET_KEY = "imbue_identity_client_secret"


def _seed_imbue_identity(db: sqlite3.Connection, openhost: dict[str, object]) -> bool:
    """Move ``cert_api_keycloak_*`` into ``settings`` as the shared ``imbue_identity_*`` credential.

    Only seeds when all three parts are present in config AND the settings table has no credential yet
    (so a later Connect/first-boot value is never clobbered). Returns True iff it seeded (the caller
    only scrubs config when the capture succeeded, so a partial credential is never lost).
    """
    already = db.execute(
        "SELECT 1 FROM settings WHERE key IN (?, ?, ?) LIMIT 1",
        (_ISSUER_KEY, _CLIENT_ID_KEY, _CLIENT_SECRET_KEY),
    ).fetchone()
    if already is not None:
        return False
    issuer = str(openhost.get("cert_api_keycloak_issuer_url", "")).strip()
    client_id = str(openhost.get("cert_api_keycloak_client_id", "")).strip()
    client_secret = str(openhost.get("cert_api_keycloak_client_secret", "")).strip()
    if not (issuer and client_id and client_secret):
        return False
    db.executemany(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        [(_ISSUER_KEY, issuer), (_CLIENT_ID_KEY, client_id), (_CLIENT_SECRET_KEY, client_secret)],
    )
    return True


def _scrub_captured_config(config_path: str) -> None:
    """Remove the now-captured ``cert_api_keycloak_*`` lines from ``config.toml``, preserving the file's
    owner/mode (this runs as root, so a naive rewrite would leave it root-owned)."""
    p = Path(config_path)
    try:
        original = p.read_text()
    except OSError:
        return
    scrubbed = _CAPTURED_LINE_RE.sub("", original)
    if scrubbed == original:
        return
    st = p.stat()
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(scrubbed)
    os.chown(tmp, st.st_uid, st.st_gid)
    os.chmod(tmp, st.st_mode & 0o777)
    os.replace(tmp, p)


def migrate(config_path: str = CONFIG_TOML_PATH, db_path: str = DB_PATH) -> None:
    """Capture ``cert_api_keycloak_*`` into the router DB settings table, then scrub those lines from
    ``config.toml``. Idempotent; a no-op if there's no router DB yet or nothing to capture, and it only
    scrubs the config once the credential is safely in the DB."""
    if not Path(db_path).exists():
        return  # the router hasn't created its DB yet — it'll seed from first_boot at first boot
    try:
        with open(config_path, "rb") as f:
            openhost = tomllib.load(f).get("openhost", {})
    except FileNotFoundError:
        return  # no config to capture; a malformed one must fail loud so the migration retries
    if not isinstance(openhost, dict):
        return

    # timeout: the old router is still running during the update, so the DB may be briefly locked.
    with closing(sqlite3.connect(db_path, timeout=30)) as db:
        db.executescript(_SCHEMA)  # frozen v13 settings schema (no-op if already created)
        seeded = _seed_imbue_identity(db, openhost)
        db.commit()

    # Any WAL/SHM files touched here must stay owned by the router's user, not root.
    if Path(db_path).exists():
        st = os.stat(db_path)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(db_path + suffix)
            if sidecar.exists():
                os.chown(sidecar, st.st_uid, st.st_gid)

    # Only scrub once the credential is captured (in the DB now, or from a prior run). If the settings
    # table already holds an imbue_identity_* credential, the config copy is redundant and safe to drop.
    if seeded or _identity_in_settings(db_path):
        _scrub_captured_config(config_path)


def _identity_in_settings(db_path: str) -> bool:
    """True if the settings table already holds all three imbue_identity_* parts."""
    try:
        with closing(sqlite3.connect(db_path, timeout=30)) as db:
            rows = db.execute(
                "SELECT key FROM settings WHERE key IN (?, ?, ?)",
                (_ISSUER_KEY, _CLIENT_ID_KEY, _CLIENT_SECRET_KEY),
            ).fetchall()
        return {r[0] for r in rows} == {_ISSUER_KEY, _CLIENT_ID_KEY, _CLIENT_SECRET_KEY}
    except sqlite3.OperationalError:
        return False


class Migration0009SeedImbueIdentityAndScrub(SystemMigration):
    version = 9

    def up(self) -> None:
        migrate()
