import json
import sqlite3

import pytest
import yaml
from jsonschema import Draft4Validator
from jsonschema import validators

from compute_space import OPENHOST_PROJECT_DIR
from compute_space.core.app_definitions import ExportMode
from compute_space.core.app_definitions import export_app_definitions
from compute_space.tests.test_app_definitions import APPS_DIR
from compute_space.tests.test_app_definitions import seed_api_token
from compute_space.tests.test_app_definitions import seed_app


@pytest.fixture(scope="module")
def export_validator() -> Draft4Validator:
    specification = yaml.safe_load((OPENHOST_PROJECT_DIR / "services/app-definitions/openapi.yaml").read_text())
    schema = specification["components"]["schemas"]["Export"]

    # OpenAPI 3.0 uses Draft 4's type vocabulary with an additional nullable flag.
    def nullable_type(validator, types, instance, schema):
        if instance is None and schema.get("nullable"):
            return
        yield from Draft4Validator.VALIDATORS["type"](validator, types, instance, schema)

    validator = validators.extend(Draft4Validator, {"type": nullable_type})
    validator.check_schema(schema)
    return validator(schema)


TOKEN = {"name": "duplicate", "token_hash": "a" * 64, "expires_at": None}


@pytest.mark.parametrize(
    ("fields", "valid"),
    [
        ({"mode": "sharing"}, True),
        ({"mode": "private", "platform_api_tokens": []}, True),
        ({"mode": "private", "platform_api_tokens": [TOKEN]}, True),
        ({"mode": "private", "platform_api_tokens": [TOKEN, {**TOKEN, "token_hash": "b" * 64}]}, True),
        ({"mode": "private", "platform_api_tokens": [{**TOKEN, "expires_at": "2000-01-01T00:00:00+00:00"}]}, True),
        ({"mode": "sharing", "platform_api_tokens": []}, False),
        ({"mode": "sharing", "platform_api_tokens": [TOKEN]}, False),
        ({"mode": "private"}, False),
        ({"mode": "private", "platform_api_tokens": None}, False),
        ({"mode": "private", "platform_api_tokens": {}}, False),
        ({"mode": "private", "platform_api_tokens": [None]}, False),
        ({"mode": "private", "platform_api_tokens": [{**TOKEN, "token_hash": "raw-api-key"}]}, False),
        ({"mode": "private", "platform_api_tokens": [{**TOKEN, "token_hash": "A" * 64}]}, False),
        ({"mode": "private", "platform_api_tokens": [{**TOKEN, "name": None}]}, False),
        ({"mode": "private", "platform_api_tokens": [{**TOKEN, "expires_at": 123}]}, False),
        ({"mode": "private", "platform_api_tokens": [{**TOKEN, "raw_key": "not-allowed"}]}, False),
        ({"mode": "private", "platform_api_tokens": [{"name": "test", "token_hash": "a" * 64}]}, False),
        ({"mode": "sharing", "schema_version": 1}, False),
        ({"mode": "private", "schema_version": 1, "platform_api_tokens": []}, False),
    ],
)
def test_export_schema_mode_constraints(
    export_validator: Draft4Validator, fields: dict[str, object], valid: bool
) -> None:
    document = {"schema_version": 2, "apps": [], **fields}
    errors = list(export_validator.iter_errors(document))
    assert (not errors) is valid, [error.message for error in errors]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sharing", "private"])
async def test_actual_export_matches_schema(
    db: sqlite3.Connection, export_validator: Draft4Validator, mode: ExportMode
) -> None:
    for name, url in [
        ("remote", "https://example.com/app"),
        ("builtin", f"file://{APPS_DIR}/secrets"),
        ("local", "/tmp/app"),
        ("unknown", None),
    ]:
        seed_app(db, name, repo_url=url)
    db.execute(
        "INSERT INTO app_port_mappings (app_id, label, container_port, host_port) VALUES ('remote', 'web', 80, 8080)"
    )
    seed_api_token(db, "no expiry", "synthetic-a")
    seed_api_token(db, "expired", "synthetic-b", "2000-01-01T00:00:00+00:00")
    document = json.loads(await export_app_definitions(db, APPS_DIR, mode))
    export_validator.validate(document)
    document["apps"][0]["unexpected"] = "not an app definition field"
    assert not export_validator.is_valid(document)
