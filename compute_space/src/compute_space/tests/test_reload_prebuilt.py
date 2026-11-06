"""Updating an app that sets prebuilt_image, through the /reload_app route: tracking its default branch it moves to the
newest release and pulls that image before stopping anything; a failure leaves the running version untouched; a
pinned ref builds as before."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from litestar import Litestar
from litestar.testing import TestClient

from compute_space.core.app_id import new_app_id
from compute_space.core.prebuilt_image import PrebuiltImageError
from compute_space.db.connection import init_db
from compute_space.tests._litestar_helpers import auth_cookie
from compute_space.tests._litestar_helpers import make_test_app
from compute_space.web.routes.api.apps import api_apps_routes

from .conftest import _make_test_config

_MANIFEST = """\
[app]
name = "rel-app"
version = "{version}"

[runtime.container]
image = "Dockerfile"
port = 5000
prebuilt_image = "registry.cloudinabottle.org/rel-app"
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    ).stdout.strip()


def _commit(origin: Path, version: str, tag: str | None) -> str:
    (origin / "cloudinabottle.toml").write_text(_MANIFEST.format(version=version))
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", version)
    if tag:
        _git(origin, "tag", tag)
    return _git(origin, "rev-parse", "HEAD")


@pytest.fixture
def cfg(tmp_path_factory: pytest.TempPathFactory) -> Any:
    c = _make_test_config(tmp_path_factory.mktemp("prebuilt-update"), port=20900)
    init_db(c.db_path)
    return c


@pytest.fixture
def client(cfg: Any) -> Iterator[TestClient[Litestar]]:
    with TestClient(app=make_test_app(api_apps_routes)) as c:
        c.cookies.update(auth_cookie(cfg))
        yield c


def _seed(
    cfg: Any, tmp_path: Path, repo_url: str = "https://github.com/x/rel-app", tag: str | None = "v1.0.0"
) -> tuple[str, Path, Path, str]:
    """An origin with one commit (tagged ``tag``), and an installed app running it from a clone. Returns (app_id,
    origin, repo, sha)."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    running = _commit(origin, "1.0.0", tag)
    repo = tmp_path / "repo"
    _git(tmp_path, "clone", "-q", str(origin), str(repo))
    app_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, repo_url, local_port, status, manifest_raw) "
            "VALUES (?, 'rel-app', '1.0.0', ?, ?, 20902, 'running', ?)",
            (app_id, str(repo), repo_url, _MANIFEST.format(version="1.0.0")),
        )
        db.commit()
    finally:
        db.close()
    return app_id, origin, repo, running


def _real_pull(repo_path: str, *_: Any, repo_url: str = "", **__: Any) -> tuple[bool, None]:
    """What git_pull does for a branch: fetch it (the default, main, when none is pinned) and check out its head."""
    branch = repo_url.rsplit("@", 1)[1] if "@" in repo_url else "main"
    _git(Path(repo_path), "fetch", "origin", branch)
    _git(Path(repo_path), "checkout", "-fB", branch, f"origin/{branch}")
    return True, None


@contextlib.contextmanager
def _mocked(pull_side_effect: Any = None) -> Iterator[dict[str, Any]]:
    with (
        patch("compute_space.web.routes.api.apps.git_pull", side_effect=_real_pull),
        patch(
            "compute_space.core.apps.pull_prebuilt_image",
            side_effect=pull_side_effect,
            return_value="openhost-rel-app:latest",
        ) as pull,
        patch("compute_space.web.routes.api.apps.stop_app_process") as stop,
        patch("compute_space.web.routes.api.apps.Thread") as thread,
    ):
        yield {"pull": pull, "stop": stop, "thread": thread}


def _row(cfg: Any, app_id: str) -> tuple[str, str | None, str]:
    db = sqlite3.connect(cfg.db_path)
    try:
        status, error, repo_url = db.execute(
            "SELECT status, error_message, repo_url FROM apps WHERE app_id = ?", (app_id,)
        ).fetchone()
        return str(status), error, str(repo_url)
    finally:
        db.close()


def test_update_moves_to_the_newest_release_and_pulls_it_before_stopping(
    cfg: Any, client: TestClient[Litestar], tmp_path: Path
) -> None:
    app_id, origin, repo, _ = _seed(cfg, tmp_path)
    _commit(origin, "1.1.0", "v1.1.0")
    target = _commit(origin, "2.0.0", "v2.0.0")  # a new major is just a newer release
    _commit(origin, "2.0.1", None)  # unreleased
    with _mocked() as m:
        resp = client.post(f"/reload_app/{app_id}", json={"update": True})
    assert resp.json() == {"ok": True}
    assert _git(repo, "rev-parse", "HEAD") == target
    m["pull"].assert_called_once()
    m["stop"].assert_called_once()
    m["thread"].assert_called_once()
    # Still tracking the default branch: nothing pinned.
    assert _row(cfg, app_id)[2] == "https://github.com/x/rel-app"


def test_failed_pull_leaves_the_running_version_untouched(
    cfg: Any, client: TestClient[Litestar], tmp_path: Path
) -> None:
    app_id, origin, repo, running = _seed(cfg, tmp_path)
    _commit(origin, "1.1.0", "v1.1.0")
    with _mocked(pull_side_effect=PrebuiltImageError("manifest unknown")) as m:
        resp = client.post(f"/reload_app/{app_id}", json={"update": True})
    assert resp.json() == {"ok": True}
    m["stop"].assert_not_called()
    m["thread"].assert_not_called()
    assert _git(repo, "rev-parse", "HEAD") == running  # rolled back to what's running
    status, error, _ = _row(cfg, app_id)
    assert status == "error"
    assert error is not None and "manifest unknown" in error


def test_no_release_tag_fails_the_update_and_leaves_the_app_running(
    cfg: Any, client: TestClient[Litestar], tmp_path: Path
) -> None:
    app_id, origin, repo, running = _seed(cfg, tmp_path, tag=None)
    _commit(origin, "1.1.0", None)
    with _mocked() as m:
        resp = client.post(f"/reload_app/{app_id}", json={"update": True})
    assert resp.json() == {"ok": True}
    m["pull"].assert_not_called()
    m["stop"].assert_not_called()
    assert _git(repo, "rev-parse", "HEAD") == running
    status, error, _ = _row(cfg, app_id)
    assert status == "error"
    assert error is not None and "no vX.Y.Z release tag" in error


@pytest.mark.parametrize("pin", ["main", "feature"])
def test_pinned_ref_ignores_prebuilt_image_and_builds_its_head(
    cfg: Any, client: TestClient[Litestar], tmp_path: Path, pin: str
) -> None:
    app_id, origin, repo, _ = _seed(cfg, tmp_path, repo_url=f"https://github.com/x/rel-app@{pin}")
    _commit(origin, "1.1.0", "v1.1.0")
    if pin != "main":
        _git(origin, "checkout", "-b", pin)
    head = _commit(origin, "1.1.1", None)
    _git(origin, "checkout", "main")
    with _mocked() as m:
        resp = client.post(f"/reload_app/{app_id}", json={"update": True})
    assert resp.json() == {"ok": True}
    assert _git(repo, "rev-parse", "HEAD") == head
    m["stop"].assert_called_once()
    m["thread"].assert_called_once()
    m["pull"].assert_not_called()
