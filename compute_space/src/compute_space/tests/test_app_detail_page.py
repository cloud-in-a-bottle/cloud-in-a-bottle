from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.plugins.jinja import JinjaTemplateEngine
from litestar.template.config import TemplateConfig
from litestar.testing import TestClient
from markupsafe import escape

from compute_space.config import Config
from compute_space.config import provide_config
from compute_space.config import set_active_config
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.web.app import _template_globals
from compute_space.web.routes.pages.apps import app_detail
from compute_space.web.routes.pages.apps import dashboard

from ._litestar_helpers import auth_cookie
from ._litestar_helpers import stash_zone_middleware
from .conftest import _make_test_config


@pytest.fixture
def cfg(tmp_path: Path) -> Iterator[Any]:
    config = _make_test_config(tmp_path)
    init_db(config.db_path)
    yield config


def _build_app(cfg: Any) -> Litestar:
    web_dir = Path(__file__).resolve().parents[1] / "web"
    template_config: TemplateConfig[JinjaTemplateEngine] = TemplateConfig(
        directory=web_dir / "templates",
        engine=JinjaTemplateEngine,
    )

    def _install_globals(app: Litestar) -> None:
        engine = app.template_engine
        if isinstance(engine, JinjaTemplateEngine):
            engine.engine.globals.update(_template_globals(cfg, web_dir / "static"))

    return Litestar(
        route_handlers=[app_detail, dashboard],
        template_config=template_config,
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        middleware=[stash_zone_middleware],
        on_startup=[_install_globals],
        openapi_config=None,
    )


def test_app_detail_renders_error_row(cfg: Any) -> None:
    set_active_config(cfg)
    cookie = auth_cookie(cfg)
    err = "Container build failed (exit code 1):\n" + ("x" * 100)

    with sqlite3.connect(cfg.db_path) as conn:
        conn.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status, error_message)
               VALUES ('err-app-id', 'errored-app', '1.0.0', '/tmp/errored-app', 19125, 'error', ?),
                      ('ok-app-id', 'ok-app', '1.0.0', '/tmp/ok-app', 19126, 'running', NULL)""",
            (err,),
        )

    with TestClient(app=_build_app(cfg)) as client:
        client.cookies.update(cookie)

        resp_err = client.get("/app_detail/errored-app")
        assert resp_err.status_code == 200
        assert '<tr id="app-error-row">' in resp_err.text
        assert "Container build failed (exit code 1):\n" in resp_err.text
        assert ("x" * 100) in resp_err.text

        resp_ok = client.get("/app_detail/ok-app")
        assert resp_ok.status_code == 200
        assert '<tr id="app-error-row" hidden>' in resp_ok.text


@pytest.mark.parametrize(
    ("public_paths", "explanation"),
    [
        ([], "None. All paths require Cloud in a Bottle login."),
        (
            ["/api", "/assets/"],
            "These paths and their subpaths are accessible without Cloud in a Bottle login.",
        ),
        (["/"], "All paths are accessible without Cloud in a Bottle login."),
        (["/api", "/"], "All paths are accessible without Cloud in a Bottle login."),
        (
            ['/"><script>alert("public-path")</script>&'],
            "These paths and their subpaths are accessible without Cloud in a Bottle login.",
        ),
    ],
    ids=["private", "subpaths", "entire-app", "root-with-other-paths", "html-escaping"],
)
def test_app_detail_shows_active_public_paths(cfg: Config, public_paths: list[str], explanation: str) -> None:
    set_active_config(cfg)
    # The router's stored paths are authoritative, even if the raw manifest differs.
    manifest_raw = (
        '[app]\nname = "public-app"\nversion = "2.0.0"\n'
        '[runtime.container]\nimage = "Dockerfile"\nport = 8000\n'
        '[routing]\npublic_paths = ["/not-active"]\n'
    )
    with sqlite3.connect(cfg.db_path) as conn:
        conn.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status, public_paths, manifest_raw)
               VALUES ('public-app-id', 'public-app', '1.0.0', '/tmp/public-app', 19125, 'running', ?, ?)""",
            (json.dumps(public_paths), manifest_raw),
        )

    with TestClient(app=_build_app(cfg)) as client:
        client.cookies.update(auth_cookie(cfg))
        response = client.get("/app_detail/public-app")

    assert response.status_code == 200
    assert '<th scope="row">Public paths</th>' in response.text
    assert explanation in response.text
    for path in public_paths:
        assert f"<code>{escape(path)}</code>" in response.text
    assert "/not-active" not in response.text
    assert '<script>alert("public-path")</script>' not in response.text


def test_app_detail_shows_private_default(cfg: Config) -> None:
    set_active_config(cfg)
    with sqlite3.connect(cfg.db_path) as conn:
        conn.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status)
               VALUES ('private-app-id', 'private-app', '1.0.0', '/tmp/private-app', 19125, 'stopped')"""
        )

    with TestClient(app=_build_app(cfg)) as client:
        client.cookies.update(auth_cookie(cfg))
        response = client.get("/app_detail/private-app")

    assert response.status_code == 200
    assert '<th scope="row">Public paths</th>' in response.text
    assert "None. All paths require Cloud in a Bottle login." in response.text


@pytest.mark.parametrize(
    ("license_value", "packaging_license"),
    [
        ("AGPL-3.0-only", "MIT"),
        ("MIT OR Apache-2.0", None),
        (None, "LicenseRef-Custom"),
        (None, None),
        ("   ", ""),
        ('<script>alert("license")</script>&', '<img src=x onerror="alert(1)">'),
    ],
    ids=["separate", "application-only", "packaging-only", "legacy", "blank", "html-escaping"],
)
def test_app_pages_show_deployed_licenses(
    cfg: Config, tmp_path: Path, license_value: str | None, packaging_license: str | None
) -> None:
    fields = "".join(
        f"{field} = {json.dumps(value)}\n"
        for field, value in [("license", license_value), ("packaging_license", packaging_license)]
        if value is not None
    )
    manifest_raw = (
        '[app]\nname = "licensed-app"\nversion = "1.0.0"\n'
        + fields
        + '[runtime.container]\nimage = "Dockerfile"\nport = 8000\n'
    )
    # Local edits are not deployed metadata until a reload stores the new manifest.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "cloudinabottle.toml").write_text(
        '[app]\nname = "licensed-app"\nversion = "2.0.0"\nlicense = "not-deployed"\n'
        '[runtime.container]\nimage = "Dockerfile"\nport = 8000\n'
    )
    with sqlite3.connect(cfg.db_path) as conn:
        conn.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status, manifest_raw)
               VALUES ('licensed-id', 'licensed-app', '1.0.0', ?, 19125, 'stopped', ?)""",
            (str(repo), manifest_raw),
        )

    with TestClient(app=_build_app(cfg)) as client:
        client.cookies.update(auth_cookie(cfg))
        for path in ("/dashboard", "/app_detail/licensed-app"):
            response = client.get(path)
            assert response.status_code == 200
            for label, value in [("Application license", license_value), ("Packaging license", packaging_license)]:
                expected = str(escape((value or "").strip() or "Not specified"))
                if path == "/dashboard":
                    assert f"{label}: {expected}</span>" in response.text
                else:
                    assert f'<th scope="row">{label}</th><td>{expected}</td>' in response.text
            assert "not-deployed" not in response.text
            assert '<script>alert("license")</script>' not in response.text
            assert '<img src=x onerror="alert(1)">' not in response.text


@pytest.mark.parametrize(
    "manifest_raw",
    [
        None,
        "",
        "not valid [ toml",
        '[app]\nname = "legacy-app"\nversion = "1.0.0"\nlicense = 42\n'
        '[runtime.container]\nimage = "Dockerfile"\nport = 8000\n',
    ],
    ids=["missing", "empty", "invalid-toml", "invalid-license"],
)
def test_app_pages_tolerate_missing_or_invalid_stored_manifests(cfg: Config, manifest_raw: str | None) -> None:
    with sqlite3.connect(cfg.db_path) as conn:
        conn.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status, manifest_raw)
               VALUES ('legacy-id', 'legacy-app', '1.0.0', '/tmp/legacy-app', 19125, 'stopped', ?)""",
            (manifest_raw,),
        )
    with TestClient(app=_build_app(cfg)) as client:
        client.cookies.update(auth_cookie(cfg))
        for path in ("/dashboard", "/app_detail/legacy-app"):
            response = client.get(path)
            assert response.status_code == 200
            assert response.text.count("Not specified") == 2


def test_app_detail_keeps_license_metadata_when_permission_display_fails(cfg: Config) -> None:
    # TOML dates in nested grants parse successfully but cannot be JSON-serialized
    # by the permission diff. The existing page fallback must still apply.
    manifest_raw = """\
[app]
name = "dated-grant"
version = "1.0.0"
license = "AGPL-3.0-only"
packaging_license = "MIT"

[runtime.container]
image = "Dockerfile"
port = 8000

[[services.v2.consumes]]
service = "github.com/example/services/data"
shortname = "data"
version = ">=1.0"
grants = [{expires = 2026-01-01}]
"""
    with sqlite3.connect(cfg.db_path) as conn:
        conn.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, local_port, status, manifest_raw)
               VALUES ('dated-id', 'dated-grant', '1.0.0', '/tmp/dated-grant', 19125, 'stopped', ?)""",
            (manifest_raw,),
        )
    with TestClient(app=_build_app(cfg)) as client:
        client.cookies.update(auth_cookie(cfg))
        response = client.get("/app_detail/dated-grant")

    assert response.status_code == 200
    assert '<th scope="row">Application license</th><td>AGPL-3.0-only</td>' in response.text
    assert '<th scope="row">Packaging license</th><td>MIT</td>' in response.text
