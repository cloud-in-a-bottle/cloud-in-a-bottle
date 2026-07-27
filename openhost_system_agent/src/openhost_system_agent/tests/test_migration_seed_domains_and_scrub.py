"""Tests for the v7 migration: the OLD-instance (upgrade) capture of the config-file domain set +
claim token into the router DB, followed by scrubbing the ``zone_domain`` line from config.toml.

The migration is stdlib-only and self-contained (it runs before ``pixi install``, so it can't import
the router's code).  We drive ``migrate()`` against a temp v12-style DB + config.toml and assert it
seeds the DB and scrubs the file — matching the router's runtime seed for the same inputs.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import openhost_system_agent.migrations.versions.v0007_seed_domains_and_scrub as mod

_CONFIG = (
    "[openhost]\n"
    'zone_domain = "Host.Example.com"\n'
    'host = "127.0.0.1"\n'
    "port = 8080\n"
    "tls_enabled = true\n"
    "# a hand-added secondary public domain\n"
    "[[openhost.domains]]\n"
    'name = "secondary.example.com"\n'
    "tls = true\n"
)


def _v12_db(path: Path, *, owner: bool = False) -> None:
    """A pre-consolidation DB: no domains/settings tables, just a users table (empty unless owner)."""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE users (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL)")
    if owner:
        db.execute("INSERT INTO users (username, password_hash) VALUES ('owner', 'x')")
    db.commit()
    db.close()


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    config = tmp_path / "config.toml"
    config.write_text(_CONFIG)
    db = tmp_path / "router.db"
    claim = tmp_path / "claim_token"
    return config, db, claim


def test_captures_domains_and_claim_token_then_scrubs(tmp_path: Path) -> None:
    config, db, claim = _paths(tmp_path)
    _v12_db(db, owner=False)
    claim.write_text("old-claim-tok:extra")

    mod.migrate(str(config), str(db), str(claim))

    conn = sqlite3.connect(db)
    rows = {
        name: (tls, mdns, is_primary)
        for name, tls, mdns, is_primary in conn.execute("SELECT name, tls, mdns, is_primary FROM domains")
    }
    # zone_domain (lowercased, primary) + [[openhost.domains]] captured.
    assert rows == {
        "host.example.com": (1, 0, 1),
        "secondary.example.com": (1, 0, 0),
    }
    # Claim token moved off the file (token before ':' only) into settings.
    token = conn.execute("SELECT value FROM settings WHERE key = 'claim_token'").fetchone()
    conn.close()
    assert token == ("old-claim-tok",)

    # config.toml scrubbed of the now-captured zone_domain + tls_enabled lines (the primary's tls=1
    # above proves tls_enabled was captured before being removed); the rest is preserved.
    scrubbed = config.read_text()
    assert "zone_domain" not in scrubbed
    assert "tls_enabled" not in scrubbed
    assert "[[openhost.domains]]" in scrubbed and 'name = "secondary.example.com"' in scrubbed
    assert "tls = true" in scrubbed  # the domain's own `tls` line must NOT be scrubbed (only tls_enabled)


def test_is_idempotent_and_preserves_existing_seed(tmp_path: Path) -> None:
    config, db, claim = _paths(tmp_path)
    _v12_db(db, owner=False)
    claim.write_text("tok")

    mod.migrate(str(config), str(db), str(claim))
    # A second run (e.g. config already scrubbed) must not duplicate or raise.
    mod.migrate(str(config), str(db), str(claim))

    conn = sqlite3.connect(db)
    names = [n for (n,) in conn.execute("SELECT name FROM domains ORDER BY name")]
    conn.close()
    assert names == ["host.example.com", "secondary.example.com"]


def test_skips_claim_token_when_owner_exists(tmp_path: Path) -> None:
    # Post-setup instance: owner exists, so the claim token is moot even if the file lingers.
    config, db, claim = _paths(tmp_path)
    _v12_db(db, owner=True)
    claim.write_text("late-tok")

    mod.migrate(str(config), str(db), str(claim))

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT 1 FROM settings WHERE key = 'claim_token'").fetchone() is None
    conn.close()


def test_no_op_without_db(tmp_path: Path) -> None:
    # No router DB yet (router hasn't booted) → nothing captured, config left untouched.
    config, db, claim = _paths(tmp_path)  # db not created

    mod.migrate(str(config), str(db), str(claim))

    assert "zone_domain" in config.read_text()
    assert not db.exists()


def test_scrub_only_when_domains_already_seeded(tmp_path: Path) -> None:
    # A fresh install's first update: the runtime seed already populated domains, so capture no-ops
    # but the migration still scrubs config.toml.
    config, db, claim = _paths(tmp_path)
    _v12_db(db, owner=True)
    conn = sqlite3.connect(db)
    conn.executescript(mod._SCHEMA)
    conn.execute(
        "INSERT INTO domains (name, tls, mdns, is_primary, cert_status, error_message) "
        "VALUES ('host.example.com', 1, 0, 1, 'none', NULL)"
    )
    conn.commit()
    conn.close()

    mod.migrate(str(config), str(db), str(claim))

    conn = sqlite3.connect(db)
    names = [n for (n,) in conn.execute("SELECT name FROM domains")]
    conn.close()
    assert names == ["host.example.com"]  # secondary NOT added (seed no-op)
    assert "zone_domain" not in config.read_text()  # but still scrubbed
