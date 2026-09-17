from __future__ import annotations

import re
import sqlite3
from collections.abc import Hashable
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import unquote
from urllib.parse import urlparse

import attr
import yaml
from yaml.events import AliasEvent
from yaml.nodes import MappingNode
from yaml.nodes import Node
from yaml.nodes import ScalarNode

from compute_space.core.app_definitions import AppDefinition
from compute_space.core.app_definitions import BuiltinSource
from compute_space.core.app_definitions import DefinitionExport
from compute_space.core.app_definitions import ExportMode
from compute_space.core.app_definitions import PlatformApiToken
from compute_space.core.app_definitions import PortableSource
from compute_space.core.app_definitions import PrivateDefinitionExport
from compute_space.core.app_definitions import PublishedPort
from compute_space.core.app_definitions import RemoteSource
from compute_space.core.app_definitions import UnavailableSource
from compute_space.core.app_definitions import portable_source
from compute_space.core.app_id import is_valid_app_name
from compute_space.core.apps import RESERVED_PATHS
from compute_space.core.git_ops import parse_repo_url
from compute_space.core.manifest import MANIFEST_FILENAMES
from compute_space.core.manifest import UNPRIVILEGED_PORT_FLOOR
from compute_space.db.connection import make_atomic_with_savepoint

MAX_DEFINITION_BYTES = 1024 * 1024


class DefinitionError(ValueError):
    """Validation messages are fixed text, never uploaded keys, values or source lines."""


class _DefinitionLoader(yaml.SafeLoader):
    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._depth = 0
        self._nodes = 0

    def compose_node(self, parent: Node | None, index: int) -> Node:
        self._nodes += 1
        self._depth += 1
        if self.check_event(AliasEvent):
            raise DefinitionError("YAML aliases are not supported.")
        if self._depth > 32 or self._nodes > 20000:
            raise DefinitionError("YAML structure exceeds the depth or node limit.")
        try:
            node = super().compose_node(parent, index)
            if node is None:
                raise DefinitionError("Expected a YAML node.")
            if isinstance(node, ScalarNode):
                # Bound numeric conversion work, including YAML's sexagesimal integers.
                if node.tag == "tag:yaml.org,2002:int" and len(node.value) > 64:
                    raise DefinitionError("YAML integer scalars must be at most 64 characters.")
                # YAML escapes can introduce lone surrogates even in an otherwise UTF-8 file.
                # Reject these before a later database write can fail after earlier writes.
                node.value.encode("utf-8")
            if node.tag not in {
                "tag:yaml.org,2002:map",
                "tag:yaml.org,2002:seq",
                "tag:yaml.org,2002:str",
                "tag:yaml.org,2002:int",
                "tag:yaml.org,2002:bool",
                "tag:yaml.org,2002:null",
            }:
                raise DefinitionError("Unsupported YAML tag or value type.")
            return node
        finally:
            self._depth -= 1

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Hashable, object]:
        result: dict[Hashable, object] = {}
        for key_node, value_node in node.value:
            # A quoted '<<' is an ordinary string; an implicit merge key has a different tag.
            if not isinstance(key_node, ScalarNode) or key_node.tag != "tag:yaml.org,2002:str":
                raise DefinitionError("YAML mapping keys must be strings; merge keys are not supported.")
            key = key_node.value
            if key in result:
                raise DefinitionError("Duplicate YAML mapping keys are not supported.")
            result[key] = self.construct_object(value_node, deep=deep)
        return result

    def flatten_mapping(self, node: MappingNode) -> None:
        # SafeConstructor normally expands merges before construct_mapping can reject them.
        pass


def _object(
    value: object, fields: set[str] | None = None, optional: set[str] | frozenset[str] = frozenset()
) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise DefinitionError("Expected an object with string keys.")
    if fields is not None and (not fields <= value.keys() or value.keys() - fields - optional):
        raise DefinitionError("Missing or unknown definition fields.")
    return value


def _string(value: object, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise DefinitionError("Expected a string value.")
    return value


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise DefinitionError("Expected a list.")
    return value


def _platform_api_token(value: object) -> PlatformApiToken:
    token = _object(value, {"name", "token_hash", "expires_at"})
    name = _string(token["name"], empty=True)
    token_hash = _string(token["token_hash"])
    if not re.fullmatch(r"[0-9a-f]{64}", token_hash):
        raise DefinitionError("API token hashes must be lowercase SHA256 verifiers (64 hex characters).")
    expiry = token["expires_at"]
    expires_at = None if expiry is None else _string(expiry)
    if expires_at is not None:
        try:
            if datetime.fromisoformat(expires_at).utcoffset() is None:
                raise ValueError
        except ValueError:
            raise DefinitionError("API token expiry must be null or a timezone-aware ISO timestamp.") from None
    return PlatformApiToken(name, token_hash, expires_at)


def _remote_url(source: RemoteSource) -> str:
    url, ref = source.repo_url, source.ref
    try:
        parsed = urlparse(url)
        path = unquote(parsed.path)
        if (
            parsed.scheme not in {"http", "https", "git"}
            or portable_source(url, "") != RemoteSource(url, None)
            or any(c in url for c in "\\?#;")
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url + path)
            or any(c in path for c in "@\\?#;%")
            or path.count("/") != parsed.path.count("/")
            or not path.strip("/")
            or "//" in path
            or any(segment in {".", ".."} for segment in path.split("/"))
        ):
            raise ValueError
        if ref is not None and (
            not ref
            or ref.startswith(("-", "/"))
            or ref.endswith("/")
            or ".." in ref
            or "//" in ref
            or any(c in ref for c in "@\\?#;%:^~[*")
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in ref)
        ):
            raise ValueError
        install_url = url if ref is None else f"{url}@{ref}"
        if parse_repo_url(install_url) != (url, ref):
            raise ValueError
        return install_url
    except ValueError:
        raise DefinitionError(
            "Remote source must be a credential-free HTTP, HTTPS or git URL with a safe ref."
        ) from None


def _source(value: object) -> PortableSource:
    source = _object(value)
    kind = source.get("kind")
    if kind == "remote":
        source = _object(value, {"kind", "repo_url", "ref"})
        ref = source["ref"]
        remote = RemoteSource(_string(source["repo_url"]), None if ref is None else _string(ref))
        _remote_url(remote)
        return remote
    if kind == "builtin":
        source = _object(value, {"kind", "identifier"})
        identifier = _string(source["identifier"])
        if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
            raise DefinitionError("Builtin identifiers must be a single bundled app directory name.")
        return BuiltinSource(identifier)
    if kind == "local" or kind == "unknown":
        _object(value, {"kind"})
        return UnavailableSource(kind)
    raise DefinitionError("Unknown source kind.")


def _port(value: object) -> PublishedPort:
    port = _object(value, {"label", "container_port", "host_port"})
    container, host = port["container_port"], port["host_port"]
    if type(container) is not int or not 1 <= container <= 65535:
        raise DefinitionError("Container ports must be integers from 1 to 65535.")
    if type(host) is not int or not (host == 0 or UNPRIVILEGED_PORT_FLOOR <= host <= 65535):
        raise DefinitionError("Host ports must be 0 or integers from 25 to 65535.")
    return PublishedPort(_string(port["label"]), container, host)


def _app(value: object) -> AppDefinition:
    app = _object(value, {"name", "source", "port_mappings"})
    name = _string(app["name"])
    if not is_valid_app_name(name) or name.endswith("\n") or f"/{name}" in RESERVED_PATHS:
        raise DefinitionError("Invalid or reserved app name.")
    ports = tuple(_port(port) for port in _list(app["port_mappings"]))
    if len({port.label for port in ports}) != len(ports):
        raise DefinitionError("Duplicate port labels are not supported.")
    return AppDefinition(name, _source(app["source"]), ports)


def parse_definition(content: str) -> DefinitionExport:
    """Validate the complete v2 document before any database write or installation."""
    try:
        if len(content.encode("utf-8")) > MAX_DEFINITION_BYTES:
            raise DefinitionError("App definition YAML must be at most 1 MiB.")
        document = _object(yaml.load(content, Loader=_DefinitionLoader))
    except DefinitionError:
        raise
    except Exception:
        # Even safe constructors can raise KeyError/TypeError with scalar values in the message.
        raise DefinitionError("Invalid app definition YAML.") from None
    if type(document.get("schema_version")) is not int or document["schema_version"] != 2:
        raise DefinitionError("schema_version must be 2. Re-export older app definition files.")
    mode = document.get("mode")
    if mode != "sharing" and mode != "private":
        raise DefinitionError("mode must be sharing or private.")
    fields = {"schema_version", "mode", "apps"}
    _object(document, fields | ({"platform_api_tokens"} if mode == "private" else set()))
    apps = tuple(_app(app) for app in _list(document["apps"]))
    if len({app.name for app in apps}) != len(apps):
        raise DefinitionError("Duplicate app names are not supported.")
    if mode == "sharing":
        return DefinitionExport(mode, apps)
    tokens = tuple(_platform_api_token(token) for token in _list(document["platform_api_tokens"]))
    if len({token.token_hash for token in tokens}) != len(tokens):
        raise DefinitionError("Duplicate API token hashes are not supported.")
    return PrivateDefinitionExport(mode, apps, tokens)


@attr.s(auto_attribs=True, frozen=True)
class DefinitionInstall:
    repo_url: str
    app_name: str
    port_overrides: dict[str, int]


@attr.s(auto_attribs=True, frozen=True)
class PlannedApp:
    name: str
    source_label: str
    status: Literal["ready", "existing", "unavailable"]
    app_id: str | None = None
    install: DefinitionInstall | None = None


@attr.s(auto_attribs=True, frozen=True)
class DefinitionPlan:
    mode: ExportMode
    apps: tuple[PlannedApp, ...]
    platform_api_token_names: tuple[str, ...]
    schema_version: int = attr.field(default=2, init=False)


def definition_plan(document: DefinitionExport, db: sqlite3.Connection, apps_dir: str) -> DefinitionPlan:
    existing = {row["name"]: row["app_id"] for row in db.execute("SELECT name, app_id FROM apps")}
    apps: list[PlannedApp] = []
    root = Path(apps_dir).resolve()
    for app in document.apps:
        repo_url = None
        match app.source:
            case RemoteSource():
                repo_url = _remote_url(app.source)
                label = repo_url
            case BuiltinSource(identifier):
                label = f"builtin: {identifier}"
                directory = (root / identifier).resolve()
                if directory.parent != root:
                    raise DefinitionError("Builtin source must stay inside the bundled apps directory.")
                if any((directory / filename).is_file() for filename in MANIFEST_FILENAMES):
                    # The existing installer expects a literal file path, not percent-encoding.
                    candidate = f"file://{directory}"
                    if parse_repo_url(candidate) == (candidate, None):
                        repo_url = candidate
            case UnavailableSource(kind):
                label = kind
        if app.name in existing:
            apps.append(PlannedApp(app.name, label, "existing", app_id=existing[app.name]))
        elif repo_url is None:
            apps.append(PlannedApp(app.name, label, "unavailable"))
        else:
            install = DefinitionInstall(
                repo_url,
                app.name,
                {port.label: port.host_port for port in app.port_mappings},
            )
            apps.append(PlannedApp(app.name, label, "ready", install=install))
    private = document if isinstance(document, PrivateDefinitionExport) else None
    return DefinitionPlan(
        document.mode,
        tuple(apps),
        tuple(token.name for token in private.platform_api_tokens) if private else (),
    )


def import_platform_api_tokens(db: sqlite3.Connection, tokens: tuple[PlatformApiToken, ...]) -> int:
    """Atomically add a validated batch, preserving existing rows and any enclosing transaction."""
    if not tokens:
        return 0
    with make_atomic_with_savepoint(db):
        cursor = db.executemany(
            """INSERT INTO api_tokens (name, token_hash, expires_at) VALUES (?, ?, ?)
               ON CONFLICT(token_hash) DO NOTHING""",
            [
                (token.name, token.token_hash, token.expires_at if token.expires_at is not None else "")
                for token in tokens
            ],
        )
    return cursor.rowcount
