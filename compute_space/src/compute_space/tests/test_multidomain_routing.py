"""Phase 1: routing resolves an app under ANY configured domain, and the request's
domain is recoverable per request.  Single-domain behavior is unchanged; a second
`.local` domain makes the same app reachable there too, over http."""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import pytest

from compute_space.config import DefaultConfig
from compute_space.config import Domain
from compute_space.config import set_active_config
from compute_space.core.apps import get_app_from_hostname
from compute_space.tests.conftest import _make_test_config
from compute_space.web.app import _reject_app_subdomain_requests
from compute_space.web.helpers.zone import ZONE_SCOPE_KEY
from compute_space.web.helpers.zone import zone_for_request
from compute_space.web.middleware.subdomain_proxy import _looks_like_app_subdomain

PRIMARY = Domain(name="host.example.com", tls=True)
LOCAL = Domain(name="myhost.local", tls=False, mdns=True)


@pytest.fixture
def multi_domain_config(tmp_path: Path) -> DefaultConfig:
    cfg = _make_test_config(
        tmp_path,
        zone_domain="host.example.com",
        tls_enabled=True,
        domains=(PRIMARY, LOCAL),
    )
    return cfg  # type: ignore[return-value]


# --- get_app_from_hostname: matches under any configured domain -------------------


@pytest.fixture
def captured_lookups(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the DB lookup so we can assert which app name routing extracted,
    without seeding a database.  Returns a sentinel App for any name."""
    names: list[str] = []
    sentinel = object()

    def fake_find(name: str) -> Any:
        names.append(name)
        return sentinel

    monkeypatch.setattr("compute_space.core.apps.find_app_by_name", fake_find)
    return names


def _route(host: str) -> Any:
    return get_app_from_hostname(host)


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
    assert _looks_like_app_subdomain("myapp.host.example.com") is True
    assert _looks_like_app_subdomain("myapp.myhost.local") is True
    assert _looks_like_app_subdomain("host.example.com") is False  # router host
    assert _looks_like_app_subdomain("myhost.local") is False
    assert _looks_like_app_subdomain("evil.example.org") is False


# --- _reject_app_subdomain_requests (defense-in-depth in Litestar) ----------------


def _fake_request(netloc: str) -> Any:
    return types.SimpleNamespace(url=types.SimpleNamespace(netloc=netloc))


def test_reject_app_subdomain_across_domains(multi_domain_config: Any) -> None:
    assert _reject_app_subdomain_requests(_fake_request("myapp.host.example.com")).status_code == 404
    assert _reject_app_subdomain_requests(_fake_request("myapp.myhost.local")).status_code == 404
    # router hosts and unrelated hosts pass through (None = defer to Litestar)
    assert _reject_app_subdomain_requests(_fake_request("host.example.com")) is None
    assert _reject_app_subdomain_requests(_fake_request("myhost.local")) is None
    assert _reject_app_subdomain_requests(_fake_request("unrelated.example.org")) is None


# --- zone_for_request -------------------------------------------------------------


def _fake_conn(netloc: str, scope: dict[str, Any] | None = None) -> Any:
    return types.SimpleNamespace(
        scope=scope if scope is not None else {},
        url=types.SimpleNamespace(netloc=netloc),
    )


def test_zone_for_request_prefers_stashed_domain(multi_domain_config: Any) -> None:
    conn = _fake_conn("anything.at.all", scope={ZONE_SCOPE_KEY: LOCAL})
    assert zone_for_request(conn) == LOCAL


def test_zone_for_request_reresolves_when_unstashed(multi_domain_config: Any) -> None:
    assert zone_for_request(_fake_conn("myapp.myhost.local")) == LOCAL
    assert zone_for_request(_fake_conn("myapp.host.example.com")) == PRIMARY


def test_zone_for_request_falls_back_to_primary(multi_domain_config: Any) -> None:
    # unrelated host, and a stashed None (host matched no domain in the middleware)
    assert zone_for_request(_fake_conn("unrelated.example.org")) == PRIMARY
    assert zone_for_request(_fake_conn("unrelated.example.org", scope={ZONE_SCOPE_KEY: None})) == PRIMARY


def test_single_domain_config_unchanged(tmp_path: Path) -> None:
    """A single-domain config routes exactly as before (now an explicit one-domain set)."""
    set_active_config(
        DefaultConfig(
            zone_domain="solo.example.com", tls_enabled=True, domains=(Domain(name="solo.example.com", tls=True),)
        )
    )
    assert _looks_like_app_subdomain("app.solo.example.com") is True
    assert _looks_like_app_subdomain("solo.example.com") is False
    assert zone_for_request(_fake_conn("app.solo.example.com")).name == "solo.example.com"
