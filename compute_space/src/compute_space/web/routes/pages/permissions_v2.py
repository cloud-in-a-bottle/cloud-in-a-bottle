import json
import sqlite3

from litestar import Router
from litestar import get
from litestar.di import NamedDependency
from litestar.exceptions import NotFoundException
from litestar.exceptions import ValidationException
from litestar.response import Template

from compute_space.core.auth.permissions_v2 import get_granted_permissions_v2
from compute_space.web.auth.auth import require_owner_auth


@get(
    "/approve-permissions-v2",
    guards=[require_owner_auth],
    raises=[ValidationException, NotFoundException],
)
async def approve_permissions_v2(
    db: NamedDependency[sqlite3.Connection],
    app: str,
    service: str,
    grant: str,
    return_to: str | None = None,
) -> Template:
    """Owner-facing page: grant or deny a V2 permission request."""
    row = db.execute("SELECT name FROM apps WHERE app_id = ?", (app,)).fetchone()
    if not row:
        raise NotFoundException(detail="App not found")

    try:
        grant_parsed = json.loads(grant)
    except json.JSONDecodeError as e:
        raise ValidationException(detail="Invalid grant JSON") from e

    existing = get_granted_permissions_v2(app, service)
    already_granted = any(g.grant == grant_parsed and g.scope == "global" for g in existing)

    return Template(
        template_name="approve_permissions_v2.html",
        context={
            "app_id": app,
            "app_name": row["name"],
            "service_url": service,
            "grant": grant_parsed,
            "grant_json": grant,
            "already_granted": already_granted,
            "redirect_url": return_to,
        },
    )


pages_permissions_v2_routes = Router(path="/", route_handlers=[approve_permissions_v2])
