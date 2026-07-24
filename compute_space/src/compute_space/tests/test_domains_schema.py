"""Phase 0: the v13 `domains` + `settings` tables (config/domains consolidation).

Locks in that both DB-init paths — fresh `schema.sql` and an upgrade through the v13 migration —
produce the tables, and that the partial unique index enforces at most one primary domain."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from compute_space.db.schema import schema_path
from compute_space.db.versioned.runner import apply_migrations
from compute_space.db.versioned.runner import read_version


def _tables(db: sqlite3.Connection) -> set[str]:
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_fresh_init_creates_domains_and_settings(tmp_path: Path) -> None:
    db_path = str(tmp_path / "fresh.db")
    apply_migrations(db_path)
    db = sqlite3.connect(db_path)
    try:
        assert {"domains", "settings"} <= _tables(db)
        assert read_version(db) == 13
    finally:
        db.close()


def test_v12_db_upgrades_to_domains_and_settings(tmp_path: Path) -> None:
    # Build a v12-era DB (full schema, tables dropped, stamped 12), then migrate.
    db_path = str(tmp_path / "v12.db")
    db = sqlite3.connect(db_path, isolation_level=None)
    db.executescript(Path(schema_path()).read_text())
    db.execute("DROP TABLE domains")
    db.execute("DROP TABLE settings")
    db.execute("INSERT OR REPLACE INTO schema_version (id, version) VALUES (1, 12)")
    db.close()

    apply_migrations(db_path)

    db = sqlite3.connect(db_path)
    try:
        assert {"domains", "settings"} <= _tables(db)
        assert read_version(db) == 13
    finally:
        db.close()


def test_at_most_one_primary_domain(tmp_path: Path) -> None:
    db_path = str(tmp_path / "fresh.db")
    apply_migrations(db_path)
    db = sqlite3.connect(db_path, isolation_level=None)
    try:
        db.execute("INSERT INTO domains (name, tls, is_primary) VALUES ('a.example.com', 1, 1)")
        # A second primary must be rejected by the partial unique index.
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO domains (name, tls, is_primary) VALUES ('b.example.com', 1, 1)")
        # Non-primary rows are unconstrained.
        db.execute("INSERT INTO domains (name, tls, is_primary) VALUES ('b.example.com', 1, 0)")
        db.execute("INSERT INTO domains (name, tls, is_primary) VALUES ('c.example.com', 1, 0)")
        assert db.execute("SELECT count(*) FROM domains WHERE is_primary = 1").fetchone()[0] == 1
    finally:
        db.close()


def test_settings_is_key_value(tmp_path: Path) -> None:
    db_path = str(tmp_path / "fresh.db")
    apply_migrations(db_path)
    db = sqlite3.connect(db_path, isolation_level=None)
    try:
        db.execute("INSERT INTO settings (key, value) VALUES ('claim_token', 'abc123')")
        assert db.execute("SELECT value FROM settings WHERE key = 'claim_token'").fetchone()[0] == "abc123"
        # key is the primary key — a duplicate insert must fail.
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO settings (key, value) VALUES ('claim_token', 'other')")
    finally:
        db.close()
