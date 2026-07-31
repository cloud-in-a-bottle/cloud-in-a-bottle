"""Instance side of the "Connect to Imbue" flow.

Managed spaces get their shared Imbue credential seeded at provision time. A
non-managed (self-hosted) instance instead obtains it here: the owner clicks
"Connect to Imbue" in Settings and is sent to Imbue to authorize; Imbue returns a
one-time code to this instance. This module builds the authorization URL and
exchanges the code for the credential. The callback route stores the result in the
DB settings table (see ``core.identity_store``), which is read live, so no restart
is needed.
"""

from __future__ import annotations

from urllib.parse import urlencode

import attr
import httpx

from compute_space.core.tls.keycloak import KeycloakClientCredentials

# The instance-side callback path Imbue returns the one-time code to (?code=).
CONNECT_CALLBACK_PATH = "/api/settings/connect-imbue/callback"

_CONNECT_START_PATH = "/connect/imbue"
_CONNECT_EXCHANGE_PATH = "/connect/imbue/exchange"


@attr.s(auto_attribs=True, frozen=True)
class ConnectError(Exception):
    """The connect exchange with Imbue failed."""

    message: str

    def __str__(self) -> str:
        return self.message


def build_connect_url(frontend_base_url: str, zone: str, instance_base_url: str) -> str:
    """Build the Imbue authorization URL the owner's browser is sent to.

    ``instance_base_url`` is this instance's own https origin; the one-time code is
    returned to it (at CONNECT_CALLBACK_PATH), so the callback must be on the
    instance's own zone.
    """
    callback = f"{instance_base_url.rstrip('/')}{CONNECT_CALLBACK_PATH}"
    query = urlencode({"zone": zone, "callback": callback})
    return f"{frontend_base_url.rstrip('/')}{_CONNECT_START_PATH}?{query}"


def exchange_code_for_credential(
    frontend_base_url: str, code: str, *, timeout: float = 30.0
) -> KeycloakClientCredentials:
    """Swap a one-time code for the instance's credential.

    Returns the shared per-instance credential. Raises ConnectError on any non-200
    response or malformed body.
    """
    url = f"{frontend_base_url.rstrip('/')}{_CONNECT_EXCHANGE_PATH}"
    try:
        resp = httpx.post(url, json={"code": code}, timeout=timeout)
    except httpx.HTTPError as e:
        raise ConnectError(f"could not reach the Imbue connect endpoint: {e}") from e
    if resp.status_code != 200:
        raise ConnectError(f"connect exchange failed: HTTP {resp.status_code} {_error_message(resp)}")
    try:
        body = resp.json()
        return KeycloakClientCredentials(
            issuer_url=str(body["issuer_url"]),
            client_id=str(body["client_id"]),
            client_secret=str(body["client_secret"]),
        )
    except (ValueError, KeyError, TypeError) as e:
        raise ConnectError("connect exchange returned a malformed response") from e


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict):
        return str(body.get("error", resp.text[:200]))
    return resp.text[:200]
