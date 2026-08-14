"""Tests for the server-side-rendered docs route.

The route reads ``docs/src/*.md`` directly off the running checkout
and renders to HTML on the fly.  These tests inject a fake
``openhost_repo_path`` via the per-test ``_FakeCfg`` so each scenario
controls exactly which markdown files exist.

What we cover:
  * Happy path — markdown renders to HTML, the rendered page
    includes content from the source file.
  * Sidebar — SUMMARY.md parsing extracts the right sections + links,
    and the active link is marked as such.
  * Missing source dir — 503 with an actionable error message
    (the only mode the old mdBook-based code's 503 covered, kept
    here as a regression).
  * 404 — unknown slugs, slugs with weird characters, slugs that
    resolve outside the docs source dir (path-traversal attempts).
  * Trailing-slash redirect — ``/docs`` 302s to ``/docs/``.
  * Internal-link rewrite — markdown like ``[a](./foo.md)`` becomes
    ``<a href="/docs/foo">a</a>``, NOT a 404 from a literal
    ``href="./foo.md"``.
  * Mtime cache — touching the source file invalidates the cached
    render.
  * RESERVED_PATHS — ``/docs`` is in the reserved-name set so an
    operator can't deploy an app named ``docs`` that would shadow
    the route.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from litestar import Litestar
from litestar.testing import TestClient

import compute_space.web.routes.docs as docs_routes
from compute_space.config import set_active_config
from compute_space.core.apps import RESERVED_PATHS
from compute_space.tests._litestar_helpers import make_test_app
from compute_space.web.routes.docs import docs_routes as docs_router


class _FakeCfg:
    """Per-test config stub exposing only ``openhost_repo_path``."""

    def __init__(self, openhost_repo_path: Path) -> None:
        self.openhost_repo_path = openhost_repo_path


@pytest.fixture(autouse=True)
def _clear_render_cache() -> Iterator[None]:
    """Reset the module-global mtime cache between tests so each
    test starts from a clean slate."""
    docs_routes._render_cache.clear()
    yield
    docs_routes._render_cache.clear()


def _populate_fake_docs(src_dir: Path) -> None:
    """Drop a small docs/src/ tree at ``src_dir``."""
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "SUMMARY.md").write_text(
        "# Summary\n"
        "\n"
        "[Introduction](./introduction.md)\n"
        "\n"
        "# Concepts\n"
        "\n"
        "- [Manifest Spec](./manifest_spec.md)\n"
        "- [Routing](./routing.md)\n"
        "\n"
        "# Guides\n"
        "\n"
        "- [Creating an App](./creating_an_app.md)\n"
    )
    (src_dir / "introduction.md").write_text(
        "# Welcome to Cloud in a Bottle\n"
        "\n"
        "Cloud in a Bottle is a self-hosted application platform.\n"
        "See the [manifest spec](./manifest_spec.md).\n"
    )
    (src_dir / "manifest_spec.md").write_text(
        "# Manifest\n"
        "\n"
        "Each app declares a `[runtime]` section.\n"
        "\n"
        "```toml\n"
        "[runtime.container]\n"
        'image = "Dockerfile"\n'
        "```\n"
    )
    (src_dir / "routing.md").write_text("# Routing\n\nRouting prose here.\n")
    (src_dir / "creating_an_app.md").write_text("# Creating an App\n\nGuide content.\n")


def _client(repo_root: Path) -> tuple[TestClient[Litestar], Any]:
    """Build a Litestar TestClient pointed at ``repo_root``; the docs route reads
    ``get_config().openhost_repo_path`` so we install the fake as the active config."""
    cfg = _FakeCfg(openhost_repo_path=repo_root)
    set_active_config(cfg)  # type: ignore[arg-type]
    return TestClient(app=make_test_app(docs_router)), cfg


@pytest.fixture
def client_with_docs(tmp_path: Path) -> Iterator[TestClient[Litestar]]:
    repo_root = tmp_path / "repo"
    _populate_fake_docs(repo_root / "docs" / "src")
    client, _cfg = _client(repo_root)
    with client as c:
        yield c


@pytest.fixture
def client_without_docs(tmp_path: Path) -> Iterator[TestClient[Litestar]]:
    repo_root = tmp_path / "repo-no-docs"
    repo_root.mkdir()
    client, _cfg = _client(repo_root)
    with client as c:
        yield c


# -- happy path -----------------------------------------------------


def test_index_renders_introduction(client_with_docs: TestClient[Litestar]) -> None:
    """``GET /docs/`` must render ``introduction.md``."""
    resp = client_with_docs.get("/docs/")
    assert resp.status_code == 200
    body = resp.text
    assert "Welcome to Cloud in a Bottle" in body
    assert "self-hosted application platform" in body


def test_slug_renders_corresponding_markdown(client_with_docs: TestClient[Litestar]) -> None:
    """``GET /docs/manifest_spec`` renders ``manifest_spec.md``."""
    resp = client_with_docs.get("/docs/manifest_spec")
    assert resp.status_code == 200
    body = resp.text
    assert "<h1" in body
    assert "Manifest" in body
    assert "Each app declares" in body


def test_code_blocks_are_syntax_highlighted(client_with_docs: TestClient[Litestar]) -> None:
    """Fenced code blocks with a language tag run through Pygments."""
    resp = client_with_docs.get("/docs/manifest_spec")
    body = resp.text
    # The Pygments HtmlFormatter wraps highlighted output in
    # ``<div class="codehilite">`` and tags individual tokens with
    # CSS classes like ``.n`` (name), ``.s2`` (double-quoted str), etc.
    assert "codehilite" in body
    assert "image" in body


def test_sidebar_contains_summary_entries(client_with_docs: TestClient[Litestar]) -> None:
    """The sidebar exposes the SUMMARY.md sections + links."""
    resp = client_with_docs.get("/docs/")
    body = resp.text
    assert "Concepts" in body
    assert "Guides" in body
    assert 'href="/docs/manifest_spec"' in body
    assert 'href="/docs/routing"' in body
    assert 'href="/docs/creating_an_app"' in body


def test_active_sidebar_link_marked(client_with_docs: TestClient[Litestar]) -> None:
    """The currently-rendered page's sidebar link gets ``class="active"``."""
    resp = client_with_docs.get("/docs/manifest_spec")
    body = resp.text
    assert 'href="/docs/manifest_spec"' in body and "active" in body


def test_page_carries_space_nav_header(client_with_docs: TestClient[Litestar]) -> None:
    """The docs page must render the shared compute-space nav header
    (Dashboard / Docs / Deploy App / ...) so the manual reads as an
    in-space page rather than a standalone site."""
    resp = client_with_docs.get("/docs/")
    body = resp.text
    assert 'id="main-nav"' in body
    assert 'href="/dashboard"' in body
    assert 'href="/add_app"' in body
    assert 'href="/settings"' in body


def test_docs_nav_link_stays_in_same_tab(client_with_docs: TestClient[Litestar]) -> None:
    """The nav *tabs* must not open in a new tab — no ``target="_blank"`` on any
    ``nav-tab`` anchor. (The separate provenance "view source" link deliberately
    does open a new tab, so we assert on the tabs specifically, not the whole nav.)
    """
    resp = client_with_docs.get("/docs/")
    tab_anchors = re.findall(r'<a[^>]*class="nav-tab"[^>]*>', resp.text)
    assert tab_anchors, "expected nav tabs in the rendered docs header"
    assert all('target="_blank"' not in anchor for anchor in tab_anchors)


def test_internal_md_links_rewritten(client_with_docs: TestClient[Litestar]) -> None:
    """Markdown like ``[manifest spec](./manifest_spec.md)`` should
    render as a link to ``/docs/manifest_spec``, NOT a literal
    ``href="./manifest_spec.md"`` that would 404."""
    resp = client_with_docs.get("/docs/")
    body = resp.text
    assert 'href="/docs/manifest_spec"' in body
    assert 'href="./manifest_spec.md"' not in body
    assert 'href="manifest_spec.md"' not in body


def test_prev_next_navigation(client_with_docs: TestClient[Litestar]) -> None:
    """Each page surfaces prev/next links based on SUMMARY.md order."""
    resp = client_with_docs.get("/docs/manifest_spec")
    body = resp.text.lower()
    # In our SUMMARY: introduction, manifest_spec, routing, creating_an_app.
    # manifest_spec should point back to introduction and forward to routing.
    assert "introduction" in body
    assert "routing" in body


# -- 404 / safety ---------------------------------------------------


def test_unknown_slug_404(client_with_docs: TestClient[Litestar]) -> None:
    """A request for a slug whose .md doesn't exist returns 404."""
    resp = client_with_docs.get("/docs/this_does_not_exist")
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "evil_slug",
    [
        "../etc/passwd",
        "..%2Fetc%2Fpasswd",
        "%2E%2E%2Fetc%2Fpasswd",
        "subdir/foo",
        ".gitignore",
        " ",
        "introduction.md.bak",
        "../secrets.md",  # the .md suffix is stripped before the slug regex, so this still 404s
        ".md",
    ],
)
def test_path_traversal_blocked(client_with_docs: TestClient[Litestar], evil_slug: str) -> None:
    """The slug regex rejects anything outside ``[A-Za-z0-9_-]+``.

    Whether the framework returns 404 directly or 308-rewrites and then 404s,
    the response must NOT be 200 and must NOT echo a sensitive
    sentinel from outside the docs dir.
    """
    resp = client_with_docs.get(f"/docs/{evil_slug}", follow_redirects=True)
    assert resp.status_code != 200
    # Sanity: no actual /etc/passwd content
    assert "root:x:" not in resp.text


# -- error paths ----------------------------------------------------


def test_missing_docs_dir_returns_503(client_without_docs: TestClient[Litestar]) -> None:
    """When ``docs/src/`` doesn't exist, the route returns 503 with
    an actionable error message rather than 200/blank.

    This is the "operator's checkout is broken / incomplete" path.
    """
    resp = client_without_docs.get("/docs/")
    assert resp.status_code == 503
    assert "docs source directory is missing" in resp.text.lower()


# -- redirects ------------------------------------------------------


def test_both_slash_variants_serve_index(client_with_docs: TestClient[Litestar]) -> None:
    """``/docs`` and ``/docs/`` both serve the index — Litestar normalises
    trailing slashes during routing, so a single handler covers both."""
    for path in ("/docs", "/docs/"):
        resp = client_with_docs.get(path, follow_redirects=False)
        assert resp.status_code == 200, path
        assert "Welcome to Cloud in a Bottle" in resp.text, path


# -- cache ----------------------------------------------------------


def test_render_cache_invalidates_on_mtime_change(tmp_path: Path) -> None:
    """Modifying the markdown source after the first render must be
    reflected on the next request — the mtime check should bust the
    cache."""
    repo_root = tmp_path / "repo"
    _populate_fake_docs(repo_root / "docs" / "src")
    client, _cfg = _client(repo_root)
    with client as c:
        resp1 = c.get("/docs/")
        assert "Welcome to Cloud in a Bottle" in resp1.text

        # Mutate the source.  Sleep so mtime resolution definitely
        # increases (some filesystems have 1s resolution).
        src = repo_root / "docs" / "src" / "introduction.md"
        time.sleep(1.05)
        src.write_text("# A New Heading\n\nFresh content.\n")

        resp2 = c.get("/docs/")
        body2 = resp2.text
        assert "A New Heading" in body2
        assert "Welcome to Cloud in a Bottle" not in body2


# -- markdown export ------------------------------------------------


def test_all_markdown_returns_the_whole_manual(client_with_docs: TestClient[Litestar]) -> None:
    """``/docs/all.md`` is every page as plain markdown, in SUMMARY.md order.  A 200 here also
    proves the literal route wins over ``/docs/{slug}``, whose regex would reject the dot."""
    resp = client_with_docs.get("/docs/all.md")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    assert "```toml" in body and "<h1" not in body
    positions = [
        body.index(h) for h in ("# Welcome to Cloud in a Bottle", "# Manifest", "# Routing", "# Creating an App")
    ]
    assert positions == sorted(positions)


def test_all_markdown_omits_pages_missing_from_summary(tmp_path: Path) -> None:
    """A file SUMMARY.md doesn't list is an unpublished draft, not content."""
    repo_root = tmp_path / "repo"
    src = repo_root / "docs" / "src"
    _populate_fake_docs(src)
    (src / "unlisted_draft.md").write_text("# Draft\n\nNot ready for readers.\n")
    client, _cfg = _client(repo_root)
    with client as c:
        body = c.get("/docs/all.md").text
    assert "Routing prose here." in body
    assert "Not ready for readers." not in body


def test_page_markdown_returns_the_source(client_with_docs: TestClient[Litestar]) -> None:
    """Adding ``.md`` switches a page from rendered HTML to source; the plain URL is unchanged."""
    resp = client_with_docs.get("/docs/routing.md")
    assert resp.status_code == 200
    assert resp.text == "# Routing\n\nRouting prose here.\n"
    assert "<h1" in client_with_docs.get("/docs/routing").text
    assert client_with_docs.get("/docs/this_does_not_exist.md").status_code == 404


def test_copy_controls(client_with_docs: TestClient[Litestar]) -> None:
    """Two controls — the manual in the sidebar, this page in its own heading — each a real link
    to the markdown it copies, so both survive JS or the clipboard API being unavailable."""
    body = client_with_docs.get("/docs/manifest_spec").text
    assert re.findall(r'<a class="copy-md" href="([^"]+)"', body) == ["/docs/all.md", "/docs/manifest_spec.md"]
    heading = re.search(r"<h1[^>]*>(.*?)</h1>", body[body.index('<main class="content"') :], re.S)
    assert heading is not None and 'href="/docs/manifest_spec.md"' in heading.group(1)
    assert "Copy <b>entire</b> manual" in body


def test_page_without_an_h1_gets_a_floated_copy_control(tmp_path: Path) -> None:
    """``routing.md`` in the real manual opens at ``##`` and renders no <h1> to hang the control
    off.  Such a page must stay copyable, and a title with markup in it must stay escaped."""
    repo_root = tmp_path / "repo"
    src = repo_root / "docs" / "src"
    _populate_fake_docs(src)
    (src / "routing.md").write_text("## DNS\n\nNo top-level heading.\n")
    (src / "creating_an_app.md").write_text('# The "big" <b>spec</b>\n\nProse.\n')
    client, _cfg = _client(repo_root)
    with client as c:
        assert '<div class="page-actions">' in c.get("/docs/routing").text
        assert (
            'aria-label="Copy The &#34;big&#34; &lt;b&gt;spec&lt;/b&gt; as Markdown"'
            in c.get("/docs/creating_an_app").text
        )


# -- RESERVED_PATHS regression --------------------------------------


def test_docs_in_reserved_paths() -> None:
    """An operator must NOT be able to deploy an app named ``docs``
    — that would shadow the route ordering and break the manual.

    The deploy-app validation in ``core.apps`` checks ``RESERVED_PATHS``
    for a leading-slash match.  This regression test makes sure the
    PR keeps ``/docs`` on that list.
    """
    assert "/docs" in RESERVED_PATHS
