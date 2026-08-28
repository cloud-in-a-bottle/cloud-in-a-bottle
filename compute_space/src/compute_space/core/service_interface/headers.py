"""The ``X-OpenHost-*`` headers a provider is entitled to see about its caller.

The router is the sole authority for these — the proxy strips any inbound copy — so they are built
in one place, whether the caller is an app going through the proxy or the router itself.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any
from urllib.parse import urlencode

from compute_space.core.app_id import ROUTER_APP_ID
from compute_space.core.app_id import ROUTER_APP_NAME
from compute_space.core.auth.permissions_v2 import get_granted_permissions_v2
from compute_space.core.domains import primary_domain_or_none
from compute_space.core.service_interface.builtin_services import Permissions

CONSUMER_ID_HEADER = "X-OpenHost-Consumer-Id"
CONSUMER_NAME_HEADER = "X-OpenHost-Consumer-Name"
PERMISSIONS_HEADER = "X-OpenHost-Permissions"


def consumer_headers(consumer_id: str, consumer_name: str, permissions: Permissions) -> list[tuple[str, str]]:
    """Identity + grants, as the provider will receive them.

    Providers get both names: the human-readable one (good for logs and UI) and the stable app_id
    (good for keying stored data that should survive a rename).
    """
    return [
        (CONSUMER_ID_HEADER, consumer_id),
        (CONSUMER_NAME_HEADER, consumer_name),
        (PERMISSIONS_HEADER, json.dumps(permissions)),
    ]


def router_consumer_headers(permissions: Permissions) -> list[tuple[str, str]]:
    """The same headers for the router's own calls.  It has no app token, but it is the authority
    for these headers in the first place, so it asserts what the proxy would have injected."""
    return consumer_headers(ROUTER_APP_ID, ROUTER_APP_NAME, permissions)


def app_consumer_headers(
    consumer_app_id: str, service_url: str, provider_app_id: str, db: sqlite3.Connection
) -> list[tuple[str, str]]:
    """The headers for a consumer app's call, with its grants filtered to this provider."""
    row = db.execute("SELECT name FROM apps WHERE app_id = ?", (consumer_app_id,)).fetchone()
    assert row is not None
    return consumer_headers(
        consumer_app_id, row["name"], grants_for_provider(consumer_app_id, service_url, provider_app_id)
    )


def grants_for_provider(consumer_app_id: str, service_url: str, provider_app_id: str) -> Permissions:
    """The consumer's grants that apply to this provider: global-scoped ones plus app-scoped ones
    aimed at it.  ``provider_app_id`` is dropped from each entry — the provider already knows it is
    the addressee."""
    grants = get_granted_permissions_v2(consumer_app_id, service_url)
    return [
        {"grant": g.grant, "scope": g.scope}
        for g in grants
        if g.scope == "global" or g.provider_app_id == provider_app_id
    ]


def approve_grant_url(consumer_app_id: str, service_url: str, grant: Any, db: sqlite3.Connection) -> str:
    """The owner-facing page for approving a grant a provider asked for."""
    # urlencode each value: service_url contains "/" and ":", grant is JSON with "{", "}", ","
    # and '"' — all of which break query-string parsing if interpolated raw.
    query = urlencode({"app": consumer_app_id, "service": service_url, "grant": json.dumps(grant, sort_keys=True)})
    approve_path = f"/approve-permissions-v2?{query}"
    # Cross-app approval is server-side (no browsing request in hand), so this stays on the
    # canonical/primary domain; use its scheme rather than a hardcoded https so a plain-http
    # primary (e.g. a `.local` instance) builds a correct URL.
    primary = primary_domain_or_none(db)
    if primary is None:
        return approve_path
    return f"{primary.scheme}://{primary.name}{approve_path}"
