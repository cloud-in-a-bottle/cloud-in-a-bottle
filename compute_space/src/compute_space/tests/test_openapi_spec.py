"""Guards that the committed ``openapi.yaml`` stays in sync with the schema
generated from the API route handlers. Regenerate with
``pixi run -e dev generate-openapi``."""

from __future__ import annotations

from typing import Any

from compute_space.web.dump_openapi import _DEFAULT_OUTPUT
from compute_space.web.dump_openapi import render_openapi_yaml
from compute_space.web.openapi import APP_SCHEME
from compute_space.web.openapi import CROSS_APP_TAG
from compute_space.web.openapi import OWNER_SCHEME
from compute_space.web.openapi import build_openapi_schema
from compute_space.web.routes.manifest import ALL_ROUTERS
from compute_space.web.routes.manifest import APP_DEPENDENCIES


def test_committed_openapi_yaml_is_up_to_date() -> None:
    committed = _DEFAULT_OUTPUT.read_text(encoding="utf-8")
    assert committed == render_openapi_yaml(), "openapi.yaml is stale; run `pixi run -e dev generate-openapi`"


def test_injected_dependencies_are_not_query_parameters() -> None:
    schema = build_openapi_schema(ALL_ROUTERS, APP_DEPENDENCIES)
    paths = schema["paths"]
    assert "/api/apps" in paths
    params = [p["name"] for op in paths.values() for h in op.values() for p in h.get("parameters", [])]
    assert "db" not in params
    assert "config" not in params


def test_owner_security_is_the_default() -> None:
    schema = build_openapi_schema(ALL_ROUTERS, APP_DEPENDENCIES)
    assert schema["components"]["securitySchemes"][OWNER_SCHEME]["scheme"] == "bearer"
    assert {OWNER_SCHEME: []} in schema["security"]


def test_public_routes_opt_out_of_owner_security() -> None:
    """``/health`` and the identity routes take no token; the global owner
    requirement must not be inherited there."""
    paths = build_openapi_schema(ALL_ROUTERS, APP_DEPENDENCIES)["paths"]
    for path in ("/health", "/.well-known/jwks.json", "/.well-known/openhost-identity"):
        assert paths[path]["get"]["security"] == [{}], path


def test_cross_app_proxy_declares_app_auth_and_links_out() -> None:
    """The proxy takes an app token, not an owner token, and its real contract
    lives in the manual — the tag has to carry that link."""
    schema = build_openapi_schema(ALL_ROUTERS, APP_DEPENDENCIES)
    proxy = schema["paths"]["/api/services/v2/call/{shortname}/{rest}"]["get"]
    assert proxy["security"] == [{APP_SCHEME: []}]
    assert proxy["tags"] == [CROSS_APP_TAG]
    tag = next(t for t in schema["tags"] if t["name"] == CROSS_APP_TAG)
    assert tag["externalDocs"]["url"] == "/docs/cross_app_services"


def _is_untyped(schema: dict[str, Any] | None) -> bool:
    """An empty schema matches any JSON, so it documents nothing. A union
    renders as ``oneOf``, where one empty branch is just as permissive."""
    if schema in ({}, None):
        return True
    assert schema is not None
    branches = schema.get("oneOf") or schema.get("anyOf") or []
    return any(_is_untyped(b) for b in branches)


def test_every_success_response_is_typed() -> None:
    """A success response carries a schema that constrains it, or no body at
    all. Catches a handler added without the ``responses=`` a union needs."""
    paths = build_openapi_schema(ALL_ROUTERS, APP_DEPENDENCIES)["paths"]
    untyped = []
    for path, ops in paths.items():
        if "services/v2/call" in path:
            continue
        for method, op in ops.items():
            success = {k: v for k, v in op["responses"].items() if k.startswith("2")}
            for code, spec in success.items():
                content = spec.get("content")
                if not content:
                    continue  # 204 and friends: no body by definition
                schema = next(iter(content.values())).get("schema")
                if _is_untyped(schema):
                    untyped.append(f"{method.upper()} {path} -> {code}")
    assert not untyped, f"untyped success responses: {untyped}"


def test_error_bodies_are_declared() -> None:
    """Handlers that return ``ErrorResponse`` on a 4xx/5xx must say so, rather
    than leaving only Litestar's generic validation-error schema."""
    paths = build_openapi_schema(ALL_ROUTERS, APP_DEPENDENCIES)["paths"]
    assert paths["/api/app_status/{app_id}"]["get"]["responses"]["404"]["content"]
    assert paths["/stop_app/{app_id}"]["post"]["responses"]["409"]["content"]
    assert paths["/api/add_app"]["post"]["responses"]["503"]["content"]


def test_generation_is_deterministic() -> None:
    """``generate_examples`` produces random values, which would make the
    committed document differ on every run and break the drift guard."""
    assert render_openapi_yaml() == render_openapi_yaml()
