"""Tests for the dashboard/layout heading name.

The top-of-page ``<h1>`` (and ``<title>``) prefers the owner's configured
username over the zone subdomain, falling back to the zone name (and then to
"Cloud in a Bottle") when no username is set. The name is exposed to templates via the
``owner_name`` callable installed in ``_template_globals``.
"""

from __future__ import annotations

import sqlite3
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

from compute_space.config import provide_config
from compute_space.config import set_active_config
from compute_space.core.auth.auth import update_owner_username
from compute_space.core.domains import Domain
from compute_space.core.domains import seed_domains
from compute_space.db import get_db
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.web.app import _template_globals
from compute_space.web.routes.pages.apps import dashboard
from compute_space.web.routes.pages.settings import settings_page

from ._litestar_helpers import auth_cookie
from ._litestar_helpers import seed_user
from .conftest import _make_test_config


def _seed_username(db_path: str, username: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        update_owner_username(conn, username)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def cfg(tmp_path: Path) -> Iterator[Any]:
    config = _make_test_config(tmp_path, zone_domain="alice-zone.example.com")
    init_db(config.db_path)
    yield config


def _build_app(cfg: Any) -> Litestar:
    """A minimal app with the real dashboard + settings routes and Jinja globals installed."""
    web_dir = Path(__file__).resolve().parents[1] / "web"
    template_config: TemplateConfig[JinjaTemplateEngine] = TemplateConfig(
        directory=web_dir / "templates",
        engine=JinjaTemplateEngine,
    )

    def _install_globals(app: Litestar) -> None:
        engine = app.template_engine
        if isinstance(engine, JinjaTemplateEngine):
            engine.engine.globals.update(_template_globals(cfg, web_dir / "static"))

    app = Litestar(
        route_handlers=[dashboard, settings_page],
        template_config=template_config,
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        on_startup=[_install_globals],
        openapi_config=None,
    )
    # The dashboard route is guarded by require_owner_auth; tests below provide a
    # real session cookie, so no guard override is needed.
    return app


def test_heading_uses_zone_name_when_no_username(cfg: Any) -> None:
    set_active_config(cfg)
    # auth_cookie seeds a user named "owner" (the default), so to exercise the
    # *fallback* we clear the username to empty after seeding.
    cookie = auth_cookie(cfg, username="owner")
    _seed_username(cfg.db_path, "")  # simulate "no username set"

    with TestClient(app=_build_app(cfg)) as client:
        client.cookies.update(cookie)
        resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "alice-zone's personal compute space" in resp.text
    assert "owner's personal compute space" not in resp.text


def test_heading_uses_owner_username_when_set(cfg: Any) -> None:
    set_active_config(cfg)
    cookie = auth_cookie(cfg, username="owner")
    _seed_username(cfg.db_path, "alice")

    with TestClient(app=_build_app(cfg)) as client:
        client.cookies.update(cookie)
        resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "alice's personal compute space" in resp.text
    # The zone subdomain must not drive the heading.
    assert "alice-zone's personal compute space" not in resp.text


def test_settings_renders_logout_button(cfg: Any) -> None:
    """The settings page exposes a Log out control that POSTs to /logout.

    The session cookie is httponly, so logout must round-trip through the
    server; a top-level form POST (samesite=lax) is the correct mechanism.
    """
    set_active_config(cfg)
    cookie = auth_cookie(cfg, username="owner")

    with TestClient(app=_build_app(cfg)) as client:
        client.cookies.update(cookie)
        settings_resp = client.get("/settings")
        dashboard_resp = client.get("/dashboard")
    assert settings_resp.status_code == 200
    assert 'action="/logout"' in settings_resp.text
    assert 'method="post"' in settings_resp.text
    assert "Log out" in settings_resp.text
    # The control lives only on the settings page, not in the shared nav.
    assert "Log out" not in dashboard_resp.text


def test_settings_renders_domains_section(cfg: Any) -> None:
    """The settings page exposes the Domains management UI over /api/domains."""
    set_active_config(cfg)
    cookie = auth_cookie(cfg, username="owner")

    with TestClient(app=_build_app(cfg)) as client:
        client.cookies.update(cookie)
        settings_resp = client.get("/settings")
    assert settings_resp.status_code == 200
    assert ">Domains</h2>" in settings_resp.text
    assert 'onclick="addDomain()"' in settings_resp.text
    assert "Local (HTTP)" in settings_resp.text
    assert "Local mDNS" not in settings_resp.text
    assert "Arrange name resolution for local domains" in settings_resp.text
    assert "<th>Domain</th><th>Scheme</th><th>Type</th>" in settings_resp.text
    assert "js/domains.js" in settings_resp.text


def test_owner_name_global_reads_live(cfg: Any) -> None:
    set_active_config(cfg)
    globals_ = _template_globals(cfg, Path("static"))
    owner_name = globals_["owner_name"]

    # Pre-setup (no user row) -> None so the heading falls back to zone_name.
    assert owner_name() is None

    seed_user(cfg.db_path, username="bob")
    assert owner_name() == "bob"

    # Changing the username is reflected immediately (read live, not cached).
    _seed_username(cfg.db_path, "carol")
    assert owner_name() == "carol"


def test_zone_name_global_reads_live(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path, zone_domain="alice-zone.example.com", seed_primary=False)
    init_db(cfg.db_path)
    set_active_config(cfg)
    globals_ = _template_globals(cfg, Path("static"))
    zone_domain = globals_["zone_domain"]
    zone_name = globals_["zone_name"]

    # Pre-seed (no primary row) -> empty/None so the heading falls back.
    assert zone_domain() == ""
    assert zone_name() is None

    with closing(get_db()) as db:
        seed_domains(db, Domain(name="alice-zone.example.com", tls=False), [])

    # A primary seeded after construction is reflected immediately (read live, not cached).
    assert zone_domain() == "alice-zone.example.com"
    assert zone_name() == "alice-zone"
