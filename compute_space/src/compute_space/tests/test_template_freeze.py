"""The template environment must be pinned to the code the process booted with.

A self-update rewrites the source tree (``git checkout``) while the OLD router is
still serving, so an environment that stats templates per render would have the
old Python render the new templates: renamed includes, new globals, and files
caught mid-write all become 500s — on ``/updating`` above all, the page the owner
watches for the whole update.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from jinja2 import TemplateSyntaxError

from compute_space.web.templating import build_environment
from compute_space.web.templating import build_template_config


def _write(directory: Path, name: str, content: str) -> Path:
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


def _rewrite(path: Path, content: str) -> None:
    """Rewrite a template the way a checkout would, and push its mtime forward.

    Jinja detects staleness by mtime equality, so on a filesystem with coarse
    timestamps a same-second rewrite would go unnoticed and these tests would
    pass even with reloading enabled.
    """
    path.write_text(content, encoding="utf-8")
    stamp = path.stat().st_mtime + 10
    os.utime(path, (stamp, stamp))


def test_rendered_template_is_not_reread_after_it_changes(tmp_path: Path) -> None:
    page = _write(tmp_path, "page.html", "original")
    env = build_environment(tmp_path)
    assert env.get_template("page.html").render() == "original"

    _rewrite(page, "rewritten by the update")

    assert env.get_template("page.html").render() == "original"


def test_template_never_rendered_before_the_swap_is_still_the_booted_one(tmp_path: Path) -> None:
    # The dangerous case: a page nobody visited before the checkout. Without
    # preloading it would be compiled from the NEW tree on its first render.
    page = _write(tmp_path, "unvisited.html", "original")
    env = build_environment(tmp_path)

    _rewrite(page, "rewritten by the update")

    assert env.get_template("unvisited.html").render() == "original"


def test_template_deleted_after_boot_still_renders(tmp_path: Path) -> None:
    # A release that renames or drops a template must not 500 the old process.
    page = _write(tmp_path, "page.html", "original")
    env = build_environment(tmp_path)

    page.unlink()

    assert env.get_template("page.html").render() == "original"


def test_include_resolves_from_the_booted_tree(tmp_path: Path) -> None:
    # Includes are resolved at render time, so the partial is the real exposure:
    # /updating pulls in _update_progress_body.html on every render.
    _write(tmp_path, "page.html", "[{% include 'partial.html' %}]")
    partial = _write(tmp_path, "partial.html", "original")
    env = build_environment(tmp_path)
    assert env.get_template("page.html").render() == "[original]"

    _rewrite(partial, "rewritten by the update")
    assert env.get_template("page.html").render() == "[original]"

    partial.unlink()
    assert env.get_template("page.html").render() == "[original]"


def test_broken_template_fails_at_build_not_on_first_request(tmp_path: Path) -> None:
    # Compiling everything up front means a broken template is a loud boot
    # failure instead of a 500 the first time someone happens to hit that page.
    _write(tmp_path, "broken.html", "{% if %}")

    with pytest.raises(TemplateSyntaxError):
        build_environment(tmp_path)


def test_real_templates_all_compile() -> None:
    # Guards the fail-loud choice above: every shipped template must compile, or
    # build_environment turns a boot into a crash loop.
    template_dir = Path(__file__).resolve().parents[1] / "web" / "templates"
    env = build_environment(template_dir)
    assert "updating.html" in env.list_templates()


def test_template_config_uses_the_frozen_environment(tmp_path: Path) -> None:
    # Litestar builds its own reloading environment when handed `directory`, so
    # the config must carry our instance instead.
    page = _write(tmp_path, "page.html", "original")
    engine = build_template_config(tmp_path).engine_instance
    assert engine.engine.auto_reload is False

    _rewrite(page, "rewritten by the update")
    assert engine.get_template("page.html").render() == "original"
