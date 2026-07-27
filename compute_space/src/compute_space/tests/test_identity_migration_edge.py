"""Cross-cutting MIGRATION / BACKWARD-COMPAT / GRACEFUL-DEGRADE / NEGATIVE-SECURITY
tests for the shared instance identity + Connect-to-Imbue seams.

These complement the single-layer unit tests (``test_instance_identity.py``,
``test_identity_edge.py``, ``test_connect.py``, ``test_connect_edge.py``,
``test_email_config.py``) by exercising the *combinations* that a per-layer test
would miss: a real old-style ``config.toml`` loaded through the production
``load_config`` / ``typed_settings.load`` path AND then resolved into working
credentials; the obsolete-key scrubber run alongside the new ``imbue_identity_*``
fields; ``persist_instance_identity`` layered onto a config that already carries a
deprecated per-service override (and which credential then wins); and the
awaiting-connect graceful-degrade state.

Everything here asserts behavior confirmed against the real source. Two results
worth calling out because they are load-bearing and easy to get wrong:

  * ``instance_identity`` follows the CERT-API resolver chain
    (``cert_api_keycloak_*`` override -> ``imbue_identity_*``). So when
    ``persist_instance_identity`` writes ``imbue_identity_*`` onto a config that
    STILL has a ``cert_api_keycloak_*`` override, the DEPRECATED override keeps
    winning (see ``test_persist_onto_old_cert_api_config_override_still_wins``).
    That is the documented precedence and is pinned here.

  * The production ``load_config`` path reads ``OPENHOST_``-prefixed env vars via
    typed-settings, which would override values from the TOML file. Tests that go
    through that path scrub the environment first (``_clear_openhost_env``) so the
    file is the sole source of truth and the tests are deterministic in CI.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from pathlib import Path

import pytest
import typed_settings

from compute_space import config as config_mod
from compute_space.config import CERT_PROVIDER_ACME
from compute_space.config import CERT_PROVIDER_CERT_API
from compute_space.config import DefaultConfig
from compute_space.config import load_config
from compute_space.core.connect import persist_instance_identity
from compute_space.core.tls.keycloak import KeycloakClientCredentials

_ISSUER = "https://kc.example.com/realms/openhost-customers"
_PROXY = "https://openhost.imbue.com"
_IP = "203.0.113.10"


def _clear_openhost_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every ``OPENHOST_``-prefixed env var so the TOML file is the sole
    config source when going through ``load_config`` / ``typed_settings.load``.

    Real deploy/dev shells (and this repo's own agent host) export
    ``OPENHOST_ZONE_DOMAIN`` and friends; typed-settings would let those shadow the
    file under test, making an assertion on a file value nondeterministic.
    """
    for name in list(os.environ):
        if name.startswith("OPENHOST_"):
            monkeypatch.delenv(name, raising=False)


def _load_file(path: Path) -> DefaultConfig:
    """Load a config file through typed-settings exactly as production does."""
    return typed_settings.load(DefaultConfig, appname="openhost", config_files=[str(path)])


def _flatten_error(exc: BaseException) -> str:
    """Join the messages of an exception and everything it wraps into one string.

    typed-settings surfaces a Config ``ValueError`` raised in
    ``__attrs_post_init__`` as an ``InvalidSettingsError`` whose real message lives
    in a nested sub-exception / ``__cause__`` chain. Flattening lets a file-load
    test assert on the original validation message regardless of the wrapping.
    """
    parts = [str(exc)]
    for sub in getattr(exc, "exceptions", ()):  # ExceptionGroup members
        parts.append(_flatten_error(sub))
    if exc.__cause__ is not None:
        parts.append(_flatten_error(exc.__cause__))
    if exc.__context__ is not None and exc.__context__ is not exc.__cause__:
        parts.append(_flatten_error(exc.__context__))
    return " ".join(parts)


def _assert_load_rejects(path: Path, expected_substring: str) -> None:
    """Assert loading ``path`` through typed-settings fails with the given message.

    A single Config ``ValueError`` becomes a wrapped ``InvalidSettingsError`` on
    the typed-settings load path, so we can't ``pytest.raises(ValueError, ...)``
    directly — we catch the wrapper and check the flattened message chain.
    """
    with pytest.raises(typed_settings.exceptions.InvalidSettingsError) as excinfo:
        _load_file(path)
    assert expected_substring in _flatten_error(excinfo.value)


def _cred(
    issuer: str = _ISSUER,
    client_id: str = "instance-alice",
    secret: str = "sekret",
) -> KeycloakClientCredentials:
    return KeycloakClientCredentials(issuer_url=issuer, client_id=client_id, client_secret=secret)


# ===========================================================================
# MIGRATION / BACKWARD-COMPAT
# ===========================================================================


def test_old_cert_api_config_loads_and_yields_working_creds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A pre-existing config.toml written the OLD way (cert_api_keycloak_* set,
    # cert_provider=cert_api, NO imbue_identity_*) must load through the real
    # typed-settings path AND produce a usable instance_identity plus resolved
    # cert/email creds — the live-verified backward-compat path.
    _clear_openhost_env(monkeypatch)
    p = tmp_path / "config.toml"
    p.write_text(
        "[openhost]\n"
        'zone_domain = "alice.host.example.com"\n'
        "tls_enabled = true\n"
        "coredns_enabled = true\n"
        'cert_provider = "cert_api"\n'
        'cert_api_base_url = "https://cert-api.example.com"\n'
        'cert_api_keycloak_issuer_url = "https://kc.old/realms/openhost-customers"\n'
        'cert_api_keycloak_client_id = "old-id"\n'
        'cert_api_keycloak_client_secret = "old-secret"\n'
    )
    cfg = _load_file(p)
    assert cfg.zone_domain == "alice.host.example.com"
    assert cfg.imbue_identity_issuer_url is None
    # cert creds resolve from the deprecated override.
    assert cfg.cert_api_keycloak_client_secret_resolved == "old-secret"
    # email creds inherit the cert-api override.
    assert cfg.email_keycloak_client_secret_resolved == "old-secret"
    ident = cfg.instance_identity
    assert ident is not None
    assert ident.client_id == "old-id"
    assert ident.client_secret == "old-secret"
    # cert-api build site consumes instance_identity, which is present.
    assert cfg.cert_provider == CERT_PROVIDER_CERT_API


def test_old_cert_api_config_loads_via_load_config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Same old-style config but exercised through the production ``load_config``
    # entrypoint (OPENHOST_ROUTER_CONFIG -> scrub -> typed_settings.load).
    _clear_openhost_env(monkeypatch)
    p = tmp_path / "config.toml"
    p.write_text(
        "[openhost]\n"
        'zone_domain = "bob.host.example.com"\n'
        'cert_provider = "cert_api"\n'
        'cert_api_base_url = "https://cert-api.example.com"\n'
        'cert_api_keycloak_issuer_url = "https://kc.old/realms/openhost-customers"\n'
        'cert_api_keycloak_client_id = "bob-id"\n'
        'cert_api_keycloak_client_secret = "bob-secret"\n'
    )
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", str(p))
    cfg = load_config()
    assert cfg.zone_domain == "bob.host.example.com"
    assert cfg.instance_identity is not None
    assert cfg.instance_identity.client_secret == "bob-secret"


def test_old_email_override_config_loads_and_email_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A pre-shared-identity email config (only email_keycloak_* overrides) loads
    # and email_enabled derives True. instance_identity stays None because the
    # email override does NOT feed the cert-api chain.
    _clear_openhost_env(monkeypatch)
    p = tmp_path / "config.toml"
    p.write_text(
        "[openhost]\n"
        'zone_domain = "alice.host.example.com"\n'
        "coredns_enabled = true\n"
        'public_ip = "203.0.113.10"\n'
        'email_proxy_base_url = "https://openhost.imbue.com"\n'
        'email_keycloak_issuer_url = "https://kc.old/realms/openhost-customers"\n'
        'email_keycloak_client_id = "instance-alice"\n'
        'email_keycloak_client_secret = "email-secret"\n'
    )
    cfg = _load_file(p)
    assert cfg.email_enabled is True
    assert cfg.email_keycloak_client_secret_resolved == "email-secret"
    assert cfg.instance_identity is None


def test_obsolete_keys_plus_imbue_identity_loads_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A config carrying BOTH obsolete keys (email_enabled + email_inbound_mode)
    # AND the new imbue_identity_* must load cleanly via load_config: the obsolete
    # keys are scrubbed while the identity survives intact.
    _clear_openhost_env(monkeypatch)
    p = tmp_path / "config.toml"
    p.write_text(
        "[openhost]\n"
        'zone_domain = "alice.host.example.com"\n'
        "coredns_enabled = true\n"
        'public_ip = "203.0.113.10"\n'
        "email_enabled = true\n"
        'email_inbound_mode = "direct"\n'
        'email_inbound_mx_host = "inbound.example.com"\n'
        'email_proxy_base_url = "https://openhost.imbue.com"\n'
        f'imbue_identity_issuer_url = "{_ISSUER}"\n'
        'imbue_identity_client_id = "iid"\n'
        'imbue_identity_client_secret = "isec"\n'
    )
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", str(p))
    cfg = load_config()
    # obsolete keys are not Config fields.
    assert not hasattr(cfg, "email_inbound_mode")
    assert not hasattr(cfg, "email_inbound_mx_host")
    # identity intact and email derived on.
    assert cfg.instance_identity is not None
    assert cfg.instance_identity.client_id == "iid"
    assert cfg.email_enabled is True


def test_persist_onto_old_cert_api_config_override_still_wins(tmp_path: Path) -> None:
    # DOCUMENTED PRECEDENCE (surprising migration behavior, pinned here): applying
    # persist_instance_identity to an OLD-style config that still has a
    # cert_api_keycloak_* override writes the imbue_identity_* fields, but on
    # reload instance_identity STILL resolves to the deprecated cert-api override
    # (the cert-api resolver chain checks the override first). So a Connect-to-Imbue
    # on a cert-api-provisioned instance does NOT silently swap the live cert
    # credential — the override keeps priority until it is removed.
    p = tmp_path / "config.toml"
    p.write_text(
        "[openhost]\n"
        'zone_domain = "alice.host.example.com"\n'
        'cert_provider = "cert_api"\n'
        'cert_api_base_url = "https://cert-api.example.com"\n'
        'cert_api_keycloak_issuer_url = "https://kc.old/realms/openhost-customers"\n'
        'cert_api_keycloak_client_id = "old-id"\n'
        'cert_api_keycloak_client_secret = "old-secret"\n'
    )
    persist_instance_identity(str(p), _cred("https://kc.new/realms/x", "new-id", "new-secret"))
    data = tomllib.loads(p.read_text())["openhost"]
    # both credentials now live in the file...
    assert data["cert_api_keycloak_client_id"] == "old-id"
    assert data["imbue_identity_client_id"] == "new-id"
    # ...but the deprecated cert-api override wins the resolver.
    cfg = _load_file(p)
    assert cfg.imbue_identity_client_id == "new-id"
    assert cfg.cert_api_keycloak_client_id == "old-id"
    ident = cfg.instance_identity
    assert ident is not None
    assert ident.client_id == "old-id"
    assert ident.client_secret == "old-secret"


def test_persist_onto_old_cert_api_config_build_sites_stay_consistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # After persisting onto a cert_api-override config, BOTH build sites must see a
    # coherent credential: the cert-api site (config.instance_identity) and the
    # email resolver chain (email_*_resolved) both surface the same winning
    # override, so a Connect-to-Imbue on a cert-api instance can't split the two
    # services onto different credentials.
    _clear_openhost_env(monkeypatch)
    p = tmp_path / "config.toml"
    p.write_text(
        "[openhost]\n"
        'zone_domain = "alice.host.example.com"\n'
        'cert_provider = "cert_api"\n'
        'cert_api_base_url = "https://cert-api.example.com"\n'
        'cert_api_keycloak_issuer_url = "https://kc.old/realms/openhost-customers"\n'
        'cert_api_keycloak_client_id = "old-id"\n'
        'cert_api_keycloak_client_secret = "old-secret"\n'
    )
    persist_instance_identity(str(p), _cred("https://kc.new/realms/x", "new-id", "new-secret"))
    cfg = _load_file(p)
    # cert-api build site (config.instance_identity)
    assert cfg.instance_identity is not None
    assert cfg.instance_identity.client_secret == "old-secret"
    # email build site (email_*_resolved) — same winning override
    assert cfg.email_keycloak_client_secret_resolved == "old-secret"
    assert cfg.email_keycloak_client_id_resolved == "old-id"


def test_persist_then_scrub_together_preserve_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Realistic upgrade: an OLD config still carrying an obsolete key gets a
    # persisted identity, then is loaded through load_config (which scrubs the
    # obsolete key). The identity written by persist must survive the scrub.
    _clear_openhost_env(monkeypatch)
    p = tmp_path / "config.toml"
    p.write_text(
        '[openhost]\nzone_domain = "alice.host.example.com"\nemail_inbound_mode = "direct"\n'  # obsolete key present
    )
    persist_instance_identity(str(p), _cred(_ISSUER, "iid", "isec"))
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", str(p))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    before = set(tmp_path.glob("openhost-config-*.toml"))
    cfg = load_config()
    assert set(tmp_path.glob("openhost-config-*.toml")) == before
    assert cfg.instance_identity is not None
    assert cfg.instance_identity.client_id == "iid"


def test_persist_onto_bare_config_makes_imbue_the_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Contrast to the previous test: with NO cert-api override present, the
    # persisted imbue identity becomes the resolved source (the connect flow's
    # normal effect on a non-managed instance).
    _clear_openhost_env(monkeypatch)
    p = tmp_path / "config.toml"
    p.write_text('[openhost]\nzone_domain = "alice.host.example.com"\n')
    persist_instance_identity(str(p), _cred("https://kc.new/realms/x", "new-id", "new-secret"))
    cfg = _load_file(p)
    ident = cfg.instance_identity
    assert ident is not None
    assert ident.client_id == "new-id"
    assert ident.client_secret == "new-secret"


def test_default_config_imbue_round_trip_identity_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # DefaultConfig with imbue_identity_* -> to_toml -> reload equals the identity.
    _clear_openhost_env(monkeypatch)
    cfg = DefaultConfig(
        zone_domain="alice.host.example.com",
        imbue_identity_issuer_url=_ISSUER,
        imbue_identity_client_id="iid",
        imbue_identity_client_secret="isec",
    )
    p = tmp_path / "config.toml"
    cfg.to_toml(str(p))
    reloaded = _load_file(p)
    assert reloaded.instance_identity is not None
    assert reloaded.instance_identity == cfg.instance_identity
    assert reloaded.instance_identity.issuer_url == _ISSUER
    assert reloaded.instance_identity.client_id == "iid"
    assert reloaded.instance_identity.client_secret == "isec"


def test_scrub_leaves_no_temp_and_preserves_imbue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # _scrub_obsolete_keys_to_temp writes a cleaned copy when obsolete keys are
    # present; load_config must unlink it afterwards (it may carry secrets) while
    # leaving imbue_identity_* uncorrupted in the loaded config.
    _clear_openhost_env(monkeypatch)
    p = tmp_path / "config.toml"
    p.write_text(
        "[openhost]\n"
        'zone_domain = "alice.host.example.com"\n'
        "email_enabled = true\n"
        'email_inbound_mode = "direct"\n'
        f'imbue_identity_issuer_url = "{_ISSUER}"\n'
        'imbue_identity_client_id = "iid"\n'
        'imbue_identity_client_secret = "isec"\n'
    )
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", str(p))
    # Force the scrub temp file to land in tmp_path so we can prove it's cleaned up.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    before = set(tmp_path.glob("openhost-config-*.toml"))
    cfg = load_config()
    assert set(tmp_path.glob("openhost-config-*.toml")) == before  # no lingering temp
    assert cfg.instance_identity is not None
    assert cfg.instance_identity.client_secret == "isec"


def test_scrub_returns_same_path_when_no_obsolete_keys(tmp_path: Path) -> None:
    # When no obsolete key is present the scrubber must return the ORIGINAL path
    # unchanged (so load_config never writes/unlinks a temp for a modern config).
    p = tmp_path / "config.toml"
    p.write_text(
        "[openhost]\n"
        'zone_domain = "alice.host.example.com"\n'
        f'imbue_identity_issuer_url = "{_ISSUER}"\n'
        'imbue_identity_client_id = "iid"\n'
        'imbue_identity_client_secret = "isec"\n'
    )
    out = config_mod._scrub_obsolete_keys_to_temp(str(p))
    assert out == str(p)
    assert list(tmp_path.glob("openhost-config-*.toml")) == []


def test_scrub_temp_copy_keeps_imbue_and_drops_obsolete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Inspect the scrubbed temp copy directly: obsolete keys gone, imbue intact.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    p = tmp_path / "config.toml"
    p.write_text(
        "[openhost]\n"
        'zone_domain = "alice.host.example.com"\n'
        "email_enabled = true\n"
        'email_inbound_mode = "direct"\n'
        'email_inbound_mx_host = "mx.example.com"\n'
        f'imbue_identity_issuer_url = "{_ISSUER}"\n'
        'imbue_identity_client_id = "iid"\n'
        'imbue_identity_client_secret = "isec"\n'
    )
    out = config_mod._scrub_obsolete_keys_to_temp(str(p))
    assert out != str(p)
    try:
        scrubbed = tomllib.loads(Path(out).read_text())["openhost"]
        assert "email_enabled" not in scrubbed
        assert "email_inbound_mode" not in scrubbed
        assert "email_inbound_mx_host" not in scrubbed
        assert scrubbed["imbue_identity_issuer_url"] == _ISSUER
        assert scrubbed["imbue_identity_client_id"] == "iid"
        assert scrubbed["imbue_identity_client_secret"] == "isec"
    finally:
        os.unlink(out)


def test_scrub_preserves_non_openhost_sections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The scrubber only rewrites the [openhost] section; unrelated top-level tables
    # must pass through the scrubbed temp copy untouched.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    p = tmp_path / "config.toml"
    p.write_text(
        '[openhost]\nzone_domain = "alice.host.example.com"\nemail_inbound_mode = "direct"\n\n[other]\nkeep = "me"\n'
    )
    out = config_mod._scrub_obsolete_keys_to_temp(str(p))
    assert out != str(p)
    try:
        data = tomllib.loads(Path(out).read_text())
        assert data["other"] == {"keep": "me"}
        assert "email_inbound_mode" not in data["openhost"]
    finally:
        os.unlink(out)


def test_upgrade_cert_api_instance_to_email_by_adding_proxy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The documented seamless-upgrade path: an existing cert_api-provisioned
    # instance (deprecated cert_api_keycloak_* only) enables email by adding ONLY
    # email_proxy_base_url + public_ip — the email creds inherit the cert-api client.
    _clear_openhost_env(monkeypatch)
    p = tmp_path / "config.toml"
    p.write_text(
        "[openhost]\n"
        'zone_domain = "alice.host.example.com"\n'
        'cert_provider = "cert_api"\n'
        'cert_api_base_url = "https://cert-api.example.com"\n'
        'cert_api_keycloak_issuer_url = "https://kc.old/realms/openhost-customers"\n'
        'cert_api_keycloak_client_id = "old-id"\n'
        'cert_api_keycloak_client_secret = "old-secret"\n'
        f'public_ip = "{_IP}"\n'
        f'email_proxy_base_url = "{_PROXY}"\n'
    )
    cfg = _load_file(p)
    assert cfg.email_enabled is True
    # email creds resolved from the cert-api override (no email_keycloak_* set).
    assert cfg.email_keycloak_client_secret_resolved == "old-secret"
    assert cfg.email_keycloak_issuer_url is None


def test_from_toml_drops_obsolete_keeps_imbue(tmp_path: Path) -> None:
    # The non-DI DefaultConfig.from_toml path also drops obsolete keys and keeps
    # the identity (mirrors the load_config scrub, but via _drop_obsolete_keys).
    p = tmp_path / "config.toml"
    p.write_text(
        "[openhost]\n"
        'zone_domain = "alice.host.example.com"\n'
        "email_enabled = true\n"
        'email_inbound_mode = "direct"\n'
        f'imbue_identity_issuer_url = "{_ISSUER}"\n'
        'imbue_identity_client_id = "iid"\n'
        'imbue_identity_client_secret = "isec"\n'
    )
    cfg = DefaultConfig.from_toml(str(p))
    # email_enabled is a DERIVED property (always present), but the obsolete
    # STORED keys must not have become attributes — assert on those instead.
    assert not hasattr(cfg, "email_inbound_mode")
    assert not hasattr(cfg, "email_inbound_mx_host")
    assert cfg.instance_identity is not None
    assert cfg.instance_identity.client_id == "iid"


# ===========================================================================
# GRACEFUL DEGRADE
# ===========================================================================


def test_no_identity_acme_no_email_loads_and_stays_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No identity at all + default acme provider + no email proxy: the config loads
    # with email off, no instance identity, and the acme path requires nothing.
    # No exception at any layer.
    _clear_openhost_env(monkeypatch)
    p = tmp_path / "config.toml"
    p.write_text('[openhost]\nzone_domain = "alice.host.example.com"\n')
    cfg = _load_file(p)
    assert cfg.cert_provider == CERT_PROVIDER_ACME
    assert cfg.email_enabled is False
    assert cfg.instance_identity is None


def test_awaiting_connect_loads_email_off_no_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Front door set (email_proxy_base_url) but no credential = awaiting-connect.
    # A valid state: loads without error, email off, identity None.
    _clear_openhost_env(monkeypatch)
    p = tmp_path / "config.toml"
    p.write_text(f'[openhost]\nzone_domain = "alice.host.example.com"\nemail_proxy_base_url = "{_PROXY}"\n')
    cfg = _load_file(p)
    assert cfg.email_enabled is False
    assert cfg.instance_identity is None
    # ...and the connect front door is advertised.
    assert cfg.imbue_connect_base_url == _PROXY


def test_acme_with_partial_imbue_and_no_proxy_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A partial imbue identity is harmless on the default acme path with no proxy:
    # graceful degrade means it loads (identity None, no error) rather than being
    # rejected as a typo — the typo guard only fires when a proxy is present.
    _clear_openhost_env(monkeypatch)
    p = tmp_path / "config.toml"
    p.write_text(f'[openhost]\nzone_domain = "alice.host.example.com"\nimbue_identity_issuer_url = "{_ISSUER}"\n')
    cfg = _load_file(p)
    assert cfg.cert_provider == CERT_PROVIDER_ACME
    assert cfg.instance_identity is None


def test_imbue_connect_base_url_none_without_proxy() -> None:
    cfg = DefaultConfig(zone_domain="alice.host.example.com")
    assert cfg.email_proxy_base_url is None
    assert cfg.imbue_connect_base_url is None


def test_imbue_connect_base_url_equals_proxy_when_set() -> None:
    cfg = DefaultConfig(zone_domain="alice.host.example.com", email_proxy_base_url=_PROXY)
    assert cfg.imbue_connect_base_url == _PROXY
    assert cfg.imbue_connect_base_url is cfg.email_proxy_base_url


def test_awaiting_connect_then_persist_enables_via_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Full graceful-degrade -> connected migration: start awaiting-connect (proxy +
    # public_ip, no creds), persist the identity, reload -> email enabled and
    # identity present. Proves persist + normal config load closes the loop.
    _clear_openhost_env(monkeypatch)
    p = tmp_path / "config.toml"
    p.write_text(
        f'[openhost]\nzone_domain = "alice.host.example.com"\nemail_proxy_base_url = "{_PROXY}"\npublic_ip = "{_IP}"\n'
    )
    awaiting = _load_file(p)
    assert awaiting.email_enabled is False
    persist_instance_identity(str(p), _cred(_ISSUER, "iid", "isec"))
    connected = _load_file(p)
    assert connected.instance_identity is not None
    assert connected.email_enabled is True


# ===========================================================================
# NEGATIVE / SECURITY
# ===========================================================================


def test_persist_removes_secret_bearing_tmp_file(tmp_path: Path) -> None:
    # persist writes secrets via <path>.connect.tmp then os.replace; that temp
    # must NOT linger (it would be a secret-bearing artifact). The final config is
    # the only file left.
    p = tmp_path / "config.toml"
    p.write_text('[openhost]\nzone_domain = "alice.host.example.com"\n')
    persist_instance_identity(str(p), _cred(secret="top-secret-value"))
    assert not (tmp_path / "config.toml.connect.tmp").exists()
    leftovers = sorted(x.name for x in tmp_path.iterdir())
    assert leftovers == ["config.toml"]
    # The secret is only in the final file, nowhere else.
    assert "top-secret-value" in p.read_text()


def test_persisted_secret_not_written_to_any_tmp_dir_file(tmp_path: Path) -> None:
    # Belt-and-suspenders: after a persist, no *.tmp sibling exists at all in the
    # config's directory (so a crash-free write leaves no scratch copy of secrets).
    p = tmp_path / "config.toml"
    p.write_text('[openhost]\nzone_domain = "alice.host.example.com"\n')
    persist_instance_identity(str(p), _cred(secret="s3cr3t"))
    tmp_siblings = [x.name for x in tmp_path.iterdir() if x.name.endswith(".tmp")]
    assert tmp_siblings == []


@pytest.mark.parametrize("present", ["issuer_client_id", "issuer_secret", "client_id_secret"])
def test_two_of_three_imbue_parts_yields_no_identity(present: str) -> None:
    # A config with only 2 of 3 imbue parts must NOT leak a partial credential
    # object — instance_identity is None (all-or-nothing).
    parts = {
        "issuer_client_id": {
            "imbue_identity_issuer_url": _ISSUER,
            "imbue_identity_client_id": "iid",
        },
        "issuer_secret": {
            "imbue_identity_issuer_url": _ISSUER,
            "imbue_identity_client_secret": "isec",
        },
        "client_id_secret": {
            "imbue_identity_client_id": "iid",
            "imbue_identity_client_secret": "isec",
        },
    }[present]
    cfg = DefaultConfig(zone_domain="alice.host.example.com", **parts)  # type: ignore[arg-type]
    assert cfg.instance_identity is None


def test_empty_string_imbue_part_via_file_load_yields_no_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A blank ("") credential field written into the file is falsy, so the resolver
    # treats it as unset -> no partial credential object leaks (identity None). This
    # is the file-load analogue of the empty-string unit test, proving the same
    # all-or-nothing guard holds after structuring.
    _clear_openhost_env(monkeypatch)
    p = tmp_path / "config.toml"
    p.write_text(
        "[openhost]\n"
        'zone_domain = "alice.host.example.com"\n'
        'imbue_identity_issuer_url = ""\n'
        'imbue_identity_client_id = "iid"\n'
        'imbue_identity_client_secret = "isec"\n'
    )
    cfg = _load_file(p)
    assert cfg.instance_identity is None


def test_proxy_with_partial_imbue_two_of_three_raises() -> None:
    # Typo guard: email_proxy_base_url + a PARTIAL imbue identity (2 of 3) is a
    # misconfiguration and must raise at construction.
    with pytest.raises(ValueError, match="only partially resolved"):
        DefaultConfig(
            zone_domain="alice.host.example.com",
            email_proxy_base_url=_PROXY,
            public_ip=_IP,
            imbue_identity_issuer_url=_ISSUER,
            imbue_identity_client_id="iid",
        )


def test_proxy_with_zero_imbue_parts_does_not_raise() -> None:
    # email_proxy_base_url + 0 of 3 imbue parts is the awaiting-connect state, NOT
    # an error (email simply stays off).
    cfg = DefaultConfig(zone_domain="alice.host.example.com", email_proxy_base_url=_PROXY)
    assert cfg.email_enabled is False
    assert cfg.instance_identity is None


def test_proxy_with_partial_imbue_raises_through_file_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The typo guard also fires when the bad config arrives via the real load path
    # (validation runs in __attrs_post_init__, after structuring).
    _clear_openhost_env(monkeypatch)
    p = tmp_path / "config.toml"
    p.write_text(
        "[openhost]\n"
        'zone_domain = "alice.host.example.com"\n'
        f'public_ip = "{_IP}"\n'
        f'email_proxy_base_url = "{_PROXY}"\n'
        f'imbue_identity_issuer_url = "{_ISSUER}"\n'
        'imbue_identity_client_id = "iid"\n'
    )
    _assert_load_rejects(p, "only partially resolved")


def test_cert_api_provider_with_only_imbue_validates_and_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # cert_provider=cert_api with ONLY imbue_identity_* (no cert_api_keycloak_*)
    # validates and instance_identity resolves — the managed-via-shared path.
    _clear_openhost_env(monkeypatch)
    p = tmp_path / "config.toml"
    p.write_text(
        "[openhost]\n"
        'zone_domain = "alice.host.example.com"\n'
        'cert_provider = "cert_api"\n'
        'cert_api_base_url = "https://cert-api.example.com"\n'
        f'imbue_identity_issuer_url = "{_ISSUER}"\n'
        'imbue_identity_client_id = "iid"\n'
        'imbue_identity_client_secret = "isec"\n'
    )
    cfg = _load_file(p)
    assert cfg.cert_provider == CERT_PROVIDER_CERT_API
    assert cfg.cert_api_keycloak_issuer_url is None
    assert cfg.instance_identity is not None
    assert cfg.instance_identity.client_id == "iid"


def test_cert_api_provider_with_only_imbue_missing_secret_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative complement: cert_api provider + imbue supplies only 2 of 3 parts ->
    # the credential can't resolve, so validation rejects it, naming the SETTABLE
    # field (not the internal *_resolved property).
    _clear_openhost_env(monkeypatch)
    p = tmp_path / "config.toml"
    p.write_text(
        "[openhost]\n"
        'zone_domain = "alice.host.example.com"\n'
        'cert_provider = "cert_api"\n'
        'cert_api_base_url = "https://cert-api.example.com"\n'
        f'imbue_identity_issuer_url = "{_ISSUER}"\n'
        'imbue_identity_client_id = "iid"\n'
    )
    _assert_load_rejects(p, "cert_api_keycloak_client_secret must be set")


def test_load_config_legacy_env_var_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Backward compat for the config-selection env var: the deprecated
    # OPENHOST_CONFIG (not the newer OPENHOST_ROUTER_CONFIG) still selects the file
    # via load_config, so an old systemd unit keeps working.
    _clear_openhost_env(monkeypatch)
    p = tmp_path / "config.toml"
    p.write_text(
        "[openhost]\n"
        'zone_domain = "legacyenv.host.example.com"\n'
        f'imbue_identity_issuer_url = "{_ISSUER}"\n'
        'imbue_identity_client_id = "iid"\n'
        'imbue_identity_client_secret = "isec"\n'
    )
    monkeypatch.setenv("OPENHOST_CONFIG", str(p))
    cfg = load_config()
    assert cfg.zone_domain == "legacyenv.host.example.com"
    assert cfg.instance_identity is not None
    assert cfg.instance_identity.client_id == "iid"


def test_email_override_partial_raises_through_file_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A partially-set email_keycloak_* override (2 of 3) is a typo and must be
    # rejected even when it arrives via the file-load path.
    _clear_openhost_env(monkeypatch)
    p = tmp_path / "config.toml"
    p.write_text(
        "[openhost]\n"
        'zone_domain = "alice.host.example.com"\n'
        'email_keycloak_issuer_url = "https://kc/realms/x"\n'
        'email_keycloak_client_id = "eid"\n'
    )
    _assert_load_rejects(p, "partially configured")


# ===========================================================================
# DEPRECATED FIELDS ROUND-TRIP (operator-edited old config isn't dropped)
# ===========================================================================


def test_deprecated_cert_api_fields_round_trip_in_to_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An operator-edited old config with cert_api_keycloak_* must survive a
    # to_toml render (not silently dropped) so a re-render keeps working.
    _clear_openhost_env(monkeypatch)
    cfg = DefaultConfig(
        zone_domain="alice.host.example.com",
        cert_api_keycloak_issuer_url="https://cert/realms/x",
        cert_api_keycloak_client_id="cid",
        cert_api_keycloak_client_secret="csec",
    )
    rendered = cfg.to_toml_str()
    assert 'cert_api_keycloak_issuer_url = "https://cert/realms/x"' in rendered
    assert 'cert_api_keycloak_client_id = "cid"' in rendered
    assert 'cert_api_keycloak_client_secret = "csec"' in rendered
    p = tmp_path / "config.toml"
    p.write_text(rendered)
    reloaded = _load_file(p)
    assert reloaded.cert_api_keycloak_client_secret == "csec"
    assert reloaded.instance_identity is not None
    assert reloaded.instance_identity.client_id == "cid"


def test_deprecated_email_fields_round_trip_in_to_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Same for the deprecated email_keycloak_* override fields.
    _clear_openhost_env(monkeypatch)
    cfg = DefaultConfig(
        zone_domain="alice.host.example.com",
        public_ip=_IP,
        email_proxy_base_url=_PROXY,
        email_keycloak_issuer_url="https://email/realms/x",
        email_keycloak_client_id="eid",
        email_keycloak_client_secret="esec",
    )
    rendered = cfg.to_toml_str()
    assert 'email_keycloak_issuer_url = "https://email/realms/x"' in rendered
    assert 'email_keycloak_client_id = "eid"' in rendered
    assert 'email_keycloak_client_secret = "esec"' in rendered
    p = tmp_path / "config.toml"
    p.write_text(rendered)
    reloaded = _load_file(p)
    assert reloaded.email_keycloak_client_secret == "esec"
    assert reloaded.email_enabled is True


def test_deprecated_and_shared_fields_coexist_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A config that carries BOTH the deprecated override and the shared identity
    # must round-trip both, preserving the documented override-wins precedence.
    _clear_openhost_env(monkeypatch)
    cfg = DefaultConfig(
        zone_domain="alice.host.example.com",
        cert_api_keycloak_issuer_url="https://cert/realms/x",
        cert_api_keycloak_client_id="cert-id",
        cert_api_keycloak_client_secret="cert-secret",
        imbue_identity_issuer_url=_ISSUER,
        imbue_identity_client_id="imbue-id",
        imbue_identity_client_secret="imbue-secret",
    )
    p = tmp_path / "config.toml"
    cfg.to_toml(str(p))
    reloaded = _load_file(p)
    # both sets present...
    assert reloaded.cert_api_keycloak_client_id == "cert-id"
    assert reloaded.imbue_identity_client_id == "imbue-id"
    # ...and the deprecated override still wins the resolver after a round trip.
    assert reloaded.instance_identity is not None
    assert reloaded.instance_identity.client_id == "cert-id"


def test_load_config_twice_is_stable_and_leaves_no_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Loading the same obsolete-key-bearing config twice must be idempotent and
    # never accumulate scratch temp files (each load cleans up its own scrub copy).
    _clear_openhost_env(monkeypatch)
    p = tmp_path / "config.toml"
    p.write_text(
        "[openhost]\n"
        'zone_domain = "alice.host.example.com"\n'
        'email_inbound_mode = "direct"\n'
        f'imbue_identity_issuer_url = "{_ISSUER}"\n'
        'imbue_identity_client_id = "iid"\n'
        'imbue_identity_client_secret = "isec"\n'
    )
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", str(p))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    first = load_config()
    second = load_config()
    assert first.instance_identity == second.instance_identity
    assert list(tmp_path.glob("openhost-config-*.toml")) == []
    # Source file is untouched by loading (scrub is copy-on-load, never in place).
    assert "email_inbound_mode" in tomllib.loads(p.read_text())["openhost"]


def test_persist_does_not_introduce_obsolete_keys(tmp_path: Path) -> None:
    # Persisting an identity must add ONLY the three imbue keys — it must never
    # (re)introduce an obsolete key that a later load would have to scrub.
    p = tmp_path / "config.toml"
    p.write_text('[openhost]\nzone_domain = "alice.host.example.com"\n')
    persist_instance_identity(str(p), _cred(_ISSUER, "iid", "isec"))
    section = tomllib.loads(p.read_text())["openhost"]
    assert "email_enabled" not in section
    assert "email_inbound_mode" not in section
    assert "email_inbound_mx_host" not in section
    assert set(section) == {
        "zone_domain",
        "imbue_identity_issuer_url",
        "imbue_identity_client_id",
        "imbue_identity_client_secret",
    }


def test_obsolete_key_not_rendered_by_to_toml(tmp_path: Path) -> None:
    # Confirm the flip side of the round-trip guarantee: a truly OBSOLETE key
    # (email_enabled) is a derived property, never stored, so to_toml never emits
    # it — an operator config re-rendered by the app won't reintroduce the dropped
    # key. (Distinguishes "deprecated-but-kept" from "obsolete-and-dropped".)
    cfg = DefaultConfig(
        zone_domain="alice.host.example.com",
        public_ip=_IP,
        email_proxy_base_url=_PROXY,
        imbue_identity_issuer_url=_ISSUER,
        imbue_identity_client_id="iid",
        imbue_identity_client_secret="isec",
    )
    assert cfg.email_enabled is True
    rendered = cfg.to_toml_str()
    assert "email_enabled" not in rendered
    assert "email_inbound_mode" not in rendered
