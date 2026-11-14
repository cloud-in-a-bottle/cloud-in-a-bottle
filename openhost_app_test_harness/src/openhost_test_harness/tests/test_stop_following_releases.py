"""The harness deploys the working tree of the app under test even if its manifest follows releases."""

from pathlib import Path

from compute_space.core.manifest import parse_manifest
from openhost_test_harness.stack import _stop_following_releases

_MANIFEST = """\
[app]
name = "app"
version = "1.0.0"
release_tag_format = "semver"

[runtime.container]
image = "Dockerfile"
port = 8080
  prebuilt_image = "registry.cloudinabottle.org/app"  # comment
shm_mb = 64
"""


def test_stop_following_releases_keeps_the_rest(tmp_path: Path) -> None:
    (tmp_path / "cloudinabottle.toml").write_text(_MANIFEST)
    _stop_following_releases(tmp_path)
    manifest = parse_manifest(str(tmp_path))
    assert manifest.release_tag_format is None
    assert manifest.prebuilt_image is None
    assert manifest.container_image == "Dockerfile"
    assert manifest.shm_mb == 64
