"""``apps.prebuilt_image_verified`` follows the last prebuilt image an app was deployed on: a verified pull sets it, an
unverified pull clears it, and a build (the app's own, or in place of an image that couldn't be fetched) leaves it."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

import compute_space.core.apps as apps_mod
from compute_space.core.apps import App
from compute_space.core.apps import ImageSource
from compute_space.core.apps import PreparedImage
from compute_space.core.apps import start_app_process
from compute_space.tests.conftest import _make_test_config

_MANIFEST = """\
[app]
name = "vaultwarden"
version = "1.0.0"
release_tag_format = "semver"

[runtime.container]
image = "Dockerfile"
port = 8080
prebuilt_image = "registry.cloudinabottle.org/bottled-vaultwarden"
"""


@pytest.fixture
def app(tmp_path: Path) -> tuple[Any, Path]:
    cfg: Any = _make_test_config(tmp_path, port=20860)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "cloudinabottle.toml").write_text(_MANIFEST)
    db = sqlite3.connect(cfg.db_path)
    db.execute(
        "INSERT INTO apps (app_id, name, version, repo_path, repo_url, local_port, status) "
        "VALUES ('vw-id', 'vaultwarden', '1.0.0', ?, 'https://github.com/cloud-in-a-bottle/bottled-vaultwarden', "
        "20861, 'building')",
        (str(repo),),
    )
    db.commit()
    db.close()
    return cfg, repo


def _flag(cfg: Any) -> int:
    db = sqlite3.connect(cfg.db_path)
    try:
        return int(db.execute("SELECT prebuilt_image_verified FROM apps WHERE app_id = 'vw-id'").fetchone()[0])
    finally:
        db.close()


def _set_flag(cfg: Any, value: int) -> None:
    db = sqlite3.connect(cfg.db_path)
    db.execute("UPDATE apps SET prebuilt_image_verified = ? WHERE app_id = 'vw-id'", (value,))
    db.commit()
    db.close()


def _deploy(cfg: Any, source: ImageSource, *, prepared: bool, run_error: Exception | None = None) -> None:
    """Deploy the app on an image from ``source``: handed in already prepared (as an update does) or prepared by the
    start itself (as an install, plain reload or rebuild does). Running the container is mocked."""
    image = PreparedImage(tag="openhost-vaultwarden:latest", source=source)
    db = sqlite3.connect(cfg.db_path)
    db.row_factory = sqlite3.Row
    try:
        with (
            mock.patch.object(apps_mod, "prepare_app_image", return_value=image) as prepare,
            mock.patch.object(apps_mod, "run_app_image", side_effect=run_error) as run,
        ):
            start_app_process("vw-id", db, cfg, prepared_image=image if prepared else None)
        assert prepare.called is not prepared
        run.assert_called_once()
    finally:
        db.close()


def test_a_new_app_has_never_run_a_verified_image(app: tuple[Any, Path]) -> None:
    assert _flag(app[0]) == 0


@pytest.mark.parametrize("prepared", [True, False])
@pytest.mark.parametrize(
    ("before", "source", "after"),
    [
        (0, ImageSource.PULLED_VERIFIED, 1),
        (1, ImageSource.PULLED_VERIFIED, 1),
        (1, ImageSource.PULLED_UNVERIFIED, 0),
        (0, ImageSource.PULLED_UNVERIFIED, 0),
        # Builds keep whatever the last pull left, so a registry outage or a stretch on a pinned ref doesn't erase
        # that the app ran a verified image.
        (1, ImageSource.FALLBACK_BUILT, 1),
        (0, ImageSource.FALLBACK_BUILT, 0),
        (1, ImageSource.BUILT, 1),
        (0, ImageSource.BUILT, 0),
    ],
)
def test_each_image_source_sets_or_keeps_the_flag(
    app: tuple[Any, Path], before: int, source: ImageSource, after: int, prepared: bool
) -> None:
    cfg, _ = app
    _set_flag(cfg, before)
    _deploy(cfg, source, prepared=prepared)
    assert _flag(cfg) == after


def test_the_flag_is_recorded_even_if_the_container_then_fails_to_start(app: tuple[Any, Path]) -> None:
    """Once pulled, the image is the app's tagged image, which a later restart runs, so the flag describes it even
    when this launch fails."""
    cfg, _ = app
    _set_flag(cfg, 1)
    with pytest.raises(RuntimeError, match="no room"):
        _deploy(cfg, ImageSource.PULLED_UNVERIFIED, prepared=True, run_error=RuntimeError("no room"))
    assert _flag(cfg) == 0


def test_the_app_row_mirror_carries_the_flag(app: tuple[Any, Path]) -> None:
    cfg, _ = app
    _set_flag(cfg, 1)
    db = sqlite3.connect(cfg.db_path)
    db.row_factory = sqlite3.Row
    try:
        row = db.execute("SELECT * FROM apps WHERE app_id = 'vw-id'").fetchone()
    finally:
        db.close()
    assert App.from_row(row).prebuilt_image_verified is True
