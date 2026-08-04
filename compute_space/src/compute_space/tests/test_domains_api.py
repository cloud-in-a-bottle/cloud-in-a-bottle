"""Phase 3b: the /api/domains endpoint — owner-authed add/list/remove of domains on a live
instance, with the TLS-domain acquisition state machine (acquiring → active|error).  ACME is
stubbed and acquisition is run synchronously so the state machine is deterministic; no Caddy
runs (reload is a no-op in tests)."""

from __future__ import annotations

import datetime
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import bcrypt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from litestar import Litestar
from litestar.di import Provide
from litestar.testing import TestClient

from compute_space.config import provide_config
from compute_space.core import caddy
from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import create_session
from compute_space.core.domains import Domain
from compute_space.core.domains import DomainCertStatus
from compute_space.core.domains import seed_domains
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db
from compute_space.web.routes.api import domains
from compute_space.web.routes.api.domains import api_domains_routes

PRIMARY = Domain("host.example.com", tls=True)


def _write_cert(cert_path: Path, key_path: Path, *, days_valid: int = 60) -> None:
    """Write a self-signed cert+key that get_cert_status can parse (expired when days_valid < 0)."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "host.example.com")])
    not_after = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=days_valid)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_after - datetime.timedelta(days=90))
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
        )
    )


def _make_app() -> Litestar:
    return Litestar(
        route_handlers=[api_domains_routes],
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        openapi_config=None,
    )


def _auth_cookie(db_path: str) -> dict[str, str]:
    pw = bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode()
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        uid = int(conn.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", ("owner", pw)).lastrowid)
        token = create_session(uid, conn)
        conn.commit()
    finally:
        conn.close()
    return {SESSION_COOKIE_NAME: token}


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    c = _make_test_config(tmp_path, zone_domain="host.example.com", tls_enabled=True, domains=(PRIMARY,))
    init_db(c.db_path)
    with closing(open_db(c)) as db:
        seed_domains(db, PRIMARY, [])  # seed the DB primary so add/remove rebuild from a real primary row
    caddy.set_active_caddy(None)  # no Caddy in tests → reload is a no-op
    return c


@pytest.fixture
def client(cfg: Any) -> Iterator[TestClient[Litestar]]:
    with TestClient(app=_make_app()) as c:
        yield c


@pytest.fixture
def sync_acquisition(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run cert acquisition synchronously so POST returns after the state machine settled."""
    monkeypatch.setattr(domains, "_spawn_acquisition", domains._run_acquisition)


# --- auth ---------------------------------------------------------------------------


def test_list_requires_auth(client: TestClient[Litestar]) -> None:
    assert client.get("/api/domains").status_code == 401


def test_list_shows_primary(cfg: Any, client: TestClient[Litestar]) -> None:
    resp = client.get("/api/domains", cookies=_auth_cookie(cfg.db_path))
    assert resp.status_code == 200
    domains = resp.json()["domains"]
    assert len(domains) == 1
    assert domains[0]["name"] == "host.example.com"
    assert domains[0]["is_primary"] is True
    assert domains[0]["scheme"] == "https"
    # No cert file on disk yet → status reflects that (not a stale 'active').
    assert domains[0]["cert_status"] == "none"


def test_primary_with_cert_on_disk_reports_active(cfg: Any, client: TestClient[Litestar]) -> None:
    # Regression: the seeded primary stores cert_status='none', but a valid cert on disk — which is
    # what Caddy actually serves — must report active, not 'none'.
    _write_cert(cfg.tls_cert_path, cfg.tls_key_path)
    info = next(d for d in client.get("/api/domains", cookies=_auth_cookie(cfg.db_path)).json()["domains"])
    assert info["name"] == "host.example.com" and info["cert_status"] == "active"


def test_primary_with_expired_cert_not_active(cfg: Any, client: TestClient[Litestar]) -> None:
    # An expired cert still has files on disk (so a mere existence check would say 'active'), but
    # browsers reject it — it must surface as an error, not active.
    _write_cert(cfg.tls_cert_path, cfg.tls_key_path, days_valid=-1)
    info = next(d for d in client.get("/api/domains", cookies=_auth_cookie(cfg.db_path)).json()["domains"])
    assert info["cert_status"] == DomainCertStatus.ERROR
    assert "expired" in (info["error_message"] or "")


# --- add mDNS .local (immediately active, no ACME) ----------------------------------


def test_add_local_domain_is_active_and_routable(cfg: Any, client: TestClient[Litestar]) -> None:
    cookies = _auth_cookie(cfg.db_path)
    resp = client.post("/api/domains", json={"name": "myhost.local", "mdns": True}, cookies=cookies)
    assert resp.status_code == 202
    # POST returns the full updated list (so the UI repaints without a follow-up GET).
    body = resp.json()
    assert {d["name"] for d in body["domains"]} == {"host.example.com", "myhost.local"}
    added = next(d for d in body["domains"] if d["name"] == "myhost.local")
    assert added["scheme"] == "http" and added["cert_status"] == DomainCertStatus.ACTIVE  # http, nothing to acquire
    # persisted + now routable via the DB-backed resolver
    with closing(open_db(cfg)) as db:
        assert Domain.match(db, "app.myhost.local") is not None


# --- add TLS domain: acquiring → active / error -------------------------------------


def test_add_tls_domain_acquires_and_becomes_active(
    cfg: Any, client: TestClient[Litestar], sync_acquisition: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(domains, "ensure_cert_for", lambda config, domain, db: None)  # "acquired"
    cookies = _auth_cookie(cfg.db_path)
    resp = client.post("/api/domains", json={"name": "host.example.org", "tls": True}, cookies=cookies)
    assert resp.status_code == 202
    # acquisition ran synchronously → status settled to active
    info = next(
        d for d in client.get("/api/domains", cookies=cookies).json()["domains"] if d["name"] == "host.example.org"
    )
    assert info["cert_status"] == DomainCertStatus.ACTIVE
    assert info["scheme"] == "https"


def test_add_tls_domain_records_acquisition_error(
    cfg: Any, client: TestClient[Litestar], sync_acquisition: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(config: Any, domain: Any, db: Any) -> None:
        raise RuntimeError("DNS not delegated")

    monkeypatch.setattr(domains, "ensure_cert_for", boom)
    cookies = _auth_cookie(cfg.db_path)
    client.post("/api/domains", json={"name": "host.example.org", "tls": True}, cookies=cookies)
    info = next(
        d for d in client.get("/api/domains", cookies=cookies).json()["domains"] if d["name"] == "host.example.org"
    )
    assert info["cert_status"] == DomainCertStatus.ERROR
    assert "DNS not delegated" in info["error_message"]


# --- validation ---------------------------------------------------------------------


def test_add_duplicate_rejected(cfg: Any, client: TestClient[Litestar]) -> None:
    cookies = _auth_cookie(cfg.db_path)
    assert (
        client.post("/api/domains", json={"name": "host.example.com", "tls": True}, cookies=cookies).status_code == 400
    )


def test_add_invalid_name_rejected(cfg: Any, client: TestClient[Litestar]) -> None:
    cookies = _auth_cookie(cfg.db_path)
    assert client.post("/api/domains", json={"name": "not a domain"}, cookies=cookies).status_code == 400
    assert client.post("/api/domains", json={"name": "nodot"}, cookies=cookies).status_code == 400


def test_add_mdns_with_tls_rejected(cfg: Any, client: TestClient[Litestar]) -> None:
    cookies = _auth_cookie(cfg.db_path)
    resp = client.post("/api/domains", json={"name": "myhost.local", "tls": True, "mdns": True}, cookies=cookies)
    assert resp.status_code == 400


def test_add_local_name_with_tls_rejected(cfg: Any, client: TestClient[Litestar]) -> None:
    # A .local name can never get a public cert, so tls must be rejected before ACME is attempted.
    cookies = _auth_cookie(cfg.db_path)
    assert client.post("/api/domains", json={"name": "myhost.local", "tls": True}, cookies=cookies).status_code == 400


def test_add_local_name_without_mdns_rejected(cfg: Any, client: TestClient[Litestar]) -> None:
    # A .local zone would point at a LAN IP the responder never answers for unless mdns is set.
    cookies = _auth_cookie(cfg.db_path)
    assert client.post("/api/domains", json={"name": "myhost.local"}, cookies=cookies).status_code == 400


def test_add_non_local_name_with_mdns_rejected(cfg: Any, client: TestClient[Litestar]) -> None:
    cookies = _auth_cookie(cfg.db_path)
    resp = client.post("/api/domains", json={"name": "host.example.org", "mdns": True}, cookies=cookies)
    assert resp.status_code == 400


# --- remove -------------------------------------------------------------------------


def test_remove_runtime_domain(cfg: Any, client: TestClient[Litestar]) -> None:
    cookies = _auth_cookie(cfg.db_path)
    client.post("/api/domains", json={"name": "myhost.local", "mdns": True}, cookies=cookies)
    assert client.delete("/api/domains/myhost.local", cookies=cookies).status_code == 200
    names = {d["name"] for d in client.get("/api/domains", cookies=cookies).json()["domains"]}
    assert names == {"host.example.com"}


def test_remove_primary_rejected(cfg: Any, client: TestClient[Litestar]) -> None:
    cookies = _auth_cookie(cfg.db_path)
    assert client.delete("/api/domains/host.example.com", cookies=cookies).status_code == 400


def test_remove_unknown_domain_404(cfg: Any, client: TestClient[Litestar]) -> None:
    cookies = _auth_cookie(cfg.db_path)
    assert client.delete("/api/domains/nope.example.net", cookies=cookies).status_code == 404
