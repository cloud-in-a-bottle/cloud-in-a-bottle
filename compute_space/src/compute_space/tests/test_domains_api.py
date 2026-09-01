"""Phase 3b: the /api/domains endpoint — owner-authed add/list/promote/remove on a live
instance, with the TLS-domain acquisition state machine (acquiring → active|error).  ACME is
stubbed, and TestClient drains the response's background tasks before returning, so acquisition
has settled by the time POST comes back; no Caddy runs (reload is a no-op in tests)."""

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

from compute_space.config import Config
from compute_space.config import provide_config
from compute_space.config import set_active_config
from compute_space.core import caddy
from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import create_session
from compute_space.core.domains import Domain
from compute_space.core.domains import DomainCertStatus
from compute_space.core.domains import DomainRecord
from compute_space.core.domains import legacy_domain_asset_owner
from compute_space.core.domains import primary_domain
from compute_space.core.domains import seed_domains
from compute_space.core.domains import upsert_record
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


async def _acquired(config: Any, domain: Any, db: Any) -> None:
    """ensure_cert_for is async now; a stub has to be too."""


# --- auth ---------------------------------------------------------------------------


def test_list_requires_auth(client: TestClient[Litestar]) -> None:
    assert client.get("/api/domains").status_code == 401


def test_list_shows_primary(cfg: Any, client: TestClient[Litestar]) -> None:
    client.cookies.update(_auth_cookie(cfg.db_path))
    resp = client.get("/api/domains")
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
    client.cookies.update(_auth_cookie(cfg.db_path))
    info = next(d for d in client.get("/api/domains").json()["domains"])
    assert info["name"] == "host.example.com" and info["cert_status"] == "active"


def test_primary_with_expired_cert_not_active(cfg: Any, client: TestClient[Litestar]) -> None:
    # An expired cert still has files on disk (so a mere existence check would say 'active'), but
    # browsers reject it — it must surface as an error, not active.
    _write_cert(cfg.tls_cert_path, cfg.tls_key_path, days_valid=-1)
    client.cookies.update(_auth_cookie(cfg.db_path))
    info = next(d for d in client.get("/api/domains").json()["domains"])
    assert info["cert_status"] == DomainCertStatus.ERROR
    assert "expired" in (info["error_message"] or "")


# --- add mDNS .local (immediately active, no ACME) ----------------------------------


def test_add_local_domain_is_active_and_routable(cfg: Any, client: TestClient[Litestar]) -> None:
    client.cookies.update(_auth_cookie(cfg.db_path))
    resp = client.post("/api/domains", json={"name": "myhost.local", "mdns": True})
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
    cfg: Any, client: TestClient[Litestar], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(domains, "ensure_cert_for", _acquired)  # "acquired"
    client.cookies.update(_auth_cookie(cfg.db_path))
    resp = client.post("/api/domains", json={"name": "host.example.org", "tls": True})
    assert resp.status_code == 202
    # the background task ran before POST returned → status settled to active
    info = next(d for d in client.get("/api/domains").json()["domains"] if d["name"] == "host.example.org")
    assert info["cert_status"] == DomainCertStatus.ACTIVE
    assert info["scheme"] == "https"


def test_add_tls_domain_records_acquisition_error(
    cfg: Any, client: TestClient[Litestar], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(config: Any, domain: Any, db: Any) -> None:
        raise RuntimeError("DNS not delegated")

    monkeypatch.setattr(domains, "ensure_cert_for", boom)
    client.cookies.update(_auth_cookie(cfg.db_path))
    client.post("/api/domains", json={"name": "host.example.org", "tls": True})
    info = next(d for d in client.get("/api/domains").json()["domains"] if d["name"] == "host.example.org")
    assert info["cert_status"] == DomainCertStatus.ERROR
    assert "DNS not delegated" in info["error_message"]


# --- validation ---------------------------------------------------------------------


def test_add_duplicate_rejected(cfg: Any, client: TestClient[Litestar]) -> None:
    client.cookies.update(_auth_cookie(cfg.db_path))
    resp = client.post("/api/domains", json={"name": "host.example.com", "tls": True})
    assert resp.status_code == 400
    # 4xx keeps `detail` unmasked, so the reason reaches the client verbatim.
    assert resp.json()["detail"] == "domain is already configured"


def test_add_invalid_name_rejected(cfg: Any, client: TestClient[Litestar]) -> None:
    client.cookies.update(_auth_cookie(cfg.db_path))
    resp = client.post("/api/domains", json={"name": "not a domain"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid domain name"
    assert client.post("/api/domains", json={"name": "nodot"}).status_code == 400


def test_add_mdns_with_tls_rejected(cfg: Any, client: TestClient[Litestar]) -> None:
    client.cookies.update(_auth_cookie(cfg.db_path))
    resp = client.post("/api/domains", json={"name": "myhost.local", "tls": True, "mdns": True})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "mDNS (.local) domains are served over http; set tls=false"


# --- make primary -------------------------------------------------------------------


def test_make_primary_requires_auth(client: TestClient[Litestar]) -> None:
    resp = client.post(
        "/api/domains/myhost.local/primary",
        json={"expected_primary": "host.example.com"},
    )
    assert resp.status_code == 401


def test_make_primary_rejected_during_shutdown(
    cfg: Config, client: TestClient[Litestar], monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(open_db(cfg)) as db:
        upsert_record(db, DomainRecord("myhost.local", tls=False, mdns=True))
    monkeypatch.setattr(domains, "is_shutdown_pending", lambda: True)
    client.cookies.update(_auth_cookie(cfg.db_path))

    resp = client.post(
        "/api/domains/myhost.local/primary",
        json={"expected_primary": "host.example.com"},
    )
    assert resp.status_code == 409
    assert resp.json()["extra"]["code"] == "shutdown_pending"


def test_make_http_domain_primary_and_remove_previous(cfg: Config, client: TestClient[Litestar]) -> None:
    with closing(open_db(cfg)) as db:
        upsert_record(db, DomainRecord("myhost.local", tls=False, mdns=True, cert_status=DomainCertStatus.ACTIVE))
    client.cookies.update(_auth_cookie(cfg.db_path))

    resp = client.post(
        "/api/domains/myhost.local/primary",
        json={"expected_primary": "host.example.com"},
    )
    assert resp.status_code == 200
    assert [d["name"] for d in resp.json()["domains"]] == ["myhost.local", "host.example.com"]
    assert resp.json()["domains"][0]["is_primary"] is True

    with closing(open_db(cfg)) as db:
        assert primary_domain(db).name == "myhost.local"
        assert legacy_domain_asset_owner(db) == "host.example.com"

    # The demoted domain can now be removed through the supported API.
    assert client.delete("/api/domains/host.example.com").status_code == 200


def test_promoting_domain_allows_removing_demoted_primary_with_port(tmp_path: Path) -> None:
    port_primary = Domain("host.example.com:8080", tls=False)
    config = _make_test_config(tmp_path, zone_domain=port_primary.name, domains=(port_primary,))
    init_db(config.db_path)
    with closing(open_db(config)) as db:
        seed_domains(db, port_primary, [])
        upsert_record(db, DomainRecord("myhost.local", tls=False, mdns=True))
    caddy.set_active_caddy(None)

    with TestClient(app=_make_app()) as local_client:
        local_client.cookies.update(_auth_cookie(config.db_path))
        listed = local_client.get("/api/domains").json()["domains"]
        assert next(d for d in listed if d["is_primary"])["name"] == "host.example.com:8080"
        assert (
            local_client.post(
                "/api/domains/myhost.local/primary",
                json={"expected_primary": "host.example.com"},
            ).status_code
            == 200
        )
        assert local_client.delete("/api/domains/host.example.com%3A8080").status_code == 200


def test_make_tls_domain_primary_requires_active_cert(cfg: Config, client: TestClient[Litestar]) -> None:
    with closing(open_db(cfg)) as db:
        upsert_record(db, DomainRecord("second.example.com", tls=True, mdns=False))
    client.cookies.update(_auth_cookie(cfg.db_path))

    resp = client.post(
        "/api/domains/second.example.com/primary",
        json={"expected_primary": "host.example.com"},
    )
    assert resp.status_code == 409
    assert resp.json()["extra"]["code"] == "domain_not_ready"


def test_make_tls_domain_primary_requires_caddy(cfg: Config, client: TestClient[Litestar]) -> None:
    with closing(open_db(cfg)) as db:
        upsert_record(db, DomainRecord("second.example.com", tls=True, mdns=False))
        cert_path, key_path = cfg.cert_key_paths_for(db, "second.example.com")
    _write_cert(cert_path, key_path)
    client.cookies.update(_auth_cookie(cfg.db_path))

    resp = client.post(
        "/api/domains/second.example.com/primary",
        json={"expected_primary": "host.example.com"},
    )
    assert resp.status_code == 409
    assert resp.json()["extra"]["code"] == "domain_not_ready"


def test_make_tls_domain_primary_preserves_cert_path(cfg: Config, client: TestClient[Litestar]) -> None:
    cfg = cfg.evolve(start_caddy=True)
    set_active_config(cfg)
    with closing(open_db(cfg)) as db:
        upsert_record(db, DomainRecord("second.example.com", tls=True, mdns=False))
        cert_path, key_path = cfg.cert_key_paths_for(db, "second.example.com")
    _write_cert(cert_path, key_path)
    client.cookies.update(_auth_cookie(cfg.db_path))

    resp = client.post(
        "/api/domains/second.example.com/primary",
        json={"expected_primary": "host.example.com"},
    )
    assert resp.status_code == 200
    with closing(open_db(cfg)) as db:
        assert primary_domain(db).name == "second.example.com"
        assert cfg.cert_key_paths_for(db, "second.example.com") == (cert_path, key_path)


def test_make_primary_rejects_stale_request(cfg: Config, client: TestClient[Litestar]) -> None:
    with closing(open_db(cfg)) as db:
        upsert_record(db, DomainRecord("one.local", tls=False, mdns=True))
        upsert_record(db, DomainRecord("two.local", tls=False, mdns=True))
    client.cookies.update(_auth_cookie(cfg.db_path))
    assert (
        client.post(
            "/api/domains/one.local/primary",
            json={"expected_primary": "host.example.com"},
        ).status_code
        == 200
    )

    resp = client.post(
        "/api/domains/two.local/primary",
        json={"expected_primary": "host.example.com"},
    )
    assert resp.status_code == 409
    assert resp.json()["extra"] == {"code": "primary_changed", "current_primary": "one.local"}


def test_make_primary_unknown_domain_404(cfg: Config, client: TestClient[Litestar]) -> None:
    client.cookies.update(_auth_cookie(cfg.db_path))
    resp = client.post(
        "/api/domains/missing.example.com/primary",
        json={"expected_primary": "host.example.com"},
    )
    assert resp.status_code == 404


# --- remove -------------------------------------------------------------------------


def test_remove_runtime_domain(cfg: Any, client: TestClient[Litestar]) -> None:
    client.cookies.update(_auth_cookie(cfg.db_path))
    client.post("/api/domains", json={"name": "myhost.local", "mdns": True})
    assert client.delete("/api/domains/myhost.local").status_code == 200
    names = {d["name"] for d in client.get("/api/domains").json()["domains"]}
    assert names == {"host.example.com"}


def test_remove_primary_rejected(cfg: Any, client: TestClient[Litestar]) -> None:
    client.cookies.update(_auth_cookie(cfg.db_path))
    resp = client.delete("/api/domains/host.example.com")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "cannot remove the primary domain"


def test_remove_unknown_domain_404(cfg: Any, client: TestClient[Litestar]) -> None:
    client.cookies.update(_auth_cookie(cfg.db_path))
    resp = client.delete("/api/domains/nope.example.net")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "domain not found"
