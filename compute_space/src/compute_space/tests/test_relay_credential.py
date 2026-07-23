"""Tests for the runtime relay-credential provider (fetch + cache + verify)."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from compute_space.core.email import relay_credential as rc
from compute_space.core.email.relay_credential import RelayCredentialProvider
from compute_space.tests.conftest import _make_test_config

_EMAIL_KW = dict(
    email_enabled=True,
    email_proxy_base_url="https://frontend.example",
    email_keycloak_issuer_url="https://kc.example/realms/openhost-customers",
    email_keycloak_client_id="instance-x",
    email_keycloak_client_secret="secret",
    email_inbound_mx_host="inbound-smtp.us-west-2.amazonaws.com",
    public_ip="203.0.113.5",
)


def _provider(tmp_path: Path, clock: list[float]) -> RelayCredentialProvider:
    config = _make_test_config(tmp_path, zone_domain="alice.selfhost.imbue.com", **_EMAIL_KW)
    return RelayCredentialProvider(config=config, monotonic=lambda: clock[0])


def _fetch_body(pw: str = "hmac-pw") -> dict:
    return {
        "configured": True,
        "smtp_relay_host": "openhost-email-proxy.fly.dev",
        "smtp_relay_port": 465,
        "smtp_relay_user": "alice.selfhost.imbue.com",
        "smtp_relay_password": pw,
        "zone_domain": "alice.selfhost.imbue.com",
    }


def test_disabled_returns_none(tmp_path: Path) -> None:
    config = _make_test_config(tmp_path)  # email disabled
    assert RelayCredentialProvider(config=config).get() is None


def test_fetch_and_cache(tmp_path: Path) -> None:
    clock = [0.0]
    provider = _provider(tmp_path, clock)
    with mock.patch.object(RelayCredentialProvider, "_fetch") as fetch:
        fetch.return_value = rc.RelayCredential(
            smtp_relay_host="h",
            smtp_relay_port=465,
            smtp_relay_user="u",
            smtp_relay_password="pw",
            zone_domain="z",
            custom_domain=None,
        )
        c1 = provider.get()
        c2 = provider.get()  # within TTL -> cached, no second fetch
        assert c1 is c2
        assert fetch.call_count == 1
        # advance past TTL -> refetch
        clock[0] += rc._CACHE_TTL_SECONDS + 1
        provider.get()
        assert fetch.call_count == 2
