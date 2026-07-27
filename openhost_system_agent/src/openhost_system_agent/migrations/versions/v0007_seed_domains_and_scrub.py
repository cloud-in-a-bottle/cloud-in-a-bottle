"""v7: migrate an OLD instance's config-file domain set + claim token into the router DB, then
scrub the now-captured ``zone_domain`` / ``tls_enabled`` lines from ``config.toml``.

This is the **old-instance (upgrade)** path of the config/domains consolidation.  Fresh installs
don't run agent migrations at first boot (ansible stamps the log at v1 and the agent applies v2+ only
on ``openhost update``), so they seed at runtime via the router's own first-boot seed; for them this
migration is a no-op capture and just scrubs on a later update.

It runs as root, during ``openhost update``, **before** the router restarts — so it does the
*capture* itself.  (A scrub-only migration would strip ``zone_domain`` before the router's runtime
seed reads it on restart, losing the domain.  Doing capture-then-scrub here avoids that race.)

stdlib only: agent migrations run *before* ``pixi install``, so this can't import the router's code.
The v13 ``domains``/``settings`` schema and the seed rules are copied here as a frozen snapshot —
kept byte-compatible with ``compute_space/db/versioned/migrations/v0013_domains_and_settings.sql`` so
the router's own ``CREATE TABLE IF NOT EXISTS`` is a no-op when it later applies v13.
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
CLAIM_TOKEN_PATH = f"{_DATA_DIR}/persistent_data/openhost/claim_token"

# Frozen copy of the v13 schema.  MUST stay byte-compatible with the router's v13 migration so its
# CREATE-IF-NOT-EXISTS is a no-op after this migration has created the tables.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS domains (
    name          TEXT PRIMARY KEY,
    tls           INTEGER NOT NULL DEFAULT 0,
    mdns          INTEGER NOT NULL DEFAULT 0,
    is_primary    INTEGER NOT NULL DEFAULT 0,
    cert_status   TEXT NOT NULL DEFAULT 'none' CHECK(cert_status IN ('none', 'acquiring', 'active', 'error')),
    error_message TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_domains_one_primary ON domains(is_primary) WHERE is_primary = 1;
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Top-level ``zone_domain``/``tls_enabled`` assignment lines (whole line, incl. its newline).  Both
# are DB-derived (the primary domain's name + its per-domain ``tls``) and captured above, so their
# config-file copies are scrubbed.  The instance-level switches (``acquire_tls_cert_if_missing``,
# ``coredns_enabled``, ``start_caddy``) are matched by neither and stay.
_CAPTURED_LINE_RE = re.compile(r"(?m)^[ \t]*(?:zone_domain|tls_enabled)[ \t]*=.*(?:\r?\n|$)")


def _seed_domains(db: sqlite3.Connection, openhost: dict[str, object]) -> None:
    """Seed the ``domains`` table from the config-file ``[openhost]`` section, only if it's empty
    (a fresh install already seeded at first boot)."""
    if db.execute("SELECT 1 FROM domains LIMIT 1").fetchone() is not None:
        return
    zone_domain = str(openhost.get("zone_domain", "")).strip().lower()
    if not zone_domain:
        return  # nothing to capture (e.g. already-scrubbed config)
    tls_enabled = bool(openhost.get("tls_enabled", False))
    seen = {zone_domain.split(":")[0]}
    rows: list[tuple[str, int, int, int, str, None]] = [(zone_domain, int(tls_enabled), 0, 1, "none", None)]
    extras = openhost.get("domains", [])
    for entry in extras if isinstance(extras, list) else []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip().lower()
        host = name.split(":")[0]
        if not name or host in seen:
            continue
        seen.add(host)
        rows.append((name, int(bool(entry.get("tls", False))), int(bool(entry.get("mdns", False))), 0, "none", None))
    db.executemany(
        "INSERT INTO domains (name, tls, mdns, is_primary, cert_status, error_message) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )


def _seed_claim_token(db: sqlite3.Connection, claim_token_path: str) -> None:
    """Move the legacy claim-token file into ``settings`` — only while still pre-setup (no owner)
    and not already present.  The file may hold ``token:extra``; the token is before the colon."""
    if db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None:
        return  # owner exists → claim token is moot
    if db.execute("SELECT 1 FROM settings WHERE key = 'claim_token'").fetchone() is not None:
        return
    try:
        token = Path(claim_token_path).read_text().strip().split(":", 1)[0]
    except OSError:
        return
    if token:
        db.execute("INSERT INTO settings (key, value) VALUES ('claim_token', ?)", (token,))


def _scrub_captured_config(config_path: str) -> None:
    """Remove the now-captured ``zone_domain`` / ``tls_enabled`` lines from ``config.toml``, preserving
    the file's owner/mode (this runs as root, so a naive rewrite would leave it root-owned)."""
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


def migrate(
    config_path: str = CONFIG_TOML_PATH,
    db_path: str = DB_PATH,
    claim_token_path: str = CLAIM_TOKEN_PATH,
) -> None:
    """Capture the config-file domain set + claim token into the router DB, then scrub the captured
    ``zone_domain`` / ``tls_enabled`` lines from ``config.toml``.  Idempotent; a no-op if there's no
    router DB yet or nothing left to capture."""
    if not Path(db_path).exists():
        return  # the router hasn't created its DB yet — it'll seed at first boot
    try:
        with open(config_path, "rb") as f:
            openhost = tomllib.load(f).get("openhost", {})
    except (OSError, tomllib.TOMLDecodeError):
        return
    if not isinstance(openhost, dict):
        return

    # timeout: the old router is still running during the update, so the DB may be briefly locked.
    with closing(sqlite3.connect(db_path, timeout=30)) as db:
        db.executescript(_SCHEMA)  # frozen v13 schema (no-op if the router already created it)
        _seed_domains(db, openhost)
        _seed_claim_token(db, claim_token_path)
        db.commit()

    # Any WAL/SHM files touched here must stay owned by the router's user, not root.
    if Path(db_path).exists():
        st = os.stat(db_path)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(db_path + suffix)
            if sidecar.exists():
                os.chown(sidecar, st.st_uid, st.st_gid)

    _scrub_captured_config(config_path)


class Migration0007SeedDomainsAndScrub(SystemMigration):
    version = 7

    def up(self) -> None:
        migrate()
