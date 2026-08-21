"""Server-side-rendered markdown route serving the Cloud in a Bottle manual.

We read ``docs/src/*.md`` directly off the running checkout, render
through ``markdown-it-py`` + ``pygments``, and inject into a Jinja
template that matches the Cloud in a Bottle dashboard's visual language.
There is no build step: ``git pull`` is enough to ship doc changes.

The rendered page carries the same top navigation header as the rest
of the compute space (Dashboard / Docs / Deploy App / ...), so the
manual reads as an in-space page rather than a standalone site — the
Docs nav link stays in the same tab.  The nav tab list + active-tab
icons are shared with ``layout.html`` via the ``_components/icon_nav.html``
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
from compute_space.web.helpers.static import STATIC_DIR
from compute_space.web.helpers.static import make_static_url

# ─── Filesystem layout ──────────────────────────────────────────────


def _docs_src_dir() -> Path:
    """Where the markdown sources live.

    Resolves via ``get_config().openhost_repo_path`` so tests can
    inject a fixture directory by overriding the config.
    """
    return get_config().openhost_repo_path / "docs" / "src"


_DEFAULT_INDEX = "introduction"
_SUMMARY_FILENAME = "SUMMARY.md"

_ALL_MARKDOWN_PATH = "/docs/all.md"


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


def _build_md() -> MarkdownIt:
    """Construct the shared markdown renderer.

    Configured with:
      * ``commonmark`` baseline + GFM-style tables/strikethrough/
        autolinks (via the ``gfm-like`` preset).
      * ``anchors_plugin`` to auto-link ``<h2>``/``<h3>`` headings
        for deep links from the sidebar / external linkers.
      * ``tasklists_plugin`` so ``- [x]`` markdown checklists
        render as proper checkboxes.
      * ``html=False`` so embedded raw HTML in the markdown
        sources is treated as plain text rather than executed
        (defence-in-depth; nothing in our docs uses raw HTML).
      * A custom code-fence renderer that runs the contents
        through Pygments for syntax highlighting.

    The renderer is stateless and thread-safe — a single shared
    instance per process suffices.
    """
    # ``linkify=False``: bare URLs in the prose are NOT auto-linked.
    # The alternative requires the optional ``linkify-it-py`` dep,
    # and our manual already wraps its handful of URLs in proper
    # ``[text](url)`` syntax — auto-linking would just bring in a
    # new transitive dep for negligible UX gain.
    md = MarkdownIt("gfm-like", {"html": False, "linkify": False, "typographer": True})
    md.use(anchors_plugin, max_level=4, permalink=False)
    md.use(tasklists_plugin, enabled=True)
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


# Pygments CSS — embedded in the response so we don't need a
# separate route for it.  Uses the same colour palette
# (light) as the dashboard's .log-output panel.
PYGMENTS_CSS = HtmlFormatter(style="default").get_style_defs(".codehilite")


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
_TEMPLATE = """{% from "_components/icon_nav.html" import icon_nav %}<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="robots" content="noindex">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ page_title }} - Cloud in a Bottle Manual</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible+Mono:wght@500;600;700&display=swap">
  <link rel="stylesheet" href="{{ static_url('css/tokens.css') }}">
  <link rel="stylesheet" href="{{ static_url('css/components.css') }}">
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
       in-space page (--layout-width, shared with layout.html), so navigating
       between Dashboard and Docs doesn't jump the header wider or shift it
       sideways.  The sidebar's left edge lines up with the icon nav / title
       above it. */
    .layout {
      display: flex;
      max-width: var(--layout-width);
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
    main.content pre code, main.content pre .codehilite {
      background: transparent;
      color: inherit;
      padding: 0;
      border-radius: 0;
      font-size: 1em;
    }
    main.content .codehilite pre { background: transparent; padding: 0; }
    main.content .page-actions { float: right; margin-top: 0.2em; }
    main.content ul, main.content ol { padding-left: 1.5em; }
    main.content hr { border: 0; border-top: 1px solid #ddd; margin: 2em 0; }
    main.content img { max-width: 100%; }
    /* Copy controls.  Sized in px, not em, so they render the same next to the 1.05em sidebar
       title and the 1.7em page heading. */
    a.copy-md {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      position: relative;
      width: 22px;
      height: 22px;
      margin-left: 0.3em;
      vertical-align: middle;
      border-radius: 3px;
      color: #aaa;
      text-decoration: none;
      opacity: 0.6;
      transition: opacity 0.12s ease, color 0.12s ease;
    }
    h1:hover a.copy-md, a.copy-md:focus-visible { opacity: 1; }
    a.copy-md:hover { color: #36c; background: #f0f0f0; opacity: 1; }
    a.copy-md.copied { color: #36c; background: transparent; opacity: 1; }
    /* Tooltip.  Not the native ``title`` attribute: it waits about a second, and browsers are
       inconsistent about showing it while the pointer is over the <svg> inside the anchor. */
    a.copy-md .tip {
      position: absolute;
      bottom: calc(100% + 5px);
      left: 50%;
      transform: translateX(-50%);
      padding: 0.35em 0.6em;
      border-radius: 4px;
      background: #333;
      color: #fff;
      font-size: 12px;
      font-weight: normal;
      font-style: normal;
      line-height: 1.2;
      white-space: nowrap;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.12s ease;
    }
    a.copy-md .tip b { font-weight: 700; }
    a.copy-md:hover .tip, a.copy-md:focus-visible .tip { opacity: 1; }
    .footer-nav {
      display: flex;
      justify-content: space-between;
      margin-top: 3em;
      padding-top: 1em;
      border-top: 1px solid #ddd;
      font-size: 0.9em;
    }
    .footer-nav a { color: #36c; text-decoration: none; }
    /* Space navigation header.  The icon row itself is styled by the shared
       components.css linked above, so the docs nav can't drift from the rest
       of the compute space; only the header's own geometry lives here. */
    .space-header { max-width: var(--layout-width); margin: 0 auto; padding: 2em 1em 0; }
    .space-header h1.space-title {
      font-family: var(--font-mono); font-size: 1.15em; font-weight: 500;
      letter-spacing: var(--tracking-title); text-transform: uppercase; margin: 0 0 0.5em;
    }
    .space-header h1.space-title a { color: inherit; text-decoration: none; }
    .space-header h1.space-title a:hover { text-decoration: underline; }
    {{ pygments_css }}
  </style>
</head>
<body>
  <header class="space-header">
    <h1 class="space-title"><a href="/dashboard" title="Back to the dashboard">{% if display_name %}{{ display_name }}'s personal compute space{% else %}Cloud in a Bottle{% endif %}</a></h1>
    {{ icon_nav(source_url) }}
  </header>
  <svg width="0" height="0" style="position: absolute" aria-hidden="true" focusable="false">
    <symbol id="i-copy" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"
            stroke-linecap="round" stroke-linejoin="round">
      <rect x="6" y="6" width="8" height="8" rx="1.5"></rect>
      <path d="M10 6V3.5A1.5 1.5 0 0 0 8.5 2h-5A1.5 1.5 0 0 0 2 3.5v5A1.5 1.5 0 0 0 3.5 10H6"></path>
    </symbol>
    <symbol id="i-check" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"
            stroke-linecap="round" stroke-linejoin="round">
      <path d="M3 8.5 6.5 12 13 4.5"></path>
    </symbol>
  </svg>
  <div class="layout">
    <aside class="sidebar">
      <h1><a href="/docs/">Cloud in a Bottle Manual</a>{{ manual_copy_button | safe }}</h1>
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
  <script>
    (function () {
      // Every control is a plain link to the markdown it copies; this upgrades them in place.
      // Without a clipboard API (insecure context, old browser) they stay plain links.
      if (!navigator.clipboard) { return; }

      function flash(button) {
        var glyph = button.querySelector('use');
        clearTimeout(button.resetTimer);
        glyph.setAttribute('href', '#i-check');
        button.classList.add('copied');
        button.resetTimer = setTimeout(function () {
          glyph.setAttribute('href', '#i-copy');
          button.classList.remove('copied');
        }, 1500);
      }

      document.addEventListener('click', function (event) {
        var button = event.target.closest('a.copy-md');
        if (!button) { return; }
        event.preventDefault();
        fetch(button.href, { headers: { 'Accept': 'text/plain' } })
          .then(function (response) {
            if (!response.ok) { throw new Error('HTTP ' + response.status); }
            return response.text();
          })
          .then(function (text) { return navigator.clipboard.writeText(text); })
          .then(function () { flash(button); })
          .catch(function () { window.location.href = button.href; });
      });
    })();
  </script>
</body>
</html>
"""


# Build the docs template through a Jinja Environment whose loader points at
# the shared templates directory, so ``{% from "_components/icon_nav.html" %}``
# pulls in the same nav the rest of the compute space uses (rather than
# duplicating the icon list here).  The docs page's HTML body itself is still an
# inline string — only the shared partial is loaded from disk — keeping the
# route's serving surface self-contained.
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_JINJA_ENV = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), autoescape=True)
_JINJA_ENV.globals["static_url"] = make_static_url(STATIC_DIR)
_COMPILED_TEMPLATE = _JINJA_ENV.from_string(_TEMPLATE)


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


# ─── Markdown export ────────────────────────────────────────────────

_COPY_ICON = '<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"><use href="#i-copy"></use></svg>'
_MANUAL_TIP = "Copy <b>entire</b> manual"
_PAGE_TIP = "Copy page as Markdown"
_H1_END_RE = re.compile(r"</h1>")

_ALL_MARKDOWN_LEAD = (
    "# Cloud in a Bottle Manual\n"
    "\n"
    "Every page of `docs/src/`, in `SUMMARY.md` reading order, as one document.  Pages are separated\n"
    "by `---` and tagged with their source path; a relative link like `./routing.md` points at\n"
    "another page in this file."
)


def _copy_button(href: str, tip_html: str, aria_label: str) -> str:
    """A copy control.  ``tip_html`` is markup, so it must only ever be a constant from above —
    never a page title or anything else read off disk."""
    return (
        f'<a class="copy-md" href="{html_escape(href)}" aria-label="{html_escape(aria_label)}">'
        f'{_COPY_ICON}<span class="tip" aria-hidden="true">{tip_html}</span></a>'
    )


def _with_page_copy_button(content_html: str, slug: str, page_title: str) -> str:
    """Put the copy control in the page's ``<h1>``, or float it above the prose when the page
    has none (``routing.md`` opens at ``##``)."""
    button = _copy_button(f"/docs/{slug}.md", _PAGE_TIP, f"Copy {page_title} as Markdown")
    html, replaced = _H1_END_RE.subn(f"{button}</h1>", content_html, count=1)
    return html if replaced else f'<div class="page-actions">{button}</div>{content_html}'


def _concatenated_markdown() -> str:
    """Every page joined into one document, in ``SUMMARY.md`` order.  Sources go out verbatim, so
    the export can't drift from what the page routes serve."""
    lead = f"{_ALL_MARKDOWN_LEAD}\n\nSource: {SOURCE_URL}" if SOURCE_URL else _ALL_MARKDOWN_LEAD
    pages: list[str] = [lead]
    for link in _flatten_links(_read_summary()):
        try:
            path = _resolve_doc_path(link.slug)
        except NotFoundException:
            logger.warning("{} lists {}.md, but no such file exists; omitting it", _SUMMARY_FILENAME, link.slug)
            continue
        pages.append(f"<!-- docs/src/{link.slug}.md -->\n\n{path.read_text(encoding='utf-8').strip()}")
    return "\n\n---\n\n".join(pages) + "\n"


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


@get(_ALL_MARKDOWN_PATH, sync_to_thread=False)
def docs_all_markdown() -> Response[str]:
    """The whole manual as one markdown document."""
    missing = _missing_src_response()
    if missing is not None:
        return missing
    return Response(content=_concatenated_markdown(), media_type=MediaType.TEXT)


@get("/docs/{slug:str}", sync_to_thread=False)
def docs_slug(slug: FromPath[str]) -> Response[str]:
    """``/docs/routing`` renders the page; ``/docs/routing.md`` returns its source.

    The suffix is stripped here rather than routed separately because Litestar matches path
    parameters per whole segment: a ``/docs/{slug:str}.md`` handler registers without complaint
    but never matches, since this handler's bare ``{slug}`` claims the segment first.  The slug
    regex still runs after stripping, so ``foo.md.bak`` and ``../secrets.md`` stay 404s.
    """
    if slug.endswith(".md"):
        return _raw_doc(slug.removesuffix(".md"))
    return _render_doc(slug)


def _missing_src_response() -> Response[str] | None:
    """The 503 for a broken checkout, or ``None`` when ``docs/src/`` is present."""
    src = _docs_src_dir()
    if src.is_dir():
        return None
    return Response(
        content=(
            "The Cloud in a Bottle docs source directory is missing on this installation. "
            f"Expected: {src}.  This usually means the Cloud in a Bottle code checkout is "
            "incomplete; reinstalling the Cloud in a Bottle service should fix it."
        ),
        status_code=503,
        media_type=MediaType.TEXT,
    )


def _raw_doc(slug: str) -> Response[str]:
    """One page's markdown source, verbatim."""
    missing = _missing_src_response()
    if missing is not None:
        return missing
    path = _resolve_doc_path(slug)
    return Response(content=path.read_text(encoding="utf-8"), media_type=MediaType.TEXT)


def _render_doc(slug: str) -> Response[str]:
    missing = _missing_src_response()
    if missing is not None:
        return missing
    path = _resolve_doc_path(slug)
    sections = _read_summary()
    content_html = _cached_render(slug, path)
    ordered = _flatten_links(sections)
    prev_l, next_l = _find_neighbors(slug, ordered)
    page_title = _page_title(slug, path)
    content_html = _with_page_copy_button(content_html, slug, page_title)
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
        manual_copy_button=_copy_button(_ALL_MARKDOWN_PATH, _MANUAL_TIP, "Copy the entire manual as Markdown"),
    )
    return Response(content=html, media_type=MediaType.HTML)


docs_routes = Router(path="/", route_handlers=[docs_index, docs_all_markdown, docs_slug])
