"""The instance's shared Imbue credential, stored in the DB ``settings`` table.

A single per-instance credential authenticates the instance to any Imbue service
(cert acquisition today, more later). It reaches the instance two ways that
produce the same result:

  - managed spaces: seeded once from ``first_boot.toml`` into the settings table
    at provision time (see ``core.first_boot``);
  - non-managed spaces: obtained at runtime via the "Connect to Imbue" flow, which
    writes it here (see ``core.connect``).

The DB is the source of truth and is read live, mirroring how ``core.domains``
sources the primary domain from the DB rather than from the frozen ``Config``.
For backward compatibility with instances provisioned before this store existed
(which carry ``cert_api_keycloak_*`` in ``config.toml``), the resolver falls back
to those config fields when the settings table has no credential.
"""

from __future__ import annotations

import sqlite3

from compute_space.config import Config
from compute_space.core import settings_store
from compute_space.core.tls.keycloak import KeycloakClientCredentials

# Setting keys for the shared Imbue credential + the connect front-end URL.
IMBUE_IDENTITY_ISSUER_URL_KEY = "imbue_identity_issuer_url"
IMBUE_IDENTITY_CLIENT_ID_KEY = "imbue_identity_client_id"
IMBUE_IDENTITY_CLIENT_SECRET_KEY = "imbue_identity_client_secret"
IMBUE_CONNECT_BASE_URL_KEY = "imbue_connect_base_url"


def get_instance_identity(db: sqlite3.Connection, config: Config) -> KeycloakClientCredentials | None:
    """The shared per-instance credential, or None when none is configured.

    Reads the settings table first, then falls back to the deprecated
    ``cert_api_keycloak_*`` config fields for instances that predate the settings
    store and have not yet run ``openhost update`` (which migrates the credential
    into the settings table + scrubs the config via system-agent migration v8).
    Once every instance has migrated, this config fallback can be removed. Returns
    None unless all three parts resolve, so callers treat None as "no identity".
    """
    issuer = settings_store.get_setting(db, IMBUE_IDENTITY_ISSUER_URL_KEY) or config.cert_api_keycloak_issuer_url
    client_id = settings_store.get_setting(db, IMBUE_IDENTITY_CLIENT_ID_KEY) or config.cert_api_keycloak_client_id
    client_secret = (
        settings_store.get_setting(db, IMBUE_IDENTITY_CLIENT_SECRET_KEY) or config.cert_api_keycloak_client_secret
    )
    if issuer and client_id and client_secret:
        return KeycloakClientCredentials(issuer_url=issuer, client_id=client_id, client_secret=client_secret)
    return None


def get_stored_instance_identity(db: sqlite3.Connection) -> KeycloakClientCredentials | None:
    """The credential from the settings table only (no config fallback), or None.

    Used by first-boot seeding to decide whether the table already holds a
    credential, independent of any deprecated config fallback.
    """
    issuer = settings_store.get_setting(db, IMBUE_IDENTITY_ISSUER_URL_KEY)
    client_id = settings_store.get_setting(db, IMBUE_IDENTITY_CLIENT_ID_KEY)
    client_secret = settings_store.get_setting(db, IMBUE_IDENTITY_CLIENT_SECRET_KEY)
    if issuer and client_id and client_secret:
        return KeycloakClientCredentials(issuer_url=issuer, client_id=client_id, client_secret=client_secret)
    return None


def set_instance_identity(db: sqlite3.Connection, credential: KeycloakClientCredentials) -> None:
    """Store the shared per-instance credential in the settings table."""
    settings_store.set_setting(db, IMBUE_IDENTITY_ISSUER_URL_KEY, credential.issuer_url)
    settings_store.set_setting(db, IMBUE_IDENTITY_CLIENT_ID_KEY, credential.client_id)
    settings_store.set_setting(db, IMBUE_IDENTITY_CLIENT_SECRET_KEY, credential.client_secret)


def get_connect_base_url(db: sqlite3.Connection) -> str | None:
    """Base URL of the Imbue API for the "Connect to Imbue" flow, or None.

    When None, the connect flow is unavailable and the Settings button is hidden.
    """
    return settings_store.get_setting(db, IMBUE_CONNECT_BASE_URL_KEY)
