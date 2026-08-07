"""Third-party browser JS/CSS the web UI serves from ``/static/vendor``.  Provisioning downloads these
(``ansible/tasks/web_vendor_assets.yml``); they are gitignored rather than committed."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import attr

from compute_space.core.logging import logger
from compute_space.core.pinned_binary import download_bytes
from compute_space.core.pinned_binary import read_tar_member
from compute_space.core.pinned_binary import verify_sha256

VENDOR_DIR = Path(__file__).resolve().parent / "static" / "vendor"


@attr.s(auto_attribs=True, frozen=True)
class VendorAsset:
    filename: str  # served as /static/vendor/<filename>; version-free so templates never name a version
    url: str
    archive_member: str
    sha256: str  # of the extracted file, so bumping a version re-downloads over the stale copy


# Redoc and xterm.js publish built bundles to npm only -- their GitHub releases carry no build artifacts --
# so the pins point at registry tarballs.  Bump: change the url and the extracted file's sha256 together.
_MANIFEST: tuple[VendorAsset, ...] = (
    VendorAsset(
        filename="redoc.js",
        url="https://registry.npmjs.org/redoc/-/redoc-2.5.0.tgz",
        archive_member="package/bundles/redoc.standalone.js",
        sha256="0ec05be285ac885a330289b02f470e1bdbd2b6b3223a9fa213f24bf805a851d1",
    ),
    VendorAsset(
        filename="xterm.js",
        url="https://registry.npmjs.org/@xterm/xterm/-/xterm-5.5.0.tgz",
        archive_member="package/lib/xterm.js",
        sha256="1f991ac3b4b283ebf96e60ae23a00a52765dd3a2e46fa6fdda9f1aab032f7495",
    ),
    VendorAsset(
        filename="xterm.css",
        url="https://registry.npmjs.org/@xterm/xterm/-/xterm-5.5.0.tgz",
        archive_member="package/css/xterm.css",
        sha256="ba8e6985669488981ccf40c0cefe3aba80722cb6c92de7ad628b0bd717faf2b6",
    ),
    VendorAsset(
        filename="xterm-addon-fit.js",
        url="https://registry.npmjs.org/@xterm/addon-fit/-/addon-fit-0.10.0.tgz",
        archive_member="package/lib/addon-fit.js",
        sha256="bdaefa370b1bfc42ee88d46fe6072400902a4d4b2d45cd93438dda9b23c97089",
    ),
)


def _is_current(dest: Path, asset: VendorAsset) -> bool:
    return dest.is_file() and hashlib.sha256(dest.read_bytes()).hexdigest() == asset.sha256


def install_vendor_assets(dest_dir: Path = VENDOR_DIR) -> list[str]:
    """Download every pinned asset that is missing or stale.  Returns the filenames written."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for asset in _MANIFEST:
        dest = dest_dir / asset.filename
        if _is_current(dest, asset):
            continue
        logger.info("Downloading {} from {}", asset.filename, asset.url)
        payload = read_tar_member(download_bytes(asset.url, asset.filename), asset.archive_member, asset.filename)
        verify_sha256(payload, asset.sha256, asset.filename)
        dest.write_bytes(payload)
        written.append(asset.filename)
    return written


def main() -> int:
    written = install_vendor_assets()
    print(f"downloaded {', '.join(written)}" if written else "vendor assets up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
