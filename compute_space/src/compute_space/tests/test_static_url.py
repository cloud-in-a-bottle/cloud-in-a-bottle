"""Tests for the ``static_url`` Jinja global and the asset references that use it."""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from compute_space.web.helpers.static import STATIC_DIR
from compute_space.web.helpers.static import WEB_DIR
from compute_space.web.helpers.static import make_static_url

_STATIC_URL_CALL = re.compile(r"""static_url\(\s*['"]([^'"]+)['"]\s*\)""")


def test_existing_file_is_versioned_by_mtime(tmp_path: Path) -> None:
    asset = tmp_path / "app.css"
    asset.write_text("body{}")
    asset.touch()
    mtime = int(asset.stat().st_mtime)

    assert make_static_url(tmp_path)("app.css") == f"/static/app.css?v={mtime}"


def test_missing_file_degrades_to_a_uncacheable_url(tmp_path: Path) -> None:
    before = int(time.time())
    url = make_static_url(tmp_path)("gone.css")
    after = int(time.time())

    # A missing asset must not take the whole page down, but it must also never be
    # answerable from a cache populated while the file still existed.
    path, _, version = url.partition("?v=")
    assert path == "/static/gone.css"
    assert before <= int(version) <= after


@pytest.mark.parametrize("template", sorted(p for p in (WEB_DIR / "templates").rglob("*.html")), ids=lambda p: p.name)
def test_every_templated_asset_exists(template: Path) -> None:
    """A typo'd or deleted asset now only logs, so catch it here instead of in production."""
    missing = [name for name in _STATIC_URL_CALL.findall(template.read_text()) if not (STATIC_DIR / name).is_file()]
    assert not missing, f"{template.relative_to(WEB_DIR)} references missing static files: {missing}"
