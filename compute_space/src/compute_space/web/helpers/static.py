from collections.abc import Callable
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = WEB_DIR / "static"


def make_static_url(static_dir: Path) -> Callable[[str], str]:
    """Build a Jinja ``static_url`` global that appends ``?v=<mtime>`` for cache-busting.

    Browsers aggressively cache static JS/CSS, so a deploy that ships a new
    template + JS would otherwise leave returning visitors running stale JS
    against new HTML.  Appending the file's mtime forces a fresh fetch.
    """

    def static_url(filename: str) -> str:
        path = static_dir / filename
        try:
            mtime = int(path.stat().st_mtime)
        except OSError as exc:
            # Only reachable when a template names a file that isn't there (a
            # typo, or an asset deleted without updating its references). Fail
            # loudly: silently emitting an un-cache-busted URL to a 404 means
            # the page renders subtly broken instead of telling anyone.
            raise RuntimeError(f"static_url({filename!r}): no such static file at {path}") from exc
        return f"/static/{filename}?v={mtime}"

    return static_url
