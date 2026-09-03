"""Tests for the multi-domain model (Domain, effective_domains, Domain.match).

The domain set lives in the DB ``domains`` table (see ``core/domains.py``); routing resolves
per-host via ``Domain.match``.  The primary is read live from the DB
(``domains.primary_domain``).
"""

from __future__ import annotations

import sqlite3

from compute_space.config import DefaultConfig
from compute_space.core.dns.coredns_provider import coredns as dns_mod
from compute_space.core.domains import Domain
from compute_space.core.domains import DomainRecord
from compute_space.core.domains import effective_domains
from compute_space.core.domains import seed_domains
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


def test_serialization_omits_db_derived_domain_keys() -> None:
    """The domain set (name + tls) is DB-backed, so a Config never serializes ``domains``,
    ``zone_domain``, or ``tls_enabled``."""
    rendered = DefaultConfig().to_toml_str()
    assert "domains" not in rendered
    assert "zone_domain" not in rendered
    assert "tls_enabled" not in rendered


def test_effective_domains_is_db_sourced_no_synthesis() -> None:
    # effective_domains is exactly what's seeded; it does NOT synthesize a primary from config (that
    # is captured into the DB once by the first-boot seed instead).
    assert effective_domains(_db()) == ()


def test_domain_scheme() -> None:
    assert Domain(name="host.example.com", tls=True).scheme == "https"
    assert Domain(name="myhost.local", tls=False).scheme == "http"


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
    matched = Domain.match(_multi(), "host.example.com")
    assert matched is not None and matched.name == "host.example.com"


def test_match_domain_app_subdomain() -> None:
    matched = Domain.match(_multi(), "myapp.host.example.com")
    assert matched is not None and matched.name == "host.example.com" and matched.tls is True


def test_match_domain_local_subdomain_is_http_mdns() -> None:
    matched = Domain.match(_multi(), "myapp.myhost.local")
    assert matched is not None and matched.name == "myhost.local"
    assert matched.tls is False and matched.mdns is True and matched.scheme == "http"


def test_match_domain_ignores_port() -> None:
    matched = Domain.match(_multi(), "myapp.myhost.local:8080")
    assert matched is not None and matched.name == "myhost.local"


def test_match_domain_unrelated_host_returns_none() -> None:
    assert Domain.match(_multi(), "unrelated.example.org") is None


def test_match_domain_longest_suffix_wins() -> None:
    db = _db((Domain(name="example.com", tls=True), Domain(name="host.example.com", tls=True)))
    matched = Domain.match(db, "app.host.example.com")
    assert matched is not None and matched.name == "host.example.com"


def test_match_domain_empty_name_never_matches() -> None:
    # An empty domain name (misconfiguration) must not `endswith(".")`-match any trailing-dot host.
    db = _db((Domain(name="host.example.com", tls=True), Domain(name="", tls=False)))
    assert Domain.match(db, "evil.com.") is None
    matched = Domain.match(db, "host.example.com")
    assert matched is not None and matched.name == "host.example.com"


def test_zonefile_path_strips_port() -> None:
    # Every zone gets a file under zones_dir, and no ':' may leak into the filename.
    cfg = DefaultConfig()
    assert dns_mod._zonefile_path(cfg.zones_dir, "host.example.com:8443") == cfg.zones_dir / "host.example.com.zone"
    assert ":" not in dns_mod._zonefile_path(cfg.zones_dir, "other.example.com:99").name
