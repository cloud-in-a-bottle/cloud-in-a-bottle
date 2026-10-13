from __future__ import annotations

import json
from typing import Any

from litestar import Request
from litestar import Router
from litestar import get
from litestar.exceptions import ClientException
from litestar.exceptions import ServiceUnavailableException
from litestar.response import Redirect

from compute_space.core.apps import find_app_by_name
from compute_space.core.apps import is_public_path
from compute_space.core.domains import host_with_request_port
from compute_space.web.helpers.zone import zone_for_request

# Landing pages under /redirect/ are where the my. redirect page forwards to, at the router's URL as looked up from
# /.well-known/cloud-in-a-bottle.  They only redirect onwards, so a published link can't cause side effects.


@get("/redirect/oauth_callback", raises=[ClientException, ServiceUnavailableException])
async def oauth_callback(request: Request[Any, Any, Any]) -> Redirect:
    """Send an OAuth provider's redirect on to the OAuth app that started the flow.

    Providers (Google, etc.) redirect to a fixed URL on the shared my. redirect domain, which forwards the browser
    here.  The OAuth app names itself in the ``state`` parameter as JSON (``{"app": "<app_name>", "nonce": ...}``),
    so this sends the whole query on to that app's ``/callback`` on the domain the request arrived on.  The app must
    list ``/callback`` in its public paths; that, and its single-use ``state``, is what guards the callback.
    """
    state_raw = request.query_params.get("state", "")
    if not state_raw:
        raise ClientException(detail="Missing state parameter", extra={"code": "bad_request"})
    try:
        state = json.loads(state_raw)
    except json.JSONDecodeError as e:
        raise ClientException(detail="Invalid state parameter", extra={"code": "bad_request"}) from e
    app_name = state.get("app") if isinstance(state, dict) else None
    if not app_name or not isinstance(app_name, str):
        raise ClientException(detail="Missing app in state", extra={"code": "bad_request"})

    app_row = find_app_by_name(app_name)
    if not app_row or not is_public_path(app_row, "/callback"):
        raise ServiceUnavailableException(
            detail=f"App '{app_name}' not found", extra={"code": "service_not_available"}
        )
    if app_row.status != "running":
        raise ServiceUnavailableException(
            detail=f"App '{app_name}' is not running", extra={"code": "service_not_available"}
        )

    zone = zone_for_request(request)
    host = host_with_request_port(f"{app_row.name}.{zone.name_no_port}", request.url.netloc)
    return Redirect(path=f"{zone.scheme}://{host}/callback?{request.url.query}")


redirect_routes = Router(path="/", route_handlers=[oauth_callback])
