from __future__ import annotations

import json
from typing import Annotated
from typing import Any

import attr
from litestar import Request
from litestar import Router
from litestar import get
from litestar.exceptions import ValidationException
from litestar.params import QueryParameter
from litestar.response import Redirect

from compute_space.core.app_id import is_valid_app_name
from compute_space.core.domains import host_with_request_port
from compute_space.web.helpers.zone import zone_for_request

# Landing pages under /redirect/ are where the my. redirect page forwards to, at the router's URL as looked up from
# /.well-known/cloud-in-a-bottle.  They only redirect onwards, so a published link can't cause side effects.


def _validate_app_name(_instance: object, _attribute: attr.Attribute[str], value: str) -> None:
    if not is_valid_app_name(value):
        raise ValueError(f"invalid app name: {value!r}")


@attr.s(auto_attribs=True, frozen=True)
class OAuthState:
    """The part of an OAuth ``state`` the router reads.  The OAuth app that started the flow encodes it as JSON,
    ``{"app": "<app name>", "nonce": ...}``, naming itself as the app to send the callback to."""

    app: str = attr.ib(validator=[attr.validators.instance_of(str), _validate_app_name])

    @classmethod
    def from_query(cls, raw: str) -> OAuthState:
        try:
            return cls(app=json.loads(raw)["app"])
        except (ValueError, TypeError, KeyError) as e:
            raise ValidationException(detail="Invalid state parameter") from e


@get("/redirect/oauth_callback", raises=[ValidationException])
async def oauth_callback(
    request: Request[Any, Any, Any],
    # "state" is a reserved handler kwarg in Litestar, so the query parameter needs another name here.
    oauth_state: Annotated[str, QueryParameter(name="state")],
) -> Redirect:
    """Send an OAuth provider's redirect on to the OAuth app that started the flow.

    Providers (Google, etc.) redirect to a fixed URL on the shared my. redirect domain, which forwards the browser
    here.  This sends the whole query on to the ``/callback`` of the app named in ``state``, on the domain and port
    the request arrived on.  It doesn't look the app up, so it reveals nothing about which apps exist: the request
    then meets the usual proxy rules at the app's host, where the app must list ``/callback`` as a public path.
    """
    app_name = OAuthState.from_query(oauth_state).app
    zone = zone_for_request(request)
    host = host_with_request_port(f"{app_name}.{zone.name_no_port}", request.url.netloc)
    # Redirect rather than proxy: a proxied response would be served from the router's origin, letting the app run
    # its own HTML/JS there (with the owner's session, which same-origin requests carry) and set cookies on the
    # router's host.  Redirected, the app's response comes from its own subdomain like any other.
    return Redirect(path=f"{zone.scheme}://{host}/callback?{request.url.query}")


redirect_routes = Router(path="/", route_handlers=[oauth_callback])
