from __future__ import annotations

from typing import Any

import attr
from litestar import Request
from litestar import Response
from litestar import Router
from litestar import get

from compute_space.core.domains import host_with_request_port
from compute_space.core.root_app import DISCOVERY_PATH
from compute_space.web.helpers.zone import zone_for_request


@attr.s(auto_attribs=True, frozen=True)
class DiscoveryResponse:
    # Where the dashboard and API live on the domain the request arrived on.  The bare domain may be serving an
    # app instead, so external clients (the CLI, the my. redirect page) look this up rather than assume.
    router_url: str


@get(DISCOVERY_PATH, sync_to_thread=False)
def discovery(request: Request[Any, Any, Any]) -> Response[DiscoveryResponse]:
    """Public: tell external clients where this instance's router lives."""
    zone = zone_for_request(request)
    router_url = f"{zone.scheme}://{host_with_request_port(zone.router_host, request.url.netloc)}"
    # Public and cookie-free, so any origin may read it (the my. redirect page fetches it cross-origin).
    return Response(content=DiscoveryResponse(router_url=router_url), headers={"Access-Control-Allow-Origin": "*"})


discovery_routes = Router(path="/", route_handlers=[discovery])
