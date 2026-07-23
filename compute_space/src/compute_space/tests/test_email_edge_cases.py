"""Edge-case suite for the relay-credential provider."""

from __future__ import annotations

from unittest import mock

import httpx
import pytest

from compute_space.core.email import relay_credential as rc
from compute_space.core.email.relay_credential import RelayCredential
from compute_space.core.email.relay_credential import RelayCredentialProvider
from compute_space.tests.conftest import _make_test_config

_EMAIL_KW = dict(
    email_enabled=True,
    email_mailbox_app_names=["stalwart-email-server"],
    email_proxy_base_url="https://frontend.example",
    email_keycloak_issuer_url="https://kc.example/realms/openhost-customers",
    email_keycloak_client_id="instance-x",
    email_keycloak_client_secret="secret",
    email_inbound_mx_host="inbound-smtp.us-west-2.amazonaws.com",
    public_ip="203.0.113.5",
)


def _provider(tmp_path, clock):
    config = _make_test_config(tmp_path, zone_domain="alice.selfhost.imbue.com", **_EMAIL_KW)
    return RelayCredentialProvider(config=config, monotonic=lambda: clock[0])


_CRED = RelayCredential(
    smtp_relay_host="h",
    smtp_relay_port=465,
    smtp_relay_user="u",
    smtp_relay_password="pw",
    zone_domain="z",
    custom_domain=None,
)


def test_provider_caches_within_ttl(tmp_path):
    clock = [0.0]
    p = _provider(tmp_path, clock)
    with mock.patch.object(RelayCredentialProvider, "_fetch", return_value=_CRED) as f:
        p.get()
        p.get()
        assert f.call_count == 1
        clock[0] += rc._CACHE_TTL_SECONDS - 1
        p.get()
        assert f.call_count == 1  # still within TTL


def test_provider_refetches_after_ttl(tmp_path):
    clock = [0.0]
    p = _provider(tmp_path, clock)
    with mock.patch.object(RelayCredentialProvider, "_fetch", return_value=_CRED) as f:
        p.get()
        clock[0] += rc._CACHE_TTL_SECONDS + 0.1
        p.get()
        assert f.call_count == 2


def test_provider_disabled_returns_none_no_fetch(tmp_path):
    config = _make_test_config(tmp_path)  # email disabled
    p = RelayCredentialProvider(config=config)
    with mock.patch.object(RelayCredentialProvider, "_fetch") as f:
        assert p.get() is None
        f.assert_not_called()


def test_provider_fetch_http_error_raises(tmp_path):
    p = _provider(tmp_path, [0.0])
    with mock.patch("httpx.Client") as C:
        C.return_value.__enter__.return_value.get.side_effect = httpx.ConnectError("down")
        with mock.patch("compute_space.core.email.relay_credential.KeycloakTokenProvider"):
            with pytest.raises(rc.RelayCredentialError):
                p._fetch()


def test_provider_fetch_non_200_raises(tmp_path):
    p = _provider(tmp_path, [0.0])
    resp = mock.Mock(status_code=503)
    with (
        mock.patch("compute_space.core.email.relay_credential.KeycloakTokenProvider"),
        mock.patch("httpx.Client") as C,
    ):
        C.return_value.__enter__.return_value.get.return_value = resp
        with pytest.raises(rc.RelayCredentialError):
            p._fetch()


def test_provider_fetch_unconfigured_body_raises(tmp_path):
    p = _provider(tmp_path, [0.0])
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {"configured": False}
    with (
        mock.patch("compute_space.core.email.relay_credential.KeycloakTokenProvider"),
        mock.patch("httpx.Client") as C,
    ):
        C.return_value.__enter__.return_value.get.return_value = resp
        with pytest.raises(rc.RelayCredentialError):
            p._fetch()


def test_provider_fetch_malformed_body_raises(tmp_path):
    p = _provider(tmp_path, [0.0])
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {"configured": True}  # missing required fields
    with (
        mock.patch("compute_space.core.email.relay_credential.KeycloakTokenProvider"),
        mock.patch("httpx.Client") as C,
    ):
        C.return_value.__enter__.return_value.get.return_value = resp
        with pytest.raises(rc.RelayCredentialError):
            p._fetch()
