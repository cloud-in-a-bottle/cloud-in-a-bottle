"""The scheduled update of an app that follows releases (see releases.follows_releases), as test_reload_prebuilt.py
covers for the update from the app's page: it lands on the newest release rather than the default branch's head, its
image (pulled, or built) is prepared before the running version is stopped, and an update that can't be prepared leaves
it running."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

import compute_space.core.apps as core_apps
import compute_space.core.auto_update.apps as apps_mod
from compute_space.config import Config
from compute_space.core import archive_backend
from compute_space.core.app_id import new_app_id
from compute_space.core.apps import ImageSource
from compute_space.core.apps import PreparedImage
from compute_space.core.auto_update.app_config import AppUpdateOutcome
from compute_space.core.prebuilt_image import ImageUnavailableError
from compute_space.core.prebuilt_image import PulledImage
from compute_space.db import get_db

from .conftest import _make_test_config

_MANIFEST = """\
[app]
name = "rel-app"
version = "{version}"
release_tag_format = "semver"

[runtime.container]
image = "Dockerfile"
port = 5000
prebuilt_image = "registry.cloudinabottle.org/rel-app"
"""

# Follows releases too, building them.
_BUILT_MANIFEST = _MANIFEST.replace('prebuilt_image = "registry.cloudinabottle.org/rel-app"\n', "")

_TAG = "openhost-rel-app:latest"


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    data = tmp_path / "data"
    data.mkdir()
    return _make_test_config(data, port=20960)


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout.strip()


def _commit(origin: Path, version: str, tag: str | None, manifest: str = _MANIFEST) -> str:
    (origin / "cloudinabottle.toml").write_text(manifest.format(version=version))
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", version)
    if tag:
        _git(origin, "tag", tag)
    return _git(origin, "rev-parse", "HEAD")


class ReleaseApp:
    """An origin whose default branch has one commit (tagged ``tag``), and a running app installed from a clone of it,
    tracking the default branch."""

    def __init__(self, tmp_path: Path, tag: str | None = "v1.0.0", manifest: str = _MANIFEST) -> None:
        self.origin = tmp_path / "origin"
        self.origin.mkdir()
        _git(self.origin, "init", "-q", "-b", "main")
        self.running = _commit(self.origin, "1.0.0", tag, manifest)
        self.repo = tmp_path / "repo"
        _git(tmp_path, "clone", "-q", str(self.origin), str(self.repo))
        if tag:
            _git(self.repo, "checkout", "-q", tag)  # as installed: detached at the release
        self.app_id = new_app_id()
        with closing(get_db()) as db:
            db.execute(
                "INSERT INTO apps (app_id, name, version, repo_path, repo_url, local_port, status, manifest_raw) "
                "VALUES (?, 'rel-app', '1.0.0', ?, 'https://github.com/x/rel-app', 20961, 'running', ?)",
                (self.app_id, str(self.repo), manifest.format(version="1.0.0")),
            )
            db.commit()

    def head(self) -> str:
        return _git(self.repo, "rev-parse", "HEAD")

    def status(self) -> str:
        with closing(get_db()) as db:
            row = db.execute("SELECT status FROM apps WHERE app_id = ?", (self.app_id,)).fetchone()
        return str(row["status"])


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """A real pull of the default branch from the local origin, with the image pull, the build, the stop and the
    background reload stubbed. ``events`` records the image pull, build and stop in order; a test sets ``pull_error`` or
    ``build_error`` to make that step fail."""
    state: dict[str, Any] = {"events": [], "reloaded": [], "pull_error": None, "build_error": None}

    def real_pull(repo_path: str, _app_name: str, **_: Any) -> tuple[bool, None]:
        _git(Path(repo_path), "fetch", "-q", "origin", "main")
        _git(Path(repo_path), "checkout", "-q", "-fB", "main", "origin/main")
        return True, None

    def pull_image(*_: Any, **__: Any) -> PulledImage:
        state["events"].append("pull")
        if state["pull_error"] is not None:
            raise state["pull_error"]
        return PulledImage(tag=_TAG, verified=False)

    def build(*_: Any, **__: Any) -> str:
        state["events"].append("build")
        if state["build_error"] is not None:
            raise state["build_error"]
        return _TAG

    def stop(_row: Any) -> None:
        state["events"].append("stop")

    def reload(app_id: str, _repo_path: str, _config: Config, prepared_image: PreparedImage | None = None) -> None:
        state["reloaded"].append(prepared_image)
        with closing(get_db()) as db:
            db.execute("UPDATE apps SET status = 'running' WHERE app_id = ?", (app_id,))
            db.commit()

    monkeypatch.setattr(apps_mod, "git_pull", real_pull)
    monkeypatch.setattr(core_apps, "pull_prebuilt_image", pull_image)
    monkeypatch.setattr(core_apps, "build_image", build)
    monkeypatch.setattr(apps_mod, "stop_app_process", stop)
    monkeypatch.setattr(apps_mod, "reload_app_background", reload)
    monkeypatch.setattr(archive_backend, "is_archive_dir_healthy", lambda _c, _db: True)
    yield state


@pytest.mark.asyncio
async def test_lands_on_the_newest_release_not_the_branch_head(
    cfg: Config, tmp_path: Path, stubs: dict[str, Any]
) -> None:
    app = ReleaseApp(tmp_path)
    _commit(app.origin, "1.1.0", "v1.1.0")
    target = _commit(app.origin, "2.0.0", "v2.0.0")
    _commit(app.origin, "2.0.1", None)  # unreleased head

    result = await apps_mod.auto_update_app(app.app_id, cfg)

    assert result.outcome == AppUpdateOutcome.UPDATED
    assert result.detail is not None and "release v2.0.0" in result.detail
    assert app.head() == target
    # The release's image was pulled before the running version was stopped, and the reload runs it.
    assert stubs["events"] == ["pull", "stop"]
    assert stubs["reloaded"] == [PreparedImage(tag=_TAG, source=ImageSource.PULLED)]
    assert app.status() == "running"


@pytest.mark.asyncio
async def test_release_without_prebuilt_image_is_built_before_stopping(
    cfg: Config, tmp_path: Path, stubs: dict[str, Any]
) -> None:
    app = ReleaseApp(tmp_path, manifest=_BUILT_MANIFEST)
    target = _commit(app.origin, "1.1.0", "v1.1.0", _BUILT_MANIFEST)
    _commit(app.origin, "1.1.1", None, _BUILT_MANIFEST)

    result = await apps_mod.auto_update_app(app.app_id, cfg)

    assert result.outcome == AppUpdateOutcome.UPDATED
    assert app.head() == target
    assert stubs["events"] == ["build", "stop"]
    assert stubs["reloaded"] == [PreparedImage(tag=_TAG, source=ImageSource.BUILT)]


@pytest.mark.asyncio
async def test_unavailable_image_is_built_before_stopping(cfg: Config, tmp_path: Path, stubs: dict[str, Any]) -> None:
    app = ReleaseApp(tmp_path)
    target = _commit(app.origin, "1.1.0", "v1.1.0")
    stubs["pull_error"] = ImageUnavailableError("registry unreachable")

    result = await apps_mod.auto_update_app(app.app_id, cfg)

    assert result.outcome == AppUpdateOutcome.UPDATED
    assert app.head() == target
    assert stubs["events"] == ["pull", "build", "stop"]
    assert stubs["reloaded"] == [PreparedImage(tag=_TAG, source=ImageSource.FALLBACK_BUILT)]


@pytest.mark.asyncio
async def test_failed_prepare_rolls_back_and_leaves_the_app_running(
    cfg: Config, tmp_path: Path, stubs: dict[str, Any]
) -> None:
    app = ReleaseApp(tmp_path)
    _commit(app.origin, "1.1.0", "v1.1.0")
    stubs["pull_error"] = ImageUnavailableError("registry unreachable")
    stubs["build_error"] = RuntimeError("Container build failed: out of memory")

    result = await apps_mod.auto_update_app(app.app_id, cfg)

    assert result.outcome == AppUpdateOutcome.FAILED
    assert result.detail is not None and "registry unreachable" in result.detail and "out of memory" in result.detail
    assert app.head() == app.running
    assert stubs["events"] == ["pull", "build"]
    assert stubs["reloaded"] == []
    assert app.status() == "running"


@pytest.mark.asyncio
async def test_no_release_tag_fails_and_leaves_the_app_running(
    cfg: Config, tmp_path: Path, stubs: dict[str, Any]
) -> None:
    app = ReleaseApp(tmp_path, tag=None)
    _commit(app.origin, "1.1.0", None)

    result = await apps_mod.auto_update_app(app.app_id, cfg)

    assert result.outcome == AppUpdateOutcome.FAILED
    assert result.detail is not None and "no vX.Y.Z release tag" in result.detail
    assert app.head() == app.running
    assert stubs["events"] == []
    assert stubs["reloaded"] == []
    assert app.status() == "running"


@pytest.mark.asyncio
async def test_unreleased_commits_alone_are_up_to_date(cfg: Config, tmp_path: Path, stubs: dict[str, Any]) -> None:
    app = ReleaseApp(tmp_path)
    _commit(app.origin, "1.0.1", None)

    result = await apps_mod.auto_update_app(app.app_id, cfg)

    assert result.outcome == AppUpdateOutcome.UP_TO_DATE
    assert app.head() == app.running
    assert stubs["events"] == []
