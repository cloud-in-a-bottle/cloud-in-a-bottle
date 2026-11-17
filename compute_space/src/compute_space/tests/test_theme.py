from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.plugins.jinja import JinjaTemplateEngine
from litestar.template.config import TemplateConfig
from litestar.testing import TestClient

from compute_space.config import Config
from compute_space.config import provide_config
from compute_space.config import set_active_config
from compute_space.core.settings_store import set_setting
from compute_space.core.theme import THEME_KEY
from compute_space.core.theme import AppColumns
from compute_space.core.theme import AppListLayout
from compute_space.core.theme import Style
from compute_space.core.theme import Theme
from compute_space.core.theme import read_theme
from compute_space.core.theme import write_theme
from compute_space.db import get_db
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.web.app import _template_globals
from compute_space.web.routes.api.theme import api_theme_routes
from compute_space.web.routes.pages.apps import dashboard
from compute_space.web.routes.pages.settings import settings_page

from ._litestar_helpers import auth_cookie
from ._litestar_helpers import make_test_app
from .conftest import _make_test_config


@pytest.fixture
def cfg(tmp_path: Path) -> Iterator[Config]:
    config = _make_test_config(tmp_path)
    init_db(config.db_path)
    set_active_config(config)
    yield config


def _pages_app(cfg: Config) -> Litestar:
    web_dir = Path(__file__).resolve().parents[1] / "web"

    def _install_globals(app: Litestar) -> None:
        engine = app.template_engine
        assert isinstance(engine, JinjaTemplateEngine)
        engine.engine.globals.update(_template_globals(cfg, web_dir / "static"))

    return Litestar(
        route_handlers=[dashboard, settings_page],
        template_config=TemplateConfig(directory=web_dir / "templates", engine=JinjaTemplateEngine),
        dependencies={"config": Provide(provide_config, sync_to_thread=False), "db": Provide(provide_db)},
        on_startup=[_install_globals],
        openapi_config=None,
    )


def _html_theme(page: str) -> dict[str, str]:
    """The data-* attributes on the page's <html> tag."""
    match = re.search(r"<html\b[^>]*>", page)
    assert match is not None
    return dict(re.findall(r'data-([a-z-]+)="([^"]*)"', match.group(0)))


def _expected(theme: Theme) -> dict[str, str]:
    return {"style": theme.style, "app-list": theme.app_list, "app-columns": theme.app_columns}


def _get(cfg: Config, cookie: dict[str, str], path: str) -> str:
    with TestClient(app=_pages_app(cfg)) as client:
        client.cookies.update(cookie)
        resp = client.get(path)
    assert resp.status_code == 200
    return resp.text


def test_default_theme_is_the_original_look(cfg: Config) -> None:
    with closing(get_db()) as db:
        assert read_theme(db) == Theme(style=Style.CLOUDY, app_list=AppListLayout.LIST, app_columns=AppColumns.AUTO)


def test_theme_roundtrip(cfg: Config) -> None:
    with closing(get_db()) as db:
        write_theme(db, Theme(style=Style.DARK, app_list=AppListLayout.GRID, app_columns=AppColumns.THREE))
        assert read_theme(db) == Theme(style=Style.DARK, app_list=AppListLayout.GRID, app_columns=AppColumns.THREE)


def test_option_missing_from_stored_theme_keeps_its_default(cfg: Config) -> None:
    with closing(get_db()) as db:
        set_setting(db, THEME_KEY, '{"style": "simple"}')
        assert read_theme(db) == Theme(style=Style.SIMPLE, app_list=AppListLayout.LIST)


def test_unknown_stored_value_fails_loudly(cfg: Config) -> None:
    with closing(get_db()) as db:
        set_setting(db, THEME_KEY, '{"style": "neon"}')
        with pytest.raises(ValueError):
            read_theme(db)


def test_pages_render_the_theme_on_html(cfg: Config) -> None:
    chosen = Theme(style=Style.DARK, app_list=AppListLayout.COMPACT, app_columns=AppColumns.FOUR)
    with TestClient(app=_pages_app(cfg)) as client:
        client.cookies.update(auth_cookie(cfg))
        assert _html_theme(client.get("/dashboard").text) == _expected(Theme())
        # Read live: the same running app picks up a change on the next page load.
        with closing(get_db()) as db:
            write_theme(db, chosen)
        settings_html = client.get("/settings").text
    assert _html_theme(settings_html) == _expected(chosen)
    assert '<option value="dark" selected>Dark</option>' in settings_html
    assert '<option value="simple-dark">Simple dark</option>' in settings_html
    assert '<option value="compact" selected>Compact</option>' in settings_html
    assert '<option value="4" selected>4</option>' in settings_html


def test_bad_stored_theme_still_renders_pages_with_the_default(cfg: Config) -> None:
    # Settings must stay reachable so the owner can pick a valid theme again.
    cookie = auth_cookie(cfg)
    with closing(get_db()) as db:
        set_setting(db, THEME_KEY, '{"style": "neon"}')
    for path in ("/dashboard", "/settings"):
        assert _html_theme(_get(cfg, cookie, path)) == _expected(Theme())


def test_api_saves_theme(cfg: Config) -> None:
    with TestClient(app=make_test_app(api_theme_routes)) as client:
        client.cookies.update(auth_cookie(cfg))
        resp = client.post("/api/settings/theme", json={"style": "dark", "app_list": "grid", "app_columns": "3"})
    assert resp.status_code == 200
    assert resp.json() == {"style": "dark", "app_list": "grid", "app_columns": "3"}
    with closing(get_db()) as db:
        assert read_theme(db) == Theme(style=Style.DARK, app_list=AppListLayout.GRID, app_columns=AppColumns.THREE)


@pytest.mark.parametrize(
    "body",
    [
        {"style": "neon", "app_list": "list", "app_columns": "auto"},
        {"style": "dark", "app_list": "carousel", "app_columns": "auto"},
        {"style": "dark", "app_list": "grid", "app_columns": "7"},
    ],
)
def test_api_rejects_unknown_values(cfg: Config, body: dict[str, Any]) -> None:
    with TestClient(app=make_test_app(api_theme_routes)) as client:
        client.cookies.update(auth_cookie(cfg))
        resp = client.post("/api/settings/theme", json=body)
    assert resp.status_code == 400
    with closing(get_db()) as db:
        assert read_theme(db) == Theme()


def test_api_requires_owner(cfg: Config) -> None:
    with TestClient(app=make_test_app(api_theme_routes)) as client:
        resp = client.post("/api/settings/theme", json={"style": "dark", "app_list": "grid"})
    assert resp.status_code == 401
    with closing(get_db()) as db:
        assert read_theme(db) == Theme()
