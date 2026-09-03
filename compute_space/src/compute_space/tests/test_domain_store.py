"""The DB-backed domain store (config/domains consolidation): CRUD against the ``domains`` table,
the effective set, the live ``primary_domain`` accessor, and first-boot seeding.  Also the per-domain
``ensure_cert_for`` acquisition wrapper (ACME stubbed)."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from compute_space.core.domains import Domain
from compute_space.core.domains import DomainCertStatus
from compute_space.core.domains import DomainRecord
from compute_space.core.domains import domain_uses_legacy_cert_paths
from compute_space.core.domains import effective_domains
from compute_space.core.domains import get_record
from compute_space.core.domains import load_records
from compute_space.core.domains import primary_domain
from compute_space.core.domains import remove_record
from compute_space.core.domains import seed_domains
from compute_space.core.domains import set_record_status
from compute_space.core.domains import upsert_record
from compute_space.core.settings_store import LEGACY_CERT_DOMAIN_KEY
from compute_space.core.tls import domain_certs
from compute_space.db.versioned import apply_migrations
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db

PRIMARY = Domain("host.example.com", tls=True)


def _cfg(tmp_path: Path):  # type: ignore[no-untyped-def]
    # seed_primary=False: these tests drive the store directly and seed their own rows, so start from
    # an empty (migrated) domains table.
    cfg = _make_test_config(tmp_path, seed_primary=False)
    apply_migrations(cfg.db_path)  # create the domains/settings tables
    return cfg


# --- CRUD round-trips -------------------------------------------------------------


def test_records_round_trip_through_db(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as db:
        assert load_records(db) == ()
        upsert_record(db, DomainRecord("myhost.local", tls=False, mdns=True, cert_status=DomainCertStatus.ACTIVE))
        upsert_record(
            db, DomainRecord("host.example.org", tls=True, mdns=False, cert_status=DomainCertStatus.ACQUIRING)
        )
        by_name = {r.name: r for r in load_records(db)}
    assert set(by_name) == {"myhost.local", "host.example.org"}
    assert by_name["myhost.local"].mdns is True and by_name["myhost.local"].tls is False
    assert by_name["host.example.org"].cert_status == DomainCertStatus.ACQUIRING


def test_upsert_replaces_same_name(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as db:
        upsert_record(db, DomainRecord("host.example.org", tls=True, mdns=False))
        set_record_status(db, "host.example.org", DomainCertStatus.ACTIVE)
        recs = load_records(db)
    assert len(recs) == 1 and recs[0].cert_status == DomainCertStatus.ACTIVE


def test_remove_record(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as db:
        upsert_record(db, DomainRecord("host.example.org", tls=True, mdns=False))
        assert remove_record(db, "host.example.org") is True
        assert remove_record(db, "host.example.org") is False
        assert load_records(db) == ()


def test_get_record_is_case_insensitive(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as db:
        upsert_record(db, DomainRecord("host.example.org", tls=True, mdns=False))
        assert get_record(db, "HOST.EXAMPLE.ORG") is not None
        assert get_record(db, "missing.example.org") is None


# --- effective set (primary first) + live primary lookup --------------------------


def test_effective_domains_primary_first(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as db:
        seed_domains(db, PRIMARY, [])  # seeds host.example.com as primary
        upsert_record(db, DomainRecord("myhost.local", tls=False, mdns=True))
        eff = effective_domains(db)
    assert [d.name for d in eff] == ["host.example.com", "myhost.local"]  # primary first


def test_primary_domain_and_matching(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as db:
        seed_domains(db, PRIMARY, [])
        upsert_record(db, DomainRecord("myhost.local", tls=False, mdns=True))
        # the .local domain is routable via the DB-backed resolver
        matched = Domain.match(db, "app.myhost.local")
        assert matched is not None and matched.mdns is True
        # the primary is read live from the DB
        assert primary_domain(db).name == "host.example.com"


def test_primary_domain_reflects_seed(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as db:
        seed_domains(db, Domain("real.example.com", tls=True), [])
        primary = primary_domain(db)
    assert primary.name == "real.example.com"
    assert primary.tls is True


# --- seeding (first-boot) ---------------------------------------------------------


def test_seed_populates_primary_once(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as db:
        seed_domains(db, PRIMARY, [])
        recs = load_records(db)
        assert len(recs) == 1 and recs[0].name == "host.example.com" and recs[0].is_primary is True
        # idempotent: a second call must not duplicate or overwrite
        upsert_record(db, DomainRecord("myhost.local", tls=False, mdns=True))
        seed_domains(db, PRIMARY, [])
        assert {r.name for r in load_records(db)} == {"host.example.com", "myhost.local"}


# --- ensure_cert_for: no-op for mDNS, acquires for TLS ----------------------------


@pytest.mark.asyncio
async def test_ensure_cert_for_noop_on_mdns(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    called = []
    monkeypatch.setattr(domain_certs, "acquire_cert_for_domain", lambda *a, **k: called.append(a))
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as db:
        await domain_certs.ensure_cert_for(cfg, Domain("myhost.local", tls=False, mdns=True), db, None)
    assert called == []  # mDNS never touches ACME


@pytest.mark.asyncio
async def test_ensure_cert_for_acquires_tls_to_per_domain_path(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured = {}

    async def fake_acquire(config, domain, cert_path, key_path, db, dns_provider):  # type: ignore[no-untyped-def]
        captured["domain"] = domain
        captured["cert_path"] = cert_path

    monkeypatch.setattr(domain_certs, "acquire_cert_for_domain", fake_acquire)
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as db:
        seed_domains(db, PRIMARY, [])  # host.example.com is the primary; host.example.org is not
        await domain_certs.ensure_cert_for(cfg, Domain("host.example.org", tls=True), db, None)
    assert captured["domain"] == "host.example.org"
    # per-domain path under certs/, NOT the primary's legacy cert file
    assert captured["cert_path"] == cfg.certs_dir / "host.example.org.pem"
    assert captured["cert_path"].parent.exists()  # ensure_cert_for created certs/


def test_certificate_paths_stay_with_original_domain_after_role_change(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as db:
        seed_domains(db, PRIMARY, [])
        upsert_record(db, DomainRecord("host.example.org", tls=True, mdns=False))
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (LEGACY_CERT_DOMAIN_KEY, PRIMARY.name))
        db.execute("UPDATE domains SET is_primary = 0 WHERE name = ?", (PRIMARY.name,))
        db.execute("UPDATE domains SET is_primary = 1 WHERE name = 'host.example.org'")
        db.commit()

        assert domain_uses_legacy_cert_paths(db, PRIMARY.name)
        assert cfg.cert_key_paths_for(db, PRIMARY.name) == (cfg.tls_cert_path, cfg.tls_key_path)
        assert cfg.cert_key_paths_for(db, "host.example.org") == (
            cfg.certs_dir / "host.example.org.pem",
            cfg.certs_dir / "host.example.org.key",
        )
