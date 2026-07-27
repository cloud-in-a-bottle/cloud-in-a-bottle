"""Tests for the multi-domain config model (Domain, all_domains, match_domain).

Single-domain configs must be byte-identical on serialization and behave exactly
as before; an explicit ``domains`` set must round-trip through ``load_config`` and
resolve per-host.
"""

from __future__ import annotations

from pathlib import Path

from compute_space.config import DefaultConfig
from compute_space.config import Domain
from compute_space.config import load_config


def test_single_domain_serialization_omits_domains_key() -> None:
    """A config that never set ``domains`` must serialize without the key, so a
    single-domain config stays clean."""
    cfg = DefaultConfig(zone_domain="host.example.com", tls_enabled=True)
    assert "domains" not in cfg.to_toml_str()


def test_all_domains_is_the_explicit_set_no_synthesis() -> None:
    # all_domains is the DB-sourced set; it does NOT synthesize a primary from the legacy
    # zone_domain field (that is captured into the DB once by the first-boot seed instead).
    bare = DefaultConfig(zone_domain="host.example.com", tls_enabled=True)
    assert bare.all_domains == ()
    populated = DefaultConfig(
        zone_domain="host.example.com", tls_enabled=True, domains=(Domain(name="host.example.com", tls=True),)
    )
    assert populated.primary_domain == Domain(name="host.example.com", tls=True)
    assert populated.primary_domain.scheme == "https"


def test_non_tls_primary_domain_scheme_is_http() -> None:
    cfg = DefaultConfig(zone_domain="myhost.local", tls_enabled=False, domains=(Domain(name="myhost.local"),))
    assert cfg.primary_domain.scheme == "http"


def test_domain_name_is_lowercased() -> None:
    assert Domain(name="Host.Example.COM").name == "host.example.com"


def test_domain_name_no_port() -> None:
    assert Domain(name="host.example.com:8080").name_no_port == "host.example.com"


def test_explicit_domains_round_trip_through_load_config(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    cfg = DefaultConfig(
        zone_domain="host.example.com",
        tls_enabled=True,
        domains=(
            Domain(name="host.example.com", tls=True),
            Domain(name="myhost.local", tls=False, mdns=True),
        ),
    )
    path = tmp_path / "config.toml"
    cfg.to_toml(str(path))
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", str(path))
    reloaded = load_config()
    assert reloaded.domains == cfg.domains


def _multi() -> DefaultConfig:
    return DefaultConfig(
        zone_domain="host.example.com",
        tls_enabled=True,
        domains=(
            Domain(name="host.example.com", tls=True),
            Domain(name="myhost.local", tls=False, mdns=True),
        ),
    )


def test_match_domain_router_host() -> None:
    assert _multi().match_domain("host.example.com").name == "host.example.com"


def test_match_domain_app_subdomain() -> None:
    matched = _multi().match_domain("myapp.host.example.com")
    assert matched is not None and matched.name == "host.example.com" and matched.tls is True


def test_match_domain_local_subdomain_is_http_mdns() -> None:
    matched = _multi().match_domain("myapp.myhost.local")
    assert matched is not None and matched.name == "myhost.local"
    assert matched.tls is False and matched.mdns is True and matched.scheme == "http"


def test_match_domain_ignores_port() -> None:
    assert _multi().match_domain("myapp.myhost.local:8080").name == "myhost.local"


def test_match_domain_unrelated_host_returns_none() -> None:
    assert _multi().match_domain("unrelated.example.org") is None


def test_match_domain_longest_suffix_wins() -> None:
    cfg = DefaultConfig(
        zone_domain="example.com",
        tls_enabled=True,
        domains=(Domain(name="example.com", tls=True), Domain(name="host.example.com", tls=True)),
    )
    assert cfg.match_domain("app.host.example.com").name == "host.example.com"


def test_match_domain_empty_name_never_matches() -> None:
    # An empty domain name (misconfiguration) must not `endswith(".")`-match any trailing-dot host.
    cfg = DefaultConfig(
        zone_domain="host.example.com",
        tls_enabled=True,
        domains=(Domain(name="host.example.com", tls=True), Domain(name="", tls=False)),
    )
    assert cfg.match_domain("evil.com.") is None
    assert cfg.match_domain("host.example.com").name == "host.example.com"


def test_coredns_zonefile_path_for_primary_ignores_port() -> None:
    # The primary must map to the legacy zonefile even when zone_domain carries a port, and no
    # ':' may leak into a per-domain filename.
    cfg = DefaultConfig(
        zone_domain="host.example.com:8443",
        tls_enabled=True,
        domains=(Domain(name="host.example.com:8443", tls=True),),
    )
    assert cfg.coredns_zonefile_path_for("host.example.com:8443") == cfg.coredns_zonefile_path
    assert ":" not in cfg.coredns_zonefile_path_for("other.example.com:99").name
