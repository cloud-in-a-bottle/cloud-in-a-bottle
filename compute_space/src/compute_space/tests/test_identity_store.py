"""The shared per-instance Imbue credential store (``core/identity_store``).

The credential lives in the DB ``settings`` table.  ``get_instance_identity``
resolves it, falling back to the deprecated ``cert_api_keycloak_*`` config fields
for already-deployed instances; ``get_stored_instance_identity`` reads the settings
table only.  These tests pin the resolution rules (all-three-or-nothing, settings
precedence, config fallback) directly against a real DB + Config.
"""

from __future__ import annotations

import sqlite3

from compute_space.config import Config
from compute_space.config import DefaultConfig
from compute_space.core.identity_store import IMBUE_CONNECT_BASE_URL_KEY
from compute_space.core.identity_store import IMBUE_IDENTITY_CLIENT_ID_KEY
from compute_space.core.identity_store import IMBUE_IDENTITY_CLIENT_SECRET_KEY
from compute_space.core.identity_store import IMBUE_IDENTITY_ISSUER_URL_KEY
from compute_space.core.identity_store import get_connect_base_url
from compute_space.core.identity_store import get_instance_identity
from compute_space.core.identity_store import get_stored_instance_identity
from compute_space.core.identity_store import set_instance_identity
from compute_space.core.settings_store import get_setting
from compute_space.core.settings_store import set_setting
from compute_space.core.tls.keycloak import KeycloakClientCredentials

# --- helpers -----------------------------------------------------------------


def _cred(
    issuer: str = "https://kc.example/realms/openhost-customers",
    client_id: str = "instance-alice",
    secret: str = "s3kret",
) -> KeycloakClientCredentials:
    return KeycloakClientCredentials(issuer_url=issuer, client_id=client_id, client_secret=secret)


def _empty_config() -> Config:
    """A config with no cert_api_keycloak_* fallback configured."""
    return DefaultConfig()


def _config_with_fallback(
    issuer: str | None = "https://cfg.example/realms/openhost-customers",
    client_id: str | None = "cfg-client",
    secret: str | None = "cfg-secret",
) -> Config:
    return DefaultConfig(
        cert_api_keycloak_issuer_url=issuer,
        cert_api_keycloak_client_id=client_id,
        cert_api_keycloak_client_secret=secret,
    )


# --- key constants (guard against silent renames) ----------------------------


def test_key_constants_are_the_documented_strings() -> None:
    assert IMBUE_IDENTITY_ISSUER_URL_KEY == "imbue_identity_issuer_url"
    assert IMBUE_IDENTITY_CLIENT_ID_KEY == "imbue_identity_client_id"
    assert IMBUE_IDENTITY_CLIENT_SECRET_KEY == "imbue_identity_client_secret"
    assert IMBUE_CONNECT_BASE_URL_KEY == "imbue_connect_base_url"


# --- set / get round-trip ----------------------------------------------------


def test_set_then_get_round_trips(db: sqlite3.Connection) -> None:
    set_instance_identity(db, _cred())
    got = get_instance_identity(db, _empty_config())
    assert got == _cred()


def test_set_writes_all_three_settings_keys(db: sqlite3.Connection) -> None:
    set_instance_identity(db, _cred("iss", "cid", "sec"))
    assert get_setting(db, IMBUE_IDENTITY_ISSUER_URL_KEY) == "iss"
    assert get_setting(db, IMBUE_IDENTITY_CLIENT_ID_KEY) == "cid"
    assert get_setting(db, IMBUE_IDENTITY_CLIENT_SECRET_KEY) == "sec"


def test_get_stored_round_trips(db: sqlite3.Connection) -> None:
    set_instance_identity(db, _cred("iss", "cid", "sec"))
    assert get_stored_instance_identity(db) == _cred("iss", "cid", "sec")


def test_get_returns_a_keycloak_client_credentials_instance(db: sqlite3.Connection) -> None:
    set_instance_identity(db, _cred())
    got = get_instance_identity(db, _empty_config())
    assert isinstance(got, KeycloakClientCredentials)
    assert got is not None
    assert got.issuer_url == "https://kc.example/realms/openhost-customers"
    assert got.client_id == "instance-alice"
    assert got.client_secret == "s3kret"


# --- None when nothing configured --------------------------------------------


def test_get_none_when_empty_and_no_fallback(db: sqlite3.Connection) -> None:
    assert get_instance_identity(db, _empty_config()) is None


def test_get_stored_none_when_empty(db: sqlite3.Connection) -> None:
    assert get_stored_instance_identity(db) is None


# --- partial settings -> None ------------------------------------------------


def test_get_none_when_only_issuer_set(db: sqlite3.Connection) -> None:
    set_setting(db, IMBUE_IDENTITY_ISSUER_URL_KEY, "iss")
    assert get_instance_identity(db, _empty_config()) is None


def test_get_none_when_only_client_id_set(db: sqlite3.Connection) -> None:
    set_setting(db, IMBUE_IDENTITY_CLIENT_ID_KEY, "cid")
    assert get_instance_identity(db, _empty_config()) is None


def test_get_none_when_only_secret_set(db: sqlite3.Connection) -> None:
    set_setting(db, IMBUE_IDENTITY_CLIENT_SECRET_KEY, "sec")
    assert get_instance_identity(db, _empty_config()) is None


def test_get_none_when_two_of_three_set_missing_secret(db: sqlite3.Connection) -> None:
    set_setting(db, IMBUE_IDENTITY_ISSUER_URL_KEY, "iss")
    set_setting(db, IMBUE_IDENTITY_CLIENT_ID_KEY, "cid")
    assert get_instance_identity(db, _empty_config()) is None


def test_get_none_when_two_of_three_set_missing_client_id(db: sqlite3.Connection) -> None:
    set_setting(db, IMBUE_IDENTITY_ISSUER_URL_KEY, "iss")
    set_setting(db, IMBUE_IDENTITY_CLIENT_SECRET_KEY, "sec")
    assert get_instance_identity(db, _empty_config()) is None


def test_get_none_when_two_of_three_set_missing_issuer(db: sqlite3.Connection) -> None:
    set_setting(db, IMBUE_IDENTITY_CLIENT_ID_KEY, "cid")
    set_setting(db, IMBUE_IDENTITY_CLIENT_SECRET_KEY, "sec")
    assert get_instance_identity(db, _empty_config()) is None


def test_get_stored_none_when_partial(db: sqlite3.Connection) -> None:
    set_setting(db, IMBUE_IDENTITY_ISSUER_URL_KEY, "iss")
    set_setting(db, IMBUE_IDENTITY_CLIENT_ID_KEY, "cid")
    assert get_stored_instance_identity(db) is None


# --- empty-string values are falsy -> treated as missing ---------------------


def test_get_none_when_a_setting_is_empty_string(db: sqlite3.Connection) -> None:
    set_setting(db, IMBUE_IDENTITY_ISSUER_URL_KEY, "iss")
    set_setting(db, IMBUE_IDENTITY_CLIENT_ID_KEY, "cid")
    set_setting(db, IMBUE_IDENTITY_CLIENT_SECRET_KEY, "")
    assert get_instance_identity(db, _empty_config()) is None


def test_get_stored_none_when_a_setting_is_empty_string(db: sqlite3.Connection) -> None:
    set_setting(db, IMBUE_IDENTITY_ISSUER_URL_KEY, "")
    set_setting(db, IMBUE_IDENTITY_CLIENT_ID_KEY, "cid")
    set_setting(db, IMBUE_IDENTITY_CLIENT_SECRET_KEY, "sec")
    assert get_stored_instance_identity(db) is None


# --- config fallback ---------------------------------------------------------


def test_config_fallback_used_when_settings_empty(db: sqlite3.Connection) -> None:
    got = get_instance_identity(db, _config_with_fallback())
    assert got == KeycloakClientCredentials(
        issuer_url="https://cfg.example/realms/openhost-customers",
        client_id="cfg-client",
        client_secret="cfg-secret",
    )


def test_config_fallback_none_when_config_partial_missing_secret(db: sqlite3.Connection) -> None:
    cfg = _config_with_fallback(secret=None)
    assert get_instance_identity(db, cfg) is None


def test_config_fallback_none_when_config_partial_missing_issuer(db: sqlite3.Connection) -> None:
    cfg = _config_with_fallback(issuer=None)
    assert get_instance_identity(db, cfg) is None


def test_config_fallback_none_when_config_partial_missing_client_id(db: sqlite3.Connection) -> None:
    cfg = _config_with_fallback(client_id=None)
    assert get_instance_identity(db, cfg) is None


def test_get_stored_ignores_config_fallback(db: sqlite3.Connection) -> None:
    # A config fallback exists, but the settings table is empty.
    assert get_stored_instance_identity(db) is None


# --- settings precedence over config fallback --------------------------------


def test_settings_take_precedence_over_config_fallback(db: sqlite3.Connection) -> None:
    set_instance_identity(db, _cred("settings-iss", "settings-cid", "settings-sec"))
    got = get_instance_identity(db, _config_with_fallback())
    assert got == _cred("settings-iss", "settings-cid", "settings-sec")


def test_per_field_fallback_mixes_settings_and_config(db: sqlite3.Connection) -> None:
    # The resolver falls back per-field: only the client_secret is in settings,
    # the other two come from config. All three resolve -> a credential.
    cfg = _config_with_fallback()
    set_setting(db, IMBUE_IDENTITY_CLIENT_SECRET_KEY, "override-secret")
    got = get_instance_identity(db, cfg)
    assert got is not None
    assert got.issuer_url == "https://cfg.example/realms/openhost-customers"
    assert got.client_id == "cfg-client"
    assert got.client_secret == "override-secret"


# --- overwrite / idempotency -------------------------------------------------


def test_set_overwrites_previous_identity(db: sqlite3.Connection) -> None:
    set_instance_identity(db, _cred("old-iss", "old-cid", "old-sec"))
    set_instance_identity(db, _cred("new-iss", "new-cid", "new-sec"))
    assert get_stored_instance_identity(db) == _cred("new-iss", "new-cid", "new-sec")


def test_repeated_set_is_idempotent(db: sqlite3.Connection) -> None:
    for _ in range(3):
        set_instance_identity(db, _cred("iss", "cid", "sec"))
    assert get_stored_instance_identity(db) == _cred("iss", "cid", "sec")
    # No duplicate rows accumulate under the ON CONFLICT upsert.
    rows = db.execute("SELECT COUNT(*) AS n FROM settings WHERE key = ?", (IMBUE_IDENTITY_ISSUER_URL_KEY,)).fetchone()
    assert rows["n"] == 1


# --- unicode / long / whitespace values --------------------------------------


def test_round_trips_unicode_values(db: sqlite3.Connection) -> None:
    cred = _cred("https://kc.exämple/realms/naïve", "instance-café", "sëcret-ü")
    set_instance_identity(db, cred)
    assert get_stored_instance_identity(db) == cred


def test_round_trips_long_values(db: sqlite3.Connection) -> None:
    cred = _cred("https://kc/" + "a" * 4000, "c" * 2000, "s" * 5000)
    set_instance_identity(db, cred)
    assert get_stored_instance_identity(db) == cred


def test_whitespace_only_values_are_truthy_and_resolve(db: sqlite3.Connection) -> None:
    # identity_store treats any non-empty string as present (no strip()), so a
    # whitespace-only value resolves (it is truthy). Pins the actual behavior.
    cred = _cred(" ", " ", " ")
    set_instance_identity(db, cred)
    assert get_stored_instance_identity(db) == cred


# --- connect base url --------------------------------------------------------


def test_get_connect_base_url_none_when_absent(db: sqlite3.Connection) -> None:
    assert get_connect_base_url(db) is None


def test_get_connect_base_url_present(db: sqlite3.Connection) -> None:
    set_setting(db, IMBUE_CONNECT_BASE_URL_KEY, "https://openhost.imbue.com")
    assert get_connect_base_url(db) == "https://openhost.imbue.com"


def test_get_connect_base_url_empty_string_returned_verbatim(db: sqlite3.Connection) -> None:
    # get_connect_base_url returns the stored value directly (no truthiness
    # collapsing), so an empty string comes back as "".
    set_setting(db, IMBUE_CONNECT_BASE_URL_KEY, "")
    assert get_connect_base_url(db) == ""


def test_connect_base_url_is_independent_of_identity(db: sqlite3.Connection) -> None:
    set_setting(db, IMBUE_CONNECT_BASE_URL_KEY, "https://openhost.imbue.com")
    # Setting the connect URL does not conjure an identity.
    assert get_instance_identity(db, _empty_config()) is None
    assert get_connect_base_url(db) == "https://openhost.imbue.com"


def test_setting_identity_does_not_set_connect_base_url(db: sqlite3.Connection) -> None:
    set_instance_identity(db, _cred())
    assert get_connect_base_url(db) is None
