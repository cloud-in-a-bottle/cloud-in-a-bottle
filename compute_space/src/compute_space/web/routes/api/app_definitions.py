import json
import sqlite3
from typing import Any

from litestar import Litestar
from litestar import Request
from litestar import Response
from litestar import Router
from litestar import post
from litestar.di import NamedDependency
from litestar.di import Provide
from litestar.exceptions import ClientException
from litestar.exceptions import HTTPException
from litestar.exceptions import NotAuthorizedException
from litestar.exceptions import SerializationException

from compute_space.config import Config
from compute_space.config import provide_config
from compute_space.core.app_definitions import ExportMode
from compute_space.core.app_definitions import export_app_definitions
from compute_space.core.app_definitions import parse_export_mode
from compute_space.core.service_interface.headers import PERMISSIONS_HEADER
from compute_space.db import provide_db
from compute_space.web.auth.auth import require_owner_auth
from compute_space.web.helpers.app_definition_export import dump_export_yaml
from compute_space.web.helpers.app_definition_export import export_media_type


def _json_response(content: str, status_code: int = 200) -> Response[str]:
    return Response(
        content, status_code=status_code, media_type="application/json", headers={"Cache-Control": "no-store"}
    )


def _export_response(request: Request[Any, Any, Any], content: str) -> Response[str]:
    document = json.loads(content)
    media_type = export_media_type(request.accept)
    return Response(
        dump_export_yaml(document) if media_type == "application/yaml" else content,
        status_code=200,
        media_type=media_type,
        headers={
            "Cache-Control": "no-store",
            "Vary": "Accept",
            "X-App-Definitions-Mode": document["mode"],
            "X-App-Definitions-Schema-Version": str(document["schema_version"]),
        },
    )


def _export_error(request: Request[Any, Any, Any], exc: Exception) -> Response[str]:
    status = exc.status_code if isinstance(exc, HTTPException) else 500
    # Never render exception details or log traceback locals: either can contain private data.
    return _json_response(json.dumps({"error": "App definition export failed."}), status)


async def _mode(request: Request[Any, Any, Any]) -> ExportMode:
    try:
        return parse_export_mode(await request.json())
    except (ValueError, SerializationException):
        raise ClientException(detail="mode must be sharing or private") from None


@post(
    "/api/app-definitions/export",
    guards=[require_owner_auth],
    exception_handlers={Exception: _export_error, NotAuthorizedException: _export_error},
    status_code=200,
)
async def owner_export(
    request: Request[Any, Any, Any],
    db: NamedDependency[sqlite3.Connection],
    config: NamedDependency[Config],
) -> Response[str]:
    mode = await _mode(request)
    return _export_response(request, await export_app_definitions(db, config.apps_dir, mode))


def _has_export_grant(request: Request[Any, Any, Any], mode: ExportMode) -> bool:
    try:
        permissions = json.loads(request.headers.get(PERMISSIONS_HEADER, "[]"))
    except ValueError:
        return False
    if not isinstance(permissions, list):
        return False
    accepted = [{"mode": mode}] if mode == "private" else [{"mode": "sharing"}, {"mode": "private"}]
    return any(
        isinstance(entry, dict) and entry.get("scope") == "global" and entry.get("grant") in accepted
        for entry in permissions
    )


@post("/export", status_code=200)
async def service_export(
    request: Request[Any, Any, Any],
    db: NamedDependency[sqlite3.Connection],
    config: NamedDependency[Config],
) -> Response[str]:
    mode = await _mode(request)
    if not _has_export_grant(request, mode):
        return _json_response(
            json.dumps(
                {"code": "permission_required", "required_grant": {"grant": {"mode": mode}, "scope": "global"}}
            ),
            403,
        )
    return _export_response(request, await export_app_definitions(db, config.apps_dir, mode))


api_app_definitions_routes = Router(path="/", route_handlers=[owner_export])

# This app is reachable only through BuiltinService's in-process transport, never a public route.
# Its headers are authoritative because the normal authenticated service proxy replaces them.
app_definitions_service_app = Litestar(
    route_handlers=[service_export],
    dependencies={"db": Provide(provide_db), "config": Provide(provide_config, sync_to_thread=False)},
    exception_handlers={Exception: _export_error},
    openapi_config=None,
)
