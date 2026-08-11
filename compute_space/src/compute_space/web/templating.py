"""Jinja environments that are pinned to the code the process booted with.

Jinja's default is to stat every template on every render (``auto_reload``) and
Litestar builds its environment that way, so templates are effectively re-read
from disk per request.  That is wrong for this process specifically: a
self-update rewrites the source tree (``git checkout``) while the OLD router is
still serving, so a disk-backed environment renders the NEW templates with the
OLD Python.  A template that was renamed or that references a new global then
500s, and because ``git`` writes files non-atomically a request landing mid-write
can read a truncated template and 500 as well — on ``/updating``, the one page
the owner is watching for the whole update.

So: compile every template up front, never re-read, never evict.  After boot
this process cannot touch the templates directory again.

Trade-off: editing a template locally requires restarting the router.  That is
deliberate — a flag to re-enable reloading would be a flag that silently
restores the bug on any host where it got set.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment
from jinja2 import FileSystemLoader
from litestar.plugins.jinja import JinjaTemplateEngine
from litestar.template.config import TemplateConfig


def build_environment(template_dir: Path) -> Environment:
    """A Jinja environment holding every template in ``template_dir``, compiled now.

    ``autoescape`` matches what Litestar would have configured.  ``cache_size=-1``
    means the cache is never pruned, so a preloaded template can't be evicted and
    silently re-read later.

    A template that fails to compile raises here, at startup, rather than when a
    user first happens to hit the page.
    """
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=True,
        auto_reload=False,
        cache_size=-1,
    )
    for name in env.list_templates():
        env.get_template(name)
    return env


def build_template_config(template_dir: Path) -> TemplateConfig[JinjaTemplateEngine]:
    """Litestar template config backed by :func:`build_environment`.

    Passed as ``instance`` rather than ``directory`` because Litestar builds its
    own reloading environment from ``directory``.
    """
    return TemplateConfig(instance=JinjaTemplateEngine(engine_instance=build_environment(template_dir)))
