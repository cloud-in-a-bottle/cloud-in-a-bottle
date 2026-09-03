"""Phase 3b: the /api/domains endpoint — owner-authed add/list/remove of domains on a live
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

from compute_space.config import provide_config
from compute_space.core import caddy
from compute_space.core.auth.auth import SESSION_COOKIE_NAME
from compute_space.core.auth.auth import create_session
from compute_space.core.dns.coredns_provider.interface import InternalDnsProvider
from compute_space.core.domains import Domain
from compute_space.core.domains import DomainCertStatus
from compute_space.core.domains import seed_domains
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db
from compute_space.tests.conftest import stub_coredns_spawn
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


def _make_app(dns_provider: Any) -> Litestar:
    return Litestar(
        route_handlers=[api_domains_routes],
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
            # Mirrors create_app: the routes are always handed the running provider.
            "dns_provider": Provide(lambda: dns_provider, sync_to_thread=False, use_cache=True),
        },
        openapi_config=None,
    )


def _unstarted_provider(tmp_path: Path, bind_ip: str | None = "203.0.113.10") -> InternalDnsProvider:
    return InternalDnsProvider(
        corefile_path=tmp_path / "Corefile",
        zones_dir=tmp_path / "zones",
        bind_ip=bind_ip,
        zones=(PRIMARY.name,) if bind_ip else (),
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
def client(cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient[Litestar]]:
    stub_coredns_spawn(monkeypatch)  # add_zone starts CoreDNS; keep that off the test machine
    with TestClient(app=_make_app(_unstarted_provider(tmp_path))) as c:
        yield c


@pytest.fixture
def dns_client(
    cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[InternalDnsProvider, TestClient[Litestar]]]:
    """A real provider, never started, so the routes drive the same zone set production would."""
    stub_coredns_spawn(monkeypatch)  # add_zone starts CoreDNS; keep that off the test machine
    dns_provider = _unstarted_provider(tmp_path)
    with TestClient(app=_make_app(dns_provider)) as c:
        c.cookies.update(_auth_cookie(cfg.db_path))
        yield dns_provider, c


def test_a_public_domain_is_accepted_when_dns_is_not_bound(cfg: Any, tmp_path: Path) -> None:
    # Running without CoreDNS is a supported choice, so it must not veto the add -- the domain is
    # recorded and served by Caddy; only the zone this instance would answer for is missing.
    with TestClient(app=_make_app(_unstarted_provider(tmp_path, bind_ip=None))) as c:
        c.cookies.update(_auth_cookie(cfg.db_path))
        r = c.post("/api/domains", json={"name": "host.example.org", "tls": False, "mdns": False})
        assert r.status_code == 202
        assert "host.example.org" in [d["name"] for d in c.get("/api/domains").json()["domains"]]


def test_a_tls_domain_added_without_dns_reports_why_its_cert_failed(cfg: Any, tmp_path: Path) -> None:
    # The consequence shows up where the user can see it: the domain's cert status, not a refusal.
    with TestClient(app=_make_app(_unstarted_provider(tmp_path, bind_ip=None))) as c:
        c.cookies.update(_auth_cookie(cfg.db_path))
        assert c.post("/api/domains", json={"name": "host.example.org", "tls": True}).status_code == 202
        info = next(d for d in c.get("/api/domains").json()["domains"] if d["name"] == "host.example.org")
        assert info["cert_status"] == DomainCertStatus.ERROR
        assert "CoreDNS must be enabled" in info["error_message"]


async def _acquired(config: Any, domain: Any, db: Any, dns_provider: Any) -> None:
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
    async def boom(config: Any, domain: Any, db: Any, dns_provider: Any) -> None:
        raise RuntimeError("DNS not delegated")

    monkeypatch.setattr(domains, "ensure_cert_for", boom)
    client.cookies.update(_auth_cookie(cfg.db_path))
    client.post("/api/domains", json={"name": "host.example.org", "tls": True})
    info = next(d for d in client.get("/api/domains").json()["domains"] if d["name"] == "host.example.org")
    assert info["cert_status"] == DomainCertStatus.ERROR
    assert "DNS not delegated" in info["error_message"]


def test_a_new_public_domain_is_served_by_the_dns_provider(
    dns_client: tuple[InternalDnsProvider, TestClient[Litestar]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The provider has to be authoritative for the zone *before* acquisition, since DNS-01 answers
    # the challenge out of that zone's file.
    monkeypatch.setattr(domains, "ensure_cert_for", _acquired)
    dns_provider, client = dns_client

    client.post("/api/domains", json={"name": "host.example.org", "tls": True})
    assert list(dns_provider.zones) == [PRIMARY.name, "host.example.org"]

    client.delete("/api/domains/host.example.org")
    assert list(dns_provider.zones) == [PRIMARY.name]


def test_an_mdns_domain_never_reaches_the_dns_provider(
    dns_client: tuple[InternalDnsProvider, TestClient[Litestar]],
) -> None:
    # .local is served by the wildcard mDNS responder; CoreDNS never sees it.
    dns_provider, client = dns_client

    client.post("/api/domains", json={"name": "myhost.local", "mdns": True})
    client.delete("/api/domains/myhost.local")

    assert list(dns_provider.zones) == [PRIMARY.name]


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
