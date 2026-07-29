"""v4: extend ``apps.status`` CHECK to include ``'removing'``.

Python (not SQL-file) migration so we can toggle ``PRAGMA
foreign_keys`` outside the transaction wrapper — SQLite makes that
PRAGMA a no-op while a transaction is open, and the standard "create
new table, copy, drop, rename" recipe for changing a CHECK constraint
needs FKs disabled across the swap (otherwise the RENAME step
rejects rows in sibling tables that reference apps).
"""

from __future__ import annotations

import sqlite3

from compute_space.db.versioned.base import Migration

_CREATE_NEW_APPS = """
CREATE TABLE apps_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    manifest_name TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL,
    description TEXT,
    runtime_type TEXT NOT NULL DEFAULT 'serverfull',
    repo_path TEXT NOT NULL,
    repo_url TEXT,
    health_check TEXT,
    local_port INTEGER NOT NULL UNIQUE,
    container_port INTEGER,
    container_id TEXT,
    status TEXT NOT NULL DEFAULT 'stopped' CHECK(status IN ('building', 'starting', 'running', 'stopped', 'error', 'removing')),
    error_message TEXT,
    memory_mb INTEGER NOT NULL DEFAULT 128,
    cpu_millicores INTEGER NOT NULL DEFAULT 100,
    gpu INTEGER NOT NULL DEFAULT 0,
    public_paths TEXT NOT NULL DEFAULT '[]',
    manifest_raw TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_COPY_APPS = """
INSERT INTO apps_new (
    id, name, manifest_name, version, description, runtime_type,
    repo_path, repo_url, health_check, local_port, container_port,
    container_id, status, error_message, memory_mb, cpu_millicores,
    gpu, public_paths, manifest_raw, created_at, updated_at
)
SELECT
    id, name, manifest_name, version, description, runtime_type,
    repo_path, repo_url, health_check, local_port, container_port,
    container_id, status, error_message, memory_mb, cpu_millicores,
    gpu, public_paths, manifest_raw, created_at, updated_at
FROM apps
"""


class Migration0004AppsRemovingStatus(Migration):
    version = 4

    def up(self, db: sqlite3.Connection) -> None:  # pragma: no cover - apply() is overridden
        raise NotImplementedError("Migration0004AppsRemovingStatus drives execution through apply()")

    def apply(self, db: sqlite3.Connection) -> None:
        # migration-lint: allow-pragma-foreign-keys
        # Deliberate non-tx-safe toggle: FKs must be off across the table swap.
        db.execute("PRAGMA foreign_keys = OFF")
        db.execute("BEGIN EXCLUSIVE")
        try:
            db.execute(_CREATE_NEW_APPS)
            db.execute(_COPY_APPS)
            db.execute("DROP TABLE apps")
            db.execute("ALTER TABLE apps_new RENAME TO apps")
            db.execute("CREATE INDEX IF NOT EXISTS idx_apps_status ON apps(status)")
            db.execute(
                "INSERT OR REPLACE INTO schema_version (id, version) VALUES (1, ?)",
                (self.version,),
            )
            db.execute("COMMIT")
        except Exception:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            db.execute("PRAGMA foreign_keys = ON")
