"""Download pinned, arch-specific binaries compute_space installs at RUNTIME.

Some tools we depend on at runtime aren't packaged for every arch we run on, so
we fetch a pinned vendor release and verify its sha256 before use.  The pins are
declared as data in ``_MANIFEST`` below -- one entry per program, with the
download URL + sha256 for each arch.  Adding a pinned tool or arch is a data-only
change here.  Callers: ``archive_backend`` (JuiceFS) and ``web/start`` (CoreDNS).

CoreDNS is normally installed by provisioning (``ansible/tasks/coredns.yml``);
the pin here is a startup fallback so a host upgraded in place -- which loses the
coredns pixi used to provide -- can re-fetch it.  Keep the CoreDNS version + per-
arch sha256 here in sync with that ansible task."""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
import urllib.error
import urllib.request

import attr

from compute_space.core.logging import logger


@attr.s(auto_attribs=True, frozen=True)
class ArchAsset:
    url: str
    sha256: str


@attr.s(auto_attribs=True, frozen=True)
class PinnedBinary:
    name: str
    version: str
    archive_member: str
    # arch string ("amd64" / "arm64") -> where to get it.  A genuine same-typed
    # mapping, so a dict is the right shape here.
    assets: dict[str, ArchAsset]

    def asset_for(self, arch: str) -> ArchAsset:
        asset = self.assets.get(arch)
        if asset is None:
            raise RuntimeError(f"No pinned {self.name} download for arch {arch!r}.")
        return asset


# Pinned runtime binaries, keyed by name.  URLs point at the vendor's GitHub
# release; bump both the version and the per-arch sha256 together.
_MANIFEST: dict[str, PinnedBinary] = {
    "juicefs": PinnedBinary(
        name="juicefs",
        version="1.3.1",
        archive_member="juicefs",  # file to extract from the tarball
        assets={
            "amd64": ArchAsset(
                url="https://github.com/juicedata/juicefs/releases/download/v1.3.1/juicefs-1.3.1-linux-amd64.tar.gz",
                sha256="eb67a7be5d174b420cb3734d441971b3a462ab522b78ad2a6ed993e7deddcd44",
            ),
            "arm64": ArchAsset(
                url="https://github.com/juicedata/juicefs/releases/download/v1.3.1/juicefs-1.3.1-linux-arm64.tar.gz",
                sha256="c29bff8f609366011cee03b9abcc76c11a06308b2c314364b8c340a2bfbc6c48",
            ),
        },
    ),
    # Keep in sync with ansible/tasks/coredns.yml (bump version + both sha256 together).
    "coredns": PinnedBinary(
        name="coredns",
        version="1.14.4",
        archive_member="coredns",  # file to extract from the tarball
        assets={
            "amd64": ArchAsset(
                url="https://github.com/coredns/coredns/releases/download/v1.14.4/coredns_1.14.4_linux_amd64.tgz",
                sha256="5b0e6a6a8b97bdcaec65baa70e83ca88ce03ab852355c656e3f7953405cfe36e",
            ),
            "arm64": ArchAsset(
                url="https://github.com/coredns/coredns/releases/download/v1.14.4/coredns_1.14.4_linux_arm64.tgz",
                sha256="b3491ed0fbe530a549640a4073d90037ffafa483f007936e1943c1ae0def7325",
            ),
        },
    ),
}


def host_arch() -> str:
    """Return the release-asset arch string ("amd64" / "arm64") for this host."""
    machine = os.uname().machine
    if machine in ("aarch64", "arm64"):
        return "arm64"
    return "amd64"


def get_pinned_binary(name: str) -> PinnedBinary:
    binary = _MANIFEST.get(name)
    if binary is None:
        raise RuntimeError(f"No pinned binary named {name!r} declared in pinned_binary.py.")
    return binary


def download_bytes(url: str, what: str) -> bytes:
    """Fetch ``url`` whole, raising a RuntimeError naming ``what`` if it can't be reached."""
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            data: bytes = resp.read()
    except (TimeoutError, urllib.error.URLError) as exc:
        raise RuntimeError(f"Failed to download {what}: {exc}") from exc
    return data


def verify_sha256(data: bytes, expected: str, what: str) -> None:
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise RuntimeError(f"{what} sha256 mismatch (expected {expected}, got {actual}).  Refusing to install.")


def read_tar_member(archive: bytes, member_name: str, what: str) -> bytes:
    """Return one member's bytes from the gzipped tar ``archive``."""
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        try:
            member = tar.getmember(member_name)
        except KeyError:
            raise RuntimeError(f"{what} archive is missing the {member_name!r} entry") from None
        extracted = tar.extractfile(member)
        if extracted is None:
            raise RuntimeError(f"{what} archive entry {member_name!r} was unreadable")
        with extracted:
            return extracted.read()


def install_pinned_binary(binary: PinnedBinary, dest_path: str) -> None:
    """Download + verify sha256 + extract ``binary`` to ``dest_path``.  Idempotent."""
    if os.path.isfile(dest_path) and os.access(dest_path, os.X_OK):
        return
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    arch = host_arch()
    asset = binary.asset_for(arch)
    logger.info("Downloading {} {} for {}", binary.name, binary.version, arch)
    tarball_bytes = download_bytes(asset.url, binary.name)
    verify_sha256(tarball_bytes, asset.sha256, f"{binary.name} tarball")
    with open(dest_path, "wb") as out:
        out.write(read_tar_member(tarball_bytes, binary.archive_member, binary.name))
    os.chmod(dest_path, 0o700)
    logger.info("{} installed at {}", binary.name, dest_path)
