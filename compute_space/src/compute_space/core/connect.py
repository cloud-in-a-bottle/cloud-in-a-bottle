"""Instance side of the "Connect to Imbue" flow.

Managed spaces get their shared Imbue credential injected at provision time. A
non-managed (self-hosted) instance instead obtains it here: the owner clicks
"Connect to Imbue" in Settings, authenticates at the Imbue front door, and the
front door hands back a one-time code via a redirect to this instance. This module
builds that redirect URL, exchanges the code for the credential server-to-server,
and persists the credential into the instance's config.toml so a restart picks it
up through the normal config path (Config.instance_identity).
"""

from __future__ import annotations

import os
import tomllib
from urllib.parse import urlencode

import attr
import httpx
import tomli_w

from compute_space.core.logging import logger
from compute_space.core.tls.keycloak import KeycloakClientCredentials

# The instance-side callback path the front door redirects back to with ?code=.
CONNECT_CALLBACK_PATH = "/api/settings/connect-imbue/callback"

_CONNECT_START_PATH = "/connect/imbue"
_CONNECT_EXCHANGE_PATH = "/connect/imbue/exchange"


@attr.s(auto_attribs=True, frozen=True)
class ConnectError(Exception):
    """The connect exchange with the Imbue front door failed."""

    message: str

    def __str__(self) -> str:
        return self.message


def build_connect_url(frontend_base_url: str, zone: str, instance_base_url: str) -> str:
    """Build the front-door URL the owner's browser is redirected to.

    ``instance_base_url`` is this instance's own https origin; the front door
    redirects the browser back to it (at CONNECT_CALLBACK_PATH) with a one-time
    code, so the callback must be on the instance's own zone.
    """
    callback = f"{instance_base_url.rstrip('/')}{CONNECT_CALLBACK_PATH}"
    query = urlencode({"zone": zone, "callback": callback})
    return f"{frontend_base_url.rstrip('/')}{_CONNECT_START_PATH}?{query}"


def exchange_code_for_credential(
    frontend_base_url: str, code: str, *, timeout: float = 30.0
) -> KeycloakClientCredentials:
    """Swap a one-time code for the minted credential (server-to-server).

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


def persist_instance_identity(config_path: str, credential: KeycloakClientCredentials) -> None:
    """Merge the shared Imbue credential into the instance's config.toml.

    Preserves every other key. Writes the ``imbue_identity_*`` fields under the
    ``[openhost]`` section so a restart loads the credential via the normal config
    path. Written atomically (temp file + replace) so a crash mid-write can't leave
    a truncated config.
    """
    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        data = {}
    section = data.get("openhost")
    if not isinstance(section, dict):
        section = {}
    section["imbue_identity_issuer_url"] = credential.issuer_url
    section["imbue_identity_client_id"] = credential.client_id
    section["imbue_identity_client_secret"] = credential.client_secret
    data["openhost"] = section

    tmp_path = f"{config_path}.connect.tmp"
    with open(tmp_path, "wb") as f:
        tomli_w.dump(data, f)
    os.replace(tmp_path, config_path)
    logger.info(f"Persisted Imbue identity (client {credential.client_id!r}) to {config_path}")


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict):
        return str(body.get("error", resp.text[:200]))
    return resp.text[:200]
