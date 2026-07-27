"""Edge-case tests for the shared per-instance Imbue credential resolution.

A single per-instance credential (``imbue_identity_*``) backs every Imbue
service. Each service resolves its credential from its own DEPRECATED per-service
override first, then falls back to the shared identity:

  * cert-api:  cert_api_keycloak_* override  ->  imbue_identity_*
  * email:     email_keycloak_* override     ->  cert_api_*_resolved  ->  imbue_identity_*

``instance_identity`` follows the *cert-api* chain (cert_api override -> imbue),
NOT the email override — that is a deliberate asymmetry these tests pin.

Everything here probes ACTUAL behavior confirmed against the source:
  * resolution uses ``a or b`` semantics, so "" (empty) is treated as unset but
    whitespace-only strings are truthy and therefore NOT treated as unset.
  * cert_provider=cert_api validation names the settable field, not *_resolved.
  * email_proxy_base_url with a *fully-absent* credential is the awaiting-connect
    state (no error, email off); with a *partially-resolved* credential it errors;
    with a *fully-resolved* credential it additionally requires public_ip.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typed_settings

from compute_space.config import CERT_PROVIDER_ACME
from compute_space.config import CERT_PROVIDER_CERT_API
from compute_space.config import DefaultConfig

_ISSUER = "https://keycloak.example.com/realms/openhost-customers"
_PROXY = "https://openhost.imbue.com"
_IP = "203.0.113.10"


def _cfg(**kwargs: object) -> DefaultConfig:
    return DefaultConfig(zone_domain="alice.host.example.com", **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# cert-api resolver chain: cert_api_keycloak_* override -> imbue_identity_*
# Each of issuer / client_id / secret resolves independently.
# ---------------------------------------------------------------------------


def test_cert_issuer_from_imbue_only() -> None:
    cfg = _cfg(imbue_identity_issuer_url=_ISSUER)
    assert cfg.cert_api_keycloak_issuer_url_resolved == _ISSUER


def test_cert_client_id_from_imbue_only() -> None:
    cfg = _cfg(imbue_identity_client_id="imbue-id")
    assert cfg.cert_api_keycloak_client_id_resolved == "imbue-id"


def test_cert_secret_from_imbue_only() -> None:
    cfg = _cfg(imbue_identity_client_secret="imbue-s")
    assert cfg.cert_api_keycloak_client_secret_resolved == "imbue-s"


def test_cert_issuer_override_beats_imbue() -> None:
    cfg = _cfg(cert_api_keycloak_issuer_url="https://ov/realms/x", imbue_identity_issuer_url=_ISSUER)
    assert cfg.cert_api_keycloak_issuer_url_resolved == "https://ov/realms/x"


def test_cert_client_id_override_beats_imbue() -> None:
    cfg = _cfg(cert_api_keycloak_client_id="ov-id", imbue_identity_client_id="imbue-id")
    assert cfg.cert_api_keycloak_client_id_resolved == "ov-id"


def test_cert_secret_override_beats_imbue() -> None:
    cfg = _cfg(cert_api_keycloak_client_secret="ov-s", imbue_identity_client_secret="imbue-s")
    assert cfg.cert_api_keycloak_client_secret_resolved == "ov-s"


def test_cert_override_only_no_imbue() -> None:
    cfg = _cfg(
        cert_api_keycloak_issuer_url="https://ov/realms/x",
        cert_api_keycloak_client_id="ov-id",
        cert_api_keycloak_client_secret="ov-s",
    )
    assert cfg.cert_api_keycloak_issuer_url_resolved == "https://ov/realms/x"
    assert cfg.cert_api_keycloak_client_id_resolved == "ov-id"
    assert cfg.cert_api_keycloak_client_secret_resolved == "ov-s"


def test_cert_mixed_issuer_override_id_secret_from_imbue() -> None:
    # A per-field mix: cert-api override supplies only the issuer, imbue the rest.
    cfg = _cfg(
        cert_api_keycloak_issuer_url="https://ov/realms/x",
        imbue_identity_client_id="imbue-id",
        imbue_identity_client_secret="imbue-s",
    )
    assert cfg.cert_api_keycloak_issuer_url_resolved == "https://ov/realms/x"
    assert cfg.cert_api_keycloak_client_id_resolved == "imbue-id"
    assert cfg.cert_api_keycloak_client_secret_resolved == "imbue-s"


def test_cert_resolvers_none_when_nothing_set() -> None:
    cfg = _cfg()
    assert cfg.cert_api_keycloak_issuer_url_resolved is None
    assert cfg.cert_api_keycloak_client_id_resolved is None
    assert cfg.cert_api_keycloak_client_secret_resolved is None


# ---------------------------------------------------------------------------
# email resolver chain: email_keycloak_* -> cert_api_*_resolved -> imbue_identity_*
# ---------------------------------------------------------------------------


def test_email_issuer_from_imbue_via_cert_chain() -> None:
    cfg = _cfg(imbue_identity_issuer_url=_ISSUER)
    assert cfg.email_keycloak_issuer_url_resolved == _ISSUER


def test_email_client_id_from_imbue_via_cert_chain() -> None:
    cfg = _cfg(imbue_identity_client_id="imbue-id")
    assert cfg.email_keycloak_client_id_resolved == "imbue-id"


def test_email_secret_from_imbue_via_cert_chain() -> None:
    cfg = _cfg(imbue_identity_client_secret="imbue-s")
    assert cfg.email_keycloak_client_secret_resolved == "imbue-s"


def test_email_issuer_from_cert_api_override_over_imbue() -> None:
    cfg = _cfg(cert_api_keycloak_issuer_url="https://cert/realms/x", imbue_identity_issuer_url=_ISSUER)
    assert cfg.email_keycloak_issuer_url_resolved == "https://cert/realms/x"


def _email_override(**overrides: str) -> dict[str, object]:
    # The email override must be all-three-or-none (partial is rejected), so to
    # test a single field's precedence we set all three and only vary the one
    # under test to a distinctive value.
    base: dict[str, object] = dict(
        email_keycloak_issuer_url="https://email/realms/x",
        email_keycloak_client_id="email-id",
        email_keycloak_client_secret="email-s",
    )
    base.update(overrides)
    return base


def test_email_override_issuer_beats_cert_api_and_imbue() -> None:
    cfg = _cfg(
        cert_api_keycloak_issuer_url="https://cert/realms/x",
        imbue_identity_issuer_url=_ISSUER,
        **_email_override(email_keycloak_issuer_url="https://email-issuer/realms/x"),
    )
    assert cfg.email_keycloak_issuer_url_resolved == "https://email-issuer/realms/x"


def test_email_override_client_id_beats_cert_api_and_imbue() -> None:
    cfg = _cfg(
        cert_api_keycloak_client_id="cert-id",
        imbue_identity_client_id="imbue-id",
        **_email_override(email_keycloak_client_id="the-email-id"),
    )
    assert cfg.email_keycloak_client_id_resolved == "the-email-id"


def test_email_override_secret_beats_cert_api_and_imbue() -> None:
    cfg = _cfg(
        cert_api_keycloak_client_secret="cert-s",
        imbue_identity_client_secret="imbue-s",
        **_email_override(email_keycloak_client_secret="the-email-s"),
    )
    assert cfg.email_keycloak_client_secret_resolved == "the-email-s"


def test_email_resolves_per_field_across_all_three_sources() -> None:
    # issuer from email override, id from cert-api override, secret from imbue.
    cfg = _cfg(
        email_keycloak_issuer_url="https://email/realms/x",
        email_keycloak_client_id="email-id",
        email_keycloak_client_secret="email-s",
        cert_api_keycloak_issuer_url="https://cert/realms/x",
        cert_api_keycloak_client_id="cert-id",
        cert_api_keycloak_client_secret="cert-s",
        imbue_identity_issuer_url=_ISSUER,
        imbue_identity_client_id="imbue-id",
        imbue_identity_client_secret="imbue-s",
    )
    # email override wins for all three.
    assert cfg.email_keycloak_issuer_url_resolved == "https://email/realms/x"
    assert cfg.email_keycloak_client_id_resolved == "email-id"
    assert cfg.email_keycloak_client_secret_resolved == "email-s"


def test_email_resolvers_none_when_nothing_set() -> None:
    cfg = _cfg()
    assert cfg.email_keycloak_issuer_url_resolved is None
    assert cfg.email_keycloak_client_id_resolved is None
    assert cfg.email_keycloak_client_secret_resolved is None


def test_cert_api_partial_plus_imbue_completes_email_chain() -> None:
    # cert-api override supplies 2 of 3; imbue supplies the missing secret. Email's
    # resolved chain must combine them.
    cfg = _cfg(
        cert_api_keycloak_issuer_url="https://cert/realms/x",
        cert_api_keycloak_client_id="cert-id",
        imbue_identity_client_secret="imbue-s",
    )
    assert cfg.email_keycloak_issuer_url_resolved == "https://cert/realms/x"
    assert cfg.email_keycloak_client_id_resolved == "cert-id"
    assert cfg.email_keycloak_client_secret_resolved == "imbue-s"


# ---------------------------------------------------------------------------
# instance_identity: present iff all three resolve via the CERT-API chain;
# None otherwise. Email override does NOT feed instance_identity.
# ---------------------------------------------------------------------------


def test_instance_identity_from_imbue_only() -> None:
    cfg = _cfg(
        imbue_identity_issuer_url=_ISSUER,
        imbue_identity_client_id="imbue-id",
        imbue_identity_client_secret="imbue-s",
    )
    ident = cfg.instance_identity
    assert ident is not None
    assert ident.issuer_url == _ISSUER
    assert ident.client_id == "imbue-id"
    assert ident.client_secret == "imbue-s"


def test_instance_identity_from_cert_api_override_only() -> None:
    cfg = _cfg(
        cert_api_keycloak_issuer_url="https://cert/realms/x",
        cert_api_keycloak_client_id="cert-id",
        cert_api_keycloak_client_secret="cert-s",
    )
    ident = cfg.instance_identity
    assert ident is not None
    assert ident.client_id == "cert-id"
    assert ident.client_secret == "cert-s"


def test_instance_identity_mixed_cert_override_and_imbue() -> None:
    cfg = _cfg(
        cert_api_keycloak_issuer_url="https://cert/realms/x",
        imbue_identity_client_id="imbue-id",
        imbue_identity_client_secret="imbue-s",
    )
    ident = cfg.instance_identity
    assert ident is not None
    assert ident.issuer_url == "https://cert/realms/x"
    assert ident.client_id == "imbue-id"
    assert ident.client_secret == "imbue-s"


def test_instance_identity_cert_override_wins_over_imbue() -> None:
    cfg = _cfg(
        cert_api_keycloak_issuer_url="https://cert/realms/x",
        cert_api_keycloak_client_id="cert-id",
        cert_api_keycloak_client_secret="cert-s",
        imbue_identity_issuer_url=_ISSUER,
        imbue_identity_client_id="imbue-id",
        imbue_identity_client_secret="imbue-s",
    )
    ident = cfg.instance_identity
    assert ident is not None
    assert ident.client_id == "cert-id"
    assert ident.client_secret == "cert-s"


def test_instance_identity_ignores_email_override() -> None:
    # A crucial asymmetry: instance_identity follows the cert-api chain only, so a
    # config whose ONLY credential is an email override yields NO instance identity.
    cfg = _cfg(
        email_keycloak_issuer_url="https://email/realms/x",
        email_keycloak_client_id="email-id",
        email_keycloak_client_secret="email-s",
    )
    assert cfg.instance_identity is None
    # ...even though the email resolver does expose that override.
    assert cfg.email_keycloak_client_id_resolved == "email-id"


@pytest.mark.parametrize("present", ["issuer", "client_id", "client_secret"])
def test_instance_identity_none_when_only_one_imbue_part(present: str) -> None:
    kwargs: dict[str, dict[str, object]] = {
        "issuer": {"imbue_identity_issuer_url": _ISSUER},
        "client_id": {"imbue_identity_client_id": "imbue-id"},
        "client_secret": {"imbue_identity_client_secret": "imbue-s"},
    }
    cfg = _cfg(**kwargs[present])
    assert cfg.instance_identity is None


@pytest.mark.parametrize("missing", ["issuer", "client_id", "client_secret"])
def test_instance_identity_none_when_one_imbue_part_missing(missing: str) -> None:
    kwargs: dict[str, object] = {
        "imbue_identity_issuer_url": _ISSUER,
        "imbue_identity_client_id": "imbue-id",
        "imbue_identity_client_secret": "imbue-s",
    }
    field = {
        "issuer": "imbue_identity_issuer_url",
        "client_id": "imbue_identity_client_id",
        "client_secret": "imbue_identity_client_secret",
    }[missing]
    del kwargs[field]
    cfg = _cfg(**kwargs)
    assert cfg.instance_identity is None


@pytest.mark.parametrize("missing", ["issuer", "client_id", "client_secret"])
def test_instance_identity_none_when_one_cert_override_part_missing(missing: str) -> None:
    kwargs: dict[str, object] = {
        "cert_api_keycloak_issuer_url": "https://cert/realms/x",
        "cert_api_keycloak_client_id": "cert-id",
        "cert_api_keycloak_client_secret": "cert-s",
    }
    field = {
        "issuer": "cert_api_keycloak_issuer_url",
        "client_id": "cert_api_keycloak_client_id",
        "client_secret": "cert_api_keycloak_client_secret",
    }[missing]
    del kwargs[field]
    cfg = _cfg(**kwargs)
    assert cfg.instance_identity is None


def test_instance_identity_split_across_cert_and_imbue_completes() -> None:
    # issuer from cert override, id from cert override, secret from imbue -> full.
    cfg = _cfg(
        cert_api_keycloak_issuer_url="https://cert/realms/x",
        cert_api_keycloak_client_id="cert-id",
        imbue_identity_client_secret="imbue-s",
    )
    assert cfg.instance_identity is not None
    assert cfg.instance_identity.client_secret == "imbue-s"


def test_instance_identity_token_endpoint_derived() -> None:
    cfg = _cfg(
        imbue_identity_issuer_url=_ISSUER,
        imbue_identity_client_id="imbue-id",
        imbue_identity_client_secret="imbue-s",
    )
    assert cfg.instance_identity is not None
    assert cfg.instance_identity.token_endpoint == _ISSUER + "/protocol/openid-connect/token"


# ---------------------------------------------------------------------------
# cert_provider = cert_api validation
# ---------------------------------------------------------------------------


def _cert_api(**kwargs: object) -> DefaultConfig:
    base: dict[str, object] = dict(
        cert_provider=CERT_PROVIDER_CERT_API,
        cert_api_base_url="https://cert-api.example.com",
    )
    base.update(kwargs)
    return _cfg(**base)


def test_cert_api_satisfied_by_imbue_alone() -> None:
    cfg = _cert_api(
        imbue_identity_issuer_url=_ISSUER,
        imbue_identity_client_id="imbue-id",
        imbue_identity_client_secret="imbue-s",
    )
    assert cfg.cert_provider == CERT_PROVIDER_CERT_API
    assert cfg.instance_identity is not None


def test_cert_api_satisfied_by_cert_override_alone() -> None:
    cfg = _cert_api(
        cert_api_keycloak_issuer_url="https://cert/realms/x",
        cert_api_keycloak_client_id="cert-id",
        cert_api_keycloak_client_secret="cert-s",
    )
    assert cfg.cert_provider == CERT_PROVIDER_CERT_API


def test_cert_api_satisfied_by_mixed_override_and_imbue() -> None:
    cfg = _cert_api(
        cert_api_keycloak_issuer_url="https://cert/realms/x",
        cert_api_keycloak_client_id="cert-id",
        imbue_identity_client_secret="imbue-s",
    )
    assert cfg.cert_provider == CERT_PROVIDER_CERT_API


def test_cert_api_rejected_when_no_credential_anywhere() -> None:
    with pytest.raises(ValueError, match="cert_api_keycloak_issuer_url must be set"):
        _cert_api()


def test_cert_api_error_names_settable_field_not_resolved() -> None:
    # imbue supplies issuer + id but no secret; the error must name the SETTABLE
    # field cert_api_keycloak_client_secret, never the internal *_resolved property.
    with pytest.raises(ValueError) as exc:
        _cert_api(
            imbue_identity_issuer_url=_ISSUER,
            imbue_identity_client_id="imbue-id",
        )
    msg = str(exc.value)
    assert "cert_api_keycloak_client_secret must be set" in msg
    assert "_resolved" not in msg


@pytest.mark.parametrize(
    ("provided", "missing_field"),
    [
        (("client_id", "client_secret"), "cert_api_keycloak_issuer_url"),
        (("issuer", "client_secret"), "cert_api_keycloak_client_id"),
        (("issuer", "client_id"), "cert_api_keycloak_client_secret"),
    ],
)
def test_cert_api_partial_imbue_reports_first_missing(provided: tuple[str, str], missing_field: str) -> None:
    src = {
        "issuer": {"imbue_identity_issuer_url": _ISSUER},
        "client_id": {"imbue_identity_client_id": "imbue-id"},
        "client_secret": {"imbue_identity_client_secret": "imbue-s"},
    }
    kwargs: dict[str, object] = {}
    for part in provided:
        kwargs.update(src[part])
    with pytest.raises(ValueError, match=f"{missing_field} must be set"):
        _cert_api(**kwargs)


def test_cert_api_two_of_three_from_cert_override_missing_secret() -> None:
    with pytest.raises(ValueError, match="cert_api_keycloak_client_secret must be set"):
        _cert_api(
            cert_api_keycloak_issuer_url="https://cert/realms/x",
            cert_api_keycloak_client_id="cert-id",
        )


def test_cert_api_missing_base_url_errors_first() -> None:
    # base_url is validated before the credential; even a full credential can't
    # rescue a missing broker URL.
    with pytest.raises(ValueError, match="cert_api_base_url must be set"):
        _cfg(
            cert_provider=CERT_PROVIDER_CERT_API,
            cert_api_base_url=None,
            imbue_identity_issuer_url=_ISSUER,
            imbue_identity_client_id="imbue-id",
            imbue_identity_client_secret="imbue-s",
        )


def test_acme_never_requires_credential() -> None:
    cfg = _cfg()
    assert cfg.cert_provider == CERT_PROVIDER_ACME
    # No identity and no error.
    assert cfg.instance_identity is None


def test_acme_ignores_partial_imbue_identity() -> None:
    # A partial shared identity is harmless on the default acme path (no validation).
    cfg = _cfg(imbue_identity_issuer_url=_ISSUER)
    assert cfg.cert_provider == CERT_PROVIDER_ACME
    assert cfg.instance_identity is None


# ---------------------------------------------------------------------------
# email_enabled across credential sources / prerequisites
# ---------------------------------------------------------------------------


def _imbue3() -> dict[str, object]:
    return dict(
        imbue_identity_issuer_url=_ISSUER,
        imbue_identity_client_id="imbue-id",
        imbue_identity_client_secret="imbue-s",
    )


def test_email_enabled_via_imbue_proxy_and_ip() -> None:
    cfg = _cfg(email_proxy_base_url=_PROXY, public_ip=_IP, **_imbue3())
    assert cfg.email_enabled is True


def test_email_enabled_via_cert_api_override_proxy_and_ip() -> None:
    cfg = _cfg(
        email_proxy_base_url=_PROXY,
        public_ip=_IP,
        cert_api_keycloak_issuer_url="https://cert/realms/x",
        cert_api_keycloak_client_id="cert-id",
        cert_api_keycloak_client_secret="cert-s",
    )
    assert cfg.email_enabled is True


def test_email_enabled_via_email_override_proxy_and_ip() -> None:
    cfg = _cfg(
        email_proxy_base_url=_PROXY,
        public_ip=_IP,
        email_keycloak_issuer_url="https://email/realms/x",
        email_keycloak_client_id="email-id",
        email_keycloak_client_secret="email-s",
    )
    assert cfg.email_enabled is True
    # This is the one enablement path that produces NO instance_identity (email
    # override doesn't feed the cert-api chain).
    assert cfg.instance_identity is None


def test_email_disabled_without_proxy_even_with_full_imbue() -> None:
    cfg = _cfg(public_ip=_IP, **_imbue3())
    assert cfg.email_enabled is False


def test_email_disabled_with_proxy_but_no_credential() -> None:
    # Awaiting-connect: no credential anywhere. Not an error, email off.
    cfg = _cfg(email_proxy_base_url=_PROXY)
    assert cfg.email_enabled is False


def test_email_enabled_is_false_when_only_two_imbue_parts_and_proxy() -> None:
    # Two of three imbue parts -> partially resolved -> this would ERROR when a
    # proxy is set, so email_enabled is exercised without the proxy here.
    cfg = _cfg(
        imbue_identity_issuer_url=_ISSUER,
        imbue_identity_client_id="imbue-id",
    )
    assert cfg.email_enabled is False


def test_email_prereq_reflects_resolved_not_raw_email_fields() -> None:
    # email_keycloak_* raw fields are all None, but the prereqs resolve via imbue,
    # so email turns on. Confirms email_enabled reads the *_resolved chain.
    cfg = _cfg(email_proxy_base_url=_PROXY, public_ip=_IP, **_imbue3())
    assert cfg.email_keycloak_issuer_url is None
    assert cfg.email_enabled is True


# ---------------------------------------------------------------------------
# Awaiting-connect: proxy + zero creds => no exception, off, no identity
# ---------------------------------------------------------------------------


def test_awaiting_connect_loads_without_exception() -> None:
    cfg = _cfg(email_proxy_base_url=_PROXY)
    assert cfg.email_enabled is False
    assert cfg.instance_identity is None


def test_awaiting_connect_exposes_connect_base_url() -> None:
    cfg = _cfg(email_proxy_base_url=_PROXY)
    assert cfg.imbue_connect_base_url == _PROXY


def test_awaiting_connect_does_not_require_public_ip() -> None:
    # No public_ip and no credential + proxy set: still fine (email off).
    cfg = _cfg(email_proxy_base_url=_PROXY)
    assert cfg.public_ip is None
    assert cfg.email_enabled is False


def test_connect_base_url_none_without_proxy() -> None:
    cfg = _cfg()
    assert cfg.imbue_connect_base_url is None


def test_awaiting_connect_then_evolve_to_connected() -> None:
    # Model the connect flow: start awaiting, then inject the shared identity + IP.
    awaiting = _cfg(email_proxy_base_url=_PROXY)
    assert awaiting.email_enabled is False
    connected = awaiting.evolve(public_ip=_IP, **_imbue3())
    assert connected.email_enabled is True
    assert connected.instance_identity is not None


# ---------------------------------------------------------------------------
# Partial-credential validation errors when a proxy is set
# ---------------------------------------------------------------------------


def test_proxy_with_imbue_issuer_only_errors() -> None:
    with pytest.raises(ValueError, match="only partially resolved"):
        _cfg(email_proxy_base_url=_PROXY, public_ip=_IP, imbue_identity_issuer_url=_ISSUER)


def test_proxy_with_imbue_two_parts_errors() -> None:
    with pytest.raises(ValueError, match="only partially resolved"):
        _cfg(
            email_proxy_base_url=_PROXY,
            public_ip=_IP,
            imbue_identity_issuer_url=_ISSUER,
            imbue_identity_client_id="imbue-id",
        )


def test_proxy_with_cert_two_of_three_errors() -> None:
    with pytest.raises(ValueError, match="only partially resolved"):
        _cfg(
            email_proxy_base_url=_PROXY,
            public_ip=_IP,
            cert_api_keycloak_issuer_url="https://cert/realms/x",
            cert_api_keycloak_client_id="cert-id",
        )


def test_proxy_with_cert_two_plus_imbue_one_completes_no_error() -> None:
    # cert override supplies 2, imbue the 3rd -> fully resolved, no partial error.
    cfg = _cfg(
        email_proxy_base_url=_PROXY,
        public_ip=_IP,
        cert_api_keycloak_issuer_url="https://cert/realms/x",
        cert_api_keycloak_client_id="cert-id",
        imbue_identity_client_secret="imbue-s",
    )
    assert cfg.email_enabled is True


def test_proxy_partial_names_missing_resolved_attrs() -> None:
    # The partial-credential error lists the missing *_resolved attrs (internal
    # names are acceptable here — this path is about resolution completeness).
    with pytest.raises(ValueError) as exc:
        _cfg(email_proxy_base_url=_PROXY, public_ip=_IP, imbue_identity_issuer_url=_ISSUER)
    msg = str(exc.value)
    assert "email_keycloak_client_id_resolved" in msg
    assert "email_keycloak_client_secret_resolved" in msg


def test_proxy_partial_error_even_without_public_ip() -> None:
    # The partial-credential check fires before the public_ip check.
    with pytest.raises(ValueError, match="only partially resolved"):
        _cfg(email_proxy_base_url=_PROXY, imbue_identity_issuer_url=_ISSUER)


def test_email_override_partial_rejected_before_proxy_logic() -> None:
    # Partial email_keycloak_* override is rejected as a typo regardless of proxy.
    with pytest.raises(ValueError, match="partially configured"):
        _cfg(
            email_keycloak_issuer_url="https://email/realms/x",
            email_keycloak_client_id="email-id",
        )


def test_email_override_partial_rejected_even_with_full_imbue() -> None:
    # A partial explicit override is a typo even though imbue could satisfy the
    # chain — the override-completeness check is independent of resolution.
    with pytest.raises(ValueError, match="partially configured"):
        _cfg(email_keycloak_issuer_url="https://email/realms/x", **_imbue3())


# ---------------------------------------------------------------------------
# public_ip requirement gating
# ---------------------------------------------------------------------------


def test_full_credential_plus_proxy_requires_public_ip() -> None:
    with pytest.raises(ValueError, match="public_ip must be set"):
        _cfg(email_proxy_base_url=_PROXY, **_imbue3())


def test_full_credential_plus_proxy_with_public_ip_ok() -> None:
    cfg = _cfg(email_proxy_base_url=_PROXY, public_ip=_IP, **_imbue3())
    assert cfg.email_enabled is True
    assert cfg.public_ip == _IP


def test_public_ip_not_required_without_proxy() -> None:
    # A full credential but no proxy: email off, no public_ip needed.
    cfg = _cfg(**_imbue3())
    assert cfg.public_ip is None
    assert cfg.email_enabled is False


def test_public_ip_not_required_in_awaiting_connect() -> None:
    cfg = _cfg(email_proxy_base_url=_PROXY)
    assert cfg.public_ip is None
    assert cfg.email_enabled is False


def test_cert_api_provider_alone_does_not_require_public_ip() -> None:
    # cert_api provider validation is independent of email; no proxy => no public_ip.
    cfg = _cert_api(**_imbue3())
    assert cfg.public_ip is None
    assert cfg.cert_provider == CERT_PROVIDER_CERT_API


# ---------------------------------------------------------------------------
# TOML round-trips
# ---------------------------------------------------------------------------


def _load(path: Path) -> DefaultConfig:
    return typed_settings.load(DefaultConfig, appname="openhost", config_files=[str(path)])


def test_toml_round_trip_preserves_imbue_identity(tmp_path: Path) -> None:
    cfg = _cfg(**_imbue3())
    rendered = cfg.to_toml_str()
    assert f'imbue_identity_issuer_url = "{_ISSUER}"' in rendered
    assert 'imbue_identity_client_id = "imbue-id"' in rendered
    assert 'imbue_identity_client_secret = "imbue-s"' in rendered
    out = tmp_path / "config.toml"
    out.write_text(rendered)
    reloaded = _load(out)
    assert reloaded.imbue_identity_issuer_url == _ISSUER
    assert reloaded.instance_identity is not None
    assert reloaded.instance_identity.client_secret == "imbue-s"


def test_toml_round_trip_resolvers_still_work(tmp_path: Path) -> None:
    cfg = _cfg(**_imbue3())
    out = tmp_path / "config.toml"
    out.write_text(cfg.to_toml_str())
    reloaded = _load(out)
    assert reloaded.cert_api_keycloak_client_id_resolved == "imbue-id"
    assert reloaded.email_keycloak_client_secret_resolved == "imbue-s"


def test_toml_round_trip_cert_api_provider_via_imbue(tmp_path: Path) -> None:
    cfg = _cert_api(**_imbue3())
    out = tmp_path / "config.toml"
    out.write_text(cfg.to_toml_str())
    reloaded = _load(out)
    assert reloaded.cert_provider == CERT_PROVIDER_CERT_API
    assert reloaded.instance_identity is not None


def test_toml_round_trip_awaiting_connect(tmp_path: Path) -> None:
    cfg = _cfg(email_proxy_base_url=_PROXY)
    out = tmp_path / "config.toml"
    out.write_text(cfg.to_toml_str())
    reloaded = _load(out)
    assert reloaded.email_enabled is False
    assert reloaded.imbue_connect_base_url == _PROXY
    assert reloaded.instance_identity is None


def test_legacy_cert_api_only_config_loads_and_resolves(tmp_path: Path) -> None:
    # Backward compat: a config written before imbue_identity_* existed (only the
    # deprecated cert_api_keycloak_* fields) loads and resolves unchanged.
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[openhost]\n"
        'zone_domain = "alice.host.example.com"\n'
        "tls_enabled = true\n"
        "coredns_enabled = true\n"
        'cert_provider = "cert_api"\n'
        'cert_api_base_url = "https://cert-api.example.com"\n'
        'cert_api_keycloak_issuer_url = "https://kc.example.com/realms/openhost-customers"\n'
        'cert_api_keycloak_client_id = "instance-alice"\n'
        'cert_api_keycloak_client_secret = "legacy-s3cr3t"\n'
    )
    cfg = _load(config_path)
    assert cfg.imbue_identity_issuer_url is None
    assert cfg.cert_api_keycloak_client_secret_resolved == "legacy-s3cr3t"
    assert cfg.email_keycloak_client_secret_resolved == "legacy-s3cr3t"
    ident = cfg.instance_identity
    assert ident is not None
    assert ident.client_id == "instance-alice"
    assert ident.client_secret == "legacy-s3cr3t"


def test_legacy_email_override_config_loads_and_resolves(tmp_path: Path) -> None:
    # A pre-shared-identity email config using only email_keycloak_* overrides.
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[openhost]\n"
        'zone_domain = "alice.host.example.com"\n'
        "coredns_enabled = true\n"
        'public_ip = "203.0.113.10"\n'
        'email_proxy_base_url = "https://openhost.imbue.com"\n'
        'email_keycloak_issuer_url = "https://kc.example.com/realms/openhost-customers"\n'
        'email_keycloak_client_id = "instance-alice"\n'
        'email_keycloak_client_secret = "email-s3cr3t"\n'
    )
    cfg = _load(config_path)
    assert cfg.email_enabled is True
    assert cfg.email_keycloak_client_secret_resolved == "email-s3cr3t"
    # Email override doesn't feed instance_identity.
    assert cfg.instance_identity is None


def test_toml_round_trip_override_and_imbue_precedence_preserved(tmp_path: Path) -> None:
    cfg = _cfg(
        cert_api_keycloak_issuer_url="https://cert/realms/x",
        cert_api_keycloak_client_id="cert-id",
        cert_api_keycloak_client_secret="cert-s",
        **_imbue3(),
    )
    out = tmp_path / "config.toml"
    out.write_text(cfg.to_toml_str())
    reloaded = _load(out)
    # cert override still wins over imbue after a round trip.
    assert reloaded.cert_api_keycloak_client_id_resolved == "cert-id"
    assert reloaded.instance_identity is not None
    assert reloaded.instance_identity.client_id == "cert-id"


def test_evolve_preserves_imbue_identity() -> None:
    cfg = _cfg(**_imbue3())
    evolved = cfg.evolve(public_ip=_IP)
    assert evolved.instance_identity is not None
    assert evolved.instance_identity.client_id == "imbue-id"


# ---------------------------------------------------------------------------
# Whitespace / empty-string handling (probed: "" is unset, whitespace is NOT)
# ---------------------------------------------------------------------------


def test_empty_string_imbue_part_treated_as_unset() -> None:
    # Empty string is falsy, so the `or` chain skips it -> instance_identity None.
    cfg = _cfg(
        imbue_identity_issuer_url="",
        imbue_identity_client_id="imbue-id",
        imbue_identity_client_secret="imbue-s",
    )
    assert cfg.instance_identity is None


def test_empty_string_cert_override_falls_through_to_imbue() -> None:
    # An empty cert-api override falls through to the imbue value.
    cfg = _cfg(cert_api_keycloak_issuer_url="", imbue_identity_issuer_url=_ISSUER)
    assert cfg.cert_api_keycloak_issuer_url_resolved == _ISSUER


def test_empty_string_email_override_falls_through_to_cert_chain() -> None:
    cfg = _cfg(
        email_keycloak_issuer_url="",
        email_keycloak_client_id="",
        email_keycloak_client_secret="",
        cert_api_keycloak_issuer_url="https://cert/realms/x",
        cert_api_keycloak_client_id="cert-id",
        cert_api_keycloak_client_secret="cert-s",
    )
    # All-empty override counts as "none set" (falsy), so it's not a partial-config
    # error and the cert-api chain supplies the values.
    assert cfg.email_keycloak_issuer_url_resolved == "https://cert/realms/x"
    assert cfg.email_keycloak_client_id_resolved == "cert-id"


def test_whitespace_imbue_part_is_truthy_and_forms_identity() -> None:
    # Documented ACTUAL behavior: whitespace is NOT stripped/treated as unset, so a
    # whitespace-only value is truthy and produces a (garbage) credential. Pinned so
    # a future change to strip whitespace is a conscious, visible decision.
    cfg = _cfg(
        imbue_identity_issuer_url="   ",
        imbue_identity_client_id="imbue-id",
        imbue_identity_client_secret="imbue-s",
    )
    ident = cfg.instance_identity
    assert ident is not None
    assert ident.issuer_url == "   "


def test_whitespace_override_beats_imbue() -> None:
    # Because whitespace is truthy, a whitespace cert override shadows the real
    # imbue value (again, pinning current behavior).
    cfg = _cfg(cert_api_keycloak_client_id="   ", imbue_identity_client_id="imbue-id")
    assert cfg.cert_api_keycloak_client_id_resolved == "   "


# ---------------------------------------------------------------------------
# email override still wins over imbue (explicit precedence pin)
# ---------------------------------------------------------------------------


def test_email_override_wins_over_imbue_all_three() -> None:
    cfg = _cfg(
        email_proxy_base_url=_PROXY,
        public_ip=_IP,
        email_keycloak_issuer_url="https://email/realms/x",
        email_keycloak_client_id="email-id",
        email_keycloak_client_secret="email-s",
        **_imbue3(),
    )
    assert cfg.email_keycloak_issuer_url_resolved == "https://email/realms/x"
    assert cfg.email_keycloak_client_id_resolved == "email-id"
    assert cfg.email_keycloak_client_secret_resolved == "email-s"
    # But instance_identity still follows the cert-api chain -> imbue values.
    assert cfg.instance_identity is not None
    assert cfg.instance_identity.client_id == "imbue-id"


def test_email_override_wins_over_cert_api_and_imbue_mixed() -> None:
    cfg = _cfg(
        email_keycloak_issuer_url="https://email/realms/x",
        email_keycloak_client_id="email-id",
        email_keycloak_client_secret="email-s",
        cert_api_keycloak_issuer_url="https://cert/realms/x",
        cert_api_keycloak_client_id="cert-id",
        cert_api_keycloak_client_secret="cert-s",
        **_imbue3(),
    )
    assert cfg.email_keycloak_client_secret_resolved == "email-s"
    # cert-api resolver is unaffected by the email override.
    assert cfg.cert_api_keycloak_client_secret_resolved == "cert-s"
