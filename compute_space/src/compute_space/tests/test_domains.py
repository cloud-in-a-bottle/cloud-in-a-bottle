"""Tests for the multi-domain model (Domain, effective_domains, match_domain).

The domain set lives in the DB ``domains`` table (see ``core/domain_store.py``); routing resolves
per-host via ``domain_store.match_domain``.  The primary's scheme/name come from the DB-derived
``zone_domain``/``tls_enabled`` scalars on the Config.
"""

from __future__ import annotations

import sqlite3

from compute_space.config import DefaultConfig
from compute_space.config import Domain
from compute_space.core.domain_store import DomainRecord
from compute_space.core.domain_store import effective_domains
from compute_space.core.domain_store import match_domain
from compute_space.core.domain_store import seed_domains
from compute_space.db.schema import schema_path


def _db(domains: tuple[Domain, ...] = ()) -> sqlite3.Connection:
    """An in-memory DB loaded with the production schema and seeded with ``domains`` (primary first)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(schema_path()) as f:
        conn.executescript(f.read())
    if domains:
        seed_domains(conn, domains[0], [DomainRecord(d.name, d.tls, d.mdns) for d in domains[1:]])
    return conn


def test_serialization_has_no_domains_key() -> None:
    """The domain set is DB-backed, so a Config never serializes a ``domains`` key."""
    cfg = DefaultConfig(zone_domain="host.example.com", tls_enabled=True)
    assert "domains" not in cfg.to_toml_str()


def test_effective_domains_is_db_sourced_no_synthesis() -> None:
    # effective_domains is exactly what's seeded; it does NOT synthesize a primary from a Config's
    # zone_domain (that is captured into the DB once by the first-boot seed instead).
    assert effective_domains(_db()) == ()
    cfg = DefaultConfig(zone_domain="host.example.com", tls_enabled=True)
    assert cfg.primary_domain == Domain(name="host.example.com", tls=True)
    assert cfg.primary_domain.scheme == "https"


def test_non_tls_primary_domain_scheme_is_http() -> None:
    cfg = DefaultConfig(zone_domain="myhost.local", tls_enabled=False)
    assert cfg.primary_domain.scheme == "http"


def test_domain_name_is_lowercased() -> None:
    assert Domain(name="Host.Example.COM").name == "host.example.com"


def test_domain_name_no_port() -> None:
    assert Domain(name="host.example.com:8080").name_no_port == "host.example.com"


def _multi() -> sqlite3.Connection:
    return _db(
        (
            Domain(name="host.example.com", tls=True),
            Domain(name="myhost.local", tls=False, mdns=True),
        )
    )


def test_match_domain_router_host() -> None:
    matched = match_domain(_multi(), "host.example.com")
    assert matched is not None and matched.name == "host.example.com"


def test_match_domain_app_subdomain() -> None:
    matched = match_domain(_multi(), "myapp.host.example.com")
    assert matched is not None and matched.name == "host.example.com" and matched.tls is True


def test_match_domain_local_subdomain_is_http_mdns() -> None:
    matched = match_domain(_multi(), "myapp.myhost.local")
    assert matched is not None and matched.name == "myhost.local"
    assert matched.tls is False and matched.mdns is True and matched.scheme == "http"


def test_match_domain_ignores_port() -> None:
    matched = match_domain(_multi(), "myapp.myhost.local:8080")
    assert matched is not None and matched.name == "myhost.local"


def test_match_domain_unrelated_host_returns_none() -> None:
    assert match_domain(_multi(), "unrelated.example.org") is None


def test_match_domain_longest_suffix_wins() -> None:
    db = _db((Domain(name="example.com", tls=True), Domain(name="host.example.com", tls=True)))
    matched = match_domain(db, "app.host.example.com")
    assert matched is not None and matched.name == "host.example.com"


def test_match_domain_empty_name_never_matches() -> None:
    # An empty domain name (misconfiguration) must not `endswith(".")`-match any trailing-dot host.
    db = _db((Domain(name="host.example.com", tls=True), Domain(name="", tls=False)))
    assert match_domain(db, "evil.com.") is None
    matched = match_domain(db, "host.example.com")
    assert matched is not None and matched.name == "host.example.com"


def test_coredns_zonefile_path_for_primary_ignores_port() -> None:
    # The primary must map to the legacy zonefile even when zone_domain carries a port, and no
    # ':' may leak into a per-domain filename.
    cfg = DefaultConfig(zone_domain="host.example.com:8443", tls_enabled=True)
    assert cfg.coredns_zonefile_path_for("host.example.com:8443") == cfg.coredns_zonefile_path
    assert ":" not in cfg.coredns_zonefile_path_for("other.example.com:99").name
