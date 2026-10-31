import sqlite3

from litestar import Router
from litestar import get
from litestar.di import NamedDependency

from compute_space.core.auto_suspend.status import AutoSuspendStatus
from compute_space.core.auto_suspend.status import get_auto_suspend_status
from compute_space.web.auth.auth import require_auto_suspend_auth


@get("/api/auto-suspend-status", guards=[require_auto_suspend_auth])
async def auto_suspend_status(db: NamedDependency[sqlite3.Connection]) -> AutoSuspendStatus:
    """For the host that suspends this instance when it's idle: whether a suspend now would stall work in progress.
    While an openhost update has the router stopped, the downtime updater answers 503 instead; the host should treat that
    as busy."""
    return get_auto_suspend_status(db)


api_auto_suspend_routes = Router(path="/", route_handlers=[auto_suspend_status])
