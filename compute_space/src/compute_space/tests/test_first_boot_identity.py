"""First-boot seeding of the shared Imbue credential + connect URL.

``first_boot.toml`` may carry an ``[imbue_identity]`` table (issuer_url/client_id/
client_secret) and a top-level ``imbue_connect_base_url``.  ``seed_first_boot``
seeds them into the settings table, but only when the table has no credential yet
(idempotent — a later Connect-to-Imbue must never be clobbered by a stale file on
disk), and only when all three credential parts are present.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from compute_space.core.first_boot import read_first_boot
from compute_space.core.first_boot import seed_first_boot
from compute_space.core.identity_store import IMBUE_CONNECT_BASE_URL_KEY
from compute_space.core.identity_store import IMBUE_IDENTITY_ISSUER_URL_KEY
from compute_space.core.identity_store import get_connect_base_url
from compute_space.core.identity_store import get_stored_instance_identity
from compute_space.core.identity_store import set_instance_identity
from compute_space.core.settings_store import get_setting
from compute_space.core.settings_store import set_setting
from compute_space.core.tls.keycloak import KeycloakClientCredentials
from compute_space.db import init_db
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db

_IMBUE = "https://openhost.imbue.com"

# A full [imbue_identity] block for first_boot.toml.
_FULL_IDENTITY = (
    "[imbue_identity]\n"
    'issuer_url = "https://kc/realms/openhost-customers"\n'
    'client_id = "instance-alice"\n'
    'client_secret = "sekret"\n'
)


def _cfg(tmp_path: Path):  # type: ignore[no-untyped-def]
    # seed_primary=False: start from an empty (migrated) domains table; the
    # first-boot seed itself installs the primary + identity.
    cfg = _make_test_config(tmp_path, seed_primary=False)
    init_db(cfg.db_path)  # migrate + point get_db() at this DB (seed opens its own conn)
    return cfg


def _point_config_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    config_toml = tmp_path / "config.toml"
    config_toml.write_text("")  # only its directory matters
    monkeypatch.setenv("OPENHOST_ROUTER_CONFIG", str(config_toml))
    return tmp_path


def _write_first_boot(config_dir: Path, body: str) -> None:
    (config_dir / "first_boot.toml").write_text(body)


def _expected_cred() -> KeycloakClientCredentials:
    return KeycloakClientCredentials(
        issuer_url="https://kc/realms/openhost-customers",
        client_id="instance-alice",
        client_secret="sekret",
    )


# --- read_first_boot: parsing the new fields ---------------------------------


def test_read_first_boot_parses_identity_and_connect_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_dir = _point_config_env(monkeypatch, tmp_path)
    _write_first_boot(
        config_dir,
        f'domain = "fresh.example.com"\nimbue_connect_base_url = "{_IMBUE}"\n' + _FULL_IDENTITY,
    )
    fb = read_first_boot()
    assert fb is not None
    assert fb.imbue_identity_issuer_url == "https://kc/realms/openhost-customers"
    assert fb.imbue_identity_client_id == "instance-alice"
    assert fb.imbue_identity_client_secret == "sekret"
    assert fb.imbue_connect_base_url == _IMBUE


def test_read_first_boot_identity_fields_none_when_block_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_dir = _point_config_env(monkeypatch, tmp_path)
    _write_first_boot(config_dir, 'domain = "fresh.example.com"\n')
    fb = read_first_boot()
    assert fb is not None
    assert fb.imbue_identity_issuer_url is None
    assert fb.imbue_identity_client_id is None
    assert fb.imbue_identity_client_secret is None
    assert fb.imbue_connect_base_url is None


def test_read_first_boot_ignores_non_table_imbue_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # If imbue_identity is a scalar (not a table), it's ignored, not a crash.
    config_dir = _point_config_env(monkeypatch, tmp_path)
    _write_first_boot(config_dir, 'domain = "fresh.example.com"\nimbue_identity = "oops"\n')
    fb = read_first_boot()
    assert fb is not None
    assert fb.imbue_identity_issuer_url is None


def test_read_first_boot_partial_identity_fields(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_dir = _point_config_env(monkeypatch, tmp_path)
    _write_first_boot(
        config_dir,
        'domain = "fresh.example.com"\n[imbue_identity]\nissuer_url = "iss"\nclient_id = "cid"\n',
    )
    fb = read_first_boot()
    assert fb is not None
    assert fb.imbue_identity_issuer_url == "iss"
    assert fb.imbue_identity_client_id == "cid"
    assert fb.imbue_identity_client_secret is None


# --- seed_first_boot: full identity seeds ------------------------------------


def test_seed_writes_identity_into_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    config_dir = _point_config_env(monkeypatch, tmp_path)
    _write_first_boot(config_dir, 'domain = "fresh.example.com"\n' + _FULL_IDENTITY)

    seed_first_boot(cfg)

    with closing(open_db(cfg)) as db:
        assert get_stored_instance_identity(db) == _expected_cred()


def test_seed_writes_connect_url_into_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    config_dir = _point_config_env(monkeypatch, tmp_path)
    _write_first_boot(
        config_dir,
        f'domain = "fresh.example.com"\nimbue_connect_base_url = "{_IMBUE}"\n' + _FULL_IDENTITY,
    )

    seed_first_boot(cfg)

    with closing(open_db(cfg)) as db:
        assert get_connect_base_url(db) == _IMBUE


def test_seed_connect_url_without_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The connect URL seeds independently of the credential: a first_boot.toml with
    # only the connect URL (no identity) still installs the URL.
    cfg = _cfg(tmp_path)
    config_dir = _point_config_env(monkeypatch, tmp_path)
    _write_first_boot(config_dir, f'domain = "fresh.example.com"\nimbue_connect_base_url = "{_IMBUE}"\n')

    seed_first_boot(cfg)

    with closing(open_db(cfg)) as db:
        assert get_connect_base_url(db) == _IMBUE
        assert get_stored_instance_identity(db) is None


# --- seed_first_boot: partial identity seeds nothing -------------------------


def test_seed_partial_identity_seeds_no_credential(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    config_dir = _point_config_env(monkeypatch, tmp_path)
    _write_first_boot(
        config_dir,
        'domain = "fresh.example.com"\n[imbue_identity]\nissuer_url = "iss"\nclient_id = "cid"\n',
    )

    seed_first_boot(cfg)

    with closing(open_db(cfg)) as db:
        assert get_stored_instance_identity(db) is None


def test_seed_no_identity_block_seeds_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    config_dir = _point_config_env(monkeypatch, tmp_path)
    _write_first_boot(config_dir, 'domain = "fresh.example.com"\n')

    seed_first_boot(cfg)

    with closing(open_db(cfg)) as db:
        assert get_stored_instance_identity(db) is None
        assert get_connect_base_url(db) is None


# --- seed_first_boot: idempotency (never clobbers a stored credential) --------


def test_seed_does_not_overwrite_existing_stored_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A credential is already in the settings table (e.g. from a prior Connect).
    # A stale first_boot.toml with a DIFFERENT identity must NOT overwrite it.
    cfg = _cfg(tmp_path)
    config_dir = _point_config_env(monkeypatch, tmp_path)
    prior = KeycloakClientCredentials(issuer_url="prior-iss", client_id="prior-cid", client_secret="prior-sec")
    with closing(open_db(cfg)) as db:
        set_instance_identity(db, prior)
    _write_first_boot(config_dir, 'domain = "fresh.example.com"\n' + _FULL_IDENTITY)

    seed_first_boot(cfg)

    with closing(open_db(cfg)) as db:
        assert get_stored_instance_identity(db) == prior


def test_seed_is_idempotent_across_two_boots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    config_dir = _point_config_env(monkeypatch, tmp_path)
    _write_first_boot(config_dir, 'domain = "fresh.example.com"\n' + _FULL_IDENTITY)
    seed_first_boot(cfg)
    # A second boot whose file carries a different identity must not re-seed.
    _write_first_boot(
        config_dir,
        'domain = "fresh.example.com"\n[imbue_identity]\n'
        'issuer_url = "other-iss"\nclient_id = "other-cid"\nclient_secret = "other-sec"\n',
    )
    seed_first_boot(cfg)

    with closing(open_db(cfg)) as db:
        assert get_stored_instance_identity(db) == _expected_cred()


# --- seed_first_boot: connect url only seeded when absent ---------------------


def test_seed_does_not_overwrite_existing_connect_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    config_dir = _point_config_env(monkeypatch, tmp_path)
    with closing(open_db(cfg)) as db:
        set_setting(db, IMBUE_CONNECT_BASE_URL_KEY, "https://existing.imbue.com")
    _write_first_boot(
        config_dir,
        f'domain = "fresh.example.com"\nimbue_connect_base_url = "{_IMBUE}"\n',
    )

    seed_first_boot(cfg)

    with closing(open_db(cfg)) as db:
        assert get_connect_base_url(db) == "https://existing.imbue.com"


def test_seed_connect_url_absent_when_not_in_first_boot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    config_dir = _point_config_env(monkeypatch, tmp_path)
    _write_first_boot(config_dir, 'domain = "fresh.example.com"\n' + _FULL_IDENTITY)

    seed_first_boot(cfg)

    with closing(open_db(cfg)) as db:
        assert get_connect_base_url(db) is None


# --- seed_first_boot: stored identity untouched even when file adds a URL ------


def test_seed_preserves_existing_credential_but_still_seeds_connect_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A pre-existing stored credential is NOT clobbered by a stale first_boot.toml,
    # but the credential and the connect URL are seeded independently — so the
    # connect URL still seeds even when the credential is already present.
    cfg = _cfg(tmp_path)
    config_dir = _point_config_env(monkeypatch, tmp_path)
    prior = KeycloakClientCredentials(issuer_url="prior-iss", client_id="prior-cid", client_secret="prior-sec")
    with closing(open_db(cfg)) as db:
        set_instance_identity(db, prior)
    _write_first_boot(
        config_dir,
        f'domain = "fresh.example.com"\nimbue_connect_base_url = "{_IMBUE}"\n' + _FULL_IDENTITY,
    )

    seed_first_boot(cfg)

    with closing(open_db(cfg)) as db:
        # Credential preserved (not clobbered); connect URL seeded independently.
        assert get_stored_instance_identity(db) == prior
        assert get_connect_base_url(db) == _IMBUE


def test_seed_does_not_write_identity_keys_when_no_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A no-identity first boot must leave the identity settings keys entirely unset
    # (not written as empty strings).
    cfg = _cfg(tmp_path)
    config_dir = _point_config_env(monkeypatch, tmp_path)
    _write_first_boot(config_dir, 'domain = "fresh.example.com"\n')

    seed_first_boot(cfg)

    with closing(open_db(cfg)) as db:
        assert get_setting(db, IMBUE_IDENTITY_ISSUER_URL_KEY) is None
