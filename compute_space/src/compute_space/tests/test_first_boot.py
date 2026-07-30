"""First-boot seeding: the `first_boot.toml` path (domain + claim token) and the legacy claim-token
file fallback, and that the seeded primary drives the effective set.  The domain is seeded only from
first_boot.toml — an old instance's config.toml domain is captured by a system-agent migration."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from compute_space.core.domains import Domain
from compute_space.core.domains import load_records
from compute_space.core.domains import seed_domains
from compute_space.core.first_boot import read_first_boot
from compute_space.core.first_boot import seed_first_boot
from compute_space.core.settings_store import CLAIM_TOKEN_KEY
from compute_space.core.settings_store import get_setting
from compute_space.db import init_db
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db


def _cfg(tmp_path: Path):  # type: ignore[no-untyped-def]
    # seed_primary=False: these tests exercise the first-boot seed itself, so start from an empty
    # (migrated) domains table.
    cfg = _make_test_config(tmp_path, seed_primary=False)
    init_db(cfg.db_path)  # migrate + point get_db() at this DB (seed_first_boot opens its own conn)
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

    with closing(open_db(cfg)) as db:
        recs = load_records(db)
        # The first_boot domain becomes the primary — NOT the config.toml zone_domain.
        assert [r.name for r in recs] == ["fresh.example.com"]
        assert recs[0].is_primary is True and recs[0].tls is True  # public TLS by default
        assert get_setting(db, CLAIM_TOKEN_KEY) == "tok-xyz"


def test_seed_first_boot_local_mdns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(tmp_path)
    config_dir = _point_config_env(monkeypatch, tmp_path)
    (config_dir / "first_boot.toml").write_text('domain = "myhost.local"\ntls = false\nmdns = true\n')

    seed_first_boot(cfg)
    with closing(open_db(cfg)) as db:
        recs = load_records(db)
    assert recs[0].name == "myhost.local" and recs[0].tls is False and recs[0].mdns is True


def test_seed_migrates_legacy_claim_file_without_first_boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No first_boot.toml → no domain is seeded at runtime (an old instance's config.toml domain is
    # captured by a system-agent migration), but the legacy claim-token file still migrates so /setup
    # can authenticate.  The file may hold `token:extra`.
    cfg = _cfg(tmp_path)
    _point_config_env(monkeypatch, tmp_path)  # dir has no first_boot.toml
    Path(cfg.claim_token_path).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.claim_token_path).write_text("legacy-tok:some-extra\n")

    seed_first_boot(cfg)

    with closing(open_db(cfg)) as db:
        assert load_records(db) == ()  # no domain seeded from config.toml at runtime
        assert get_setting(db, CLAIM_TOKEN_KEY) == "legacy-tok"  # extra after ':' stripped


def test_seed_does_not_touch_config_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The seed never edits config.toml, and it never reads a domain out of it.
    cfg = _cfg(tmp_path)
    config_toml = tmp_path / "config.toml"
    body = '[openhost]\nzone_domain = "config-domain.example.com"\nhost = "127.0.0.1"\ntls_enabled = true\n'
    config_toml.write_text(body)
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", str(config_toml))

    seed_first_boot(cfg)

    assert config_toml.read_text() == body  # untouched
    with closing(open_db(cfg)) as db:
        assert load_records(db) == ()  # config.toml zone_domain is NOT captured at runtime


def test_claim_token_migrates_even_when_domains_already_seeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Intermediate-build upgrade: the domain set was seeded by an earlier build (seed_domains
    # no-ops now), no owner exists yet, and the claim token still sits in its legacy file.  It must
    # still be migrated into settings so /setup can authenticate.
    cfg = _cfg(tmp_path)
    _point_config_env(monkeypatch, tmp_path)
    with closing(open_db(cfg)) as db:
        seed_domains(db, Domain("host.example.com", tls=True), [])  # pre-seed → domain seed will no-op
    Path(cfg.claim_token_path).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.claim_token_path).write_text("intermediate-tok")

    seed_first_boot(cfg)

    with closing(open_db(cfg)) as db:
        assert get_setting(db, CLAIM_TOKEN_KEY) == "intermediate-tok"


def test_seed_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(tmp_path)
    config_dir = _point_config_env(monkeypatch, tmp_path)
    (config_dir / "first_boot.toml").write_text('domain = "fresh.example.com"\nclaim_token = "tok"\n')
    seed_first_boot(cfg)
    # A second boot must not re-seed or duplicate, even if the file changes.
    (config_dir / "first_boot.toml").write_text('domain = "other.example.com"\nclaim_token = "tok2"\n')
    seed_first_boot(cfg)
    with closing(open_db(cfg)) as db:
        assert [r.name for r in load_records(db)] == ["fresh.example.com"]
