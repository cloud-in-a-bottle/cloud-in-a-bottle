import sqlite3

import attr
from litestar import Router
from litestar import delete
from litestar import get
from litestar import post
from litestar.di import NamedDependency
from litestar.exceptions import NotFoundException
from litestar.params import FromQuery

from compute_space.core.app_id import ROUTER_APP_ID
from compute_space.core.service_interface.services import ServiceDefault
from compute_space.core.service_interface.services import ServiceProvider
from compute_space.core.service_interface.services import all_defaults
from compute_space.core.service_interface.services import clear_default
from compute_space.core.service_interface.services import list_all_service_providers
from compute_space.core.service_interface.services import providers_for
from compute_space.core.service_interface.services import set_default
from compute_space.web.auth.auth import require_owner_auth
from compute_space.web.auth.auth import require_owner_or_app_auth


@attr.s(auto_attribs=True, frozen=True)
class OkResponse:
    ok: bool


@attr.s(auto_attribs=True, frozen=True)
class SetDefaultRequest:
    service_url: str
    app_id: str


@attr.s(auto_attribs=True, frozen=True)
class RemoveDefaultRequest:
    service_url: str


@get("/api/services/v2", guards=[require_owner_auth])
async def list_services_v2(db: NamedDependency[sqlite3.Connection]) -> list[ServiceProvider]:
    """List all registered V2 service providers."""
    return list_all_service_providers(db)


@get("/api/services/v2/providers", guards=[require_owner_or_app_auth])
async def discover_providers(
    db: NamedDependency[sqlite3.Connection], service: FromQuery[str]
) -> list[ServiceProvider]:
    return providers_for(service, db)


@get("/api/services/v2/defaults", guards=[require_owner_auth])
async def list_defaults(db: NamedDependency[sqlite3.Connection]) -> list[ServiceDefault]:
    """List all default provider settings."""
    return all_defaults(db)


@post("/api/services/v2/defaults", status_code=200, guards=[require_owner_auth], raises=[NotFoundException])
async def set_default_route(data: SetDefaultRequest, db: NamedDependency[sqlite3.Connection]) -> OkResponse:
    """Set the default provider for a service.

    Choosing the router means clearing the default rather than storing a row: ``service_defaults``
    has a foreign key into ``apps``, and the builtin serves whenever no app is picked.
    """
    if data.app_id == ROUTER_APP_ID:
        clear_default(data.service_url, db)
        return OkResponse(ok=True)
    try:
        set_default(data.service_url, data.app_id, db)
    except LookupError as e:
        raise NotFoundException(detail="No such provider") from e
    return OkResponse(ok=True)


@delete("/api/services/v2/defaults", status_code=200, guards=[require_owner_auth])
async def remove_default(data: RemoveDefaultRequest, db: NamedDependency[sqlite3.Connection]) -> OkResponse:
    """Remove the default provider for a service, handing it back to the builtin if there is one."""
    clear_default(data.service_url, db)
    return OkResponse(ok=True)


api_services_v2_routes = Router(
    path="/",
    route_handlers=[list_services_v2, discover_providers, list_defaults, set_default_route, remove_default],
)
