"""Router-internal OAuth token helper.

The router occasionally needs to act as an OAuth client itself — most notably to clone or pull private GitHub repos
on behalf of the operator. It does this by calling the v2 oauth service (provider app
``github.com/imbue-openhost/openhost/services/oauth``) over HTTP loopback, authenticating as the router's own
identity (``ROUTER_APP_ID``) with a hard-coded grant for the requested provider+scopes.
"""

import sqlite3

import httpx

from compute_space.core.proxy_target import client_for
from compute_space.core.service_interface.headers import router_consumer_headers
from compute_space.core.service_interface.resolve import resolve_provider
from compute_space.core.util import assert_str
from compute_space.db import get_db

OAUTH_SERVICE_URL = "github.com/imbue-openhost/openhost/services/oauth"


class OAuthRequired(RuntimeError):
    def __init__(self, authorize_url: str):
        super().__init__(authorize_url)
        self.authorize_url = authorize_url


async def get_oauth_token(
    provider: str,
    scopes: list[str],
    return_to: str,
    db: sqlite3.Connection | None = None,
) -> str:
    """Fetch an OAuth access token from the v2 oauth service.

    Raises:
        RuntimeError: The oauth service isn't installed/running, or didn't respond.
        OAuthRequired: User authorization is needed (carries authorize_url).
    """
    if db is None:
        db = get_db()
    oauth_provider = resolve_provider(OAUTH_SERVICE_URL, ">=0", db)

    # Forge an app-scoped grant. We bypass the v2 service proxy (which would normally do the per-provider
    # filter + provider_app_id strip) by going straight to the provider, so we hand-build the same
    # post-filter shape the proxy would produce. The oauth service only honours app-scoped grants
    # (see services/oauth/openapi.yaml).
    grant_payload = {"provider": provider, "scopes": list(scopes)}
    headers = dict(router_consumer_headers([{"grant": grant_payload, "scope": "app"}]))
    headers["Accept"] = "application/json"
    client, base_url = client_for(oauth_provider.target, 5)
    url = f"{base_url}{oauth_provider.endpoint.rstrip('/')}/token"
    try:
        async with client:
            resp = await client.post(
                url,
                json={"provider": provider, "scopes": list(scopes), "return_to": return_to},
                headers=headers,
            )
    except httpx.HTTPError as e:
        raise RuntimeError(f"OAuth service is not responding: {e}") from e

    data = resp.json()
    if resp.status_code == 200:
        return assert_str(data["access_token"])
    if resp.status_code == 401 and data.get("status") == "authorization_required":
        raise OAuthRequired(data["authorize_url"])
    raise RuntimeError(f"OAuth service returned unexpected status {resp.status_code}: {resp.text}")
