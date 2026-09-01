"""The DB-backed domain store: CRUD, atomic primary changes, first-boot seeding, and per-domain
certificate acquisition."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from compute_space.core.domains import Domain
from compute_space.core.domains import DomainCertStatus
from compute_space.core.domains import DomainNotFoundError
from compute_space.core.domains import DomainRecord
from compute_space.core.domains import PrimaryDomainChangedError
from compute_space.core.domains import domain_uses_legacy_paths
from compute_space.core.domains import effective_domains
from compute_space.core.domains import get_record
from compute_space.core.domains import legacy_domain_asset_owner
from compute_space.core.domains import load_records
from compute_space.core.domains import primary_domain
from compute_space.core.domains import remove_non_primary_record
from compute_space.core.domains import remove_record
from compute_space.core.domains import seed_domains
from compute_space.core.domains import set_primary_domain
from compute_space.core.domains import set_record_status
from compute_space.core.domains import upsert_record
from compute_space.core.tls import domain_certs
from compute_space.core.tls import provision as tls_provision
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


def test_get_record_ignores_port(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as db:
        seed_domains(db, Domain("host.example.org:8080", tls=False), [])
        assert get_record(db, "host.example.org") is not None
        assert get_record(db, "host.example.org:9090") is not None


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


def test_set_primary_domain_preserves_legacy_asset_owner(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as db:
        seed_domains(db, PRIMARY, [])
        upsert_record(db, DomainRecord("myhost.local", tls=False, mdns=True))

        assert legacy_domain_asset_owner(db) == "host.example.com"
        assert set_primary_domain(db, "myhost.local", expected_primary="host.example.com") is True
        assert primary_domain(db).name == "myhost.local"
        assert legacy_domain_asset_owner(db) == "host.example.com"
        assert domain_uses_legacy_paths(db, "host.example.com") is True
        assert domain_uses_legacy_paths(db, "myhost.local") is False

        # Repeating a completed request is idempotent, even if its expected-primary value is stale.
        assert set_primary_domain(db, "myhost.local", expected_primary="host.example.com") is False


def test_set_primary_domain_rejects_stale_and_unknown_targets(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as db:
        seed_domains(db, PRIMARY, [])
        upsert_record(db, DomainRecord("one.local", tls=False, mdns=True))
        upsert_record(db, DomainRecord("two.local", tls=False, mdns=True))
        set_primary_domain(db, "one.local", expected_primary="host.example.com")

        with pytest.raises(PrimaryDomainChangedError, match="primary domain is now one.local"):
            set_primary_domain(db, "two.local", expected_primary="host.example.com")
        with pytest.raises(DomainNotFoundError):
            set_primary_domain(db, "missing.local", expected_primary="one.local")

        assert primary_domain(db).name == "one.local"
        assert legacy_domain_asset_owner(db) == "host.example.com"


def test_set_primary_domain_rolls_back_every_write_on_failure(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as db:
        seed_domains(db, PRIMARY, [])
        upsert_record(db, DomainRecord("myhost.local", tls=False, mdns=True))
        db.execute(
            "CREATE TRIGGER reject_promotion BEFORE UPDATE OF is_primary ON domains "
            "WHEN NEW.name = 'myhost.local' AND NEW.is_primary = 1 "
            "BEGIN SELECT RAISE(ABORT, 'reject promotion'); END"
        )

        with pytest.raises(sqlite3.IntegrityError, match="reject promotion"):
            set_primary_domain(db, "myhost.local", expected_primary="host.example.com")

        assert primary_domain(db).name == "host.example.com"
        assert db.execute("SELECT value FROM settings WHERE key = 'legacy_domain_asset_owner'").fetchone() is None


def test_removal_cannot_delete_a_domain_promoted_after_lookup(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as setup_db:
        seed_domains(setup_db, PRIMARY, [])
        upsert_record(setup_db, DomainRecord("myhost.local", tls=False, mdns=True))

    with closing(open_db(cfg)) as removal_db, closing(open_db(cfg)) as promotion_db:
        record_selected_for_removal = get_record(removal_db, "myhost.local")
        assert record_selected_for_removal is not None
        set_primary_domain(promotion_db, "myhost.local", expected_primary="host.example.com")

        assert remove_non_primary_record(removal_db, record_selected_for_removal.name) is False
        promoted = get_record(removal_db, "myhost.local")
        assert promoted is not None and promoted.is_primary is True


def test_cert_and_zone_paths_do_not_move_with_primary(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as db:
        seed_domains(db, PRIMARY, [])
        upsert_record(db, DomainRecord("second.example.com", tls=True, mdns=False))
        before = {
            name: (
                cfg.cert_key_paths_for(db, name),
                cfg.coredns_zonefile_path_for(name, domain_uses_legacy_paths(db, name)),
            )
            for name in ("host.example.com", "second.example.com")
        }

        set_primary_domain(db, "second.example.com", expected_primary="host.example.com")

        after = {
            name: (
                cfg.cert_key_paths_for(db, name),
                cfg.coredns_zonefile_path_for(name, domain_uses_legacy_paths(db, name)),
            )
            for name in ("host.example.com", "second.example.com")
        }
        assert after == before


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
        await domain_certs.ensure_cert_for(cfg, Domain("myhost.local", tls=False, mdns=True), db)
    assert called == []  # mDNS never touches ACME


@pytest.mark.asyncio
async def test_ensure_cert_for_acquires_tls_to_per_domain_path(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured = {}

    async def fake_acquire(config, domain, cert_path, key_path, db):  # type: ignore[no-untyped-def]
        captured["domain"] = domain
        captured["cert_path"] = cert_path

    monkeypatch.setattr(domain_certs, "acquire_cert_for_domain", fake_acquire)
    cfg = _cfg(tmp_path)
    with closing(open_db(cfg)) as db:
        seed_domains(db, PRIMARY, [])  # host.example.com is the primary; host.example.org is not
        await domain_certs.ensure_cert_for(cfg, Domain("host.example.org", tls=True), db)
    assert captured["domain"] == "host.example.org"
    # per-domain path under certs/, NOT the primary's legacy cert file
    assert captured["cert_path"] == cfg.certs_dir / "host.example.org.pem"
    assert captured["cert_path"].parent.exists()  # ensure_cert_for created certs/


@pytest.mark.asyncio
async def test_promoted_primary_acme_uses_portless_domain_and_path(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    cfg = _cfg(tmp_path)
    captured = {}

    async def fake_acquire(config, domain, cert_path, key_path, db):  # type: ignore[no-untyped-def]
        captured["domain"] = domain
        captured["cert_path"] = cert_path

    monkeypatch.setattr(tls_provision, "acquire_cert_for_domain", fake_acquire)
    with closing(open_db(cfg)) as db:
        seed_domains(db, PRIMARY, [])
        upsert_record(db, DomainRecord("second.example.com:8443", tls=True, mdns=False))
        set_primary_domain(db, "second.example.com", expected_primary="host.example.com")
        await tls_provision.provision_cert(cfg, db)

    assert captured == {
        "domain": "second.example.com",
        "cert_path": cfg.certs_dir / "second.example.com.pem",
    }
    assert cfg.certs_dir.exists()
