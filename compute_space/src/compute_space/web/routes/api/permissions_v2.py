"""Owner-facing CRUD + provider-app grant endpoint for v2 permissions."""

import sqlite3
from typing import Annotated
from typing import Any

import attr
from litestar import MediaType
from litestar import Request
from litestar import Response
from litestar import Router
from litestar import get
from litestar import post
from litestar.di import NamedDependency
from litestar.params import Body

from compute_space.core.auth.permissions_v2 import get_all_permissions_v2
from compute_space.core.auth.permissions_v2 import grant_permission_v2
from compute_space.core.auth.permissions_v2 import revoke_permission_v2
from compute_space.web.auth.auth import require_app_auth
from compute_space.web.auth.auth import require_owner_auth
from compute_space.web.auth.auth import verify_app_auth


def _json_error(message: str, status: int) -> Response[dict[str, str]]:
    return Response(content={"error": message}, status_code=status, media_type=MediaType.JSON)


@get("/api/permissions/v2", guards=[require_owner_auth], sync_to_thread=False)
def list_permissions_v2(app_id: str | None = None) -> list[dict[str, Any]]:
    """List all V2 permissions, optionally filtered by app_id."""
    return [attr.asdict(p) for p in get_all_permissions_v2(app_id)]


@post(
    "/api/permissions/v2/grant_global_scoped",
    guards=[require_owner_auth],
    status_code=200,
    sync_to_thread=False,
)
def grant_global_scoped(
    data: Annotated[dict[str, Any], Body(media_type=MediaType.JSON)],
) -> Response[dict[str, Any]]:
    """Grant a global-scoped V2 permission (owner-authed)."""
    app_id = data.get("app_id")
    service_url = data.get("service_url")
    grant_payload = data.get("grant")
    if not app_id or not service_url or grant_payload is None:
        return _json_error("app_id, service_url, and grant are required", 400)

    grant_permission_v2(
        consumer_app_id=app_id,
        service_url=service_url,
        grant_payload=grant_payload,
        scope="global",
    )
    return Response(content={"ok": True})


@post(
    "/api/permissions/v2/revoke",
    guards=[require_owner_auth],
    status_code=200,
    sync_to_thread=False,
)
def revoke_v2(
    data: Annotated[dict[str, Any], Body(media_type=MediaType.JSON)],
) -> Response[dict[str, Any]]:
    """Revoke a V2 permission."""
    app_id = data.get("app_id")
    service_url = data.get("service_url")
    grant_payload = data.get("grant")
    if not app_id or not service_url or grant_payload is None:
        return _json_error("app_id, service_url, and grant are required", 400)

    revoked = revoke_permission_v2(
        consumer_app_id=app_id,
        service_url=service_url,
        grant_payload=grant_payload,
        scope=data.get("scope", "global"),
        provider_app_id=data.get("provider_app_id"),
    )
    if not revoked:
        return _json_error("Permission not found", 404)
    return Response(content={"ok": True})


@post(
    "/api/permissions/v2/grant_app_scoped",
    guards=[require_app_auth],
    status_code=200,
    sync_to_thread=False,
)
def grant_app_scoped(
    request: Request[Any, Any, Any],
    data: Annotated[dict[str, Any], Body(media_type=MediaType.JSON)],
    db: NamedDependency[sqlite3.Connection],
) -> Response[dict[str, Any]]:
    """Grant an app-scoped V2 permission, authenticated with the provider app's token.

    The calling app must be a registered provider for the specified service.
    The permission is automatically scoped to the calling provider app.
    """
    # ``require_app_auth`` already enforced this; verify_app_auth re-derives
    # the app_id for us (it returns the resolved id, raising if missing).
    provider_app_id = verify_app_auth(request)

    consumer_app_id = data.get("consumer_app_id")
    service_url = data.get("service_url")
    grant_payload = data.get("grant")
    if not consumer_app_id or not service_url or grant_payload is None:
        return _json_error("consumer_app_id, service_url, and grant are required", 400)

    # Verify the calling app is actually a registered provider for this
    # service.  Without this check any app with a token could grant
    # permissions for services it doesn't provide.
    is_provider = db.execute(
        "SELECT 1 FROM service_providers_v2 WHERE service_url = ? AND app_id = ?",
        (service_url, provider_app_id),
    ).fetchone()
    if not is_provider:
        return _json_error(f"App {provider_app_id} is not a registered provider for {service_url}", 403)

    grant_permission_v2(
        consumer_app_id=consumer_app_id,
        service_url=service_url,
        grant_payload=grant_payload,
        scope="app",
        provider_app_id=provider_app_id,
    )
    return Response(content={"ok": True})


api_permissions_v2_routes = Router(
    path="/",
    route_handlers=[
        list_permissions_v2,
        grant_global_scoped,
        revoke_v2,
        grant_app_scoped,
    ],
)
