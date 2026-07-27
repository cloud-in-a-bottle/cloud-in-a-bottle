import datetime
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from compute_space.config import Config
from compute_space.config import DefaultConfig
from compute_space.config import Domain
from compute_space.core.domain_store import get_record
from compute_space.core.domain_store import seed_domains
from compute_space.core.tls.renewal import RENEW_BEFORE
from compute_space.core.tls.renewal import CertStatus
from compute_space.core.tls.renewal import _sync_cert_statuses
from compute_space.core.tls.renewal import get_cert_status
from compute_space.core.tls.renewal import renew_cert_if_needed
from compute_space.db.versioned import apply_migrations
from compute_space.tests.conftest import _make_test_config

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


def _config(tmp_path: Path) -> Config:
    config = DefaultConfig(
        zone_domain="test.example.com",
        data_root_dir=str(tmp_path),
        domains=(Domain("test.example.com", tls=True),),
    )
    config.openhost_data_path.mkdir(parents=True)
    return config


def test_renew_skips_valid_cert(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_self_signed_cert(
        config.tls_cert_path, config.tls_key_path, datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=60)
    )
    calls: list[str] = []
    renewed = renew_cert_if_needed(
        config, lambda: calls.append("restart"), provision=lambda c: calls.append("provision")
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
        config, lambda: calls.append("restart"), provision=lambda c: calls.append("provision")
    )
    assert renewed is True
    assert calls == ["provision", "restart"]


def test_renew_failure_does_not_restart_caddy(tmp_path: Path) -> None:
    config = _config(tmp_path)
    calls: list[str] = []

    def _failing_provision(config: Config) -> None:
        raise RuntimeError("ACME is down")

    with pytest.raises(RuntimeError, match="ACME is down"):
        renew_cert_if_needed(config, lambda: calls.append("restart"), provision=_failing_provision)
    assert calls == []


def _multidomain_config(tmp_path: Path, *secondaries: str) -> Config:
    config = DefaultConfig(
        zone_domain="test.example.com",
        data_root_dir=str(tmp_path),
        tls_enabled=True,
        domains=(Domain("test.example.com", tls=True), *(Domain(s, tls=True) for s in secondaries)),
    )
    config.openhost_data_path.mkdir(parents=True)
    # Primary cert valid so only the secondaries drive behavior.
    _write_self_signed_cert(
        config.tls_cert_path, config.tls_key_path, datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=60)
    )
    return config


def test_renew_acquires_stale_secondary_domain(tmp_path: Path) -> None:
    # A secondary TLS domain with no cert on disk must be acquired to its per-domain path, and
    # Caddy restarted — without touching the (valid) primary.
    config = _multidomain_config(tmp_path, "second.example.com")
    calls: list[str] = []
    acquired: list[str] = []
    renewed = renew_cert_if_needed(
        config,
        lambda: calls.append("restart"),
        provision=lambda c: calls.append("provision"),
        acquire=lambda c, name, cp, kp: acquired.append(name),
    )
    assert renewed is True
    assert acquired == ["second.example.com"]
    assert calls == ["restart"]  # primary was OK, so provision was never called


def test_renew_isolates_a_failing_secondary(tmp_path: Path) -> None:
    # One secondary whose acquisition fails (e.g. DNS not delegated) must not block the others.
    config = _multidomain_config(tmp_path, "bad.example.com", "good.example.com")
    acquired: list[str] = []

    def _acquire(c: Config, name: str, cert_path: Path, key_path: Path) -> None:
        if name == "bad.example.com":
            raise RuntimeError("DNS not delegated")
        acquired.append(name)

    calls: list[str] = []
    renewed = renew_cert_if_needed(config, lambda: calls.append("restart"), provision=lambda c: None, acquire=_acquire)
    assert renewed is True
    assert acquired == ["good.example.com"]  # bad one failed but didn't abort the loop
    assert calls == ["restart"]


def test_sync_cert_statuses_marks_primary_active_when_cert_present(tmp_path: Path) -> None:
    # The primary is seeded 'none' (its legacy cert predates the domains table); once its cert is on
    # disk, the boot-time sync reconciles the stored column to 'active' to match the dashboard.
    cfg = _make_test_config(tmp_path, zone_domain="host.example.com", tls_enabled=True)
    apply_migrations(cfg.db_path)
    seed_domains(cfg, Domain("host.example.com", tls=True), [])
    assert get_record(cfg, "host.example.com").cert_status == "none"

    # far-future not_valid_after => OK against real 'now' (the sync doesn't inject a clock)
    _write_self_signed_cert(cfg.tls_cert_path, cfg.tls_key_path, datetime.datetime(2100, 1, 1, tzinfo=datetime.UTC))
    _sync_cert_statuses(cfg)

    assert get_record(cfg, "host.example.com").cert_status == "active"


def test_sync_cert_statuses_leaves_domain_without_cert_alone(tmp_path: Path) -> None:
    # No cert on disk => the stored status is not touched (stays 'none' for the add-flow to drive).
    cfg = _make_test_config(tmp_path, zone_domain="host.example.com", tls_enabled=True)
    apply_migrations(cfg.db_path)
    seed_domains(cfg, Domain("host.example.com", tls=True), [])

    _sync_cert_statuses(cfg)

    assert get_record(cfg, "host.example.com").cert_status == "none"
