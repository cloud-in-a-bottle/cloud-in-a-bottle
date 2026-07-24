"""The DB-backed domain store (config/domains consolidation): CRUD against the ``domains`` table,
the effective-set load into the active config, and first-boot/upgrade seeding.  Also the per-domain
``ensure_cert_for`` acquisition wrapper (ACME stubbed)."""

from __future__ import annotations

from pathlib import Path

from compute_space.config import Domain
from compute_space.config import get_config
from compute_space.core.domain_store import CERT_STATUS_ACQUIRING
from compute_space.core.domain_store import CERT_STATUS_ACTIVE
from compute_space.core.domain_store import DomainRecord
from compute_space.core.domain_store import effective_domains
from compute_space.core.domain_store import get_record
from compute_space.core.domain_store import load_records
from compute_space.core.domain_store import rebuild_active_domains
from compute_space.core.domain_store import remove_record
from compute_space.core.domain_store import seed_domains
from compute_space.core.domain_store import seed_domains_from_legacy
from compute_space.core.domain_store import set_record_status
from compute_space.core.domain_store import upsert_record
from compute_space.core.tls import domain_certs
from compute_space.db.versioned import apply_migrations
from compute_space.tests.conftest import _make_test_config

PRIMARY = Domain("host.example.com", tls=True)


def _cfg(tmp_path: Path):  # type: ignore[no-untyped-def]
    cfg = _make_test_config(tmp_path, zone_domain="host.example.com", tls_enabled=True, domains=(PRIMARY,))
    apply_migrations(cfg.db_path)  # create the domains/settings tables
    return cfg


# --- CRUD round-trips -------------------------------------------------------------


def test_records_round_trip_through_db(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert load_records(cfg) == ()
    upsert_record(cfg, DomainRecord("myhost.local", tls=False, mdns=True, cert_status=CERT_STATUS_ACTIVE))
    upsert_record(cfg, DomainRecord("host.example.org", tls=True, mdns=False, cert_status=CERT_STATUS_ACQUIRING))
    by_name = {r.name: r for r in load_records(cfg)}
    assert set(by_name) == {"myhost.local", "host.example.org"}
    assert by_name["myhost.local"].mdns is True and by_name["myhost.local"].tls is False
    assert by_name["host.example.org"].cert_status == CERT_STATUS_ACQUIRING


def test_upsert_replaces_same_name(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    upsert_record(cfg, DomainRecord("host.example.org", tls=True, mdns=False))
    set_record_status(cfg, "host.example.org", CERT_STATUS_ACTIVE)
    recs = load_records(cfg)
    assert len(recs) == 1 and recs[0].cert_status == CERT_STATUS_ACTIVE


def test_remove_record(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    upsert_record(cfg, DomainRecord("host.example.org", tls=True, mdns=False))
    assert remove_record(cfg, "host.example.org") is True
    assert remove_record(cfg, "host.example.org") is False
    assert load_records(cfg) == ()


def test_get_record_is_case_insensitive(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    upsert_record(cfg, DomainRecord("host.example.org", tls=True, mdns=False))
    assert get_record(cfg, "HOST.EXAMPLE.ORG") is not None
    assert get_record(cfg, "missing.example.org") is None


# --- effective set (primary first) + active-config swap ---------------------------


def test_effective_domains_primary_first(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    seed_domains_from_legacy(cfg)  # seeds host.example.com as primary
    upsert_record(cfg, DomainRecord("myhost.local", tls=False, mdns=True))
    eff = effective_domains(cfg)
    assert [d.name for d in eff] == ["host.example.com", "myhost.local"]  # primary first


def test_rebuild_active_domains_swaps_active_config(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    seed_domains_from_legacy(cfg)
    upsert_record(cfg, DomainRecord("myhost.local", tls=False, mdns=True))
    rebuild_active_domains(cfg)
    active = get_config()
    assert active.match_domain("app.myhost.local") is not None
    assert active.match_domain("app.myhost.local").mdns is True
    # the primary is preserved as domains[0]
    assert active.primary_domain.name == "host.example.com"


def test_rebuild_derives_zone_domain_and_tls_from_primary(tmp_path: Path) -> None:
    # The DB primary drives the legacy zone_domain / tls_enabled scalars (used by cert paths + the
    # OPENHOST_ZONE_DOMAIN handed to apps), so a primary seeded from first_boot takes effect there.
    cfg = _make_test_config(tmp_path, zone_domain="placeholder.example.com", tls_enabled=False)
    apply_migrations(cfg.db_path)
    seed_domains(cfg, Domain("real.example.com", tls=True), [])
    new_config = rebuild_active_domains(cfg)
    assert new_config.zone_domain == "real.example.com"
    assert new_config.tls_enabled is True


# --- seeding (first-boot + upgrade) -----------------------------------------------


def test_seed_populates_primary_once(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    seed_domains_from_legacy(cfg)
    recs = load_records(cfg)
    assert len(recs) == 1 and recs[0].name == "host.example.com" and recs[0].is_primary is True
    # idempotent: a second call must not duplicate or overwrite
    upsert_record(cfg, DomainRecord("myhost.local", tls=False, mdns=True))
    seed_domains_from_legacy(cfg)
    assert {r.name for r in load_records(cfg)} == {"host.example.com", "myhost.local"}


def test_seed_captures_config_domains_and_dedups_primary(tmp_path: Path) -> None:
    # An instance whose config.toml carries [[openhost.domains]]: the zone_domain primary + the extra
    # domains are all captured, and a config domain duplicating the primary isn't doubled.
    cfg = _make_test_config(
        tmp_path,
        zone_domain="host.example.com",
        tls_enabled=True,
        domains=(Domain("host.example.com", tls=True), Domain("extra.example.com", tls=True)),
    )
    apply_migrations(cfg.db_path)
    seed_domains_from_legacy(cfg)
    by_name = {r.name: r for r in load_records(cfg)}
    assert set(by_name) == {"host.example.com", "extra.example.com"}  # primary not doubled
    assert by_name["host.example.com"].is_primary is True
    assert by_name["extra.example.com"].is_primary is False


# --- ensure_cert_for: no-op for mDNS, acquires for TLS ----------------------------


def test_ensure_cert_for_noop_on_mdns(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    called = []
    monkeypatch.setattr(domain_certs, "acquire_cert_for_domain", lambda *a, **k: called.append(a))
    domain_certs.ensure_cert_for(_cfg(tmp_path), Domain("myhost.local", tls=False, mdns=True))
    assert called == []  # mDNS never touches ACME


def test_ensure_cert_for_acquires_tls_to_per_domain_path(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured = {}

    def fake_acquire(config, domain, cert_path, key_path):  # type: ignore[no-untyped-def]
        captured["domain"] = domain
        captured["cert_path"] = cert_path

    monkeypatch.setattr(domain_certs, "acquire_cert_for_domain", fake_acquire)
    cfg = _cfg(tmp_path)
    domain_certs.ensure_cert_for(cfg, Domain("host.example.org", tls=True))
    assert captured["domain"] == "host.example.org"
    # per-domain path under certs/, NOT the primary's legacy cert file
    assert captured["cert_path"] == cfg.certs_dir / "host.example.org.pem"
    assert captured["cert_path"].parent.exists()  # ensure_cert_for created certs/
