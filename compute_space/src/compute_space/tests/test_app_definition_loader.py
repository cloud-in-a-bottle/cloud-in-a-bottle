import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import attr
import httpx
import pytest
import yaml

from compute_space.core.app_definition_loader import MAX_DEFINITION_BYTES
from compute_space.core.app_definition_loader import DefinitionError
from compute_space.core.app_definition_loader import _DefinitionLoader
from compute_space.core.app_definition_loader import definition_plan
from compute_space.core.app_definition_loader import import_platform_api_tokens
from compute_space.core.app_definition_loader import parse_definition
from compute_space.core.app_definitions import PlatformApiToken
from compute_space.core.app_definitions import PrivateDefinitionExport
from compute_space.core.app_definitions import export_app_definitions
from compute_space.core.apps import _plain_dir_to_copy
from compute_space.core.auth.auth import validate_api_token
from compute_space.core.git_ops import parse_repo_url
from compute_space.db import get_db
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.test_app_definitions import SENTINEL
from compute_space.tests.test_app_definitions import seed_api_token
from compute_space.tests.test_app_definitions import seed_app
from compute_space.tests.test_app_definitions_yaml import STRINGS
from compute_space.web.helpers.app_definition_export import dump_export_yaml


def app_document(source: object = None) -> dict[str, object]:
    return {
        "name": "demo",
        "source": source
        if source is not None
        else {"kind": "remote", "repo_url": "https://example.com/demo", "ref": None},
        "port_mappings": [{"label": "web", "container_port": 8080, "host_port": 9876}],
    }


def token_record(name: object = "owner key", raw: str = SENTINEL, expiry: object = None) -> dict[str, object]:
    return {"name": name, "token_hash": hashlib.sha256(raw.encode()).hexdigest(), "expires_at": expiry}


def document(*, private: bool = False, apps: list[object] | None = None) -> dict[str, object]:
    body: dict[str, object] = {
        "schema_version": 2,
        "mode": "private" if private else "sharing",
        "apps": [app_document()] if apps is None else apps,
    }
    if private:
        body["platform_api_tokens"] = [token_record()]
    return body


def test_actual_parent_exports_roundtrip_with_safe_plans(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_test_config(tmp_path)
    monkeypatch.setattr(httpx.AsyncClient, "request", lambda *a, **kw: pytest.fail("unexpected HTTP request"))
    monkeypatch.setattr(httpx.Client, "request", lambda *a, **kw: pytest.fail("unexpected HTTP request"))
    with closing(get_db()) as db:
        seed_app(db, "demo", repo_url="https://example.com/demo@feature/export")
        seed_app(db, "secrets", repo_url="https://example.com/secrets")
        for index, name in enumerate(STRINGS):
            seed_api_token(db, name, f"synthetic-{index}")
        db.execute(
            "INSERT INTO app_port_mappings (app_id, label, container_port, host_port) VALUES ('demo', 'web', 8080, 9876)"
        )
        db.commit()

        for mode in ("sharing", "private"):
            exported_json = asyncio.run(export_app_definitions(db, str(tmp_path), mode))
            exported = json.loads(exported_json)
            parsed = parse_definition(dump_export_yaml(exported))
            assert json.loads(json.dumps(attr.asdict(parsed))) == exported
            before = list(db.iterdump())
            plan = definition_plan(parsed, db, str(tmp_path))
            assert list(db.iterdump()) == before
            assert all(app.status == "existing" and app.install is None for app in plan.apps)
            assert SENTINEL not in json.dumps(attr.asdict(plan))
            if mode == "private":
                assert isinstance(parsed, PrivateDefinitionExport)
                assert sorted(plan.platform_api_token_names) == sorted(STRINGS)
                assert all(
                    token.token_hash not in json.dumps(attr.asdict(plan)) for token in parsed.platform_api_tokens
                )


def test_install_plan_only_source_name_and_published_host_ports(tmp_path: Path) -> None:
    _make_test_config(tmp_path)
    parsed = parse_definition(dump_export_yaml(document(private=True)))
    with closing(get_db()) as db:
        plan = definition_plan(parsed, db, str(tmp_path))
    assert plan.platform_api_token_names == ("owner key",)
    app = plan.apps[0]
    assert app.status == "ready"
    assert app.install is not None
    payload = json.loads(json.dumps(attr.asdict(app.install)))
    assert payload == {
        "app_name": "demo",
        "repo_url": "https://example.com/demo",
        "port_overrides": {"web": 9876},
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
    [
        "https://example.com/demo.git",
        "http://example.com:1234/demo",
        "git://example.com/demo",
        "https://[::1]/demo",
        "https://dev.azure.com/example/Project%20Name/_git/demo",
    ],
)
@pytest.mark.parametrize(
    "ref",
    [None, "main", "feature/foo", "refs/pull/390/head", "a" * 40, "référence", "release%candidate", "main%3fsecret"],
)
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
        "https://example.com/demo%09private",
        "https://example.com/demo%00private",
        "https://example.com/demo%7fprivate",
        "https://example.com/demo%C2%85private",
        "https://example.com/demo%E2%80%A8private",
        "https://example.com/demo%E2%80%A9private",
        "https://example.com/demo private",
        "https://example.com/demo\n",
        "https://example.com:invalid/demo",
        "https://example.com",
        "https://example.com//demo",
    ],
)
def test_remote_url_smuggling_rejected(url: str) -> None:
    with pytest.raises(DefinitionError):
        parse_definition(
            dump_export_yaml(document(apps=[app_document({"kind": "remote", "repo_url": url, "ref": None})]))
        )


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "main@other",
        "main?secret",
        "main#secret",
        "main;secret",
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
        {"schema_version": "2", "mode": "sharing", "apps": []},
        {"schema_version": 1, "mode": "sharing", "apps": []},
        {"schema_version": 3, "mode": "sharing", "apps": []},
        {"schema_version": 2, "mode": None, "apps": []},
        {"schema_version": 2, "mode": "private", "apps": []},
        document() | {"typo": SENTINEL},
        document() | {"secret_values": {}},
        document() | {"missing_secret_keys": []},
        document() | {"apps": {}},
        document() | {"platform_api_tokens": []},
        document(private=True) | {"platform_api_tokens": {}},
        document(private=True) | {"platform_api_tokens": None},
        document(private=True) | {"app_tokens": []},
        document(private=True) | {"sessions": []},
        document(private=True) | {"passwords": []},
        document(private=True) | {"secret_values": {}},
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
        app_document() | {"secret_keys": []},
        app_document() | {"permissions_v2_grants": []},
        app_document() | {"grant_all": True},
        app_document() | {"typo": SENTINEL},
        app_document({"kind": "local", "path": SENTINEL}),
        app_document({"kind": "remote", "repo_url": "https://example.com/demo", "ref": 123}),
        app_document({"kind": "remote", "repo_url": "https://example.com/demo"}),
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
        "schema_version: 2\nschema_version: 2\nmode: sharing\napps: []",
        "schema_version: 2\nmode: sharing\napps: &apps [*apps]",
        "schema_version: 2\nmode: sharing\napps: *missing",
        "schema_version: 2\nmode: sharing\napps: []\n<<: {mode: sharing}",
        f"!!python/object/apply:os.system ['{SENTINEL}']",
        f"!unknown {SENTINEL}",
        f"!!bool {SENTINEL}",
        f"!!str [{SENTINEL}]",
        f"!!timestamp {SENTINEL}",
        f"schema_version: 2\nmode: private\napps: []\nplatform_api_tokens: [{{name: !!bool {SENTINEL}}}]",
        f"schema_version: 2\nmode: private\napps: []\nplatform_api_tokens: [{{name: {SENTINEL}, name: x}}]",
        f"schema_version: 2\nmode: private\napps: []\nplatform_api_tokens: [{{<<: {SENTINEL}}}]",
        f"schema_version: 2\nmode: private\napps: []\nplatform_api_tokens: [{{[key]: {SENTINEL}}}]",
        f"schema_version: 2\nmode: sharing\napps: [{SENTINEL}",
        "schema_version: 2\nmode: sharing\napps: []\n---\n{}",
        "[" * 1000 + "]" * 1000,
        "[" + "{}," * 20001 + "]",
        "#" + "é" * (MAX_DEFINITION_BYTES // 2),
        "\ud800",
        'schema_version: 2\nmode: private\napps: []\nplatform_api_tokens: [{name: "\\uD800"}]',
        'schema_version: 2\nmode: private\napps: []\nplatform_api_tokens: [{"\\uDFFF": value}]',
    ],
    ids=lambda _: "invalid-yaml",
)
def test_untrusted_yaml_limits_tags_aliases_and_errors(content: str) -> None:
    with pytest.raises(DefinitionError) as error:
        parse_definition(content)
    assert SENTINEL not in str(error.value)
    assert len(str(error.value)) < 150


@pytest.mark.parametrize(
    "scalar",
    ["1:" * 64 + "1", "0b" + "1" * 65, "0x" + "f" * 65, "2" + "_" * 64, "7" * 65],
    ids=["sexagesimal", "binary", "hex", "underscores", "decimal"],
)
@pytest.mark.parametrize("explicit_tag", [False, True])
def test_oversized_integer_rejected_before_construction(
    monkeypatch: pytest.MonkeyPatch, scalar: str, explicit_tag: bool
) -> None:
    def forbidden_constructor(*args):
        pytest.fail("Oversized integer reached the YAML integer constructor")

    monkeypatch.setattr(
        _DefinitionLoader,
        "yaml_constructors",
        _DefinitionLoader.yaml_constructors | {"tag:yaml.org,2002:int": forbidden_constructor},
    )
    value = f"!!int '{scalar}'" if explicit_tag else scalar
    with pytest.raises(DefinitionError) as error:
        parse_definition(f"schema_version: {value}\nmode: sharing\napps: []\n")
    assert str(error.value) == "YAML integer scalars must be at most 64 characters."


@pytest.mark.parametrize(
    ("version", "port", "expected"),
    [
        ("2", "25", 25),
        ("0x2", "0x19", 25),
        ("0b10", "0b11001", 25),
        ("02", "031", 25),
        ("+2", "+25", 25),
        ("2" + "_" * 63, "2_5", 25),
        ("2", "1:00", 60),
        ("2", "0x" + "0" * 58 + "ffff", 65535),
        ("2", "0x10000", None),
        ("2", "0b11000", None),
        ("2", "18:12:16", None),
    ],
)
def test_bounded_integer_forms_keep_schema_and_port_validation(version: str, port: str, expected: int | None) -> None:
    content = (
        f"schema_version: {version}\nmode: sharing\napps:\n"
        "- name: demo\n  source: {kind: local}\n  port_mappings:\n"
        f"  - {{label: web, container_port: {port}, host_port: {port}}}\n"
    )
    if expected is None:
        with pytest.raises(DefinitionError, match="ports must be"):
            parse_definition(content)
    else:
        parsed = parse_definition(content)
        assert parsed.schema_version == 2
        assert parsed.apps[0].port_mappings[0].container_port == expected
        assert parsed.apps[0].port_mappings[0].host_port == expected


def test_integer_bound_does_not_restrict_string_token_names() -> None:
    name = "1:" * 5000 + "1"
    body = document(private=True) | {"platform_api_tokens": [token_record(name=name)]}
    parsed = parse_definition(dump_export_yaml(body))
    assert isinstance(parsed, PrivateDefinitionExport)
    assert parsed.platform_api_tokens[0].name == name
    assert parsed.platform_api_tokens[0].token_hash == token_record()["token_hash"]


def test_old_files_require_reexport() -> None:
    with pytest.raises(DefinitionError, match="Re-export"):
        parse_definition(dump_export_yaml(document() | {"schema_version": 1}))


@pytest.mark.parametrize("field", ["expires_at", "ref"])
@pytest.mark.parametrize("scalar", ["2000-01-01T00:00:00Z", "false", "42", f'"{SENTINEL}"', '" null "', "nUlL"])
def test_invalid_tagged_null_cannot_discard_expiry_or_ref(field: str, scalar: str) -> None:
    content = dump_export_yaml(document(private=True)).replace(f"{field}: null", f"{field}: !!null {scalar}")
    with pytest.raises(DefinitionError, match="Invalid YAML null value") as error:
        parse_definition(content)
    assert SENTINEL not in str(error.value)


@pytest.mark.parametrize("field", ["expires_at", "ref"])
@pytest.mark.parametrize("scalar", ["", "''", "~", "null", "Null", "NULL"])
def test_valid_tagged_null_representations_remain_supported(field: str, scalar: str) -> None:
    content = dump_export_yaml(document(private=True)).replace(f"{field}: null", f"{field}: !!null {scalar}")
    parsed = parse_definition(content)
    assert isinstance(parsed, PrivateDefinitionExport)
    assert attr.asdict(parsed.apps[0].source)["ref"] is None
    assert parsed.platform_api_tokens[0].expires_at is None


INVALID_TOKENS = [
    None,
    {},
    [],
    *[
        token_record() | {"token_hash": value}
        for value in [None, 123, True, "", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 64 + "\n", SENTINEL]
    ],
    *[token_record(name=value) for value in [None, 123, True, [], {}, "\ud800"]],
    *[
        token_record(expiry=value)
        for value in [
            "",
            True,
            123,
            [],
            {},
            "tomorrow",
            SENTINEL,
            "2026-01-01",
            "2026-01-01T00:00:00",
            "2026-01-01T00:00:00+24:00",
            "2026-02-30T00:00:00Z",
        ]
    ],
    *[token_record() | {field: SENTINEL} for field in ["token", "raw_key", "id", "created_at"]],
    {"name": "key", "token_hash": "a" * 64},
]


@pytest.mark.parametrize("bad", INVALID_TOKENS)
def test_invalid_platform_api_tokens(bad: object) -> None:
    body = document(private=True) | {"platform_api_tokens": [token_record(raw="valid-first"), bad]}
    with pytest.raises(DefinitionError) as error:
        parse_definition(dump_export_yaml(body))
    assert SENTINEL not in str(error.value)


def test_duplicate_hashes_rejected_but_names_preserved() -> None:
    records = [token_record(name="duplicate"), token_record(name="duplicate", raw="another-key")]
    body = document(private=True) | {"platform_api_tokens": records}
    parsed = parse_definition(dump_export_yaml(body))
    assert isinstance(parsed, PrivateDefinitionExport)
    assert [token.name for token in parsed.platform_api_tokens] == ["duplicate", "duplicate"]
    records.append(token_record(name="different name"))
    with pytest.raises(DefinitionError, match="Duplicate API token hashes"):
        parse_definition(dump_export_yaml(body))


@pytest.mark.parametrize(
    ("expiry", "valid"),
    [
        (None, True),
        ("2999-01-01T12:34:56.123456+05:30", True),
        ("2999-01-01T00:00:00Z", True),
        ("2999-01-01T00:00:00-07:00", True),
        ("2000-01-01T12:34:56.123456+05:30", False),
        ("2000-01-01T00:00:00Z", False),
    ],
)
def test_real_export_migration_and_authentication(tmp_path: Path, expiry: str | None, valid: bool) -> None:
    (tmp_path / "source").mkdir()
    (tmp_path / "destination").mkdir()
    _make_test_config(tmp_path / "source")
    raw_key = "synthetic-source-original-key"
    with closing(get_db()) as source:
        record = seed_api_token(source, "same name", raw_key, expiry or "")
        assert (validate_api_token(raw_key, source) is not None) == valid
        content = asyncio.run(export_app_definitions(source, str(tmp_path), "private"))
    parsed = parse_definition(dump_export_yaml(json.loads(content)))
    assert isinstance(parsed, PrivateDefinitionExport)

    _make_test_config(tmp_path / "destination")
    with closing(get_db()) as destination:
        seed_api_token(destination, "same name", "synthetic-destination-key")
        original = tuple(destination.execute("SELECT * FROM api_tokens").fetchone())
        assert import_platform_api_tokens(destination, parsed.platform_api_tokens) == 1
        imported = tuple(
            destination.execute("SELECT * FROM api_tokens WHERE token_hash=?", (record["token_hash"],)).fetchone()
        )
        assert imported[1:4] == ("same name", record["token_hash"], expiry or "")
        assert not destination.in_transaction
        assert (validate_api_token(raw_key, destination) is not None) == valid
        assert validate_api_token(record["token_hash"], destination) is None
        assert validate_api_token("synthetic-destination-key", destination) is not None
        assert tuple(destination.execute("SELECT * FROM api_tokens WHERE id=?", (original[0],)).fetchone()) == original
        for tokens in (
            parsed.platform_api_tokens,
            (PlatformApiToken("renamed", record["token_hash"], "9999-12-31T23:59:59Z"),),
        ):
            assert import_platform_api_tokens(destination, tokens) == 0
            assert (
                tuple(
                    destination.execute(
                        "SELECT * FROM api_tokens WHERE token_hash=?", (record["token_hash"],)
                    ).fetchone()
                )
                == imported
            )
            assert (validate_api_token(raw_key, destination) is not None) == valid
        assert destination.execute("SELECT count(*) FROM api_tokens WHERE name='same name'").fetchone()[0] == 2


@pytest.mark.parametrize("nested", [False, True])
def test_atomic_batch_rollback_preserves_enclosing_transaction(db: sqlite3.Connection, nested: bool) -> None:
    seed_api_token(db, "existing", "synthetic-existing")
    db.execute(
        "CREATE TRIGGER fail_second BEFORE INSERT ON api_tokens WHEN NEW.name='fail' BEGIN SELECT RAISE(ABORT, 'synthetic DB error'); END"
    )
    if nested:
        db.execute("INSERT INTO api_tokens (name, token_hash, expires_at) VALUES ('outer', ?, '')", ("c" * 64,))
    before = list(db.iterdump())
    with pytest.raises(sqlite3.IntegrityError):
        import_platform_api_tokens(
            db, (PlatformApiToken("first", "a" * 64, None), PlatformApiToken("fail", "b" * 64, None))
        )
    assert list(db.iterdump()) == before
    assert db.in_transaction == nested
    if nested:
        assert import_platform_api_tokens(db, (PlatformApiToken("inner", "d" * 64, None),)) == 1
        assert db.in_transaction
        db.rollback()
        assert db.execute("SELECT name FROM api_tokens").fetchall()[0][0] == "existing"
        assert db.execute("SELECT count(*) FROM api_tokens").fetchone()[0] == 1


@pytest.mark.parametrize("mode", ["sharing", "private"])
def test_no_token_entries_never_access_token_records(db: sqlite3.Connection, tmp_path: Path, mode: str) -> None:
    body = document(private=mode == "private")
    if mode == "private":
        body["platform_api_tokens"] = []
    parsed = parse_definition(dump_export_yaml(body))

    def authorize(action: int, table: str, *args: object) -> int:
        if table == "api_tokens" or action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    db.set_authorizer(authorize)
    try:
        assert definition_plan(parsed, db, str(tmp_path)).platform_api_token_names == ()
        assert import_platform_api_tokens(db, ()) == 0
        if mode == "sharing":
            assert "platform_api_tokens" not in asyncio.run(export_app_definitions(db, str(tmp_path), "sharing"))
    finally:
        db.set_authorizer(None)
