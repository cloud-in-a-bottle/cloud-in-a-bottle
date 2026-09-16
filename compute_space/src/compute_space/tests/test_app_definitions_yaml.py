from collections.abc import Callable

import pytest
import yaml
from ruamel.yaml import YAML

from compute_space.web.helpers.app_definition_export import dump_export_yaml

STRINGS = [
    "",
    "plain text",
    "ключ: café 日本語 🔑",
    "true",
    "FALSE",
    "yes",
    "NO",
    "on",
    "Off",
    "null",
    "Null",
    "~",
    "2026-09-16",
    "2001-12-15T02:59:43.1Z",
    "0",
    "01",
    "-123",
    "+42",
    "1.25",
    ".5",
    "-.5",
    "._5",
    ".__5",
    "._5e2",
    "+._5",
    "-._5",
    "1e3",
    "1E+3",
    "-2e-3",
    "0o12",
    "0x12",
    "0b10",
    "1_000",
    ".inf",
    "-.Inf",
    ".NaN",
    "1:20",
    "'quoted'",
    '"double quoted"',
    "back\\slash",
    "a: b # comment",
    "# comment",
    "[one, two]",
    "{one: two}",
    "!tag value",
    "!!python/object:example {}",
    "&anchor value",
    "*alias",
    "<<",
    "---",
    "...",
    "|",
    ">",
    " leading and trailing ",
    "\tindent\tinside\t",
    "a\rb",
    "a\r\nb\r\n",
    "a\x85b\x85",
    "a\u2028b\u2028",
    "a\u2029b\u2029",
    "a\nb\x85c\u2028d\u2029e\r\nf\tg\n\n",
    "\ufeffvalue",
    "\ufffe\uffff",
    "__proto__",
    "constructor",
    *[f"left{chr(code)}right" for code in (*range(32), *range(127, 160))],
    *[
        text + "\n" * count
        for text in ("", "first\nsecond", "\n  indented\n\nlast", "line \nnext", "\ttab\nend")
        for count in range(5)
    ],
]


@pytest.fixture(scope="module", params=["pyyaml", "ruamel-yaml-1.2"])
def load_yaml(request: pytest.FixtureRequest) -> Callable[[str], object]:
    if request.param == "pyyaml":
        return yaml.safe_load
    reader = YAML(typ="safe", pure=True)
    reader.version = (1, 2)
    return reader.load


def test_scalar_values_and_mapping_keys_roundtrip_exactly(load_yaml: Callable[[str], object]) -> None:
    document = {
        "schema_version": 1,
        "mode": "private",
        "apps": [{"name": "true", "source": {"ref": None}, "port_mappings": [{"container_port": 8080}]}],
        "secret_values": {text: text for text in STRINGS},
        "missing_secret_keys": STRINGS,
    }
    rendered = dump_export_yaml(document)
    assert load_yaml(rendered) == document
    assert rendered == dump_export_yaml(document)
    assert "ключ: café 日本語 🔑" in rendered


@pytest.mark.parametrize("trailing_newlines", range(5))
@pytest.mark.parametrize(
    "text", ["-----BEGIN TEST KEY-----\n  café\n\n  abcdef\n-----END TEST KEY-----", "1234\n5678"]
)
def test_readable_literal_multiline_secrets_preserve_chomping(
    load_yaml: Callable[[str], object], trailing_newlines: int, text: str
) -> None:
    value = text + "\n" * trailing_newlines
    document = {"apps": [], "mode": "private", "schema_version": 1, "secret_values": {"KEY": value}}
    rendered = dump_export_yaml(document)
    assert "KEY: |" in rendered
    assert load_yaml(rendered) == document
    assert list(yaml.safe_load(rendered))[:2] == ["schema_version", "mode"]


def test_codec_leaves_global_yaml_configuration_unchanged() -> None:
    dumpers = (yaml.Dumper, yaml.SafeDumper)
    # Also catch registration during import, before this test captured the global state.
    for dumper in dumpers:
        assert dumper.yaml_representers[str] is yaml.representer.SafeRepresenter.represent_str
    before = [(d.yaml_representers.copy(), d.yaml_implicit_resolvers.copy()) for d in dumpers]
    sample = {"a": "1e3", "b": "first\nsecond\n", "c": "a\x85b"}
    default_output = yaml.safe_dump(sample)
    dump_export_yaml({"schema_version": 1, "mode": "sharing", "apps": [], "sample": sample})
    assert before == [(d.yaml_representers, d.yaml_implicit_resolvers) for d in dumpers]
    assert default_output == yaml.safe_dump(sample)
