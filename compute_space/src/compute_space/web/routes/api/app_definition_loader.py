import asyncio
import json
import sqlite3
from typing import Any

import attr
from litestar import Request
from litestar import Response
from litestar import post
from litestar.di import NamedDependency
from litestar.exceptions import HTTPException
from litestar.exceptions import NotAuthorizedException
from litestar.exceptions import SerializationException

from compute_space.config import Config
from compute_space.core.app_definition_loader import MAX_DEFINITION_BYTES
from compute_space.core.app_definition_loader import DefinitionError
from compute_space.core.app_definition_loader import definition_plan
from compute_space.core.app_definition_loader import parse_definition
from compute_space.core.app_definition_secrets import SecretImportError
from compute_space.core.app_definition_secrets import import_secret_values
from compute_space.core.app_definitions import DefinitionExport
from compute_space.core.app_definitions import PrivateDefinitionExport
from compute_space.web.auth.auth import require_owner_auth


def _response(body: object, status: int = 200) -> Response[str]:
    return Response(
        json.dumps(body), status_code=status, media_type="application/json", headers={"Cache-Control": "no-store"}
    )


def _load_error(request: Request[Any, Any, Any], exc: Exception) -> Response[str]:
    if isinstance(exc, DefinitionError):
        return _response({"error": str(exc)}, 400)
    if isinstance(exc, SecretImportError):
        return _response({"error": str(exc), "saved_secret_count": exc.saved_secret_count}, 502)
    status = exc.status_code if isinstance(exc, HTTPException) else 500
    # Uploaded YAML and provider errors may contain values in their details and traceback locals.
    return _response({"error": "App definition loading failed."}, status)


async def _document(request: Request[Any, Any, Any], *, importing: bool = False) -> DefinitionExport:
    try:
        body = await request.json()
    except (SerializationException, ValueError):
        raise DefinitionError("Expected a JSON object containing YAML content.") from None
    allowed = {"content", "replace_existing"} if importing else {"content"}
    if (
        not isinstance(body, dict)
        or body.keys() - allowed
        or not isinstance(body.get("content"), str)
        or ("replace_existing" in body and type(body["replace_existing"]) is not bool)
    ):
        raise DefinitionError("Expected YAML content and, for secret import, a boolean replace_existing.")
    document = await asyncio.to_thread(parse_definition, body["content"])
    if importing and isinstance(document, PrivateDefinitionExport) and document.secret_values:
        if body.get("replace_existing") is not True:
            raise DefinitionError("Confirm replacement of the named secret values with replace_existing: true.")
    return document


@post(
    "/api/app-definitions/parse",
    guards=[require_owner_auth],
    exception_handlers={Exception: _load_error, NotAuthorizedException: _load_error},
    request_max_body_size=6 * MAX_DEFINITION_BYTES + 1024,
    status_code=200,
)
async def owner_parse(
    request: Request[Any, Any, Any],
    db: NamedDependency[sqlite3.Connection],
    config: NamedDependency[Config],
) -> Response[str]:
    document = await _document(request)
    plan = definition_plan(document, db, config.apps_dir)
    return _response(attr.asdict(plan, filter=lambda field, value: value is not None))


@post(
    "/api/app-definitions/import-secrets",
    guards=[require_owner_auth],
    exception_handlers={Exception: _load_error, NotAuthorizedException: _load_error},
    request_max_body_size=6 * MAX_DEFINITION_BYTES + 1024,
    status_code=200,
)
async def owner_import_secrets(
    request: Request[Any, Any, Any],
    db: NamedDependency[sqlite3.Connection],
    config: NamedDependency[Config],
) -> Response[str]:
    document = await _document(request, importing=True)
    # Recheck the entire plan, including builtin containment, before the first write.
    definition_plan(document, db, config.apps_dir)
    values = document.secret_values if isinstance(document, PrivateDefinitionExport) else {}
    return _response({"ok": True, "saved_secret_count": await import_secret_values(db, values)})
