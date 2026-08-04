import datetime
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from compute_space.config import Config
from compute_space.config import DefaultConfig
from compute_space.core.caddy import config_cert_resolver
from compute_space.core.caddy import generate_caddyfile
from compute_space.core.domains import Domain
from compute_space.core.domains import DomainRecord
from compute_space.core.domains import effective_domains
from compute_space.core.domains import get_record
from compute_space.core.domains import seed_domains
from compute_space.core.tls.renewal import RENEW_BEFORE
from compute_space.core.tls.renewal import CertStatus
from compute_space.core.tls.renewal import _sync_cert_statuses
from compute_space.core.tls.renewal import get_cert_status
from compute_space.core.tls.renewal import renew_cert_if_needed
from compute_space.db import init_db
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db

_NOW = datetime.datetime(2026, 7, 9, tzinfo=datetime.UTC)


def _write_self_signed_cert(cert_path: Path, key_path: Path, not_valid_after: datetime.datetime) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "test.example.com")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_valid_after - datetime.timedelta(days=90))
        .not_valid_after(not_valid_after)
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )


def test_status_missing_when_no_files(tmp_path: Path) -> None:
    assert get_cert_status(tmp_path / "cert.pem", tmp_path / "key.pem", now=_NOW) == CertStatus.MISSING


def test_status_missing_when_key_absent(tmp_path: Path) -> None:
    cert_path = tmp_path / "cert.pem"
    _write_self_signed_cert(cert_path, tmp_path / "elsewhere.pem", _NOW + datetime.timedelta(days=60))
    assert get_cert_status(cert_path, tmp_path / "key.pem", now=_NOW) == CertStatus.MISSING


def test_status_expired(tmp_path: Path) -> None:
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    _write_self_signed_cert(cert_path, key_path, _NOW - datetime.timedelta(days=1))
    assert get_cert_status(cert_path, key_path, now=_NOW) == CertStatus.EXPIRED


def test_status_expiring_soon(tmp_path: Path) -> None:
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    _write_self_signed_cert(cert_path, key_path, _NOW + RENEW_BEFORE - datetime.timedelta(days=1))
    assert get_cert_status(cert_path, key_path, now=_NOW) == CertStatus.EXPIRING_SOON


def test_status_ok_when_outside_renewal_window(tmp_path: Path) -> None:
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    _write_self_signed_cert(cert_path, key_path, _NOW + RENEW_BEFORE + datetime.timedelta(days=1))
    assert get_cert_status(cert_path, key_path, now=_NOW) == CertStatus.OK


def test_status_unparseable_cert_treated_as_expired(tmp_path: Path) -> None:
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_text("not a certificate")
    key_path.write_text("not a key")
    assert get_cert_status(cert_path, key_path, now=_NOW) == CertStatus.EXPIRED


def _seed_cfg(config: Config, *domains: Domain) -> None:
    """Migrate the config's DB and seed ``domains`` (primary first) so ``renew_cert_if_needed`` —
    which reads the domain set and the primary from the DB — sees them."""
    init_db(config.db_path)
    with closing(open_db(config)) as db:
        seed_domains(db, domains[0], [DomainRecord(d.name, d.tls) for d in domains[1:]])


def _config(tmp_path: Path) -> Config:
    config = DefaultConfig(data_root_dir=str(tmp_path))
    config.openhost_data_path.mkdir(parents=True)
    _seed_cfg(config, Domain("test.example.com", tls=True))
    return config


def test_renew_skips_valid_cert(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_self_signed_cert(
        config.tls_cert_path, config.tls_key_path, datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=60)
    )
    calls: list[str] = []
    renewed = renew_cert_if_needed(
        config, lambda c, db: calls.append("restart"), provision=lambda c, db: calls.append("provision")
    )
    assert renewed is False
    assert calls == []


@pytest.mark.parametrize(
    "expires_in", [datetime.timedelta(days=-1), RENEW_BEFORE - datetime.timedelta(days=1)], ids=["expired", "expiring"]
)
def test_renew_provisions_and_restarts_caddy(tmp_path: Path, expires_in: datetime.timedelta) -> None:
    config = _config(tmp_path)
    _write_self_signed_cert(
        config.tls_cert_path,
        config.tls_key_path,
        datetime.datetime.now(datetime.UTC) + expires_in,
    )
    calls: list[str] = []
    renewed = renew_cert_if_needed(
        config, lambda c, db: calls.append("restart"), provision=lambda c, db: calls.append("provision")
    )
    assert renewed is True
    assert calls == ["provision", "restart"]


def test_renew_failure_does_not_restart_caddy(tmp_path: Path) -> None:
    config = _config(tmp_path)
    calls: list[str] = []

    def _failing_provision(config: Config, db: sqlite3.Connection) -> None:
        raise RuntimeError("ACME is down")

    with pytest.raises(RuntimeError, match="ACME is down"):
        renew_cert_if_needed(config, lambda c, db: calls.append("restart"), provision=_failing_provision)
    assert calls == []


def _multidomain_config(tmp_path: Path, *secondaries: str) -> Config:
    config = DefaultConfig(data_root_dir=str(tmp_path))
    config.openhost_data_path.mkdir(parents=True)
    # Primary cert valid so only the secondaries drive behavior.
    _write_self_signed_cert(
        config.tls_cert_path, config.tls_key_path, datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=60)
    )
    _seed_cfg(config, Domain("test.example.com", tls=True), *(Domain(s, tls=True) for s in secondaries))
    return config


def test_renew_acquires_stale_secondary_domain(tmp_path: Path) -> None:
    # A secondary TLS domain with no cert on disk must be acquired to its per-domain path, and
    # Caddy restarted — without touching the (valid) primary.
    config = _multidomain_config(tmp_path, "second.example.com")
    calls: list[str] = []
    acquired: list[str] = []
    renewed = renew_cert_if_needed(
        config,
        lambda c, db: calls.append("restart"),
        provision=lambda c, db: calls.append("provision"),
        acquire=lambda c, name, cp, kp, db: acquired.append(name),
    )
    assert renewed is True
    assert acquired == ["second.example.com"]
    assert calls == ["restart"]  # primary was OK, so provision was never called


def test_renew_acquires_secondary_under_non_tls_primary(tmp_path: Path) -> None:
    # A non-TLS (.local) primary with a public TLS secondary: the primary has no cert to provision,
    # but the secondary must still be acquired and Caddy restarted (the renewal thread now runs
    # whenever any domain needs TLS, and the primary block is skipped for a non-TLS primary).
    config = DefaultConfig(data_root_dir=str(tmp_path))
    config.openhost_data_path.mkdir(parents=True)
    _seed_cfg(config, Domain("host.local", tls=False), Domain("public.example.com", tls=True))
    calls: list[str] = []
    acquired: list[str] = []
    renewed = renew_cert_if_needed(
        config,
        lambda c, db: calls.append("restart"),
        provision=lambda c, db: calls.append("provision"),
        acquire=lambda c, name, cp, kp, db: acquired.append(name),
    )
    assert renewed is True
    assert acquired == ["public.example.com"]
    assert calls == ["restart"]  # primary is non-TLS → provision never called


def test_renew_isolates_a_failing_secondary(tmp_path: Path) -> None:
    # One secondary whose acquisition fails (e.g. DNS not delegated) must not block the others.
    config = _multidomain_config(tmp_path, "bad.example.com", "good.example.com")
    acquired: list[str] = []

    def _acquire(c: Config, name: str, cert_path: Path, key_path: Path, db: sqlite3.Connection) -> None:
        if name == "bad.example.com":
            raise RuntimeError("DNS not delegated")
        acquired.append(name)

    calls: list[str] = []
    renewed = renew_cert_if_needed(
        config, lambda c, db: calls.append("restart"), provision=lambda c, db: None, acquire=_acquire
    )
    assert renewed is True
    assert acquired == ["good.example.com"]  # bad one failed but didn't abort the loop
    assert calls == ["restart"]


def test_renew_reload_regenerates_caddyfile_for_new_secondary_cert(tmp_path: Path) -> None:
    # Regression: after a secondary cert is acquired, the reload must *regenerate* the Caddyfile so
    # the domain's `tls internal` fallback block is rewritten to point at the acquired cert.  A bare
    # restart re-read the stale Caddyfile and left the domain on Caddy's self-signed cert forever.
    config = _multidomain_config(tmp_path, "second.example.com")

    def _acquire(c: Config, name: str, cert_path: Path, key_path: Path, db: sqlite3.Connection) -> None:
        cert_path.write_text("cert")  # generate_caddyfile only checks the files exist, not validity
        key_path.write_text("key")

    caddyfile = tmp_path / "Caddyfile"

    def _reload(c: Config, db: sqlite3.Connection) -> None:
        caddyfile.write_text(generate_caddyfile(effective_domains(db), c.port, config_cert_resolver(c, db)))

    renewed = renew_cert_if_needed(config, _reload, provision=lambda c, db: None, acquire=_acquire)
    assert renewed is True
    content = caddyfile.read_text()
    # The secondary now serves its acquired file cert rather than falling back to `tls internal`.
    assert str(config.cert_path_for("second.example.com", is_primary=False)) in content
    assert "tls internal" not in content


def test_sync_cert_statuses_marks_primary_active_when_cert_present(tmp_path: Path) -> None:
    # The primary is seeded 'none' (its legacy cert predates the domains table); once its cert is on
    # disk, the boot-time sync reconciles the stored column to 'active' to match the dashboard.
    cfg = _make_test_config(tmp_path, zone_domain="host.example.com", tls_enabled=True)
    init_db(cfg.db_path)
    with closing(open_db(cfg)) as db:
        seed_domains(db, Domain("host.example.com", tls=True), [])
        assert get_record(db, "host.example.com").cert_status == "none"

    # far-future not_valid_after => OK against real 'now' (the sync doesn't inject a clock)
    _write_self_signed_cert(cfg.tls_cert_path, cfg.tls_key_path, datetime.datetime(2100, 1, 1, tzinfo=datetime.UTC))
    _sync_cert_statuses(cfg)

    with closing(open_db(cfg)) as db:
        assert get_record(db, "host.example.com").cert_status == "active"


def test_sync_cert_statuses_leaves_domain_without_cert_alone(tmp_path: Path) -> None:
    # No cert on disk => the stored status is not touched (stays 'none' for the add-flow to drive).
    cfg = _make_test_config(tmp_path, zone_domain="host.example.com", tls_enabled=True)
    init_db(cfg.db_path)
    with closing(open_db(cfg)) as db:
        seed_domains(db, Domain("host.example.com", tls=True), [])

    _sync_cert_statuses(cfg)

    with closing(open_db(cfg)) as db:
        assert get_record(db, "host.example.com").cert_status == "none"


def test_renew_marks_primary_active_same_cycle(tmp_path: Path) -> None:
    # A successful primary renewal flips the DB status to 'active' in the same cycle — not only on the
    # next cycle's _sync_cert_statuses, which ran (at the top) before the new cert existed.
    cfg = _make_test_config(tmp_path, zone_domain="host.example.com", tls_enabled=True)
    init_db(cfg.db_path)
    with closing(open_db(cfg)) as db:
        seed_domains(db, Domain("host.example.com", tls=True), [])
    # Expired cert: the top-of-cycle sync sees it non-OK and leaves the record 'none'; the primary
    # block then renews, so only the post-renewal mark can produce 'active'.
    _write_self_signed_cert(
        cfg.tls_cert_path, cfg.tls_key_path, datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)
    )

    def _provision(c: Config, db: sqlite3.Connection) -> None:
        _write_self_signed_cert(c.tls_cert_path, c.tls_key_path, datetime.datetime(2100, 1, 1, tzinfo=datetime.UTC))

    renewed = renew_cert_if_needed(cfg, lambda c, db: None, provision=_provision)
    assert renewed is True
    with closing(open_db(cfg)) as db:
        assert get_record(db, "host.example.com").cert_status == "active"
