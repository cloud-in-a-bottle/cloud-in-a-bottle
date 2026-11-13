"""Updating an app that sets prebuilt_image, through the /reload_app route: tracking its default branch it moves to the
newest release and pulls that image (or, when it can't be fetched, builds the release) before stopping anything; a
failure leaves the running version untouched; a pinned ref builds as before."""

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
from compute_space.core.apps import ImageSource
from compute_space.core.apps import PreparedImage
from compute_space.core.prebuilt_image import ImageUnavailableError
from compute_space.core.prebuilt_image import ImageVerificationError
from compute_space.core.prebuilt_image import PulledImage
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
prebuilt_image = "{image}"
"""
_SIGNED = "registry.cloudinabottle.org/rel-app"
_UNSIGNED = "ghcr.io/x/rel-app"


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


def _commit(origin: Path, version: str, tag: str | None, image: str = _SIGNED) -> str:
    (origin / "cloudinabottle.toml").write_text(_MANIFEST.format(version=version, image=image))
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
    cfg: Any,
    tmp_path: Path,
    repo_url: str = "https://github.com/x/rel-app",
    tag: str | None = "v1.0.0",
    verified: bool = False,
) -> tuple[str, Path, Path, str]:
    """An origin with one commit (tagged ``tag``), and an installed app running it from a clone, its last prebuilt
    image ``verified`` or not. Returns (app_id, origin, repo, sha)."""
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
            "INSERT INTO apps (app_id, name, version, repo_path, repo_url, local_port, status, manifest_raw, "
            "prebuilt_image_verified) VALUES (?, 'rel-app', '1.0.0', ?, ?, 20902, 'running', ?, ?)",
            (app_id, str(repo), repo_url, _MANIFEST.format(version="1.0.0", image=_SIGNED), int(verified)),
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
def _mocked(
    pull_side_effect: Any = None, build_side_effect: Any = None, pulled_verified: bool = False
) -> Iterator[dict[str, Any]]:
    """Mocks git_pull (with a real fetch and checkout), the image pull (its result ``pulled_verified`` or not) and
    build, and everything after the claim. ``events`` records the pull, build and stop in order."""
    events: list[str] = []

    def record(name: str, side_effect: Any, result: Any) -> Any:
        def call(*_: Any, **__: Any) -> Any:
            events.append(name)
            if isinstance(side_effect, BaseException):
                raise side_effect
            return result

        return call

    with (
        patch("compute_space.web.routes.api.apps.git_pull", side_effect=_real_pull),
        patch(
            "compute_space.core.apps.pull_prebuilt_image",
            side_effect=record(
                "pull", pull_side_effect, PulledImage(tag="openhost-rel-app:latest", verified=pulled_verified)
            ),
        ) as pull,
        patch(
            "compute_space.core.apps.build_image",
            side_effect=record("build", build_side_effect, "openhost-rel-app:latest"),
        ) as build,
        patch("compute_space.web.routes.api.apps.stop_app_process", side_effect=record("stop", None, None)) as stop,
        patch("compute_space.web.routes.api.apps.Thread") as thread,
    ):
        yield {"pull": pull, "build": build, "stop": stop, "thread": thread, "events": events}


def _prepared_image(thread: Any) -> PreparedImage | None:
    """The image the update handed to the background reload."""
    prepared: PreparedImage | None = thread.call_args.kwargs["kwargs"]["prepared_image"]
    return prepared


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
    assert m["events"] == ["pull", "stop"]
    m["thread"].assert_called_once()
    assert _prepared_image(m["thread"]) == PreparedImage(
        tag="openhost-rel-app:latest", source=ImageSource.PULLED_UNVERIFIED
    )
    # Still tracking the default branch: nothing pinned.
    assert _row(cfg, app_id)[2] == "https://github.com/x/rel-app"


def test_unavailable_image_is_built_before_stopping_the_running_version(
    cfg: Any, client: TestClient[Litestar], tmp_path: Path
) -> None:
    app_id, origin, repo, _ = _seed(cfg, tmp_path)
    target = _commit(origin, "1.1.0", "v1.1.0")
    with _mocked(pull_side_effect=ImageUnavailableError("registry unreachable")) as m:
        resp = client.post(f"/reload_app/{app_id}", json={"update": True})
    assert resp.json() == {"ok": True}
    assert _git(repo, "rev-parse", "HEAD") == target
    assert m["events"] == ["pull", "build", "stop"]
    assert m["build"].call_args.args[:2] == ("rel-app", str(repo))
    m["thread"].assert_called_once()
    # The background reload runs what was built here, rather than pulling and building again after the stop.
    assert _prepared_image(m["thread"]) == PreparedImage(
        tag="openhost-rel-app:latest", source=ImageSource.FALLBACK_BUILT
    )
    log = (Path(cfg.temporary_data_dir) / "app_temp_data" / "rel-app" / "docker.log").read_text()
    assert "falling back to building it locally" in log


def test_failed_fallback_build_leaves_the_running_version_untouched(
    cfg: Any, client: TestClient[Litestar], tmp_path: Path
) -> None:
    app_id, origin, repo, running = _seed(cfg, tmp_path)
    _commit(origin, "1.1.0", "v1.1.0")
    with _mocked(
        pull_side_effect=ImageUnavailableError("registry unreachable"),
        build_side_effect=RuntimeError("Container build failed: out of memory"),
    ) as m:
        resp = client.post(f"/reload_app/{app_id}", json={"update": True})
    assert resp.json() == {"ok": True}
    assert m["events"] == ["pull", "build"]
    m["thread"].assert_not_called()
    assert _git(repo, "rev-parse", "HEAD") == running  # rolled back to what's running
    status, error, _ = _row(cfg, app_id)
    assert status == "error"
    assert error is not None
    assert error.startswith("Update not applied: ")
    assert "registry unreachable" in error and "out of memory" in error


def test_failed_verification_leaves_the_running_version_untouched_without_building(
    cfg: Any, client: TestClient[Litestar], tmp_path: Path
) -> None:
    app_id, origin, repo, running = _seed(cfg, tmp_path)
    _commit(origin, "1.1.0", "v1.1.0")
    with _mocked(pull_side_effect=ImageVerificationError("signature mismatch")) as m:
        resp = client.post(f"/reload_app/{app_id}", json={"update": True})
    assert resp.json() == {"ok": True}
    assert m["events"] == ["pull"]
    m["thread"].assert_not_called()
    assert _git(repo, "rev-parse", "HEAD") == running  # rolled back to what's running
    status, error, _ = _row(cfg, app_id)
    assert status == "error"
    assert error is not None and "signature mismatch" in error


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
    # Built after the stop, by the background reload, as for any app that doesn't use a prebuilt image.
    assert m["events"] == ["stop"]
    m["thread"].assert_called_once()
    assert _prepared_image(m["thread"]) is None


# --- an image that stops being signature-verified ---


def _verified_flag(cfg: Any, app_id: str) -> bool:
    db = sqlite3.connect(cfg.db_path)
    try:
        return bool(db.execute("SELECT prebuilt_image_verified FROM apps WHERE app_id = ?", (app_id,)).fetchone()[0])
    finally:
        db.close()


def _run_background_reload(thread: Any) -> None:
    """Run the reload the route handed to its thread, with the container launch mocked to succeed: the deploy that
    records which image the app now runs."""

    def launched(app_id: str, _tag: str, _manifest: Any, db: sqlite3.Connection, _config: Any, **__: Any) -> None:
        db.execute("UPDATE apps SET status = 'running' WHERE app_id = ?", (app_id,))
        db.commit()

    call = thread.call_args
    with patch("compute_space.core.apps.run_app_image", side_effect=launched):
        call.kwargs["target"](*call.kwargs["args"], **call.kwargs["kwargs"])


def test_update_moving_a_verified_app_off_our_registry_asks_before_pulling_or_stopping(
    cfg: Any, client: TestClient[Litestar], tmp_path: Path
) -> None:
    app_id, origin, repo, running = _seed(cfg, tmp_path, verified=True)
    _commit(origin, "1.1.0", "v1.1.0", image=_UNSIGNED)
    with _mocked() as m:
        resp = client.post(f"/reload_app/{app_id}", json={"update": True})
    body = resp.json()
    assert body["review_required"] is True
    assert body["image_verification_lost"] == {
        "previous_image": _SIGNED,
        "previous_registry": "registry.cloudinabottle.org",
        "new_image": _UNSIGNED,
        "new_registry": "ghcr.io",
    }
    assert "no longer signature-verified" in body["error"]
    assert body["permissions_required"] == []
    assert [c["label"] for c in body["settings_changed"] if c["label"] == "Prebuilt image"] == ["Prebuilt image"]
    assert m["events"] == []
    m["thread"].assert_not_called()
    assert _git(repo, "rev-parse", "HEAD") == running  # rolled back to what's running
    assert _row(cfg, app_id)[0] == "running"
    assert _verified_flag(cfg, app_id)


def test_an_approved_move_off_our_registry_is_applied_and_clears_the_flag(
    cfg: Any, client: TestClient[Litestar], tmp_path: Path
) -> None:
    app_id, origin, repo, _ = _seed(cfg, tmp_path, verified=True)
    target = _commit(origin, "1.1.0", "v1.1.0", image=_UNSIGNED)
    with _mocked() as m:
        resp = client.post(f"/reload_app/{app_id}", json={"update": True, "approve_new_permissions": True})
        assert resp.json() == {"ok": True}
        assert m["events"] == ["pull", "stop"]
        assert _git(repo, "rev-parse", "HEAD") == target
        assert _prepared_image(m["thread"]) == PreparedImage(
            tag="openhost-rel-app:latest", source=ImageSource.PULLED_UNVERIFIED
        )
        _run_background_reload(m["thread"])
    assert not _verified_flag(cfg, app_id)

    # Now that the app runs an unverified image, a further update on that registry doesn't ask.
    _commit(origin, "1.2.0", "v1.2.0", image=_UNSIGNED)
    with _mocked() as m:
        resp = client.post(f"/reload_app/{app_id}", json={"update": True})
    assert resp.json() == {"ok": True}
    assert m["events"] == ["pull", "stop"]


def test_an_app_that_never_ran_a_verified_image_moves_registries_without_asking(
    cfg: Any, client: TestClient[Litestar], tmp_path: Path
) -> None:
    app_id, origin, _, _ = _seed(cfg, tmp_path, verified=False)
    _commit(origin, "1.1.0", "v1.1.0", image=_UNSIGNED)
    with _mocked() as m:
        resp = client.post(f"/reload_app/{app_id}", json={"update": True})
    assert resp.json() == {"ok": True}
    assert m["events"] == ["pull", "stop"]


def test_a_verified_app_staying_on_our_registry_updates_without_asking(
    cfg: Any, client: TestClient[Litestar], tmp_path: Path
) -> None:
    app_id, origin, _, _ = _seed(cfg, tmp_path, verified=True)
    _commit(origin, "1.1.0", "v1.1.0")
    with _mocked(pulled_verified=True) as m:
        resp = client.post(f"/reload_app/{app_id}", json={"update": True})
        assert resp.json() == {"ok": True}
        assert m["events"] == ["pull", "stop"]
        _run_background_reload(m["thread"])
    assert _verified_flag(cfg, app_id)


def test_a_pinned_verified_app_whose_branch_moves_registries_builds_without_asking(
    cfg: Any, client: TestClient[Litestar], tmp_path: Path
) -> None:
    """A pinned ref ignores prebuilt_image and builds, so nothing would run unverified."""
    app_id, origin, repo, _ = _seed(cfg, tmp_path, repo_url="https://github.com/x/rel-app@main", verified=True)
    head = _commit(origin, "1.1.0", "v1.1.0", image=_UNSIGNED)
    with _mocked() as m:
        resp = client.post(f"/reload_app/{app_id}", json={"update": True})
        assert resp.json() == {"ok": True}
        assert _git(repo, "rev-parse", "HEAD") == head
        assert m["events"] == ["stop"]
        _run_background_reload(m["thread"])
    assert m["events"] == ["stop", "build"]
    # The build leaves the flag alone.
    assert _verified_flag(cfg, app_id)


def test_an_approved_move_that_falls_back_to_building_asks_again_next_time(
    cfg: Any, client: TestClient[Litestar], tmp_path: Path
) -> None:
    """The owner approved an unverified image, but the registry couldn't be reached and the release was built
    instead. The app never ran the unverified image, so its flag stays set and the next update asks again."""
    app_id, origin, repo, _ = _seed(cfg, tmp_path, verified=True)
    _commit(origin, "1.1.0", "v1.1.0", image=_UNSIGNED)
    with _mocked(pull_side_effect=ImageUnavailableError("registry unreachable")) as m:
        resp = client.post(f"/reload_app/{app_id}", json={"update": True, "approve_new_permissions": True})
        assert resp.json() == {"ok": True}
        assert m["events"] == ["pull", "build", "stop"]
        assert _prepared_image(m["thread"]) == PreparedImage(
            tag="openhost-rel-app:latest", source=ImageSource.FALLBACK_BUILT
        )
        _run_background_reload(m["thread"])
    assert _verified_flag(cfg, app_id)

    built = _git(repo, "rev-parse", "HEAD")
    _commit(origin, "1.2.0", "v1.2.0", image=_UNSIGNED)
    with _mocked() as m:
        resp = client.post(f"/reload_app/{app_id}", json={"update": True})
    body = resp.json()
    assert body["review_required"] is True
    # The running manifest names the unverified image, so the warning names our registry as the verified one.
    assert body["image_verification_lost"]["previous_image"] is None
    assert body["image_verification_lost"]["previous_registry"] == "registry.cloudinabottle.org"
    assert m["events"] == []
    assert _git(repo, "rev-parse", "HEAD") == built  # rolled back to what's running
