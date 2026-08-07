import re

from compute_space import OPENHOST_PROJECT_DIR
from compute_space.web.vendor_assets import VENDOR_DIR
from compute_space.web.vendor_assets import _MANIFEST

_REFERENCE = re.compile(r"vendor/([A-Za-z0-9._-]+)")


def test_every_referenced_vendor_file_is_pinned() -> None:
    """Templates and docs must only load assets provisioning actually downloads."""
    pinned = {asset.filename for asset in _MANIFEST}
    sources = [
        *(VENDOR_DIR.parent.parent / "templates").rglob("*.html"),
        *(OPENHOST_PROJECT_DIR / "docs" / "src").rglob("*.md"),
    ]
    referenced = {name for path in sources for name in _REFERENCE.findall(path.read_text())}
    assert referenced, "found no vendor references to check"
    assert referenced <= pinned, f"unpinned vendor assets referenced: {sorted(referenced - pinned)}"
