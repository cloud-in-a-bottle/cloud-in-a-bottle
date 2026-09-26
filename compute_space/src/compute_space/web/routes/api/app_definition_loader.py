import asyncio
import json
import sqlite3
from contextlib import closing
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
from compute_space.core.app_definition_loader import import_platform_api_tokens
from compute_space.core.app_definition_loader import parse_definition
from compute_space.core.app_definitions import DefinitionExport
from compute_space.core.app_definitions import PlatformApiToken
from compute_space.core.app_definitions import PrivateDefinitionExport
from compute_space.db import get_db
from compute_space.web.auth.auth import require_owner_auth


def _response(body: object, status: int = 200) -> Response[str]:
    return Response(
        json.dumps(body), status_code=status, media_type="application/json", headers={"Cache-Control": "no-store"}
    )


def _load_error(request: Request[Any, Any, Any], exc: Exception) -> Response[str]:
    if isinstance(exc, DefinitionError):
        return _response({"error": str(exc)}, 400)
    status = exc.status_code if isinstance(exc, HTTPException) else 500
    # Uploaded YAML and database errors may contain private data in their details and traceback locals.
    return _response({"error": "App definition loading failed."}, status)


async def _document(request: Request[Any, Any, Any]) -> DefinitionExport:
    try:
        body = await request.json()
    except (SerializationException, ValueError):
        raise DefinitionError("Expected a JSON object containing YAML content.") from None
    if not isinstance(body, dict) or body.keys() != {"content"} or not isinstance(body.get("content"), str):
        raise DefinitionError("Expected a JSON object containing only YAML content.")
    return await asyncio.to_thread(parse_definition, body["content"])


def _import_tokens(tokens: tuple[PlatformApiToken, ...]) -> int:
    # The worker owns its connection even if the awaiting request is cancelled.
    with closing(get_db()) as db:
        return import_platform_api_tokens(db, tokens)


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
    "/api/app-definitions/import-private",
    guards=[require_owner_auth],
    exception_handlers={Exception: _load_error, NotAuthorizedException: _load_error},
    request_max_body_size=6 * MAX_DEFINITION_BYTES + 1024,
    status_code=200,
)
async def owner_import_private(
    request: Request[Any, Any, Any],
    db: NamedDependency[sqlite3.Connection],
    config: NamedDependency[Config],
) -> Response[str]:
    document = await _document(request)
    # Recheck the entire plan, including builtin containment, before the first write.
    definition_plan(document, db, config.apps_dir)
    tokens = document.platform_api_tokens if isinstance(document, PrivateDefinitionExport) else ()
    added = await asyncio.to_thread(_import_tokens, tokens)
    return _response({"ok": True, "added_api_token_count": added, "existing_api_token_count": len(tokens) - added})
