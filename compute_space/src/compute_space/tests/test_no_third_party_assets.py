"""Instance pages must load their assets from the instance itself, not a third-party CDN.

A <link> or <script> pointing off-box sends every visitor's IP address (and the page's URL as the referrer) to that
host and breaks the page offline. Third-party browser assets are vendored by ``scripts/vendor_assets.py``.
"""

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

from compute_space.web.helpers.static import STATIC_DIR
from compute_space.web.helpers.static import WEB_DIR

REPO_ROOT = WEB_DIR.parents[3]
VENDOR_SCRIPT = REPO_ROOT / "scripts" / "vendor_assets.py"

# Subresources fetched from another origin: <link href="https://...">, <script src="//...">, CSS @import and url().
# The CSS forms are case-sensitive so JavaScript's `new URL("https://...")` doesn't match.
_EXTERNAL_SUBRESOURCE = re.compile(
    r"""(?i:<(?:link|script)\b[^>]*\b(?:href|src)=["']?(?:https?:)?//)"""
    r"""|@import\s+(?:url\()?["']?(?:https?:)?//"""
    r"""|\burl\(\s*["']?(?:https?:)?//"""
)
_STATIC_URL_CALL = re.compile(r"""static_url\(\s*['"]([^'"]+)['"]\s*\)""")
_FONT_URL = re.compile(r'url\("/static/(fonts/[^"]+\.woff2)"\)')


@pytest.fixture(scope="module")
def vendor_assets() -> ModuleType:
    spec = importlib.util.spec_from_file_location("vendor_assets", VENDOR_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # attrs resolves the module's annotations through sys.modules.
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[spec.name]
    return module


def _sources(*patterns: str) -> list[Path]:
    return sorted(path for pattern in patterns for path in WEB_DIR.glob(pattern))


def _relative(paths: set[Path], root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in paths}


def test_pages_load_no_third_party_subresources() -> None:
    sources = _sources("templates/**/*.html", "static/**/*.css", "static/**/*.js", "routes/**/*.py", "*.py")
    assert any(path.name == "terminal.html" for path in sources)
    offenders = {
        str(path.relative_to(WEB_DIR))
        for path in sources
        if _EXTERNAL_SUBRESOURCE.search(path.read_text(encoding="utf-8"))
    }
    assert offenders == set()


@pytest.mark.parametrize(
    "snippet",
    [
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">',
        "<script src='//cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.min.js'></script>",
        '<LINK REL="preconnect" HREF="https://fonts.gstatic.com" crossorigin>',
        "@import url('https://fonts.googleapis.com/css2?family=Inter');",
        '@import "https://example.com/a.css";',
        'src: url("https://fonts.gstatic.com/s/inter.woff2") format("woff2");',
        "background: url(//example.com/a.png);",
    ],
)
def test_detector_flags_third_party_subresources(snippet: str) -> None:
    assert _EXTERNAL_SUBRESOURCE.search(snippet)


@pytest.mark.parametrize(
    "snippet",
    [
        """<link rel="stylesheet" href="{{ static_url('vendor/xterm/xterm.css') }}">""",
        '<a href="https://github.com/cloud-in-a-bottle/cloud-in-a-bottle">Source</a>',
        'src: url("/static/fonts/inter-5.3.0/inter-latin-400-normal.woff2") format("woff2");',
        "try { parsed = new URL('https://' + spec); } catch (e) {}",
        'input.placeholder = "https://github.com/user/repo";',
    ],
)
def test_detector_ignores_same_origin_assets_and_plain_links(snippet: str) -> None:
    assert not _EXTERNAL_SUBRESOURCE.search(snippet)


def test_every_static_url_reference_exists() -> None:
    # static_url() only logs a missing file, so a typo in a vendored path would ship as a silently broken page.
    referenced = {
        name
        for path in _sources("templates/**/*.html", "routes/**/*.py", "*.py")
        for name in _STATIC_URL_CALL.findall(path.read_text(encoding="utf-8"))
    }
    assert {"css/fonts.css", "vendor/xterm/xterm.js", "vendor/xterm/xterm.css", "vendor/addon-fit/addon-fit.js"} <= (
        referenced
    )
    assert {name for name in referenced if not (STATIC_DIR / name).is_file()} == set()


def test_fonts_css_references_exactly_the_vendored_fonts(vendor_assets: ModuleType) -> None:
    referenced = set(_FONT_URL.findall((STATIC_DIR / "css" / "fonts.css").read_text(encoding="utf-8")))
    vendored = _relative(set((STATIC_DIR / "fonts").glob("*/*.woff2")), STATIC_DIR)
    assert referenced
    assert referenced == vendored
    font_dirs = {path.name for path in (STATIC_DIR / "fonts").iterdir()}
    assert font_dirs == {font.dir_name for font in vendor_assets.FONTS}
    for font_dir in font_dirs:
        assert (STATIC_DIR / "fonts" / font_dir / "LICENSE").is_file()
    for weight in vendor_assets.FONT_WEIGHTS:
        assert f"font-weight: {weight};" in (STATIC_DIR / "css" / "fonts.css").read_text(encoding="utf-8")


def test_vendor_dir_matches_the_vendoring_script(vendor_assets: ModuleType) -> None:
    vendor_dir = STATIC_DIR / "vendor"
    expected = {f"{script.out_dir}/{dest}" for script in vendor_assets.SCRIPTS for _, dest in script.files}
    expected |= {f"{script.out_dir}/LICENSE" for script in vendor_assets.SCRIPTS}
    assert _relative({path for path in vendor_dir.rglob("*") if path.is_file()}, vendor_dir) == expected
    for script in vendor_assets.SCRIPTS:
        for _, dest in script.files:
            text = (vendor_dir / script.out_dir / dest).read_text(encoding="utf-8")
            # The header records which pinned release is on disk, so a hand edit or a stale copy is visible.
            assert text.startswith(f"/* {script.package.spec} ")
            assert "sourceMappingURL" not in text


def test_font_weights_match_the_design_tokens(vendor_assets: ModuleType) -> None:
    tokens = (STATIC_DIR / "css" / "tokens.css").read_text(encoding="utf-8")
    token_weights = {int(weight) for weight in re.findall(r"--weight-[\w-]+:\s*(\d+);", tokens)}
    assert token_weights == set(vendor_assets.FONT_WEIGHTS)
