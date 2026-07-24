"""First-boot seeding: `first_boot.toml` vs the legacy (config-file + claim-token file) path, and
that the seeded primary drives the effective set."""

from __future__ import annotations

from pathlib import Path

import pytest

from compute_space.config import Domain
from compute_space.core import system_agent
from compute_space.core.domain_store import load_records
from compute_space.core.domain_store import seed_domains
from compute_space.core.first_boot import read_first_boot
from compute_space.core.first_boot import seed_first_boot
from compute_space.core.settings_store import CLAIM_TOKEN_KEY
from compute_space.core.settings_store import get_setting
from compute_space.db.versioned import apply_migrations
from compute_space.tests.conftest import _make_test_config
from openhost_system_agent.config_edit import scrub_zone_domain


@pytest.fixture(autouse=True)
def _stub_agent_scrub(monkeypatch: pytest.MonkeyPatch) -> None:
    """The config scrub delegates to ``sudo openhost_system_agent``; stub it so tests never shell out."""
    monkeypatch.setattr(system_agent, "scrub_config_zone_domain", lambda: None)


def _cfg(tmp_path: Path):  # type: ignore[no-untyped-def]
    cfg = _make_test_config(tmp_path, zone_domain="config-domain.example.com", tls_enabled=True)
    apply_migrations(cfg.db_path)
    return cfg


def _point_config_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point OPENHOST_ROUTER_CONFIG at a config.toml in tmp_path so first_boot.toml is looked up
    beside it; returns the config dir."""
    config_toml = tmp_path / "config.toml"
    config_toml.write_text("")  # only its directory matters
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", str(config_toml))
    return tmp_path


def test_read_first_boot_none_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENHOST_ROUTER_CONFIG", raising=False)
    monkeypatch.delenv("OPENHOST_CONFIG", raising=False)
    assert read_first_boot() is None


def test_seed_prefers_first_boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(tmp_path)
    config_dir = _point_config_env(monkeypatch, tmp_path)
    (config_dir / "first_boot.toml").write_text('domain = "fresh.example.com"\nclaim_token = "tok-xyz"\n')

    seed_first_boot(cfg)

    recs = load_records(cfg)
    # The first_boot domain becomes the primary — NOT the config.toml zone_domain.
    assert [r.name for r in recs] == ["fresh.example.com"]
    assert recs[0].is_primary is True and recs[0].tls is True  # public TLS by default
    assert get_setting(cfg, CLAIM_TOKEN_KEY) == "tok-xyz"


def test_seed_first_boot_local_mdns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(tmp_path)
    config_dir = _point_config_env(monkeypatch, tmp_path)
    (config_dir / "first_boot.toml").write_text('domain = "myhost.local"\ntls = false\nmdns = true\n')

    seed_first_boot(cfg)
    recs = load_records(cfg)
    assert recs[0].name == "myhost.local" and recs[0].tls is False and recs[0].mdns is True


def test_seed_falls_back_to_config_and_legacy_claim_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No first_boot.toml → seed the primary from config.toml zone_domain, and the claim token from
    # its legacy file (upgrade path).  The file may hold `token:extra`.
    cfg = _cfg(tmp_path)
    _point_config_env(monkeypatch, tmp_path)  # dir has no first_boot.toml
    Path(cfg.claim_token_path).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.claim_token_path).write_text("legacy-tok:some-extra\n")

    seed_first_boot(cfg)

    assert [r.name for r in load_records(cfg)] == ["config-domain.example.com"]
    assert get_setting(cfg, CLAIM_TOKEN_KEY) == "legacy-tok"  # extra after ':' stripped


def test_seed_delegates_scrub_to_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # After capturing zone_domain into the DB, the router asks the system agent to scrub the line.
    # Simulate the agent's edit so the end-to-end result is covered (only that line removed).
    cfg = _cfg(tmp_path)
    config_toml = tmp_path / "config.toml"
    config_toml.write_text(
        '[openhost]\nzone_domain = "config-domain.example.com"\nhost = "127.0.0.1"\ntls_enabled = true\n'
    )
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", str(config_toml))
    calls: list[int] = []

    def fake_scrub() -> None:
        calls.append(1)
        scrub_zone_domain(str(config_toml))  # what the agent does, as root, in prod

    monkeypatch.setattr(system_agent, "scrub_config_zone_domain", fake_scrub)

    seed_first_boot(cfg)

    assert calls == [1]  # router delegated the scrub to the agent, once, on first boot
    text = config_toml.read_text()
    assert "zone_domain" not in text
    assert 'host = "127.0.0.1"' in text and "tls_enabled = true" in text  # other lines preserved
    assert [r.name for r in load_records(cfg)] == ["config-domain.example.com"]


def test_scrub_only_delegated_on_first_boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(tmp_path)
    _point_config_env(monkeypatch, tmp_path)
    calls: list[int] = []
    monkeypatch.setattr(system_agent, "scrub_config_zone_domain", lambda: calls.append(1))
    seed_first_boot(cfg)  # first boot: seeded -> scrub delegated
    seed_first_boot(cfg)  # second boot: already seeded -> no scrub
    assert calls == [1]


def test_claim_token_migrates_even_when_domains_already_seeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Intermediate-build upgrade: the domain set was seeded by an earlier build (seed_domains
    # no-ops now), no owner exists yet, and the claim token still sits in its legacy file.  It must
    # still be migrated into settings so /setup can authenticate.
    cfg = _cfg(tmp_path)
    _point_config_env(monkeypatch, tmp_path)
    seed_domains(cfg, Domain("host.example.com", tls=True), [])  # pre-seed → domain seed will no-op
    Path(cfg.claim_token_path).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.claim_token_path).write_text("intermediate-tok")

    seed_first_boot(cfg)

    assert get_setting(cfg, CLAIM_TOKEN_KEY) == "intermediate-tok"


def test_seed_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(tmp_path)
    config_dir = _point_config_env(monkeypatch, tmp_path)
    (config_dir / "first_boot.toml").write_text('domain = "fresh.example.com"\nclaim_token = "tok"\n')
    seed_first_boot(cfg)
    # A second boot must not re-seed or duplicate, even if the file changes.
    (config_dir / "first_boot.toml").write_text('domain = "other.example.com"\nclaim_token = "tok2"\n')
    seed_first_boot(cfg)
    assert [r.name for r in load_records(cfg)] == ["fresh.example.com"]
