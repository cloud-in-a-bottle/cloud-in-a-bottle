import base64
import sqlite3
import urllib.parse
from typing import Annotated

import attr
from cryptography.hazmat.primitives.asymmetric import rsa as rsa_module
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from litestar import Router
from litestar import get
from litestar import post
from litestar.di import NamedDependency
from litestar.enums import RequestEncodingType
from litestar.exceptions import HTTPException
from litestar.params import Body
from litestar.response import Redirect
from litestar.response import Template

from compute_space.core.auth import identity
from compute_space.core.auth.keys import get_public_key_pem
from compute_space.core.logging import logger
from compute_space.web.auth.auth import require_owner_auth


@attr.s(auto_attribs=True, frozen=True)
class JwkRSA:
    kty: str
    alg: str
    use: str
    n: str
    e: str


@attr.s(auto_attribs=True, frozen=True)
class JwksResponse:
    keys: list[JwkRSA]


@attr.s(auto_attribs=True, frozen=True)
class ZoneIdentityResponse:
    domain: str
    public_key_pem: str
    protocol: str


@attr.s(auto_attribs=True, frozen=True)
class IdentityApproveForm:
    callback: str


# Public: apps fetch this to verify JWTs before they hold any owner token.
@get("/.well-known/jwks.json", sync_to_thread=False, security=[{}])
def jwks() -> JwksResponse:
    """Expose the public key in JWKS format for app JWT verification."""
    public_key_pem = get_public_key_pem()
    public_key = load_pem_public_key(public_key_pem.encode())
    assert isinstance(public_key, rsa_module.RSAPublicKey)
    numbers = public_key.public_numbers()

    def _b64url(num: int, length: int) -> str:
        b = num.to_bytes(length, byteorder="big")
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    n_bytes = (numbers.n.bit_length() + 7) // 8
    return JwksResponse(
        keys=[
            JwkRSA(
                kty="RSA",
                alg="RS256",
                use="sig",
                n=_b64url(numbers.n, n_bytes),
                e=_b64url(numbers.e, 3),
            )
        ]
    )


@get("/.well-known/openhost-identity", sync_to_thread=False, security=[{}])
def openhost_identity(db: NamedDependency[sqlite3.Connection]) -> ZoneIdentityResponse:
    """Public endpoint: expose this zone's identity (domain + public key)."""
    try:
        data = identity.get_zone_identity(db)
    except RuntimeError as e:
        raise HTTPException(detail="Identity not yet available", status_code=503) from e
    return ZoneIdentityResponse(
        domain=data["domain"],
        public_key_pem=data["public_key_pem"],
        protocol=data["protocol"],
    )


# not documented in openAPI because it redirects with http fields
@get("/identity/approve", guards=[require_owner_auth], include_in_schema=False)
async def identity_approve(callback: str, app_name: str, requesting_domain: str) -> Template:
    """Show the owner an approval page for a federated login request."""
    parsed = urllib.parse.urlparse(callback)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise HTTPException(detail="Invalid callback URL", status_code=400)

    return Template(
        template_name="identity_approve.html",
        context={
            "callback": callback,
            "app_name": app_name,
            "requesting_domain": requesting_domain,
        },
    )


# not documented in openAPI because it redirects with http fields
@post("/identity/approve", status_code=302, guards=[require_owner_auth], include_in_schema=False)
async def identity_approve_submit(
    data: Annotated[IdentityApproveForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
    db: NamedDependency[sqlite3.Connection],
) -> Redirect:
    """Owner approved the login — sign an identity token and redirect back."""
    parsed = urllib.parse.urlparse(data.callback)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise HTTPException(detail="Invalid callback URL", status_code=400)

    try:
        token = identity.sign_identity_token(data.callback, db)
    except RuntimeError as e:
        logger.error("Failed to sign identity token: %s", e)
        raise HTTPException(detail="Identity service unavailable", status_code=503) from e

    separator = "&" if "?" in data.callback else "?"
    encoded_token = urllib.parse.quote(token, safe="")
    return Redirect(path=f"{data.callback}{separator}identity_token={encoded_token}")


identity_routes = Router(
    path="/",
    route_handlers=[jwks, openhost_identity, identity_approve, identity_approve_submit],
)
