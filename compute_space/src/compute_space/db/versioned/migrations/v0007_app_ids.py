"""v7: introduce opaque ``app_id`` as the cross-table identity for apps.

Previously every FK table keyed off ``apps.name``, which made rename a
multi-table rewrite and risked dangling cross-app references. After
this migration every child table FKs to the immutable ``apps.app_id``
(12-char base58); ``apps.name`` becomes a label / subdomain only.

Python migration (not SQL-file) so we can:
  - mint a fresh app_id per row in a loop (SQLite has no portable rand-text)
  - toggle ``PRAGMA foreign_keys`` outside the BEGIN EXCLUSIVE wrapper
    (PRAGMA is a no-op inside a transaction in SQLite)
  - drive the table-recreate dance for every FK table
"""

from __future__ import annotations

import sqlite3

from compute_space.core.app_id import new_app_id
from compute_space.db.versioned.base import Migration


class Migration0007AppIds(Migration):
    version = 7

    def up(self, db: sqlite3.Connection) -> None:  # pragma: no cover - apply() is overridden
        raise NotImplementedError("Migration0007AppIds drives execution through apply()")

    def apply(self, db: sqlite3.Connection) -> None:
        # migration-lint: allow-pragma-foreign-keys
        # Deliberate non-tx-safe toggle: FKs must be off across the FK-table recreates.
        db.execute("PRAGMA foreign_keys = OFF")
        db.execute("BEGIN EXCLUSIVE")
        try:
            self._add_app_id_to_apps(db)
            self._recreate_app_databases(db)
            self._recreate_app_port_mappings(db)
            self._recreate_app_tokens(db)
            self._recreate_service_providers(db)
            self._recreate_service_providers_v2(db)
            self._recreate_service_defaults(db)
            self._recreate_permissions(db)
            self._recreate_permissions_v2(db)
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

    def _add_app_id_to_apps(self, db: sqlite3.Connection) -> None:
        # Full table recreate so the post-v7 shape matches schema.sql exactly
        # (NOT NULL UNIQUE app_id with no DEFAULT). ALTER TABLE ADD would
        # need a DEFAULT to satisfy NOT NULL, which would then live on the
        # column forever and diverge from a fresh-init shape.
        db.execute(
            """
            CREATE TABLE apps_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app_id TEXT NOT NULL UNIQUE,
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
                installed_by TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        rows = db.execute(
            """SELECT id, name, manifest_name, version, description, runtime_type,
                      repo_path, repo_url, health_check, local_port, container_port,
                      container_id, status, error_message, memory_mb, cpu_millicores,
                      gpu, public_paths, manifest_raw, installed_by, created_at, updated_at
               FROM apps"""
        ).fetchall()
        for r in rows:
            db.execute(
                """INSERT INTO apps_new
                   (id, app_id, name, manifest_name, version, description, runtime_type,
                    repo_path, repo_url, health_check, local_port, container_port,
                    container_id, status, error_message, memory_mb, cpu_millicores,
                    gpu, public_paths, manifest_raw, installed_by, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (r[0], new_app_id(), *r[1:]),
            )
        db.execute("DROP TABLE apps")
        db.execute("ALTER TABLE apps_new RENAME TO apps")
        db.execute("CREATE INDEX IF NOT EXISTS idx_apps_status ON apps(status)")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_apps_app_id ON apps(app_id)")

    def _recreate_app_databases(self, db: sqlite3.Connection) -> None:
        db.execute(
            """
            CREATE TABLE app_databases_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app_id TEXT NOT NULL,
                db_name TEXT NOT NULL,
                db_path TEXT NOT NULL,
                FOREIGN KEY (app_id) REFERENCES apps(app_id) ON DELETE CASCADE,
                UNIQUE(app_id, db_name)
            )
            """
        )
        db.execute(
            """
            INSERT INTO app_databases_new (id, app_id, db_name, db_path)
            SELECT d.id, a.app_id, d.db_name, d.db_path
            FROM app_databases d
            JOIN apps a ON a.name = d.app_name
            """
        )
        db.execute("DROP TABLE app_databases")
        db.execute("ALTER TABLE app_databases_new RENAME TO app_databases")

    def _recreate_app_port_mappings(self, db: sqlite3.Connection) -> None:
        db.execute(
            """
            CREATE TABLE app_port_mappings_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app_id TEXT NOT NULL,
                label TEXT NOT NULL,
                container_port INTEGER NOT NULL,
                host_port INTEGER NOT NULL,
                FOREIGN KEY (app_id) REFERENCES apps(app_id) ON DELETE CASCADE,
                UNIQUE(app_id, label)
            )
            """
        )
        db.execute(
            """
            INSERT INTO app_port_mappings_new (id, app_id, label, container_port, host_port)
            SELECT p.id, a.app_id, p.label, p.container_port, p.host_port
            FROM app_port_mappings p
            JOIN apps a ON a.name = p.app_name
            """
        )
        db.execute("DROP TABLE app_port_mappings")
        db.execute("ALTER TABLE app_port_mappings_new RENAME TO app_port_mappings")
        db.execute("CREATE UNIQUE INDEX idx_port_mappings_host_port ON app_port_mappings(host_port)")

    def _recreate_app_tokens(self, db: sqlite3.Connection) -> None:
        db.execute(
            """
            CREATE TABLE app_tokens_new (
                app_id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                FOREIGN KEY (app_id) REFERENCES apps(app_id) ON DELETE CASCADE
            )
            """
        )
        db.execute(
            """
            INSERT INTO app_tokens_new (app_id, token_hash)
            SELECT a.app_id, t.token_hash
            FROM app_tokens t
            JOIN apps a ON a.name = t.app_name
            """
        )
        db.execute("DROP TABLE app_tokens")
        db.execute("ALTER TABLE app_tokens_new RENAME TO app_tokens")

    def _recreate_service_providers(self, db: sqlite3.Connection) -> None:
        db.execute(
            """
            CREATE TABLE service_providers_new (
                service_name TEXT NOT NULL,
                app_id TEXT NOT NULL,
                PRIMARY KEY (service_name, app_id),
                FOREIGN KEY (app_id) REFERENCES apps(app_id) ON DELETE CASCADE
            )
            """
        )
        db.execute(
            """
            INSERT INTO service_providers_new (service_name, app_id)
            SELECT sp.service_name, a.app_id
            FROM service_providers sp
            JOIN apps a ON a.name = sp.app_name
            """
        )
        db.execute("DROP TABLE service_providers")
        db.execute("ALTER TABLE service_providers_new RENAME TO service_providers")

    def _recreate_service_providers_v2(self, db: sqlite3.Connection) -> None:
        db.execute(
            """
            CREATE TABLE service_providers_v2_new (
                service_url TEXT NOT NULL,
                app_id TEXT NOT NULL,
                service_version TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                PRIMARY KEY (service_url, app_id, service_version),
                FOREIGN KEY (app_id) REFERENCES apps(app_id) ON DELETE CASCADE
            )
            """
        )
        db.execute(
            """
            INSERT INTO service_providers_v2_new (service_url, app_id, service_version, endpoint)
            SELECT sp.service_url, a.app_id, sp.service_version, sp.endpoint
            FROM service_providers_v2 sp
            JOIN apps a ON a.name = sp.app_name
            """
        )
        db.execute("DROP TABLE service_providers_v2")
        db.execute("ALTER TABLE service_providers_v2_new RENAME TO service_providers_v2")

    def _recreate_service_defaults(self, db: sqlite3.Connection) -> None:
        db.execute(
            """
            CREATE TABLE service_defaults_new (
                service_url TEXT PRIMARY KEY,
                app_id TEXT NOT NULL,
                FOREIGN KEY (app_id) REFERENCES apps(app_id) ON DELETE CASCADE
            )
            """
        )
        db.execute(
            """
            INSERT INTO service_defaults_new (service_url, app_id)
            SELECT sd.service_url, a.app_id
            FROM service_defaults sd
            JOIN apps a ON a.name = sd.app_name
            """
        )
        db.execute("DROP TABLE service_defaults")
        db.execute("ALTER TABLE service_defaults_new RENAME TO service_defaults")

    def _recreate_permissions(self, db: sqlite3.Connection) -> None:
        db.execute(
            """
            CREATE TABLE permissions_new (
                consumer_app_id TEXT NOT NULL,
                permission_key TEXT NOT NULL,
                PRIMARY KEY (consumer_app_id, permission_key),
                FOREIGN KEY (consumer_app_id) REFERENCES apps(app_id) ON DELETE CASCADE
            )
            """
        )
        db.execute(
            """
            INSERT INTO permissions_new (consumer_app_id, permission_key)
            SELECT a.app_id, p.permission_key
            FROM permissions p
            JOIN apps a ON a.name = p.consumer_app
            """
        )
        db.execute("DROP TABLE permissions")
        db.execute("ALTER TABLE permissions_new RENAME TO permissions")

    def _recreate_permissions_v2(self, db: sqlite3.Connection) -> None:
        # provider_app_id is '' when the grant is global (any provider).
        # LEFT JOIN preserves the empty-marker case without trying to look
        # it up in apps. The WHERE filter drops orphan provider rows
        # (provider_app non-empty but no matching app) — same silent-drop
        # behaviour as the INNER JOIN recreates above. Without the filter
        # an orphan would map to NULL and trip the NOT NULL on
        # provider_app_id, aborting the whole migration.
        db.execute(
            """
            CREATE TABLE permissions_v2_new (
                consumer_app_id TEXT NOT NULL,
                service_url TEXT NOT NULL,
                grant_payload TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'global' CHECK(scope IN ('global', 'app')),
                provider_app_id TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (consumer_app_id, service_url, grant_payload, scope, provider_app_id),
                FOREIGN KEY (consumer_app_id) REFERENCES apps(app_id) ON DELETE CASCADE
            )
            """
        )
        db.execute(
            """
            INSERT INTO permissions_v2_new
                (consumer_app_id, service_url, grant_payload, scope, provider_app_id)
            SELECT
                consumer.app_id,
                p.service_url,
                p.grant_payload,
                p.scope,
                CASE WHEN p.provider_app = '' THEN '' ELSE provider.app_id END
            FROM permissions_v2 p
            JOIN apps consumer ON consumer.name = p.consumer_app
            LEFT JOIN apps provider ON provider.name = p.provider_app AND p.provider_app != ''
            WHERE p.provider_app = '' OR provider.app_id IS NOT NULL
            """
        )
        db.execute("DROP TABLE permissions_v2")
        db.execute("ALTER TABLE permissions_v2_new RENAME TO permissions_v2")
