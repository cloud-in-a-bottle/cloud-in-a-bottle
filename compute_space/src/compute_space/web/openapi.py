"""OpenAPI schema configuration, shared by the live ``/schema`` endpoint
(see ``app.py``) and the committed-spec generator (see ``dump_openapi.py``)."""

from typing import Any

from litestar import Litestar
from litestar.di import Provide
from litestar.openapi import OpenAPIConfig
from litestar.openapi.spec import Components
from litestar.openapi.spec import SecurityScheme
from litestar.serialization import decode_json
from litestar.serialization import encode_json

from compute_space.config import provide_config
from compute_space.db import provide_db
from compute_space.web.routes.manifest import ALL_ROUTERS

# Version of the HTTP API contract, independent of the deployed git sha
# reported by ``GET /api/version``. Bump on breaking API-surface changes.
API_VERSION = "1.0.0"

_BEARER_SCHEME = "BearerToken"

OPENAPI_CONFIG = OpenAPIConfig(
    title="OpenHost Zone API",
    version=API_VERSION,
    description=(
        "HTTP API the `oh` CLI and other owner clients call on a running "
        "OpenHost zone. Every route requires an owner bearer token."
    ),
    security=[{_BEARER_SCHEME: []}],
    components=Components(
        security_schemes={
            _BEARER_SCHEME: SecurityScheme(
                type="http",
                scheme="bearer",
                description="Owner API token, sent as `Authorization: Bearer <token>`.",
            )
        }
    ),
)


def build_openapi_schema() -> dict[str, Any]:
    """Generate the OpenAPI document from every registered router, matching
    what the live app serves at ``/schema/openapi.json``. Endpoints opt out
    per-handler via ``include_in_schema=False``. Round-tripped through
    Litestar's serializer to resolve embedded attrs defaults to plain types."""
    app = Litestar(
        route_handlers=list(ALL_ROUTERS),
        # Mirror the app-level dependencies in app.py so injected params
        # (config, db) are recognized as DI, not surfaced as query params.
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        openapi_config=OPENAPI_CONFIG,
    )
    schema: dict[str, Any] = decode_json(encode_json(app.openapi_schema.to_schema()))
    return schema
