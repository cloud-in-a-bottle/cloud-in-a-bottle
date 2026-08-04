"""Tests for the v9 migration: the OLD-instance (upgrade) capture of the config-file
``cert_api_keycloak_*`` credential into the router DB ``settings`` table as the shared
``imbue_identity_*`` credential, followed by scrubbing those lines from config.toml.

Stdlib-only + self-contained (runs before ``pixi install``). We drive ``migrate()`` against a temp DB
+ config.toml and assert it seeds the settings table and scrubs the file — and that it never clobbers
an existing DB credential or loses a partial one.
"""

from __future__ import annotations

import sqlite3
import tomllib
from pathlib import Path

from openhost_system_agent.migrations.versions.v0009_seed_imbue_identity_and_scrub import _CLIENT_ID_KEY
from openhost_system_agent.migrations.versions.v0009_seed_imbue_identity_and_scrub import _CLIENT_SECRET_KEY
from openhost_system_agent.migrations.versions.v0009_seed_imbue_identity_and_scrub import _ISSUER_KEY
from openhost_system_agent.migrations.versions.v0009_seed_imbue_identity_and_scrub import _SCHEMA
from openhost_system_agent.migrations.versions.v0009_seed_imbue_identity_and_scrub import migrate

_FULL_CONFIG = (
    "[openhost]\n"
    'host = "127.0.0.1"\n'
    "port = 8080\n"
    'cert_provider = "cert_api"\n'
    'cert_api_base_url = "https://cert.example.com"\n'
    'cert_api_keycloak_issuer_url = "https://kc.example.com/realms/openhost-customers"\n'
    'cert_api_keycloak_client_id = "instance-alice"\n'
    'cert_api_keycloak_client_secret = "s3cr3t"\n'
)


def _db_with_settings(path: Path) -> None:
    """A router DB that already has the settings table (as v7/v13 would create it)."""
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()


def _settings(db_path: Path) -> dict[str, str]:
    conn = sqlite3.connect(db_path)
    try:
        return {k: v for k, v in conn.execute("SELECT key, value FROM settings")}
    finally:
        conn.close()


def _openhost(config_path: Path) -> dict[str, object]:
    with open(config_path, "rb") as f:
        section = tomllib.load(f).get("openhost", {})
    return section if isinstance(section, dict) else {}


# --- happy path ---------------------------------------------------------------


def test_captures_credential_into_settings_then_scrubs(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_FULL_CONFIG)
    db = tmp_path / "router.db"
    _db_with_settings(db)

    migrate(str(config), str(db))

    s = _settings(db)
    assert s[_ISSUER_KEY] == "https://kc.example.com/realms/openhost-customers"
    assert s[_CLIENT_ID_KEY] == "instance-alice"
    assert s[_CLIENT_SECRET_KEY] == "s3cr3t"
    # The cert_api_keycloak_* lines are scrubbed from config...
    oh = _openhost(config)
    assert "cert_api_keycloak_issuer_url" not in oh
    assert "cert_api_keycloak_client_id" not in oh
    assert "cert_api_keycloak_client_secret" not in oh
    # ...but everything else is preserved.
    assert oh["cert_provider"] == "cert_api"
    assert oh["cert_api_base_url"] == "https://cert.example.com"
    assert oh["host"] == "127.0.0.1"


def test_idempotent(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_FULL_CONFIG)
    db = tmp_path / "router.db"
    _db_with_settings(db)

    migrate(str(config), str(db))
    first = _settings(db)
    first_config = config.read_text()
    # Second run: config no longer has the keys, settings already populated -> no change.
    migrate(str(config), str(db))
    assert _settings(db) == first
    assert config.read_text() == first_config


# --- do-not-clobber / do-not-lose ---------------------------------------------


def test_does_not_clobber_existing_settings_credential(tmp_path: Path) -> None:
    # An instance that already has an imbue_identity_* credential (e.g. from Connect) must keep it,
    # and its config copy is redundant -> still scrubbed.
    config = tmp_path / "config.toml"
    config.write_text(_FULL_CONFIG)
    db = tmp_path / "router.db"
    _db_with_settings(db)
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        [(_ISSUER_KEY, "existing-iss"), (_CLIENT_ID_KEY, "existing-id"), (_CLIENT_SECRET_KEY, "existing-sec")],
    )
    conn.commit()
    conn.close()

    migrate(str(config), str(db))

    s = _settings(db)
    assert s[_CLIENT_SECRET_KEY] == "existing-sec"  # not clobbered by the config value
    # config copy is redundant, so it is scrubbed.
    assert "cert_api_keycloak_client_secret" not in _openhost(config)


def test_partial_credential_not_seeded_and_not_scrubbed(tmp_path: Path) -> None:
    # If config has only 2 of the 3 parts (a typo), seed nothing and DO NOT scrub -> nothing is lost.
    config = tmp_path / "config.toml"
    config.write_text(
        "[openhost]\n"
        'cert_api_keycloak_issuer_url = "https://kc/realms/oh"\n'
        'cert_api_keycloak_client_id = "instance-x"\n'
    )
    db = tmp_path / "router.db"
    _db_with_settings(db)

    migrate(str(config), str(db))

    assert _settings(db) == {}  # nothing seeded
    oh = _openhost(config)
    assert oh["cert_api_keycloak_issuer_url"] == "https://kc/realms/oh"  # preserved (not scrubbed)
    assert oh["cert_api_keycloak_client_id"] == "instance-x"


# --- no-ops -------------------------------------------------------------------


def test_no_db_is_noop(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_FULL_CONFIG)
    db = tmp_path / "router.db"  # does not exist
    migrate(str(config), str(db))
    # config untouched; no db created.
    assert "cert_api_keycloak_issuer_url" in _openhost(config)
    assert not db.exists()


def test_no_config_keys_is_noop(tmp_path: Path) -> None:
    # A fresh install (no cert_api_keycloak_*) -> nothing to capture, nothing to scrub.
    config = tmp_path / "config.toml"
    config.write_text('[openhost]\nhost = "127.0.0.1"\nport = 8080\n')
    db = tmp_path / "router.db"
    _db_with_settings(db)

    migrate(str(config), str(db))

    assert _settings(db) == {}
    assert config.read_text() == '[openhost]\nhost = "127.0.0.1"\nport = 8080\n'


def test_missing_config_is_noop(tmp_path: Path) -> None:
    db = tmp_path / "router.db"
    _db_with_settings(db)
    migrate(str(tmp_path / "nope.toml"), str(db))
    assert _settings(db) == {}


def test_creates_settings_table_if_absent(tmp_path: Path) -> None:
    # An older DB without the settings table: the migration's frozen CREATE handles it.
    config = tmp_path / "config.toml"
    config.write_text(_FULL_CONFIG)
    db = tmp_path / "router.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE users (username TEXT PRIMARY KEY)")  # some table so the file exists
    conn.commit()
    conn.close()

    migrate(str(config), str(db))

    assert _settings(db)[_CLIENT_ID_KEY] == "instance-alice"
