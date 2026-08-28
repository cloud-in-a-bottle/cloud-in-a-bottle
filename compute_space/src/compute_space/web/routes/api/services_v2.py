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
from compute_space.core.app_id import ROUTER_APP_NAME
from compute_space.core.service_interface.builtin_services import builtin_for
from compute_space.core.service_interface.registry import all_defaults
from compute_space.core.service_interface.registry import all_providers
from compute_space.core.service_interface.registry import clear_default
from compute_space.core.service_interface.registry import default_provider_id
from compute_space.core.service_interface.registry import providers_for
from compute_space.core.service_interface.registry import set_default
from compute_space.web.auth.auth import require_owner_auth
from compute_space.web.auth.auth import require_owner_or_app_auth


@attr.s(auto_attribs=True, frozen=True)
class ProviderV2:
    service_url: str
    app_id: str
    app_name: str
    service_version: str
    endpoint: str
    status: str


@attr.s(auto_attribs=True, frozen=True)
class DiscoveredProvider:
    app_id: str
    app_name: str
    service_version: str
    endpoint: str
    status: str
    is_default: bool


@attr.s(auto_attribs=True, frozen=True)
class DiscoverProvidersResponse:
    providers: list[DiscoveredProvider]


@attr.s(auto_attribs=True, frozen=True)
class DefaultEntry:
    service_url: str
    app_id: str
    app_name: str


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
async def list_services_v2(db: NamedDependency[sqlite3.Connection]) -> list[ProviderV2]:
    """List all registered V2 service providers."""
    return [
        ProviderV2(
            service_url=p.service_url,
            app_id=p.app_id,
            app_name=p.app_name,
            service_version=p.service_version,
            endpoint=p.endpoint,
            status=p.status,
        )
        for p in all_providers(db)
    ]


@get("/api/services/v2/providers", guards=[require_owner_or_app_auth])
async def discover_providers(
    db: NamedDependency[sqlite3.Connection], service: FromQuery[str]
) -> DiscoverProvidersResponse:
    """Discover providers for a service.

    A router builtin is listed alongside the apps, so the owner can see what is serving the
    service today and switch between them.  It is the default exactly when no app has been picked.
    """
    default_app_id = default_provider_id(service, db)
    providers = [
        DiscoveredProvider(
            app_id=p.app_id,
            app_name=p.app_name,
            service_version=p.service_version,
            endpoint=p.endpoint,
            status=p.status,
            is_default=p.app_id == default_app_id,
        )
        for p in providers_for(service, db)
    ]

    builtin = builtin_for(service, db, provider_override=ROUTER_APP_ID)
    if builtin is not None:
        providers.insert(
            0,
            DiscoveredProvider(
                app_id=ROUTER_APP_ID,
                app_name=ROUTER_APP_NAME,
                service_version=builtin.version,
                endpoint="/",
                status="running",
                is_default=default_app_id is None,
            ),
        )
    return DiscoverProvidersResponse(providers=providers)


@get("/api/services/v2/defaults", guards=[require_owner_auth])
async def list_defaults(db: NamedDependency[sqlite3.Connection]) -> list[DefaultEntry]:
    """List all default provider settings."""
    return [DefaultEntry(service_url=d.service_url, app_id=d.app_id, app_name=d.app_name) for d in all_defaults(db)]


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
