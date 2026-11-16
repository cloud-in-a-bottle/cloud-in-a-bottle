import sqlite3

from litestar import Router
from litestar import post
from litestar.di import NamedDependency

from compute_space.core.theme import Theme
from compute_space.core.theme import write_theme
from compute_space.web.auth.auth import require_owner_auth


@post("/api/settings/theme", status_code=200, guards=[require_owner_auth])
async def set_theme(data: Theme, db: NamedDependency[sqlite3.Connection]) -> Theme:
    write_theme(db, data)
    return data


api_theme_routes = Router(path="/", route_handlers=[set_theme])
