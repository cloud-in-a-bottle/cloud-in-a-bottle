import json
from pathlib import Path
from typing import NoReturn

from litestar import Litestar
from litestar.di import Provide
from litestar.serialization import encode_json

from compute_space.web.app import ROUTE_HANDLERS

type JsonValue = dict[str, JsonValue] | list[JsonValue] | str | int | float | bool | None

SPEC_PATH = Path(__file__).resolve().parents[4] / "openapi.json"

# Names of everything create_app injects.  Schema generation never resolves them, but litestar refuses to build an
# app whose handlers ask for a dependency nobody provides, so a new dependency in create_app needs adding here too.
_DEPENDENCY_NAMES = ("config", "db", "dns_provider", "mdns_responder")

# Fields that only document the API: changing them can't break a client, so they're left out of the spec.
_DOC_ONLY_KEYS = frozenset(
    {"description", "summary", "title", "example", "examples", "externalDocs", "tags", "operationId", "deprecated"}
)

# The top-level fields that describe how to call the API, as opposed to `info`, `servers` etc.
_FUNCTIONAL_TOP_LEVEL_KEYS = ("openapi", "paths", "components")

# Fields whose value maps names chosen by the API (paths, schema and property names, status codes, media types)
# to objects.  Their keys are data, so a property that happens to be called `description` must survive.
_NAME_MAP_KEYS = frozenset(
    {"paths", "schemas", "properties", "patternProperties", "responses", "content", "headers", "parameters"}
)


def _unavailable_dependency(name: str) -> Provide:
    # A new function per dependency: litestar rejects one provider callable registered under several names.
    def provide() -> NoReturn:
        raise RuntimeError(f"dependency {name!r} is not available while generating the OpenAPI spec")

    return Provide(provide, sync_to_thread=False)


def _strip_docs(value: JsonValue, is_name_map: bool = False) -> JsonValue:
    if isinstance(value, list):
        return [_strip_docs(v) for v in value]
    if isinstance(value, dict):
        return {
            k: _strip_docs(v, is_name_map=not is_name_map and k in _NAME_MAP_KEYS)
            for k, v in value.items()
            if is_name_map or k not in _DOC_ONLY_KEYS
        }
    return value


def generate_functional_spec() -> dict[str, JsonValue]:
    """The router's OpenAPI spec as litestar generates it, minus everything that is only documentation."""
    app = Litestar(
        route_handlers=ROUTE_HANDLERS,
        dependencies={name: _unavailable_dependency(name) for name in _DEPENDENCY_NAMES},
    )
    # Round-trip through litestar's encoder: the raw schema dict still holds attrs instances (eg in examples).
    full_spec = json.loads(encode_json(app.openapi_schema.to_schema()))
    return {key: _strip_docs(full_spec[key]) for key in _FUNCTIONAL_TOP_LEVEL_KEYS}


def render_spec(spec: dict[str, JsonValue]) -> str:
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"
