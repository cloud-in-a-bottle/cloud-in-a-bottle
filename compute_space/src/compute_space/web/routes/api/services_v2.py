import sqlite3

import attr
from litestar import Router
from litestar import delete
from litestar import get
from litestar import post
from litestar.di import NamedDependency
from litestar.exceptions import HTTPException
from litestar.params import FromQuery

from compute_space.web.auth.auth import require_owner_auth
from compute_space.web.auth.auth import require_owner_or_app_auth
from compute_space.web.routes.api.responses import OkResponse


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
class SetDefaultRequest:
    service_url: str
    app_id: str


@attr.s(auto_attribs=True, frozen=True)
class RemoveDefaultRequest:
    service_url: str


@get("/api/services/v2", guards=[require_owner_auth])
async def list_services_v2(db: NamedDependency[sqlite3.Connection]) -> list[ProviderV2]:
    """List all registered V2 service providers."""
    rows = db.execute(
        """SELECT sp.service_url, sp.app_id, a.name AS app_name, sp.service_version, sp.endpoint, a.status
           FROM service_providers_v2 sp
           JOIN apps a ON a.app_id = sp.app_id"""
    ).fetchall()
    return [
        ProviderV2(
            service_url=r["service_url"],
            app_id=r["app_id"],
            app_name=r["app_name"],
            service_version=r["service_version"],
            endpoint=r["endpoint"],
            status=r["status"],
        )
        for r in rows
    ]


@get("/api/services/v2/providers", guards=[require_owner_or_app_auth])
async def discover_providers(
    db: NamedDependency[sqlite3.Connection], service: FromQuery[str]
) -> DiscoverProvidersResponse:
    """Discover providers for a service, optionally filtered by version specifier."""

    rows = db.execute(
        """SELECT sp.app_id, a.name AS app_name, sp.service_version, sp.endpoint, a.status
           FROM service_providers_v2 sp
           JOIN apps a ON a.app_id = sp.app_id
           WHERE sp.service_url = ?""",
        (service,),
    ).fetchall()

    default = db.execute(
        "SELECT app_id FROM service_defaults WHERE service_url = ?",
        (service,),
    ).fetchone()
    default_app_id = default["app_id"] if default else None

    return DiscoverProvidersResponse(
        providers=[
            DiscoveredProvider(
                app_id=r["app_id"],
                app_name=r["app_name"],
                service_version=r["service_version"],
                endpoint=r["endpoint"],
                status=r["status"],
                is_default=r["app_id"] == default_app_id,
            )
            for r in rows
        ]
    )


@get("/api/services/v2/defaults", guards=[require_owner_auth])
async def list_defaults(db: NamedDependency[sqlite3.Connection]) -> list[DefaultEntry]:
    """List all default provider settings."""
    rows = db.execute(
        """SELECT sd.service_url, sd.app_id, a.name AS app_name
           FROM service_defaults sd
           JOIN apps a ON a.app_id = sd.app_id"""
    ).fetchall()
    return [DefaultEntry(service_url=r["service_url"], app_id=r["app_id"], app_name=r["app_name"]) for r in rows]


@post("/api/services/v2/defaults", status_code=200, guards=[require_owner_auth])
async def set_default(data: SetDefaultRequest, db: NamedDependency[sqlite3.Connection]) -> OkResponse:
    """Set the default provider for a service."""
    row = db.execute(
        "SELECT 1 FROM service_providers_v2 WHERE service_url = ? AND app_id = ?",
        (data.service_url, data.app_id),
    ).fetchone()
    if not row:
        raise HTTPException(detail="No such provider", status_code=404)

    db.execute(
        "INSERT OR REPLACE INTO service_defaults (service_url, app_id) VALUES (?, ?)",
        (data.service_url, data.app_id),
    )
    db.commit()
    return OkResponse(ok=True)


@delete("/api/services/v2/defaults", status_code=200, guards=[require_owner_auth])
async def remove_default(data: RemoveDefaultRequest, db: NamedDependency[sqlite3.Connection]) -> OkResponse:
    """Remove the default provider for a service (falls back to highest version)."""
    db.execute("DELETE FROM service_defaults WHERE service_url = ?", (data.service_url,))
    db.commit()
    return OkResponse(ok=True)


api_services_v2_routes = Router(
    path="/",
    route_handlers=[list_services_v2, discover_providers, list_defaults, set_default, remove_default],
)
