from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import attr
from litestar import Request
from litestar import Router
from litestar import get
from litestar import post
from litestar.di import NamedDependency
from litestar.exceptions import NotFoundException
from litestar.exceptions import ServiceUnavailableException
from litestar.exceptions import ValidationException

from compute_space.config import Config
from compute_space.core.domains import host_with_request_port
from compute_space.core.operation_locks import detach_operation
from compute_space.core.operation_locks import start_exclusive_operation
from compute_space.core.root_app import AppsBusyForDashboardMoveError
from compute_space.core.root_app import RootAppNotFoundError
from compute_space.core.root_app import RootKind
from compute_space.core.root_app import RootSetting
from compute_space.core.root_app import RootSettingError
from compute_space.core.root_app import apply_root_setting
from compute_space.core.root_app import load_root_setting
from compute_space.core.root_app import validate_root_setting
from compute_space.core.system_agent.client import SystemAgentError
from compute_space.core.system_agent.client import system_agent_reset_restart_limit_sync
from compute_space.core.updates import PROCESS_GENERATION
from compute_space.core.updates import is_shutdown_pending
from compute_space.core.updates import trigger_restart
from compute_space.web.auth.auth import require_owner_auth
from compute_space.web.exceptions import ConflictException
from compute_space.web.helpers.zone import zone_for_request


@attr.s(auto_attribs=True, frozen=True)
class RootAppOption:
    app_id: str
    name: str


@attr.s(auto_attribs=True, frozen=True)
class RootAppResponse:
    kind: RootKind
    # Set iff kind is "app".
    app_id: str | None
    # Where the dashboard lives while it isn't at the bare domain.
    router_subdomain: str
    apps: list[RootAppOption]
    # The router on the domain (and access port) the request arrived on, under this setting.
    router_url: str
    # True if saving moved the dashboard: the instance is restarting, and so are running apps, to pick up the new
    # OPENHOST_ROUTER_PUBLIC_URL.  Wait for /health at router_url to report a generation other than this one.
    restarting: bool = False
    generation: str = PROCESS_GENERATION


@attr.s(auto_attribs=True, frozen=True)
class SetRootAppRequest:
    kind: RootKind
    app_id: str | None
    router_subdomain: str


def _response(db: sqlite3.Connection, request: Request[Any, Any, Any], restarting: bool = False) -> RootAppResponse:
    setting = load_root_setting(db)
    zone = zone_for_request(request)
    rows = db.execute("SELECT app_id, name FROM apps WHERE status != 'removing' ORDER BY name").fetchall()
    return RootAppResponse(
        kind=setting.kind,
        app_id=setting.app_id,
        router_subdomain=setting.router_subdomain,
        apps=[RootAppOption(app_id=row["app_id"], name=row["name"]) for row in rows],
        router_url=f"{zone.scheme}://{host_with_request_port(setting.router_host(zone), request.url.netloc)}",
        restarting=restarting,
    )


@get("/api/settings/root-app", guards=[require_owner_auth])
async def get_root_app(db: NamedDependency[sqlite3.Connection], request: Request[Any, Any, Any]) -> RootAppResponse:
    return _response(db, request)


@post(
    "/api/settings/root-app",
    status_code=200,
    guards=[require_owner_auth],
    raises=[NotFoundException, ValidationException, ServiceUnavailableException],
)
async def set_root_app(
    data: SetRootAppRequest,
    config: NamedDependency[Config],
    db: NamedDependency[sqlite3.Connection],
    request: Request[Any, Any, Any],
) -> RootAppResponse:
    """Choose what the bare domain serves.  Moving the dashboard restarts the instance and its running apps, the same
    way a primary-domain change does."""
    setting = RootSetting(kind=data.kind, app_id=data.app_id, router_subdomain=data.router_subdomain.strip().lower())
    if is_shutdown_pending():
        raise ConflictException(detail="the instance is already restarting", extra={"code": "restart_pending"})

    def _run() -> bool:
        worker_db = sqlite3.connect(config.db_path)
        worker_db.row_factory = sqlite3.Row
        try:
            if load_root_setting(worker_db).router_label != setting.router_label:
                # Refuse a bad setting before touching the system agent; apply_root_setting checks again.
                validate_root_setting(worker_db, setting)
                system_agent_reset_restart_limit_sync()
            return apply_root_setting(worker_db, setting)
        finally:
            worker_db.close()

    async def _apply_and_restart() -> bool:
        if is_shutdown_pending():
            raise ConflictException(detail="the instance is already restarting", extra={"code": "restart_pending"})
        moved = await asyncio.to_thread(_run)
        if moved:
            trigger_restart()
        return moved

    worker = await start_exclusive_operation(_apply_and_restart)
    if worker is None:
        raise ConflictException(
            detail="Another archive, domain or dashboard operation is already in progress.",
            extra={"code": "operation_busy"},
        )
    try:
        moved = await asyncio.shield(worker)
    except asyncio.CancelledError:
        detach_operation(worker)
        raise
    except SystemAgentError as exc:
        raise ServiceUnavailableException(
            detail="could not prepare the instance for a safe restart", extra={"code": "restart_unavailable"}
        ) from exc
    except RootAppNotFoundError as exc:
        raise NotFoundException(detail="App not found") from exc
    except RootSettingError as exc:
        raise ValidationException(detail=str(exc)) from exc
    except AppsBusyForDashboardMoveError as exc:
        raise ConflictException(
            detail=f"Wait for these apps to finish before moving the dashboard: {', '.join(exc.app_names)}",
            extra={"code": "apps_busy"},
        ) from exc
    return _response(db, request, restarting=moved)


api_root_app_routes = Router(path="/", route_handlers=[get_root_app, set_root_app])
