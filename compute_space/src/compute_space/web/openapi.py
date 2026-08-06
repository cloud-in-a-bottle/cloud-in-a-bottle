"""OpenAPI schema generation, dumped to ``compute_space/openapi.yaml``.
Imports nothing from ``routes``: handlers reference the names below."""

from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any

from litestar import Litestar
from litestar.di import Provide
from litestar.openapi import OpenAPIConfig
from litestar.openapi.spec import Components
from litestar.openapi.spec import ExternalDocumentation
from litestar.openapi.spec import SecurityScheme
from litestar.openapi.spec import Tag
from litestar.serialization import decode_json
from litestar.serialization import encode_json
from litestar.types import ControllerRouterHandler

# Version of the HTTP API contract, independent of the deployed git sha
# reported by ``GET /api/version``. Bump on breaking API-surface changes.
API_VERSION = "1.0.0"

OWNER_SCHEME = "OwnerToken"
APP_SCHEME = "AppToken"

# Operations whose real contract lives in a manual chapter rather than in this
# document — tagged here so the generated page links out instead of pretending
# the schema is the whole story.
CROSS_APP_TAG = "Cross-app services"

_OPENAPI_CONFIG = OpenAPIConfig(
    title="OpenHost Zone API",
    version=API_VERSION,
    description=(
        "HTTP API for a running OpenHost zone. Most routes are called by the "
        "`oh` CLI and other owner clients with an owner token; `/health` and "
        "the `/.well-known/` identity routes are public, and the cross-app "
        "service proxy is called by apps with an app token."
    ),
    security=[{OWNER_SCHEME: []}],
    # Handler docstrings become the operation descriptions, so the prose lives
    # next to the code it describes.
    use_handler_docstrings=True,
    # The chapter this document is embedded in — for readers who got the raw
    # file instead. Relative because the zone domain differs per install.
    external_docs=ExternalDocumentation(url="/docs/api", description="HTTP API chapter"),
    tags=[
        Tag(
            name=CROSS_APP_TAG,
            description=(
                "Requests one app makes to another through the router. The path "
                "after the shortname, the request body and the response are all "
                "passed through to the provider app, so the contract belongs to "
                "that service's own spec — not to this document."
            ),
            external_docs=ExternalDocumentation(
                url="/docs/cross_app_services",
                description="Cross-App Services",
            ),
        )
    ],
    components=Components(
        security_schemes={
            OWNER_SCHEME: SecurityScheme(
                type="http",
                scheme="bearer",
                description="Owner API token, sent as `Authorization: Bearer <token>`.",
            ),
            APP_SCHEME: SecurityScheme(
                type="http",
                scheme="bearer",
                description=(
                    "App token, injected by the router as `OPENHOST_APP_TOKEN` and "
                    "sent as `Authorization: Bearer <token>`. Not an owner token — "
                    "an owner token is rejected on these routes."
                ),
            ),
        }
    ),
)


def build_openapi_schema(
    route_handlers: Sequence[ControllerRouterHandler],
    dependencies: Mapping[str, Provide],
) -> dict[str, Any]:
    """Takes the live app's routers and dependencies so injected params read as
    DI, not query parameters. Round-tripped to resolve attrs defaults."""
    app = Litestar(
        route_handlers=list(route_handlers),
        dependencies=dict(dependencies),
        openapi_config=_OPENAPI_CONFIG,
    )
    schema: dict[str, Any] = decode_json(encode_json(app.openapi_schema.to_schema()))
    return schema
