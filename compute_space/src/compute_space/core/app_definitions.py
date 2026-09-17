from __future__ import annotations

import json
import re
import sqlite3
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import unquote
from urllib.parse import urlparse

import attr

from compute_space.core.git_ops import is_ssh_url
from compute_space.core.git_ops import parse_repo_url
from compute_space.db.connection import make_atomic_with_savepoint

type ExportMode = Literal["sharing", "private"]


def parse_export_mode(body: object) -> ExportMode:
    if isinstance(body, dict):
        mode = body.get("mode", "sharing")
        if mode == "sharing":
            return "sharing"
        if mode == "private":
            return "private"
    raise ValueError("mode must be sharing or private")


@attr.s(auto_attribs=True, frozen=True)
class RemoteSource:
    repo_url: str
    ref: str | None
    kind: Literal["remote"] = attr.field(default="remote", init=False)


@attr.s(auto_attribs=True, frozen=True)
class BuiltinSource:
    identifier: str
    kind: Literal["builtin"] = attr.field(default="builtin", init=False)


@attr.s(auto_attribs=True, frozen=True)
class UnavailableSource:
    kind: Literal["local", "unknown"]


type PortableSource = RemoteSource | BuiltinSource | UnavailableSource


def portable_source(repo_url: str | None, apps_dir: str) -> PortableSource:
    if not repo_url:
        return UnavailableSource("unknown")
    if repo_url.startswith(("/", ".", "~")):
        return UnavailableSource("local")
    try:
        original = urlparse(repo_url)
        if original.scheme == "file":
            path = PurePosixPath(unquote(original.path))
            if (
                not original.netloc
                and path.parent == PurePosixPath(apps_dir)
                and re.fullmatch(r"[A-Za-z0-9_-]+", path.name)
            ):
                return BuiltinSource(path.name)
            return UnavailableSource("local")
        if is_ssh_url(repo_url) or ("://" in repo_url and original.scheme not in ("https", "http", "git")):
            return UnavailableSource("unknown")
        parsed = original if original.scheme in ("https", "http", "git") else urlparse("https://" + repo_url)
        host = parsed.hostname
        if not host or any(c in host for c in "\\/%") or any(c.isspace() for c in repo_url):
            return UnavailableSource("unknown")
        if "://" not in repo_url and "." not in host:
            return UnavailableSource("local")
        netloc = f"[{host}]" if ":" in host else host
        if parsed.port is not None:
            netloc += f":{parsed.port}"
        # Strip all parameters before interpreting @ref: an @ inside a credential-bearing
        # parameter must not move that credential into the exported ref.
        clean_path = "/".join(segment.partition(";")[0] for segment in parsed.path.split("/"))
        sanitized = parsed._replace(netloc=netloc, path=clean_path, params="", query="", fragment="").geturl()
        base, ref = parse_repo_url(sanitized)
        return RemoteSource(base, ref)
    except ValueError:
        return UnavailableSource("unknown")


@attr.s(auto_attribs=True, frozen=True)
class PublishedPort:
    label: str
    container_port: int
    host_port: int


@attr.s(auto_attribs=True, frozen=True)
class AppDefinition:
    name: str
    source: PortableSource
    port_mappings: tuple[PublishedPort, ...]


@attr.s(auto_attribs=True, frozen=True)
class PlatformApiToken:
    name: str
    token_hash: str
    expires_at: str | None


@attr.s(auto_attribs=True, frozen=True)
class DefinitionExport:
    mode: ExportMode
    apps: tuple[AppDefinition, ...]
    schema_version: int = attr.field(default=2, init=False)


@attr.s(auto_attribs=True, frozen=True)
class PrivateDefinitionExport(DefinitionExport):
    platform_api_tokens: tuple[PlatformApiToken, ...]


def _snapshot(db: sqlite3.Connection, apps_dir: str, mode: ExportMode) -> DefinitionExport:
    # Read only allow-listed metadata. In particular, never select or parse manifest_raw.
    # All app, port and API-token records come from one database snapshot.
    with make_atomic_with_savepoint(db):
        app_rows = db.execute("SELECT app_id, name, repo_url FROM apps ORDER BY name").fetchall()
        ports_by_app: dict[str, list[PublishedPort]] = {}
        for row in db.execute(
            "SELECT app_id, label, container_port, host_port FROM app_port_mappings ORDER BY label"
        ).fetchall():
            ports_by_app.setdefault(row["app_id"], []).append(
                PublishedPort(row["label"], row["container_port"], row["host_port"])
            )
        apps = tuple(
            AppDefinition(
                name=row["name"],
                source=portable_source(row["repo_url"], apps_dir),
                port_mappings=tuple(ports_by_app.get(row["app_id"], [])),
            )
            for row in app_rows
        )
        if mode == "private":
            tokens = tuple(
                PlatformApiToken(row["name"], row["token_hash"], row["expires_at"] or None)
                for row in db.execute(
                    "SELECT name, token_hash, expires_at FROM api_tokens ORDER BY name, token_hash"
                ).fetchall()
            )
            return PrivateDefinitionExport(mode=mode, apps=apps, platform_api_tokens=tokens)
        return DefinitionExport(mode=mode, apps=apps)


async def export_app_definitions(db: sqlite3.Connection, apps_dir: str, mode: ExportMode) -> str:
    """Export stored definitions after owner auth or an export-service grant check."""
    document = _snapshot(db, apps_dir, mode)
    return json.dumps(attr.asdict(document), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
