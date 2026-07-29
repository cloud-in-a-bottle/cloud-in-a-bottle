"""Backward-compat: an OLD instance upgrading to the config/domains-consolidation code.

Simulates a pre-consolidation instance in-process — a v12 DB (no domains/settings tables), a
config.toml with ``zone_domain``, and a legacy claim-token file — then runs the upgrade-boot sequence
(migrate → seed → rebuild) and asserts the primary + claim token are captured into the DB and the
cert layout is preserved.  (config.toml is left untouched — removing the now-ignored ``zone_domain``
line is a later system-agent migration.)"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from compute_space.config import load_config
from compute_space.core.domain_store import effective_domains
from compute_space.core.domain_store import load_records
from compute_space.core.domain_store import rebuild_active_domains
from compute_space.core.first_boot import seed_first_boot
from compute_space.core.settings_store import CLAIM_TOKEN_KEY
from compute_space.core.settings_store import get_setting
from compute_space.db.connection import init_db
from compute_space.db.schema import schema_path
from compute_space.tests.conftest import open_db
from compute_space.web.start import _require_configured_domain


def _build_v12_db(db_path: str) -> None:
    """A pre-consolidation DB: the full schema minus the v13 tables, stamped at v12."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    db.executescript(Path(schema_path()).read_text())
    db.execute("DROP TABLE domains")
    db.execute("DROP TABLE settings")
    db.execute("INSERT OR REPLACE INTO schema_version (id, version) VALUES (1, 12)")
    db.commit()
    db.close()


def _old_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, owner: bool):  # type: ignore[no-untyped-def]
    """Lay down an old instance's on-disk state and return its loaded Config."""
    data_root = tmp_path / "data"
    data_root.mkdir()
    config_toml = tmp_path / "config.toml"
    config_toml.write_text(
        "[openhost]\n"
        f'data_root_dir = "{data_root}"\n'
        'zone_domain = "host.example.com"\n'
        'host = "127.0.0.1"\n'
        "port = 8080\n"
        "tls_enabled = true\n"
    )
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", str(config_toml))
    config = load_config()
    config.make_all_dirs()

    _build_v12_db(config.db_path)
    if owner:
        db = sqlite3.connect(config.db_path)
        db.execute("INSERT INTO users (username, password_hash) VALUES ('owner', 'x')")
        db.commit()
        db.close()

    # A claim-token file from provisioning.
    Path(config.claim_token_path).write_text("old-claim-tok:extra")
    return config, config_toml


def test_upgrade_captures_primary_and_claim_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, config_toml = _old_instance(tmp_path, monkeypatch, owner=False)

    # Upgrade boot: migrate the DB, then seed + rebuild.
    init_db(config.db_path)  # v12 -> v13
    seed_first_boot(config)
    with closing(open_db(config)) as db:
        new_config = rebuild_active_domains(config, db)
        by_name = {r.name: r for r in load_records(db)}
        claim_token = get_setting(db, CLAIM_TOKEN_KEY)

    # The config-file zone_domain is captured into the DB as the primary.
    assert set(by_name) == {"host.example.com"}
    assert by_name["host.example.com"].is_primary is True

    # Claim token migrated off the file (token before ':' only) into settings.
    assert claim_token == "old-claim-tok"

    # Runtime now sources everything from the DB primary; cert layout unchanged for the primary.
    assert new_config.primary_domain.name == "host.example.com"
    assert new_config.zone_domain == "host.example.com"  # derived from the primary
    assert new_config.cert_path_for("host.example.com") == new_config.tls_cert_path  # legacy path kept
    # A non-primary domain would get its own per-domain cert path.
    assert new_config.cert_path_for("secondary.example.com") == new_config.certs_dir / "secondary.example.com.pem"

    # config.toml is left untouched — the now-ignored zone_domain line stays (scrubbed later, by a
    # system-agent migration).
    assert "zone_domain" in config_toml.read_text()


def test_upgrade_is_idempotent_across_restarts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An instance that already completed setup (owner exists): the claim-token file is normally gone,
    # but even if present, a second boot must not re-seed or duplicate anything.
    config, config_toml = _old_instance(tmp_path, monkeypatch, owner=True)
    init_db(config.db_path)
    seed_first_boot(config)
    with closing(open_db(config)) as db:
        first = {r.name for r in load_records(db)}

    # Second boot: config.toml is unchanged (still has zone_domain) but the DB is authoritative.
    reloaded = load_config()
    assert reloaded.zone_domain == "host.example.com"  # still present (not scrubbed), just ignored
    seed_first_boot(reloaded)
    with closing(open_db(reloaded)) as db:
        rebuild_active_domains(reloaded, db)
        assert {r.name for r in load_records(db)} == first  # unchanged, no duplicates
        assert reloaded_primary(db) == "host.example.com"


def reloaded_primary(db: sqlite3.Connection) -> str:
    return next(r.name for r in load_records(db) if r.is_primary)


def test_boot_fails_loud_when_no_domain_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Ensure failure when no domain source given
    data_root = tmp_path / "data"
    data_root.mkdir()
    config_toml = tmp_path / "config.toml"
    config_toml.write_text(f'[openhost]\ndata_root_dir = "{data_root}"\nhost = "127.0.0.1"\nport = 8080\n')
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", str(config_toml))
    config = load_config()
    config.make_all_dirs()
    init_db(config.db_path)

    seed_first_boot(config)
    with closing(open_db(config)) as db:
        config = rebuild_active_domains(config, db)
        assert effective_domains(db) == ()  # nothing seeded it — the misconfiguration the guard catches

    with pytest.raises(RuntimeError, match="No domain configured"):
        _require_configured_domain(config)


def test_boot_guard_passes_for_seeded_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, _ = _old_instance(tmp_path, monkeypatch, owner=False)
    init_db(config.db_path)
    seed_first_boot(config)
    with closing(open_db(config)) as db:
        config = rebuild_active_domains(config, db)
    _require_configured_domain(config)  # a seeded instance boots fine (no raise)
