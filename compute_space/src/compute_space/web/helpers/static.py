import time
from collections.abc import Callable
from pathlib import Path

from loguru import logger

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
            # A template naming a file that isn't there.  Log it and carry on: the
            # only admin surface for this box is these pages, so taking every page
            # that references a missing asset down to a 500 is worse than one 404.
            # Version on the current time so the request always reaches the server
            # -- an un-versioned URL can be served from a cache populated before
            # the file went missing, hiding the breakage entirely.
            logger.warning("static_url({!r}): no such static file at {} ({})", filename, path, exc)
            return f"/static/{filename}?v={int(time.time())}"
        return f"/static/{filename}?v={mtime}"

    return static_url
