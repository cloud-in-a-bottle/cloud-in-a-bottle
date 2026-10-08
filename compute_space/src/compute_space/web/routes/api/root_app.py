from __future__ import annotations

import sqlite3
from typing import Any

import attr
from litestar import Request
from litestar import Router
from litestar import get
from litestar import post
from litestar.di import NamedDependency
from litestar.exceptions import NotFoundException

from compute_space.core.domains import host_with_request_port
from compute_space.core.root_app import RootAppNotFoundError
from compute_space.core.root_app import get_root_app_id
from compute_space.core.root_app import set_root_app_id
from compute_space.web.auth.auth import require_owner_auth
from compute_space.web.helpers.zone import zone_for_request


@attr.s(auto_attribs=True, frozen=True)
class RootAppOption:
    app_id: str
    name: str


@attr.s(auto_attribs=True, frozen=True)
class RootAppResponse:
    # None: the dashboard is served at the bare domain.
    app_id: str | None
    apps: list[RootAppOption]
    # The router subdomain of the domain (and access port) the request arrived on.
    router_url: str


@attr.s(auto_attribs=True, frozen=True)
class SetRootAppRequest:
    # None: serve the dashboard at the bare domain.
    app_id: str | None


def _response(db: sqlite3.Connection, request: Request[Any, Any, Any]) -> RootAppResponse:
    zone = zone_for_request(request)
    rows = db.execute("SELECT app_id, name FROM apps WHERE status != 'removing' ORDER BY name").fetchall()
    return RootAppResponse(
        app_id=get_root_app_id(db),
        apps=[RootAppOption(app_id=row["app_id"], name=row["name"]) for row in rows],
        router_url=f"{zone.scheme}://{host_with_request_port(zone.router_host, request.url.netloc)}",
    )


@get("/api/settings/root-app", guards=[require_owner_auth])
async def get_root_app(db: NamedDependency[sqlite3.Connection], request: Request[Any, Any, Any]) -> RootAppResponse:
    return _response(db, request)


@post("/api/settings/root-app", status_code=200, guards=[require_owner_auth], raises=[NotFoundException])
async def set_root_app(
    data: SetRootAppRequest, db: NamedDependency[sqlite3.Connection], request: Request[Any, Any, Any]
) -> RootAppResponse:
    try:
        set_root_app_id(db, data.app_id)
    except RootAppNotFoundError as e:
        raise NotFoundException(detail="App not found") from e
    return _response(db, request)


api_root_app_routes = Router(path="/", route_handlers=[get_root_app, set_root_app])
