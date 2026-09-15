import pytest
import yaml
from jsonschema import Draft4Validator

from compute_space import OPENHOST_PROJECT_DIR


@pytest.fixture(scope="module")
def export_validator() -> Draft4Validator:
    specification = yaml.safe_load((OPENHOST_PROJECT_DIR / "services/app-definitions/openapi.yaml").read_text())
    schema = specification["components"]["schemas"]["Export"]
    # Draft 4 supports the OpenAPI 3.0 keywords used by the envelope, but ignores
    # OpenAPI's nullable extension. Empty apps keep these tests scoped to the envelope.
    Draft4Validator.check_schema(schema)
    return Draft4Validator(schema)


@pytest.mark.parametrize(
    ("fields", "valid"),
    [
        pytest.param({"mode": "sharing"}, True, id="sharing-empty"),
        pytest.param({"mode": "private", "secret_values": {}}, True, id="private-empty"),
        pytest.param({"mode": "private", "secret_values": {"KEY": "value"}}, True, id="private-value"),
        pytest.param({"mode": "private", "secret_values": {"KEY": ""}}, True, id="private-empty-string"),
        pytest.param({"mode": "sharing", "secret_values": {}}, False, id="sharing-empty-values"),
        pytest.param({"mode": "sharing", "secret_values": {"KEY": "value"}}, False, id="sharing-values"),
        pytest.param({"mode": "sharing", "secret_values": None}, False, id="sharing-null-values"),
        pytest.param({"mode": "private"}, False, id="private-missing-values"),
        pytest.param({"mode": "private", "secret_values": None}, False, id="private-null-values"),
        pytest.param({"mode": "private", "secret_values": []}, False, id="private-array-values"),
        pytest.param({"mode": "private", "secret_values": {"KEY": None}}, False, id="private-null-value"),
        pytest.param({"mode": "private", "secret_values": {"KEY": 123}}, False, id="private-numeric-value"),
    ],
)
def test_export_schema_mode_constraints(
    export_validator: Draft4Validator, fields: dict[str, object], valid: bool
) -> None:
    document = {"schema_version": 1, "apps": [], **fields}
    errors = list(export_validator.iter_errors(document))
    assert (not errors) is valid, [error.message for error in errors]
