"""Tests for the "view source" provenance link in the shared nav.

The nav header (``_nav_header.html``, included by both ``layout.html`` and the
standalone docs template) surfaces a GitHub icon linking to the exact branch/fork
of OpenHost the instance is running. The link is built from the running checkout's
origin remote + current branch via ``get_github_source_url`` and exposed to
templates as the ``source_url`` global; it is hidden entirely when that is None
(tarball deploy, detached HEAD with no branch, non-GitHub remote, ...).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from litestar import Litestar
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.di import Provide
from litestar.template.config import TemplateConfig
from litestar.testing import TestClient

import compute_space.web.app as web_app
from compute_space.config import provide_config
from compute_space.config import set_active_config
from compute_space.core.git_ops import github_web_url
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.web.app import _template_globals
from compute_space.web.routes.pages.apps import dashboard

from ._litestar_helpers import auth_cookie
from .conftest import _make_test_config


@pytest.mark.parametrize(
    ("remote_url", "branch", "expected"),
    [
        # HTTPS clone URL, with and without the .git suffix.
        (
            "https://github.com/imbue-openhost/openhost.git",
            "main",
            "https://github.com/imbue-openhost/openhost/tree/main",
        ),
        ("https://github.com/owner/repo", "dev", "https://github.com/owner/repo/tree/dev"),
        # SCP-style SSH shorthand.
        ("git@github.com:owner/repo.git", "dev", "https://github.com/owner/repo/tree/dev"),
        # Credentials in the URL must never leak into the browsable link.
        ("https://oauth2:TOKEN@github.com/owner/repo.git", "main", "https://github.com/owner/repo/tree/main"),
        # Branch names containing slashes stay literal (GitHub tree paths use them).
        (
            "https://github.com/owner/repo.git",
            "samuel/open-app-new-tab",
            "https://github.com/owner/repo/tree/samuel/open-app-new-tab",
        ),
        # Detached HEAD (no branch) -> repo root, still a valid link.
        ("https://github.com/owner/repo.git", None, "https://github.com/owner/repo"),
        # Non-GitHub remotes are not linked.
        ("https://gitlab.com/owner/repo.git", "main", None),
    ],
)
def test_github_web_url(remote_url: str, branch: str | None, expected: str | None) -> None:
    assert github_web_url(remote_url, branch) == expected


@pytest.fixture
def cfg(tmp_path: Path) -> Iterator[Any]:
    config = _make_test_config(tmp_path, zone_domain="alice-zone.example.com")
    init_db(config.db_path)
    yield config


def _build_dashboard_app(cfg: Any) -> Litestar:
    web_dir = Path(web_app.__file__).resolve().parent
    template_config: TemplateConfig[JinjaTemplateEngine] = TemplateConfig(
        directory=web_dir / "templates",
        engine=JinjaTemplateEngine,
    )

    def _install_globals(app: Litestar) -> None:
        engine = app.template_engine
        if isinstance(engine, JinjaTemplateEngine):
            engine.engine.globals.update(_template_globals(cfg, web_dir / "static"))

    return Litestar(
        route_handlers=[dashboard],
        template_config=template_config,
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        on_startup=[_install_globals],
        openapi_config=None,
    )


def test_nav_shows_source_icon(cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """When a source URL resolves, the nav renders a GitHub link to that branch."""
    set_active_config(cfg)
    monkeypatch.setattr(web_app, "get_github_source_url", lambda _p: "https://github.com/owner/repo/tree/feature")
    cookie = auth_cookie(cfg, username="owner")

    with TestClient(app=_build_dashboard_app(cfg)) as client:
        resp = client.get("/dashboard", cookies=cookie)

    assert resp.status_code == 200
    assert 'class="nav-source"' in resp.text
    assert 'href="https://github.com/owner/repo/tree/feature"' in resp.text
    # Opens in a new tab, per the app-link convention, and carries the a11y label.
    assert 'target="_blank"' in resp.text
    assert 'aria-label="View source on GitHub"' in resp.text


def test_nav_hides_source_icon_when_unresolved(cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tarball deploy (no resolvable source) renders no source link at all."""
    set_active_config(cfg)
    monkeypatch.setattr(web_app, "get_github_source_url", lambda _p: None)
    cookie = auth_cookie(cfg, username="owner")

    with TestClient(app=_build_dashboard_app(cfg)) as client:
        resp = client.get("/dashboard", cookies=cookie)

    assert resp.status_code == 200
    # The always-present nav <style> block references ``.nav-source``; assert the
    # anchor element itself is absent rather than the bare class name.
    assert 'class="nav-source"' not in resp.text
    assert "github.com" not in resp.text
