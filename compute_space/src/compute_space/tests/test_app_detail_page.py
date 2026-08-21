from __future__ import annotations

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

from compute_space.config import provide_config
from compute_space.config import set_active_config
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.web.app import _template_globals
from compute_space.web.routes.pages.apps import app_detail

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
        route_handlers=[app_detail],
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
        assert "Container build failed (exit code 1):\n" in resp_err.text
        assert ("x" * 100) in resp_err.text

        resp_ok = client.get("/app_detail/ok-app")
        assert resp_ok.status_code == 200
        assert "<th>Error</th>" not in resp_ok.text
