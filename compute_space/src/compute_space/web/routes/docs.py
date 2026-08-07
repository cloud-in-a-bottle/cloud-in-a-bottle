"""Server-side-rendered markdown route serving the OpenHost manual.

We read ``docs/src/*.md`` directly off the running checkout, render
through ``markdown-it-py`` + ``pygments``, and inject into a Jinja
template that matches the OpenHost dashboard's visual language.
There is no build step: ``git pull`` is enough to ship doc changes.

The rendered page carries the same top navigation header as the rest
of the compute space (Dashboard / Docs / Deploy App / ...), so the
manual reads as an in-space page rather than a standalone site — the
Docs nav link stays in the same tab.  The nav tab list + active-tab
highlighter are shared with ``layout.html`` via the ``_nav_header.html``
partial (this route's inline template ``{% include %}``s it through a
Jinja ``Environment`` whose loader points at the templates dir), so the
nav can't drift from the rest of the UI.  The docs page's own
body/sidebar layout stays inline here.

Why server-side render instead of mdBook (or any other static-site
generator)?

  * **No build step.**  ``docs/book/`` and ``mdbook`` binary disappear
    entirely.  The route reads the same ``docs/src/*.md`` files
    that show up in ``git diff``, so the docs an operator sees on
    ``/docs/`` are exactly what's in the commit compute_space is
    running.  This eliminates a whole class of bugs where the
    rendered HTML is stale relative to the running code (operator
    forgot to run ``mdbook build`` after ``git pull``, CI artifact
    is from a different commit than the running version, etc.).
  * **Smaller surface area.**  No ~5 MB Rust binary on every
    instance, no CI workflow to maintain, no ``book.toml`` to keep
    in sync, no theme/CSS-override directory.  Markdown rendering
    is pure-Python and pulls in ``markdown-it-py`` (already a
    transitive dep) plus ``mdit-py-plugins`` and ``pygments``
    (also already present via test deps).
  * **Easier to extend.**  Custom rendering — admonitions, mermaid
    diagrams, cross-references — becomes plain Python rather than
    mdBook preprocessor plugins.
  * **Reasonable performance.**  Each page renders in <20 ms on
    cold cache; subsequent hits are mtime-cached.  Our corpus is
    eight markdown files, so the cache footprint is trivial.

What we lose vs. mdBook (and why it doesn't matter much here):

  * **Client-side full-text search.**  We don't ship a search box.
    Browser Ctrl-F per page is sufficient for an 8-page manual.
    If the corpus grows to 50+ pages we can revisit by building
    a JSON search index at startup; the rendering pipeline already
    has parsed-AST access for free.
  * **Theme picker.**  Always renders in the dashboard's light
    palette + a ``prefers-color-scheme: dark`` media query.  No
    runtime theme switcher.

Path-traversal safety: ``_resolve_doc_path`` requires the resolved
absolute path to live under ``docs/src/`` and refuses anything else.
This is the only security-sensitive surface in the route.
"""

from __future__ import annotations

import re
import threading
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment
from jinja2 import FileSystemLoader
from litestar import MediaType
from litestar import Response
from litestar import Router
from litestar import get
from litestar.exceptions import NotFoundException
from litestar.params import FromPath
from markdown_it import MarkdownIt
from markupsafe import escape as html_escape
from mdit_py_plugins.anchors import anchors_plugin
from mdit_py_plugins.attrs import attrs_plugin
from mdit_py_plugins.tasklists import tasklists_plugin
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

from compute_space.config import get_config
from compute_space.core.auth.auth import read_owner_username
from compute_space.core.domains import Domain
from compute_space.core.domains import primary_domain_or_none
from compute_space.core.git_ops import SOURCE_URL
from compute_space.core.logging import logger
from compute_space.db import get_db

# ─── Filesystem layout ──────────────────────────────────────────────


def _docs_src_dir() -> Path:
    """Where the markdown sources live.

    Resolves via ``get_config().openhost_repo_path`` so tests can
    inject a fixture directory by overriding the config.
    """
    return get_config().openhost_repo_path / "docs" / "src"


def _services_dir() -> Path:
    """Where the bundled service specs live, one ``<name>/openapi.yaml`` each."""
    return get_config().openhost_repo_path / "services"


def _openapi_spec_path() -> Path:
    """The committed OpenAPI document, generated from the route handlers by
    ``pixi run -e dev generate-openapi`` and kept in sync by a test."""
    return get_config().openhost_repo_path / "compute_space" / "openapi.yaml"


_DEFAULT_INDEX = "introduction"
_SUMMARY_FILENAME = "SUMMARY.md"


# ─── Space navigation header ────────────────────────────────────────


def _space_display_name() -> str | None:
    """The instance name shown in the nav header — owner username if
    set, else the zone subdomain, mirroring ``layout.html``.

    Every lookup is wrapped defensively: the docs template renders
    through a standalone Jinja environment (not the app engine), so the
    header must degrade gracefully rather than break the manual if the
    DB or the active primary domain is unavailable (e.g. pre-setup, or a
    route-level test harness).
    """
    owner: str | None = None
    primary: Domain | None = None
    try:
        with closing(get_db()) as db:
            owner = read_owner_username(db)
            primary = primary_domain_or_none(db)
    except Exception as exc:
        # Benign pre-setup / uninitialised-DB paths (and the route-level
        # test harness) land here; the header just falls back to the zone
        # name.  Debug-level so we don't spam ERROR logs for an expected,
        # non-fatal condition.
        logger.debug("could not read docs header identity: {}", exc)
    if owner:
        return owner
    return primary.name.split(".")[0] if primary else None


# ─── Markdown engine ────────────────────────────────────────────────

# Attributes a chapter may set on a link.  Anything else and markdown-it leaves the
# whole ``{...}`` as visible literal text, so a typo shows up rather than passing through.
_MD_LINK_ATTRS = ("target", "rel")


def _build_md() -> MarkdownIt:
    """Construct a shared markdown renderer: ``gfm-like`` plus heading anchors,
    tasklists, and Pygments-highlighted fences. Stateless and thread-safe.

    Raw HTML stays inert, so stray angle brackets in the prose render as text.
    """
    # ``linkify=False``: bare URLs in the prose are NOT auto-linked.
    # The alternative requires the optional ``linkify-it-py`` dep,
    # and our manual already wraps its handful of URLs in proper
    # ``[text](url)`` syntax — auto-linking would just bring in a
    # new transitive dep for negligible UX gain.
    md = MarkdownIt("gfm-like", {"html": False, "linkify": False, "typographer": True})
    md.use(anchors_plugin, max_level=4, permalink=False)
    md.use(tasklists_plugin, enabled=True)
    # ``[text](url){target=_blank}`` — the only way to leave the manual's tab, since
    # raw HTML is inert.  Allowlisted so markdown can't set arbitrary attributes.
    md.use(attrs_plugin, allowed=_MD_LINK_ATTRS)
    md.add_render_rule("fence", _render_fence_with_pygments)
    return md


def _render_fence_with_pygments(
    self: object,  # noqa: ARG001 — markdown-it's render-rule signature passes the renderer as the first arg; we don't need it.
    tokens: list[object],
    idx: int,
    options: dict[str, object],  # noqa: ARG001
    env: object,  # noqa: ARG001
) -> str:
    """Render a ```fenced``` code block through Pygments.

    The info string after ``` becomes the lexer name (e.g. ``toml``,
    ``python``, ``bash``).  Unknown lexers fall back to no
    highlighting — we emit a plain ``<pre><code>...</code></pre>``
    that mdBook-style theme CSS can colour as a generic block.

    The render-rule signature is fixed by markdown-it-py's plugin
    protocol; we ignore the ``self``, ``options``, and ``env``
    args (the only inputs we need are the token list + index).
    """
    token = tokens[idx]
    code = getattr(token, "content", "")
    info = (getattr(token, "info", "") or "").strip().split(None, 1)
    lang = info[0] if info else ""
    if lang:
        try:
            lexer = get_lexer_by_name(lang, stripall=False)
        except ClassNotFound:
            lexer = None
    else:
        lexer = None
    if lexer is None:
        # No language tag (or unrecognised) — emit a plain block.
        # ``html_escape`` (markupsafe) prevents the code contents
        # from being interpreted as HTML markup.
        return f"<pre><code>{html_escape(code)}</code></pre>\n"
    formatter = HtmlFormatter(nowrap=False, cssclass="codehilite")
    # ``pygments.highlight`` is typed ``Any`` upstream; explicit
    # cast keeps mypy strict-no-any-return happy.
    rendered: str = highlight(code, lexer, formatter)
    return rendered


# Pygments CSS — embedded in the response so we don't need a separate route for it.
# A dark style, because fenced code renders on the dashboard's dark .log-output panel;
# a light one puts near-white tokens on near-white background.
PYGMENTS_CSS = HtmlFormatter(style="native").get_style_defs(".codehilite")


_MD = _build_md()


# ─── Sidebar / SUMMARY.md parsing ───────────────────────────────────


@dataclass(frozen=True)
class _SidebarLink:
    """One entry in the rendered sidebar."""

    title: str
    slug: str  # filename without .md, e.g. "manifest_spec"


@dataclass(frozen=True)
class _SidebarSection:
    """A header + the links beneath it."""

    title: str  # may be empty (for the unsectioned intro entry)
    links: tuple[_SidebarLink, ...]


_SUMMARY_LINE_RE = re.compile(r"^\s*[-*]\s+\[(?P<title>[^\]]+)\]\((?P<href>[^)]+)\)\s*$")
_SUMMARY_INTRO_RE = re.compile(r"^\s*\[(?P<title>[^\]]+)\]\((?P<href>[^)]+)\)\s*$")
_SUMMARY_HEADER_RE = re.compile(r"^#\s+(?P<title>.+?)\s*$")


def _parse_summary(summary_text: str) -> tuple[_SidebarSection, ...]:
    """Parse ``SUMMARY.md`` into a list of sections.

    Recognised line shapes:
      ``# Title``             → starts a new section
      ``[Foo](./foo.md)``     → unsectioned intro link
      ``- [Foo](./foo.md)``   → link inside the current section

    Everything else (blank lines, comments, etc.) is ignored.
    Slugs are derived by stripping the leading ``./`` and trailing
    ``.md`` from the link target.  Links pointing at anything other
    than a sibling ``.md`` file are dropped — we don't try to
    follow external URLs from the sidebar.
    """
    sections: list[_SidebarSection] = []
    intro_links: list[_SidebarLink] = []
    current_title: str | None = None
    current_links: list[_SidebarLink] = []

    def _flush() -> None:
        if current_title is None and not current_links:
            return
        if current_title is None:
            return  # intro entries handled below
        sections.append(_SidebarSection(title=current_title, links=tuple(current_links)))

    for raw_line in summary_text.splitlines():
        line = raw_line.rstrip()
        if (
            not line.strip()
            or line.lstrip().startswith("#")
            and line.lstrip().lstrip("#").strip().lower() == "summary"
        ):
            continue
        m_header = _SUMMARY_HEADER_RE.match(line)
        if m_header:
            _flush()
            current_title = m_header.group("title")
            current_links = []
            continue
        m_link = _SUMMARY_LINE_RE.match(line)
        if m_link:
            slug = _slug_from_href(m_link.group("href"))
            if slug:
                current_links.append(_SidebarLink(title=m_link.group("title"), slug=slug))
            continue
        m_intro = _SUMMARY_INTRO_RE.match(line)
        if m_intro and current_title is None:
            slug = _slug_from_href(m_intro.group("href"))
            if slug:
                intro_links.append(_SidebarLink(title=m_intro.group("title"), slug=slug))
            continue

    _flush()
    # Intro section (unsectioned links at the top) prepends to the list.
    if intro_links:
        sections.insert(0, _SidebarSection(title="", links=tuple(intro_links)))
    return tuple(sections)


def _slug_from_href(href: str) -> str | None:
    """Convert a SUMMARY.md href like ``./manifest_spec.md`` into a
    slug like ``manifest_spec``.  Returns None for anything that's
    not a sibling .md file (e.g. external URLs)."""
    href = href.strip()
    if "://" in href or href.startswith("/"):
        return None
    if href.startswith("./"):
        href = href[2:]
    if not href.endswith(".md"):
        return None
    name = href[:-3]
    # Reject anything with a directory component — our docs are flat.
    if "/" in name:
        return None
    return name


# ─── Mtime-keyed render cache ───────────────────────────────────────


_render_cache_lock = threading.Lock()
_render_cache: dict[str, tuple[float, str]] = {}


def _cached_render(slug: str, path: Path) -> str:
    """Render ``path`` to HTML, caching by file mtime so we don't
    re-render unchanged files on every request.

    The cache is process-local and bounded by the number of doc
    files (~tens).  We use mtime rather than a content hash because
    the docs source is on local disk under our control — mtime is
    free, accurate, and survives file replacements via rename.
    """
    mtime = path.stat().st_mtime
    with _render_cache_lock:
        cached = _render_cache.get(slug)
        if cached and cached[0] == mtime:
            return cached[1]
    html = _MD.render(path.read_text(encoding="utf-8"))
    html = _rewrite_internal_links(html)
    with _render_cache_lock:
        _render_cache[slug] = (mtime, html)
    return html


def _rewrite_internal_links(html: str) -> str:
    """Rewrite ``href="./foo.md"`` (and ``foo.md``) in rendered
    HTML to point at our route paths (``/docs/foo``) instead.

    Without this rewrite the sibling-page links inside the
    rendered markdown would 404 — markdown-it doesn't know about
    our URL scheme, so a link in ``introduction.md`` written as
    ``[manifest spec](./manifest_spec.md)`` would emit
    ``href="./manifest_spec.md"`` verbatim.

    We only rewrite hrefs that look like flat ``.md`` files (no
    ``://`` schemes, no leading ``/``).  External links and
    in-page fragments (``#section``) are left alone.
    """

    def _repl(match: re.Match[str]) -> str:
        href = match.group(1)
        anchor = ""
        if "#" in href:
            href, _, anchor = href.partition("#")
            anchor = "#" + anchor
        if "://" in href or href.startswith("/") or not href.endswith(".md"):
            return match.group(0)
        if href.startswith("./"):
            href = href[2:]
        slug = href[:-3]
        if "/" in slug:
            return match.group(0)
        return f'href="/docs/{slug}{anchor}"'

    return re.sub(r'href="([^"]+)"', _repl, html)


# ─── Path resolution + safety ───────────────────────────────────────


_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _resolve_doc_path(slug: str) -> Path:
    """Resolve a sidebar slug to an absolute filesystem path,
    refusing anything that doesn't live under ``docs/src/``.

    The slug regex (``[A-Za-z0-9_-]+``) is already strict enough
    to reject anything pathological, but we still resolve the
    final path and assert it's under the docs dir as
    defence in depth.
    """
    if not _SLUG_RE.match(slug):
        raise NotFoundException()
    src = _docs_src_dir()
    candidate = (src / f"{slug}.md").resolve()
    try:
        candidate.relative_to(src.resolve())
    except ValueError as e:
        raise NotFoundException() from e
    if not candidate.is_file():
        raise NotFoundException()
    return candidate


# ─── HTML template ──────────────────────────────────────────────────

# Inline Jinja template.  Kept in this module rather than a
# separate file so the docs feature is self-contained: one .py file
# is the entire serving surface.  The CSS matches the dashboard's
# system-font stack, #36c accent, #ddd borders, #f5f5f5 muted bg.
_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="robots" content="noindex">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ page_title }} - OpenHost Manual</title>
  <style>
    /* The docs page deliberately reuses layout.html's exact palette and
       typography (hardcoded light colours, #222 text on #fff, #ddd
       borders, #36c links, #f5f5f5 table headers) rather than its own
       CSS-variable theme.  layout.html has no dark-mode media query, so
       neither does this page — otherwise a dark-preference browser would
       flip from light (Dashboard) to dark (Docs) mid-navigation.  Keeping
       the two in lockstep is the whole point: the manual should look like
       just another in-space page. */
    /* Match layout.html: always reserve the scrollbar gutter so the centred
       column doesn't shift sideways when navigating to/from a page whose height
       toggles the vertical scrollbar. Progressive enhancement — unsupported
       browsers simply ignore it. */
    html { scrollbar-gutter: stable; }
    body {
      font-family: -apple-system, system-ui, sans-serif;
      color: #222;
      background: #fff;
      margin: 0;
    }
    /* The docs layout is pinned to the same centred column as every other
       in-space page (layout.html uses max-width:960px + 1em side padding),
       so navigating between Dashboard and Docs doesn't jump the header
       wider or shift it sideways.  The sidebar's left edge lines up with
       the nav header / title above it. */
    .layout {
      display: flex;
      max-width: 960px;
      margin: 0 auto;
      padding: 0 1em;
    }
    aside.sidebar {
      width: 200px;
      flex-shrink: 0;
      border-right: 1px solid #e8e8e8;
      /* No left padding so the sidebar text lines up with the nav header
         and title above it (both sit at the .layout's 1em left edge). */
      padding: 1.5em 1em 1.5em 0;
      box-sizing: border-box;
      font-size: 0.95em;
    }
    aside.sidebar h1 {
      font-size: 1.05em;
      margin: 0 0 1em;
      padding: 0;
    }
    aside.sidebar h1 a { color: #222; text-decoration: none; }
    aside.sidebar .section { margin: 1.2em 0; }
    aside.sidebar .section-title {
      font-size: 0.8em;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: #888;
      margin: 0 0 0.4em;
    }
    aside.sidebar ul { list-style: none; margin: 0; padding: 0; }
    aside.sidebar li { margin: 0.25em 0; }
    aside.sidebar li a {
      color: #222;
      text-decoration: none;
      display: block;
      padding: 0.2em 0.4em;
      border-radius: 3px;
    }
    aside.sidebar li a:hover { background: #f0f0f0; }
    aside.sidebar li a.active {
      color: #36c;
      font-weight: bold;
      background: #e8f0fe;
    }
    /* line-height lives on the prose column only (like the dashboard's
       default body line-height) so the space-title h1 above renders at
       the same height as layout.html's <h1> — otherwise the nav row would
       sit lower here than on the dashboard and appear to jump on nav. */
    main.content {
      flex: 1;
      padding: 1.5em 0 2em 1.5em;
      box-sizing: border-box;
      min-width: 0;
      line-height: 1.55;
    }
    main.content h1 { margin-top: 0; font-size: 1.7em; }
    main.content h2 { margin-top: 2em; font-size: 1.3em; }
    main.content h3 { margin-top: 1.5em; font-size: 1.1em; }
    main.content a { color: #36c; text-decoration: none; }
    main.content a:hover { text-decoration: underline; }
    main.content table {
      border-collapse: collapse;
      width: 100%;
      margin: 1em 0;
    }
    main.content th, main.content td {
      border: 1px solid #ddd;
      padding: 0.5em 0.8em;
      text-align: left;
    }
    main.content th { background: #f5f5f5; font-weight: bold; }
    main.content blockquote {
      margin: 1em 0;
      padding: 0.4em 1em;
      border-left: 4px solid #ddd;
      background: #fafafa;
      color: #666;
    }
    main.content code {
      background: #f5f5f5;
      color: #c0392b;
      padding: 0.1em 0.35em;
      border-radius: 3px;
      font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
      font-size: 0.9em;
    }
    /* Code blocks reuse the dashboard's .log-output panel styling
       (dark #1e1e1e panel, #d4d4d4 text) so fenced code looks the same
       as the log output shown elsewhere in the space. */
    main.content pre {
      background: #1e1e1e;
      color: #d4d4d4;
      padding: 1em;
      border-radius: 4px;
      overflow-x: auto;
      font-size: 0.85em;
      line-height: 1.45;
    }
    main.content pre code {
      background: transparent;
      color: inherit;
      padding: 0;
      border-radius: 0;
      font-size: 1em;
    }
    /* Pygments wraps a highlighted fence in <div class="codehilite"> around the <pre>
       and paints that wrapper; the <pre> stays the panel, so the wrapper draws nothing. */
    main.content .codehilite { background: transparent; }
    main.content ul, main.content ol { padding-left: 1.5em; }
    main.content hr { border: 0; border-top: 1px solid #ddd; margin: 2em 0; }
    main.content img { max-width: 100%; }
    .footer-nav {
      display: flex;
      justify-content: space-between;
      margin-top: 3em;
      padding-top: 1em;
      border-top: 1px solid #ddd;
      font-size: 0.9em;
    }
    .footer-nav a { color: #36c; text-decoration: none; }
    /* Space navigation header — mirrors layout.html so the docs page keeps
       the same top nav (and header geometry) as the rest of the compute
       space.  The h1 uses the browser-default h1 size/margins (2em, 0.67em)
       exactly like layout.html's bare <h1>, so the title + nav land at the
       same vertical position on both pages. */
    .space-header { max-width: 960px; margin: 2em auto 0; padding: 0 1em; }
    .space-header h1.space-title { font-size: 2em; font-weight: bold; margin: 0.67em 0; }
    nav#main-nav { display: flex; align-items: flex-end; gap: 0.25em; border-bottom: 1px solid #e8e8e8; }
    nav#main-nav .nav-tab {
      display: inline-block; padding: 0.4em 1em; border: 1px solid transparent;
      border-bottom: none; border-radius: 4px 4px 0 0; text-decoration: none;
      color: #444; background: transparent;
    }
    nav#main-nav .nav-tab:hover { background: #f0f0f0; color: #222; border-color: #ddd; }
    nav#main-nav .nav-tab.active {
      background: #fff; border-color: #ddd; color: #222;
      margin-bottom: -1px; padding-bottom: calc(0.4em + 1px);
    }
    {{ pygments_css }}
  </style>
</head>
<body>
  <header class="space-header">
    <h1 class="space-title">{% if display_name %}{{ display_name }}'s personal compute space{% else %}OpenHost{% endif %}</h1>
    {% include "_nav_header.html" %}
  </header>
  <div class="layout">
    <aside class="sidebar">
      <h1><a href="/docs/">OpenHost Manual</a></h1>
      {% for section in sections %}
        <div class="section">
          {% if section.title %}<div class="section-title">{{ section.title }}</div>{% endif %}
          <ul>
            {% for link in section.links %}
              <li><a href="/docs/{{ link.slug }}"
                     {% if link.slug == current_slug %}class="active"{% endif %}>{{ link.title }}</a></li>
            {% endfor %}
          </ul>
        </div>
      {% endfor %}
    </aside>
    <main class="content">
      {{ content_html | safe }}
      {% if prev_link or next_link %}
        <div class="footer-nav">
          <div>{% if prev_link %}← <a href="/docs/{{ prev_link.slug }}">{{ prev_link.title }}</a>{% endif %}</div>
          <div>{% if next_link %}<a href="/docs/{{ next_link.slug }}">{{ next_link.title }}</a> →{% endif %}</div>
        </div>
      {% endif %}
    </main>
  </div>
</body>
</html>
"""


# The OpenAPI browsers are not manual chapters: Redoc renders its own full-window
# three-pane layout with its own sidebar, so it gets the bare document — no space
# header, no nav tabs, no manual sidebar.  Reached by link from the prose chapters.
_REFERENCE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="robots" content="noindex">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ page_title }}</title>
  <style>
    html, body { margin: 0; padding: 0; }
    body { font-family: -apple-system, system-ui, sans-serif; color: #222; background: #fff; }
    .service-spec-title { margin: 2.5em 0 0 1.5rem; font-size: 1.3em; }
  </style>
</head>
<body>
  <div id="{{ container_id }}"></div>
  <script src="/static/vendor/redoc.js"></script>
  <script>{{ init_js | safe }}</script>
</body>
</html>
"""

# Each browser's init script.  Both degrade to a message pointing at the raw
# document if the pinned Redoc bundle is missing (see web/vendor_assets.py).
_API_REFERENCE_JS = """
  (() => {
    const host = document.getElementById("redoc");
    if (typeof Redoc === "undefined") {
      host.textContent = "Could not load the spec browser. The raw document is at /docs/openapi.yaml.";
      return;
    }
    Redoc.init(
      "/docs/openapi.yaml",
      {hideDownloadButton: true, expandResponses: "200,201", nativeScrollbars: true},
      host
    );
  })();
"""

_SERVICE_REFERENCE_JS = """
  (async () => {
    const host = document.getElementById("service-specs");
    if (typeof Redoc === "undefined") {
      host.textContent = "Could not load the spec browser. The raw documents are at /docs/services/<name>/openapi.yaml.";
      return;
    }
    let names;
    try {
      names = await (await fetch("/docs/services")).json();
    } catch (e) {
      host.textContent = "Could not list the bundled services.";
      return;
    }
    if (!names.length) {
      host.textContent = "No bundled service specs found in this checkout.";
      return;
    }
    for (const name of names) {
      const title = document.createElement("h2");
      title.className = "service-spec-title";
      title.textContent = name;
      host.appendChild(title);
      const pane = document.createElement("div");
      host.appendChild(pane);
      Redoc.init(`/docs/services/${name}/openapi.yaml`, {hideDownloadButton: true, nativeScrollbars: true}, pane);
    }
  })();
"""


# Build the docs template through a Jinja Environment whose loader points at
# the shared templates directory, so ``{% include "_nav_header.html" %}`` pulls
# in the same nav partial the rest of the compute space uses (rather than
# duplicating the tab list + highlighter here).  The docs page's HTML body
# itself is still an inline string — only the shared partial is loaded from
# disk — keeping the route's serving surface self-contained.
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_JINJA_ENV = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), autoescape=True)
_COMPILED_TEMPLATE = _JINJA_ENV.from_string(_TEMPLATE)
_COMPILED_REFERENCE_TEMPLATE = _JINJA_ENV.from_string(_REFERENCE_TEMPLATE)


def _flatten_links(sections: tuple[_SidebarSection, ...]) -> list[_SidebarLink]:
    """Flatten the sidebar into a single ordered list — used for
    prev/next navigation at the bottom of each page."""
    out: list[_SidebarLink] = []
    for section in sections:
        out.extend(section.links)
    return out


def _find_neighbors(slug: str, ordered: list[_SidebarLink]) -> tuple[_SidebarLink | None, _SidebarLink | None]:
    """Locate ``slug`` in ``ordered`` and return its (prev, next)
    siblings.  Both may be ``None`` (first or last page)."""
    for i, link in enumerate(ordered):
        if link.slug == slug:
            prev_l = ordered[i - 1] if i > 0 else None
            next_l = ordered[i + 1] if i < len(ordered) - 1 else None
            return prev_l, next_l
    return None, None


def _read_summary() -> tuple[_SidebarSection, ...]:
    """Read + parse ``SUMMARY.md`` if present.  Falls back to a
    single ungrouped list of every ``.md`` file in ``docs/src/``
    when the SUMMARY file is missing (e.g. tests that drop fixture
    markdown in directly)."""
    src = _docs_src_dir()
    summary_path = src / _SUMMARY_FILENAME
    if summary_path.is_file():
        return _parse_summary(summary_path.read_text(encoding="utf-8"))
    # Fallback: list every .md alphabetically.
    links = []
    for p in sorted(src.glob("*.md")):
        slug = p.stem
        if slug == _SUMMARY_FILENAME.removesuffix(".md"):
            continue
        # Use the first-line "# Title" if present, otherwise the slug.
        first_line = p.read_text(encoding="utf-8").splitlines()[0:1]
        title = (
            first_line[0].lstrip("# ").strip()
            if first_line and first_line[0].startswith("# ")
            else slug.replace("_", " ").title()
        )
        links.append(_SidebarLink(title=title, slug=slug))
    return (_SidebarSection(title="", links=tuple(links)),)


def _page_title(slug: str, path: Path) -> str:
    """Use the page's first ``# H1`` as the title, falling back to
    the slug with underscores converted to spaces."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return slug.replace("_", " ").title()


# ─── Routes ─────────────────────────────────────────────────────────


@get("/docs", sync_to_thread=False)
def docs_index() -> Response[str]:
    """The docs landing page, served from ``introduction.md``.

    Litestar normalises trailing slashes during routing, so this single
    handler serves both ``/docs`` and ``/docs/``.

    Falls back to a 503 (with a clear, operator-actionable
    message) if the markdown source dir is missing — that
    shouldn't happen in production but is what tests use to
    verify the missing-dir path.
    """
    return _render_doc(_DEFAULT_INDEX)


@get("/docs/openapi.yaml", sync_to_thread=False)
def docs_openapi_yaml() -> Response[str]:
    """The OpenAPI document the ``api_reference.md`` chapter renders, and the only URL
    it is served from. Read off the checkout like the markdown is, so it
    matches the commit this instance is running."""
    path = _openapi_spec_path()
    if not path.is_file():
        return Response(
            content=(
                f"The OpenAPI spec is missing on this installation. Expected: {path}. "
                "This usually means the OpenHost code checkout is incomplete; "
                "reinstalling the openhost service should fix it."
            ),
            status_code=503,
            media_type=MediaType.TEXT,
        )
    return Response(content=path.read_text(encoding="utf-8"), media_type="application/yaml")


@get("/docs/services", sync_to_thread=False)
def docs_services_index() -> list[str]:
    """Names of the bundled services that ship an OpenAPI spec. The
    ``service_reference.md`` chapter reads this to decide what to render, so
    adding ``services/<name>/openapi.yaml`` needs no doc edit."""
    services = _services_dir()
    if not services.is_dir():
        return []
    return sorted(p.parent.name for p in services.glob("*/openapi.yaml") if _SLUG_RE.match(p.parent.name))


@get("/docs/services/{name:str}/openapi.yaml", sync_to_thread=False)
def docs_service_spec(name: FromPath[str]) -> Response[str]:
    """Serve ``services/<name>/openapi.yaml`` off the checkout.

    Same containment rule as the markdown route: the resolved path has to sit
    under ``services/`` or it's a 404."""
    if not _SLUG_RE.match(name):
        raise NotFoundException()
    services = _services_dir()
    candidate = (services / name / "openapi.yaml").resolve()
    if not candidate.is_file() or services.resolve() not in candidate.parents:
        raise NotFoundException()
    return Response(content=candidate.read_text(encoding="utf-8"), media_type="application/yaml")


def _reference_page(page_title: str, container_id: str, init_js: str) -> Response[str]:
    html = _COMPILED_REFERENCE_TEMPLATE.render(page_title=page_title, container_id=container_id, init_js=init_js)
    return Response(content=html, media_type=MediaType.HTML)


@get("/docs/reference/api", sync_to_thread=False)
def docs_api_reference() -> Response[str]:
    """The OpenAPI browser for the instance's own HTTP API, linked from ``api.md``."""
    return _reference_page("HTTP API Reference", "redoc", _API_REFERENCE_JS)


@get("/docs/reference/services", sync_to_thread=False)
def docs_service_reference() -> Response[str]:
    """The OpenAPI browser for every bundled service spec, linked from ``bundled_services.md``."""
    return _reference_page("Bundled Service Reference", "service-specs", _SERVICE_REFERENCE_JS)


@get("/docs/{slug:str}", sync_to_thread=False)
def docs_slug(slug: FromPath[str]) -> Response[str]:
    """Serve ``docs/src/<slug>.md`` rendered to HTML.

    ``slug`` is the markdown filename without extension.  Anything
    not matching the slug regex (alphanumerics + ``-`` + ``_``)
    returns a 404 — protects against path traversal, weird unicode,
    and the implicit ``./``/``../`` shenanigans.
    """
    return _render_doc(slug)


def _render_doc(slug: str) -> Response[str]:
    src = _docs_src_dir()
    if not src.is_dir():
        return Response(
            content=(
                "The OpenHost docs source directory is missing on this installation. "
                f"Expected: {src}.  This usually means the OpenHost code checkout is "
                "incomplete; reinstalling the openhost service should fix it."
            ),
            status_code=503,
            media_type=MediaType.TEXT,
        )
    path = _resolve_doc_path(slug)
    sections = _read_summary()
    content_html = _cached_render(slug, path)
    ordered = _flatten_links(sections)
    prev_l, next_l = _find_neighbors(slug, ordered)
    page_title = _page_title(slug, path)
    html = _COMPILED_TEMPLATE.render(
        sections=sections,
        current_slug=slug,
        content_html=content_html,
        page_title=page_title,
        prev_link=prev_l,
        next_link=next_l,
        pygments_css=PYGMENTS_CSS,
        display_name=_space_display_name(),
        source_url=SOURCE_URL,
    )
    return Response(content=html, media_type=MediaType.HTML)


docs_routes = Router(
    path="/",
    route_handlers=[
        docs_index,
        docs_openapi_yaml,
        docs_services_index,
        docs_service_spec,
        docs_api_reference,
        docs_service_reference,
        docs_slug,
    ],
    include_in_schema=False,
)
