"""Whether email is active on this instance, and the identity it authenticates with.

Email has no stored on/off flag: it is enabled iff its prerequisites resolve. Two
of the three live outside the frozen ``Config`` — the shared per-instance Imbue
identity is read live from the DB settings table (``core.identity_store``), so
enablement needs the DB. Kept in its own tiny module (rather than as a Config
property) precisely so it can take the DB.
"""

from __future__ import annotations

import sqlite3

from compute_space.config import Config
from compute_space.core.identity_store import get_instance_identity
from compute_space.core.tls.keycloak import KeycloakClientCredentials


def email_enabled(config: Config, db: sqlite3.Connection) -> bool:
    """True iff all email prerequisites are present.

    Requires the email proxy URL, the shared Imbue identity (DB settings, with the
    deprecated cert_api_keycloak_* config fallback), and the public IP (inbound is
    always direct-to-instance, so the MX/A records need the instance's own IP).
    """
    return bool(config.email_proxy_base_url and get_instance_identity(db, config) is not None and config.public_ip)


def resolve_email_identity(config: Config, db: sqlite3.Connection) -> KeycloakClientCredentials | None:
    """The Keycloak client-credentials email authenticates with, or None.

    A thin wrapper over ``get_instance_identity`` — email reuses the shared
    per-instance Imbue identity rather than a separate email-only client.
    """
    return get_instance_identity(db, config)
