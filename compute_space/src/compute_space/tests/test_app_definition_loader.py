import asyncio
import json
from contextlib import closing
from pathlib import Path

import attr
import httpx
import pytest
import yaml

from compute_space.core import app_definition_secrets
from compute_space.core.app_definition_loader import MAX_DEFINITION_BYTES
from compute_space.core.app_definition_loader import DefinitionError
from compute_space.core.app_definition_loader import definition_plan
from compute_space.core.app_definition_loader import parse_definition
from compute_space.core.app_definition_secrets import SECRETS_SERVICE_URL
from compute_space.core.app_definitions import PrivateDefinitionExport
from compute_space.core.app_definitions import export_app_definitions
from compute_space.core.apps import _plain_dir_to_copy
from compute_space.core.git_ops import parse_repo_url
from compute_space.db import get_db
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.test_app_definitions import SENTINEL
from compute_space.tests.test_app_definitions import fake_secrets
from compute_space.tests.test_app_definitions import seed_app
from compute_space.tests.test_app_definitions import seed_grant
from compute_space.tests.test_app_definitions import seed_provider
from compute_space.tests.test_app_definitions_yaml import STRINGS
from compute_space.web.helpers.app_definition_export import dump_export_yaml


def app_document(source: object = None) -> dict[str, object]:
    return {
        "name": "demo",
        "source": source if source is not None else {"kind": "remote", "repo_url": "https://example.com/demo"},
        "port_mappings": [{"label": "web", "container_port": 8080, "host_port": 9876}],
        "secret_keys": ["TOKEN", "*"],
    }


def document(*, private: bool = False, apps: list[object] | None = None) -> dict[str, object]:
    body: dict[str, object] = {
        "schema_version": 1,
        "mode": "private" if private else "sharing",
        "apps": [app_document()] if apps is None else apps,
    }
    if private:
        body.update(secret_values={"TOKEN": SENTINEL}, missing_secret_keys=["MISSING"])
    return body


def test_actual_parent_exports_roundtrip_with_safe_plans(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_test_config(tmp_path)
    values = {f"KEY{i}": value for i, value in enumerate(STRINGS)} | {"<<": "literal merge key"}
    with closing(get_db()) as db:
        seed_app(db, "demo", repo_url="https://example.com/demo@feature/export")
        seed_provider(db)
        for key in (*values, "MISSING"):
            seed_grant(db, "demo", {"key": key})
        db.execute(
            "INSERT INTO app_port_mappings (app_id, label, container_port, host_port) VALUES ('demo', 'web', 8080, 9876)"
        )
        db.commit()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"secrets": values, "missing": ["MISSING"]})

        fake_secrets(monkeypatch, handler)
        for mode in ("sharing", "private"):
            exported = json.loads(asyncio.run(export_app_definitions(db, str(tmp_path), mode)))
            parsed = parse_definition(dump_export_yaml(exported))
            assert json.loads(json.dumps(attr.asdict(parsed))) == exported
            if mode == "private":
                assert isinstance(parsed, PrivateDefinitionExport)
                assert parsed.secret_values == values
            before = list(db.iterdump())
            monkeypatch.setattr(
                app_definition_secrets, "client_for", lambda *a, **kw: pytest.fail("parse provider IO")
            )
            plan = definition_plan(parsed, db, str(tmp_path))
            assert list(db.iterdump()) == before
            assert all(app.status == "existing" and app.install is None for app in plan.apps)
            assert SENTINEL not in json.dumps(attr.asdict(plan))
            fake_secrets(monkeypatch, handler)


def test_install_plan_only_explicit_secrets_grants_and_published_host_ports(tmp_path: Path) -> None:
    _make_test_config(tmp_path)
    parsed = parse_definition(dump_export_yaml(document(private=True)))
    with closing(get_db()) as db:
        plan = definition_plan(parsed, db, str(tmp_path))
    assert plan.secret_keys == ("TOKEN",)
    assert plan.missing_secret_keys == ("MISSING",)
    app = plan.apps[0]
    assert app.status == "ready"
    assert app.secret_keys == ("TOKEN", "*")
    assert app.install is not None
    payload = json.loads(json.dumps(attr.asdict(app.install)))
    assert payload == {
        "app_name": "demo",
        "repo_url": "https://example.com/demo",
        "port_overrides": {"web": 9876},
        "permissions_v2_grants": [
            {"service_url": SECRETS_SERVICE_URL, "grant": {"key": "TOKEN"}},
            {"service_url": SECRETS_SERVICE_URL, "grant": {"key": "*"}},
        ],
    }


@pytest.mark.parametrize("kind", ["local", "unknown"])
def test_unavailable_and_existing_skip(tmp_path: Path, kind: str) -> None:
    _make_test_config(tmp_path)
    parsed = parse_definition(dump_export_yaml(document(apps=[app_document({"kind": kind})])))
    with closing(get_db()) as db:
        missing = definition_plan(parsed, db, str(tmp_path)).apps[0]
        assert missing.status == "unavailable" and missing.install is None
        seed_app(db, "demo", repo_url="https://example.com/existing@keep")
        before = list(db.iterdump())
        existing = definition_plan(parsed, db, str(tmp_path)).apps[0]
        assert existing.status == "existing" and existing.app_id == "demo" and existing.install is None
        assert list(db.iterdump()) == before


def test_builtin_availability_and_containment(tmp_path: Path) -> None:
    _make_test_config(tmp_path)
    apps_dir = tmp_path / "bundled café apps"
    apps_dir.mkdir()
    bundled = apps_dir / "file_browser"
    bundled.mkdir()
    parsed = parse_definition(
        dump_export_yaml(document(apps=[app_document({"kind": "builtin", "identifier": "file_browser"})]))
    )
    with closing(get_db()) as db:
        assert definition_plan(parsed, db, str(apps_dir)).apps[0].status == "unavailable"
        # Only manifest presence is checked, not its contents or a clone/network probe.
        (bundled / "cloudinabottle.toml").write_text("not a valid manifest")
        planned = definition_plan(parsed, db, str(apps_dir)).apps[0]
        assert planned.status == "ready" and planned.install.repo_url == f"file://{bundled}"
        assert _plain_dir_to_copy(planned.install.repo_url) == str(bundled)
        parsed = parse_definition(
            dump_export_yaml(document(apps=[app_document({"kind": "builtin", "identifier": "escape"})]))
        )
        (apps_dir / "escape").symlink_to(tmp_path, target_is_directory=True)
        with pytest.raises(DefinitionError, match="inside"):
            definition_plan(parsed, db, str(apps_dir))


@pytest.mark.parametrize(
    "url",
    ["https://example.com/demo.git", "http://example.com:1234/demo", "git://example.com/demo", "https://[::1]/demo"],
)
@pytest.mark.parametrize("ref", [None, "main", "feature/foo", "refs/pull/390/head", "a" * 40, "référence"])
def test_remote_refs_roundtrip_exactly(tmp_path: Path, url: str, ref: str | None) -> None:
    _make_test_config(tmp_path)
    source = {"kind": "remote", "repo_url": url, "ref": ref}
    parsed = parse_definition(dump_export_yaml(document(apps=[app_document(source)])))
    with closing(get_db()) as db:
        install = definition_plan(parsed, db, str(tmp_path)).apps[0].install
    assert install is not None
    assert parse_repo_url(install.repo_url) == (url, ref)


@pytest.mark.parametrize(
    "url",
    [
        "github.com/acme/demo",
        "ssh://git@example.com/demo",
        "git@example.com:demo",
        "file:///demo",
        "/demo",
        "https://user:private@example.com/demo",
        "https://example.com/demo?token=private",
        "https://example.com/demo#private",
        "https://example.com/demo;private",
        "https://example.com/demo@main",
        "https://example.com/../private",
        "https://example.com/%2e%2e/private",
        "https://example.com/demo%3fprivate",
        "https://example.com/demo%253fprivate",
        "https://example.com/demo%40private",
        "https://example.com/demo%2fprivate",
        "https://example.com/\\private",
        "https://example.com/demo%0aprivate",
        "https://example.com/demo\n",
        "https://example.com:invalid/demo",
        "https://example.com",
        "https://example.com//demo",
    ],
)
def test_remote_url_smuggling_rejected(url: str) -> None:
    with pytest.raises(DefinitionError):
        parse_definition(dump_export_yaml(document(apps=[app_document({"kind": "remote", "repo_url": url})])))


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "main@other",
        "main?secret",
        "main#secret",
        "main;secret",
        "main%3fsecret",
        "--detach",
        "main\n",
        "a b",
        "/main",
        "../main",
    ],
)
def test_unsafe_ref_rejected(ref: str) -> None:
    with pytest.raises(DefinitionError):
        parse_definition(
            dump_export_yaml(
                document(apps=[app_document({"kind": "remote", "repo_url": "https://example.com/demo", "ref": ref})])
            )
        )


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        "not an object",
        {},
        {"schema_version": True, "mode": "sharing", "apps": []},
        {"schema_version": "1", "mode": "sharing", "apps": []},
        {"schema_version": 2, "mode": "sharing", "apps": []},
        {"schema_version": 1, "mode": None, "apps": []},
        {"schema_version": 1, "mode": "private", "apps": []},
        document() | {"typo": SENTINEL},
        document() | {"secret_values": {}},
        document() | {"missing_secret_keys": []},
        document() | {"apps": {}},
        document(private=True) | {"secret_values": {"KEY": None}},
        document(private=True) | {"secret_values": {"KEY": 123}},
        document(private=True) | {"secret_values": {"": ""}},
        document(private=True) | {"missing_secret_keys": ["TOKEN"]},
        document(private=True) | {"missing_secret_keys": ["* ", "* "]},
        document(apps=[app_document(), app_document()]),
    ],
)
def test_invalid_envelopes(bad: object) -> None:
    with pytest.raises(DefinitionError) as error:
        parse_definition(yaml.safe_dump(bad))
    assert SENTINEL not in str(error.value)


@pytest.mark.parametrize(
    "bad_app",
    [
        app_document() | {"name": "api"},
        app_document() | {"name": "bad_name"},
        app_document() | {"name": "demo\n"},
        app_document() | {"name": True},
        app_document() | {"secret_keys": [None]},
        app_document() | {"secret_keys": ["A", "A"]},
        app_document() | {"typo": SENTINEL},
        app_document({"kind": "local", "path": SENTINEL}),
        app_document({"kind": "remote", "repo_url": "https://example.com/demo", "ref": 123}),
        app_document({"kind": "other"}),
        app_document({"kind": "builtin", "identifier": "../escape"}),
        app_document({"kind": "builtin", "identifier": "/escape"}),
        app_document({"kind": "builtin", "identifier": "a/b"}),
        app_document()
        | {"port_mappings": [{"label": "web", "container_port": port, "host_port": 9800 + port} for port in (80, 81)]},
        app_document()
        | {"port_mappings": [{"label": "web", "container_port": 80, "host_port": 9876, "typo": SENTINEL}]},
    ],
)
def test_invalid_nested_app(bad_app: object) -> None:
    with pytest.raises(DefinitionError):
        parse_definition(dump_export_yaml(document(apps=[bad_app])))


@pytest.mark.parametrize(
    ("field", "port"),
    [
        ("container_port", 0),
        ("container_port", -1),
        ("container_port", 65536),
        ("container_port", True),
        ("host_port", -1),
        ("host_port", 24),
        ("host_port", 65536),
        ("host_port", False),
        ("host_port", "9876"),
    ],
)
def test_invalid_port_types_and_ranges(field: str, port: object) -> None:
    mapping = {"label": "web", "container_port": 8080, "host_port": 9876} | {field: port}
    with pytest.raises(DefinitionError):
        parse_definition(dump_export_yaml(document(apps=[app_document() | {"port_mappings": [mapping]}])))


@pytest.mark.parametrize("port", [0, 25, 65535])
def test_host_port_policy(port: int) -> None:
    mapping = {"label": "web", "container_port": 65535, "host_port": port}
    parsed = parse_definition(dump_export_yaml(document(apps=[app_document() | {"port_mappings": [mapping]}])))
    assert parsed.apps[0].port_mappings[0].host_port == port


@pytest.mark.parametrize(
    "content",
    [
        "schema_version: 1\nschema_version: 1\nmode: sharing\napps: []",
        "schema_version: 1\nmode: sharing\napps: &apps [*apps]",
        "schema_version: 1\nmode: sharing\napps: *missing",
        "schema_version: 1\nmode: sharing\napps: []\n<<: {mode: sharing}",
        f"!!python/object/apply:os.system ['{SENTINEL}']",
        f"!unknown {SENTINEL}",
        f"!!bool {SENTINEL}",
        f"!!str [{SENTINEL}]",
        f"!!timestamp {SENTINEL}",
        f"schema_version: 1\nmode: private\napps: []\nsecret_values: {{KEY: !!bool {SENTINEL}}}\nmissing_secret_keys: []",
        f"schema_version: 1\nmode: private\napps: []\nsecret_values: {{KEY: {SENTINEL}, KEY: x}}\nmissing_secret_keys: []",
        f"schema_version: 1\nmode: private\napps: []\nsecret_values: {{<<: {SENTINEL}}}\nmissing_secret_keys: []",
        f"schema_version: 1\nmode: private\napps: []\nsecret_values: {{[key]: {SENTINEL}}}\nmissing_secret_keys: []",
        f"schema_version: 1\nmode: sharing\napps: [{SENTINEL}",
        "schema_version: 1\nmode: sharing\napps: []\n---\n{}",
        "[" * 1000 + "]" * 1000,
        "[" + "{}," * 20001 + "]",
        "#" + "é" * (MAX_DEFINITION_BYTES // 2),
        "\ud800",
        'schema_version: 1\nmode: private\napps: []\nsecret_values: {KEY: "\\uD800"}\nmissing_secret_keys: []',
        'schema_version: 1\nmode: private\napps: []\nsecret_values: {"\\uDFFF": value}\nmissing_secret_keys: []',
    ],
    ids=lambda _: "invalid-yaml",
)
def test_untrusted_yaml_limits_tags_aliases_and_errors(content: str) -> None:
    with pytest.raises(DefinitionError) as error:
        parse_definition(content)
    assert SENTINEL not in str(error.value)
    assert len(str(error.value)) < 150
