"""Backward-compat: an OLD instance upgrading to the config/domains-consolidation code.

Simulates a pre-consolidation instance in-process — a v12 DB (no domains/settings tables), a
config.toml with ``zone_domain`` + ``[[openhost.domains]]``, and a legacy claim-token file — then
runs the upgrade-boot sequence (migrate → seed → rebuild) and asserts everything is captured into the
DB, the primary/cert layout is preserved, and the ``zone_domain`` line is scrubbed from config.toml."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from compute_space.config import load_config
from compute_space.core import system_agent
from compute_space.core.domain_store import load_records
from compute_space.core.domain_store import rebuild_active_domains
from compute_space.core.first_boot import seed_first_boot
from compute_space.core.settings_store import CLAIM_TOKEN_KEY
from compute_space.core.settings_store import get_setting
from compute_space.db.connection import init_db
from compute_space.db.schema import schema_path
from openhost_system_agent.config_edit import scrub_zone_domain


@pytest.fixture(autouse=True)
def _stub_agent_scrub(monkeypatch: pytest.MonkeyPatch) -> None:
    """The config scrub delegates to the system agent (unavailable in tests); stub it by default."""
    monkeypatch.setattr(system_agent, "scrub_config_zone_domain", lambda: None)


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
        "# a hand-added secondary public domain\n"
        "[[openhost.domains]]\n"
        'name = "secondary.example.com"\n'
        "tls = true\n"
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


def test_upgrade_captures_all_domains_and_claim_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, config_toml = _old_instance(tmp_path, monkeypatch, owner=False)
    # Simulate the system agent performing the scrub (it's the privileged writer in production).
    monkeypatch.setattr(system_agent, "scrub_config_zone_domain", lambda: scrub_zone_domain(str(config_toml)))

    # Upgrade boot: migrate the DB, then seed + rebuild.
    init_db(config.db_path)  # v12 -> v13
    seed_first_boot(config)
    new_config = rebuild_active_domains(config)

    by_name = {r.name: r for r in load_records(config)}
    # zone_domain (primary) + [[openhost.domains]] captured into the DB.
    assert set(by_name) == {"host.example.com", "secondary.example.com"}
    assert by_name["host.example.com"].is_primary is True
    assert by_name["secondary.example.com"].is_primary is False

    # Claim token migrated off the file (token before ':' only) into settings.
    assert get_setting(config, CLAIM_TOKEN_KEY) == "old-claim-tok"

    # Runtime now sources everything from the DB primary; cert layout unchanged for the primary.
    assert new_config.primary_domain.name == "host.example.com"
    assert new_config.zone_domain == "host.example.com"  # derived from the primary
    assert new_config.cert_path_for("host.example.com") == new_config.tls_cert_path  # legacy path kept
    assert new_config.cert_path_for("secondary.example.com") == new_config.certs_dir / "secondary.example.com.pem"

    # The captured zone_domain line is scrubbed; the rest of config.toml is preserved and still loads.
    text = config_toml.read_text()
    assert "zone_domain" not in text
    assert "[[openhost.domains]]" in text and 'host = "127.0.0.1"' in text


def test_upgrade_is_idempotent_across_restarts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An instance that already completed setup (owner exists): the claim-token file is normally gone,
    # but even if present, a second boot must not re-seed or duplicate anything.
    config, config_toml = _old_instance(tmp_path, monkeypatch, owner=True)
    monkeypatch.setattr(system_agent, "scrub_config_zone_domain", lambda: scrub_zone_domain(str(config_toml)))
    init_db(config.db_path)
    seed_first_boot(config)
    first = {r.name for r in load_records(config)}

    # Second boot: config.toml is already scrubbed (zone_domain gone) and the DB is authoritative.
    reloaded = load_config()
    assert reloaded.zone_domain == ""  # scrubbed
    seed_first_boot(reloaded)
    rebuild_active_domains(reloaded)

    assert {r.name for r in load_records(config)} == first  # unchanged, no duplicates
    assert reloaded_primary(config) == "host.example.com"


def reloaded_primary(config) -> str:  # type: ignore[no-untyped-def]
    return next(r.name for r in load_records(config) if r.is_primary)
