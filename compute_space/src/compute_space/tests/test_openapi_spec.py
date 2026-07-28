"""Guards that the committed ``openapi.yaml`` stays in sync with the schema
generated from the API route handlers. Regenerate with
``pixi run -e dev generate-openapi``."""

from __future__ import annotations

from compute_space.web.dump_openapi import _DEFAULT_OUTPUT
from compute_space.web.dump_openapi import render_openapi_yaml
from compute_space.web.openapi import build_openapi_schema


def test_committed_openapi_yaml_is_up_to_date() -> None:
    committed = _DEFAULT_OUTPUT.read_text(encoding="utf-8")
    assert committed == render_openapi_yaml(), "openapi.yaml is stale; run `pixi run -e dev generate-openapi`"


def test_schema_covers_api_only() -> None:
    schema = build_openapi_schema()
    paths = schema["paths"]
    assert "/api/apps" in paths
    # HTML/page routes are excluded via include_in_schema=False.
    assert "/dashboard" not in paths
    assert "/docs" not in paths
    # Injected dependencies must not surface as query parameters.
    params = [p["name"] for op in paths.values() for h in op.values() for p in h.get("parameters", [])]
    assert "db" not in params
    assert "config" not in params


def test_bearer_security_is_declared() -> None:
    schema = build_openapi_schema()
    assert schema["components"]["securitySchemes"]["BearerToken"]["scheme"] == "bearer"
    assert {"BearerToken": []} in schema["security"]
