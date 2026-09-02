"""Phase 1: routing resolves an app under ANY configured domain, and the request's
domain is recoverable per request.  Single-domain behavior is unchanged; a second
`.local` domain makes the same app reachable there too, over http."""

from __future__ import annotations

import types
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from compute_space.config import DefaultConfig
from compute_space.core.apps import get_app_from_hostname
from compute_space.core.domains import Domain
from compute_space.core.domains import DomainRecord
from compute_space.core.domains import seed_domains
from compute_space.db import get_db
from compute_space.db import init_db
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db
from compute_space.web.app import _reject_app_subdomain_requests
from compute_space.web.helpers.zone import RequestOrigin
from compute_space.web.helpers.zone import set_request_origin
from compute_space.web.helpers.zone import zone_for_request

PRIMARY = Domain(name="host.example.com", tls=True)
LOCAL = Domain(name="myhost.local", tls=False, mdns=True)


def _seed(cfg: DefaultConfig, *domains: Domain) -> DefaultConfig:
    """Migrate the config's DB and seed ``domains`` (primary first) so the DB-backed resolvers see them."""
    init_db(cfg.db_path)
    with closing(open_db(cfg)) as db:
        seed_domains(db, domains[0], [DomainRecord(d.name, d.tls, d.mdns) for d in domains[1:]])
    return cfg


@pytest.fixture
def multi_domain_config(tmp_path: Path) -> DefaultConfig:
    # seed_primary=False: this fixture seeds the full set (primary + extras) itself.
    cfg = _make_test_config(tmp_path, seed_primary=False)
    return _seed(cfg, PRIMARY, LOCAL)  # type: ignore[arg-type]


# --- get_app_from_hostname: matches under any configured domain -------------------


@pytest.fixture
def captured_lookups(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the app-row lookup so we can assert which app name routing extracted.
    Returns a sentinel App for any name (the domain set still comes from the seeded DB)."""
    names: list[str] = []
    sentinel = object()

    def fake_find(name: str) -> Any:
        names.append(name)
        return sentinel

    monkeypatch.setattr("compute_space.core.apps.find_app_by_name", fake_find)
    return names


def _route(host: str) -> Any:
    with closing(get_db()) as db:
        return get_app_from_hostname(host, db)


def _looks(host: str) -> bool:
    with closing(get_db()) as db:
        matched = Domain.match(db, host)
    return matched is not None and matched.is_app_subdomain(host)


def test_app_reachable_under_primary_domain(multi_domain_config: Any, captured_lookups: list[str]) -> None:
    assert _route("myapp.host.example.com") is not None
    assert captured_lookups == ["myapp"]


def test_same_app_reachable_under_local_domain(multi_domain_config: Any, captured_lookups: list[str]) -> None:
    assert _route("myapp.myhost.local") is not None
    assert captured_lookups == ["myapp"]


def test_local_domain_ignores_port(multi_domain_config: Any, captured_lookups: list[str]) -> None:
    assert _route("myapp.myhost.local:8080") is not None
    assert captured_lookups == ["myapp"]


def test_bare_domain_is_router_not_app(multi_domain_config: Any, captured_lookups: list[str]) -> None:
    assert _route("host.example.com") is None
    assert _route("myhost.local") is None
    assert captured_lookups == []  # no DB lookup for the router host


def test_nested_subdomain_rejected(multi_domain_config: Any, captured_lookups: list[str]) -> None:
    assert _route("a.b.myhost.local") is None
    assert captured_lookups == []


def test_unrelated_host_is_not_routed(multi_domain_config: Any, captured_lookups: list[str]) -> None:
    assert _route("evil.example.org") is None
    assert captured_lookups == []


# --- _looks_like_app_subdomain ----------------------------------------------------


def test_looks_like_app_subdomain_across_domains(multi_domain_config: Any) -> None:
    assert _looks("myapp.host.example.com") is True
    assert _looks("myapp.myhost.local") is True
    assert _looks("host.example.com") is False  # router host
    assert _looks("myhost.local") is False
    assert _looks("evil.example.org") is False


# --- _reject_app_subdomain_requests (defense-in-depth in Litestar) ----------------


def _fake_request(netloc: str) -> Any:
    # No stashed zone (empty scope) so the guard falls back to a DB lookup, exercising the bypass path.
    return types.SimpleNamespace(url=types.SimpleNamespace(netloc=netloc), scope={})


def test_reject_app_subdomain_across_domains(multi_domain_config: Any) -> None:
    assert _reject_app_subdomain_requests(_fake_request("myapp.host.example.com")).status_code == 404
    assert _reject_app_subdomain_requests(_fake_request("myapp.myhost.local")).status_code == 404
    # router hosts and unrelated hosts pass through (None = defer to Litestar)
    assert _reject_app_subdomain_requests(_fake_request("host.example.com")) is None
    assert _reject_app_subdomain_requests(_fake_request("myhost.local")) is None
    assert _reject_app_subdomain_requests(_fake_request("unrelated.example.org")) is None


# --- zone_for_request -------------------------------------------------------------


def test_zone_for_request_reads_recorded_origin(multi_domain_config: Any) -> None:
    set_request_origin(RequestOrigin(zone=LOCAL, netloc="anything.at.all"))
    assert zone_for_request() == LOCAL


def test_zone_for_request_raises_when_no_origin(multi_domain_config: Any) -> None:
    # SubdomainProxyMiddleware records an origin on every request; reaching a handler without one is a
    # bug, not a silent fallback.
    set_request_origin(None)
    with pytest.raises(RuntimeError, match="SubdomainProxyMiddleware is required"):
        zone_for_request()


def test_single_domain_config_unchanged(tmp_path: Path) -> None:
    """A single-domain config routes exactly as before (now a one-row DB domain set)."""
    cfg = _make_test_config(tmp_path, seed_primary=False)
    _seed(cfg, Domain(name="solo.example.com", tls=True))  # type: ignore[arg-type]
    assert _looks("app.solo.example.com") is True
    assert _looks("solo.example.com") is False
    solo = Domain(name="solo.example.com", tls=True)
    set_request_origin(RequestOrigin(zone=solo, netloc="app.solo.example.com"))
    assert zone_for_request().name == "solo.example.com"
