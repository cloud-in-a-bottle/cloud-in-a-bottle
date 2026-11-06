"""Pulling an app's prebuilt image, and that a failure never falls back to a build."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

import compute_space.core.apps as apps_mod
from compute_space.core.prebuilt_image import PrebuiltImageError
from compute_space.core.prebuilt_image import pull_prebuilt_image

_SHA = "d961ca45c2995c6e80fb4f539b1fb7b163e07bdd"
_IMAGE = "registry.cloudinabottle.org/bottled-vaultwarden"
_REPO_URL = "https://github.com/cloud-in-a-bottle/bottled-vaultwarden"


class _FakeRun:
    """Stands in for subprocess.run, answering podman by its subcommand."""

    def __init__(self, answers: dict[tuple[str, str], subprocess.CompletedProcess[str]]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        return self.answers[(cmd[0], cmd[1])]


def _done(returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


def test_pulls_the_commits_image_and_tags_it_as_the_app_image() -> None:
    fake = _FakeRun({("podman", "pull"): _done(), ("podman", "tag"): _done()})
    with mock.patch("compute_space.core.prebuilt_image.subprocess.run", fake):
        tag = pull_prebuilt_image("vaultwarden", _IMAGE, _SHA, _REPO_URL, log=lambda _: None)
    assert tag == "openhost-vaultwarden:latest"
    assert fake.calls == [
        ["podman", "pull", f"{_IMAGE}:{_SHA}"],
        ["podman", "tag", f"{_IMAGE}:{_SHA}", "openhost-vaultwarden:latest"],
    ]


def test_missing_image_fails() -> None:
    fake = _FakeRun({("podman", "pull"): _done(returncode=125, stderr="manifest unknown")})
    with mock.patch("compute_space.core.prebuilt_image.subprocess.run", fake):
        with pytest.raises(PrebuiltImageError, match="manifest unknown"):
            pull_prebuilt_image("vaultwarden", _IMAGE, _SHA, _REPO_URL, log=lambda _: None)


def test_requires_a_full_commit_sha() -> None:
    with pytest.raises(PrebuiltImageError, match="full commit sha"):
        pull_prebuilt_image("vaultwarden", _IMAGE, "main", _REPO_URL, log=lambda _: None)


def test_app_using_a_prebuilt_image_never_builds(tmp_path: Path) -> None:
    """A failed pull propagates; build_image is never called as a fallback."""
    manifest = mock.Mock(prebuilt_image=_IMAGE)
    db = mock.Mock()
    db.execute.return_value.fetchone.return_value = {"repo_url": _REPO_URL}
    with (
        mock.patch.object(apps_mod, "follows_releases", return_value=True),
        mock.patch.object(apps_mod, "head_commit", return_value=_SHA),
        mock.patch.object(apps_mod, "pull_prebuilt_image", side_effect=PrebuiltImageError("no such image")),
        mock.patch.object(apps_mod, "build_image") as build,
        mock.patch.object(apps_mod, "run_app_image") as run,
    ):
        with pytest.raises(PrebuiltImageError, match="no such image"):
            apps_mod._build_then_run_app(
                "app-id",
                "app",
                str(tmp_path),
                manifest,
                db,
                mock.Mock(temporary_data_dir=str(tmp_path)),
                max_build_attempts=3,
            )
    build.assert_not_called()
    run.assert_not_called()


def test_app_with_a_pinned_ref_builds_despite_prebuilt_image(tmp_path: Path) -> None:
    manifest = mock.Mock(prebuilt_image=_IMAGE, container_image="Dockerfile", effective_build_memory_mb=512)
    db = mock.Mock()
    db.execute.return_value.fetchone.return_value = {"repo_url": f"{_REPO_URL}@feature"}
    with (
        mock.patch.object(apps_mod, "pull_prebuilt_image") as pull,
        mock.patch.object(apps_mod, "build_image", return_value="openhost-app:latest") as build,
        mock.patch.object(apps_mod, "run_app_image") as run,
    ):
        apps_mod._build_then_run_app(
            "app-id", "app", str(tmp_path), manifest, db, mock.Mock(temporary_data_dir=str(tmp_path))
        )
    pull.assert_not_called()
    build.assert_called_once()
    run.assert_called_once()
