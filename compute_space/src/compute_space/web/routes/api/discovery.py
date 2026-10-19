from __future__ import annotations

from typing import Any

import attr
from litestar import Request
from litestar import Response
from litestar import Router
from litestar import get

from compute_space.web.helpers.zone import router_base_url

DISCOVERY_PATH = "/.well-known/cloud-in-a-bottle"


@attr.s(auto_attribs=True, frozen=True)
class DiscoveryResponse:
    # Where the dashboard and API live on the domain the request arrived on.  External clients (the CLI, the my.
    # redirect page) look this up rather than assume they're at the bare domain.
    router_url: str


@get(DISCOVERY_PATH)
async def discovery(request: Request[Any, Any, Any]) -> Response[DiscoveryResponse]:
    """Public: tell external clients where this instance's router lives."""
    router_url = router_base_url(request)
    # Public and cookie-free, so any origin may read it (the my. redirect page fetches it cross-origin).
    return Response(content=DiscoveryResponse(router_url=router_url), headers={"Access-Control-Allow-Origin": "*"})


discovery_routes = Router(path="/", route_handlers=[discovery])
