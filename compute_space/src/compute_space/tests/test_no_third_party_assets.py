"""Instance pages must load their assets from the instance itself, not a third-party CDN.

A <link> or <script> pointing off-box sends every visitor's IP address (and the page's URL as the referrer) to that
host and breaks the page offline. The fonts are vendored by ``scripts/vendor_fonts.py``.
"""

import re
from pathlib import Path

from compute_space.web.helpers.static import STATIC_DIR
from compute_space.web.helpers.static import WEB_DIR

# Subresources fetched from another origin: <link href="https://...">, <script src="//...">, CSS @import and url().
# The CSS forms are case-sensitive so JavaScript's `new URL("https://...")` doesn't match.
_EXTERNAL_SUBRESOURCE = re.compile(
    r"""(?i:<(?:link|script)\b[^>]*\b(?:href|src)=["']?(?:https?:)?//)"""
    r"""|@import\s+(?:url\()?["']?(?:https?:)?//"""
    r"""|\burl\(\s*["']?(?:https?:)?//"""
)
# Still loaded from a CDN; remove an entry when that page is fixed.
_KNOWN_EXTERNAL = {"templates/terminal.html"}  # xterm.js from jsDelivr

_FONT_URL = re.compile(r'url\("/static/(fonts/[^"]+\.woff2)"\)')


def _page_sources() -> list[Path]:
    return sorted(
        path
        for pattern in ("templates/**/*.html", "static/**/*.css", "static/**/*.js", "routes/**/*.py")
        for path in WEB_DIR.glob(pattern)
    )


def test_pages_load_no_third_party_subresources() -> None:
    offenders = {
        str(path.relative_to(WEB_DIR))
        for path in _page_sources()
        if _EXTERNAL_SUBRESOURCE.search(path.read_text(encoding="utf-8"))
    }
    assert offenders == _KNOWN_EXTERNAL


def test_fonts_css_references_exactly_the_vendored_fonts() -> None:
    referenced = set(_FONT_URL.findall((STATIC_DIR / "css" / "fonts.css").read_text(encoding="utf-8")))
    vendored = {str(path.relative_to(STATIC_DIR)) for path in (STATIC_DIR / "fonts").glob("*/*.woff2")}
    assert referenced
    assert referenced == vendored
    for font_dir in {path.parent for path in (STATIC_DIR / "fonts").glob("*/*.woff2")}:
        assert (font_dir / "LICENSE").is_file()
