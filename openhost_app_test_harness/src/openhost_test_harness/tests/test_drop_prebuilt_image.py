"""The harness builds the app under test even if its manifest sets prebuilt_image."""

from pathlib import Path

from compute_space.core.manifest import parse_manifest
from openhost_test_harness.stack import _drop_prebuilt_image

_MANIFEST = """\
[app]
name = "app"
version = "1.0.0"

[runtime.container]
image = "Dockerfile"
port = 8080
  prebuilt_image = "registry.cloudinabottle.org/app"  # comment
shm_mb = 64
"""


def test_drop_prebuilt_image_keeps_the_rest(tmp_path: Path) -> None:
    (tmp_path / "cloudinabottle.toml").write_text(_MANIFEST)
    _drop_prebuilt_image(tmp_path)
    manifest = parse_manifest(str(tmp_path))
    assert manifest.prebuilt_image is None
    assert manifest.container_image == "Dockerfile"
    assert manifest.shm_mb == 64
