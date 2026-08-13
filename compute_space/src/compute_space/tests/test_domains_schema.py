"""Phase 0: the v13 `domains` + `settings` tables (config/domains consolidation).

Locks in that both DB-init paths — fresh `schema.sql` and an upgrade through the v13 migration —
produce the tables, and that the partial unique index enforces at most one primary domain."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from compute_space.db.schema import schema_path
from compute_space.db.versioned.migrations.v0013_domains_and_settings import Migration0013DomainsAndSettings
from compute_space.db.versioned.runner import apply_migrations
from compute_space.db.versioned.runner import read_version
from openhost_system_agent.migrations.versions.v0007_seed_domains_and_scrub import _SCHEMA as _AGENT_SCHEMA


def _tables(db: sqlite3.Connection) -> set[str]:
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


_DDL_OBJECTS = ("domains", "settings", "idx_domains_one_primary")


def _normalize_ddl(sql: str) -> str:
    """Strip inline comments and collapse whitespace so cosmetically-different copies compare equal."""
    return re.sub(r"\s+", " ", re.sub(r"--[^\n]*", "", sql)).strip()


def _ddl_fingerprint(script: str) -> dict[str, str]:
    """Normalized CREATE text for the domains/settings objects a DDL script materializes."""
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(script)
        rows = db.execute("SELECT name, sql FROM sqlite_master WHERE name IN (?, ?, ?)", _DDL_OBJECTS).fetchall()
    finally:
        db.close()
    return {name: _normalize_ddl(sql) for name, sql in rows}


def test_fresh_init_creates_domains_and_settings(tmp_path: Path) -> None:
    db_path = str(tmp_path / "fresh.db")
    apply_migrations(db_path)
    db = sqlite3.connect(db_path)
    try:
        assert {"domains", "settings"} <= _tables(db)
        assert read_version(db) == 14
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
        assert read_version(db) == 14
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


def test_frozen_v13_schema_identical_across_projects() -> None:
    """The router's v13 migration and the system-agent's frozen v13 _SCHEMA are the same DDL kept in
    two projects; they must stay byte-identical so the agent's CREATE-IF-NOT-EXISTS is a no-op at v13."""
    v13 = _ddl_fingerprint(Migration0013DomainsAndSettings()._load_sql())
    agent = _ddl_fingerprint(_AGENT_SCHEMA)

    assert set(v13) == set(_DDL_OBJECTS), "the v13 migration is missing a domains/settings object"
    assert v13 == agent, "the v13 migration and system-agent v0007 _SCHEMA diverged"


def test_head_schema_is_v13_minus_dropped_mdns() -> None:
    """db/schema.sql is the live head: v14 dropped domains.mdns, so its domains DDL differs from the
    frozen v13 copy, but settings + the primary index are untouched and must still match."""
    fresh = _ddl_fingerprint(Path(schema_path()).read_text())
    v13 = _ddl_fingerprint(Migration0013DomainsAndSettings()._load_sql())

    assert set(fresh) == set(_DDL_OBJECTS), "db/schema.sql is missing a domains/settings object"
    assert fresh["settings"] == v13["settings"]
    assert fresh["idx_domains_one_primary"] == v13["idx_domains_one_primary"]
    assert "mdns" in v13["domains"] and "mdns" not in fresh["domains"]
