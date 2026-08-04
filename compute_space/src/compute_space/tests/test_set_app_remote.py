"""Tests for editing an app's git upstream.

Covers the ``/set_app_remote/<app_id>`` route (persisting a new repo_url with
optional ``@ref``) and the branch-switching behaviour added to ``git_pull`` so
that changing the upstream ref actually checks out a different branch on the
next update.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from litestar.testing import TestClient

import compute_space.web.routes.api.apps as apps_routes
from compute_space.core.app_id import new_app_id
from compute_space.core.apps import git_pull
from compute_space.core.auth.auth import api_token_installed_by
from compute_space.core.manifest import parse_manifest
from compute_space.db.connection import init_db
from compute_space.web.routes.api.apps import api_apps_routes

from ._litestar_helpers import auth_cookie
from ._litestar_helpers import make_test_app
from .conftest import _make_test_config


def _seed_owner_token(db_path: str, token_id: str, raw: str, name: str = "installer") -> str:
    """Insert a never-expiring owner-scoped api_tokens row; return its token_id."""
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO api_tokens (token_id, name, token_hash, expires_at, scopes) VALUES (?, ?, ?, '', ?)",
            (token_id, name, token_hash, '["owner"]'),
        )
        conn.commit()
        return token_id
    finally:
        conn.close()


def _make_add_app_clone(clone: Path) -> None:
    clone.mkdir()
    (clone / "openhost.toml").write_text(
        '[app]\nname = "myapp"\nversion = "0.1.0"\n[runtime.container]\nimage = "Dockerfile"\nport = 8080\n'
    )


def _fake_add_app_deps(clone: Path) -> tuple[Any, Any, dict[str, Any]]:
    """(fake_clone, fake_insert_and_deploy, capture) for driving api_add_app."""
    manifest = parse_manifest(str(clone))
    captured: dict[str, Any] = {}

    def fake_insert_and_deploy(*args: Any, **kwargs: Any) -> str:
        captured["installed_by"] = kwargs.get("installed_by")
        return new_app_id()

    async def fake_clone(repo_url: str, return_to: str) -> tuple[Any, str, None, None]:
        return manifest, str(clone), None, None

    return fake_clone, fake_insert_and_deploy, captured


def test_add_app_by_token_stamps_installed_by_with_token_identity(cfg: Any, tmp_path: Path) -> None:
    """A token-initiated deploy stamps apps.installed_by with the token's opaque
    identity (``apitoken:tok_…``) so a future ownership scope can attribute the
    app to the token that installed it."""
    clone = tmp_path / "clone"
    _make_add_app_clone(clone)
    fake_clone, fake_insert, captured = _fake_add_app_deps(clone)
    token_id = _seed_owner_token(cfg.db_path, token_id="tok_deploy", raw="deploy-tok")

    with (
        mock.patch.object(apps_routes, "clone_with_github_fallback", side_effect=fake_clone),
        mock.patch.object(apps_routes, "validate_manifest", return_value=None),
        mock.patch.object(apps_routes, "move_clone_to_app_temp_dir", return_value=str(clone)),
        mock.patch.object(apps_routes, "insert_and_deploy", side_effect=fake_insert),
        TestClient(app=make_test_app(api_apps_routes)) as client,
    ):
        resp = client.post(
            "/api/add_app",
            json={"repo_url": f"file://{clone}"},
            headers={"Authorization": "Bearer deploy-tok"},
        )
    assert resp.status_code == 200, resp.text
    assert captured["installed_by"] == api_token_installed_by(token_id), captured


def test_add_app_by_session_owner_leaves_installed_by_none(cfg: Any, tmp_path: Path) -> None:
    """An owner-initiated (session) deploy leaves installed_by as None — attribution
    is only for token installs."""
    clone = tmp_path / "clone"
    _make_add_app_clone(clone)
    fake_clone, fake_insert, captured = _fake_add_app_deps(clone)

    cookies = auth_cookie(cfg)
    with (
        mock.patch.object(apps_routes, "clone_with_github_fallback", side_effect=fake_clone),
        mock.patch.object(apps_routes, "validate_manifest", return_value=None),
        mock.patch.object(apps_routes, "move_clone_to_app_temp_dir", return_value=str(clone)),
        mock.patch.object(apps_routes, "insert_and_deploy", side_effect=fake_insert),
        TestClient(app=make_test_app(api_apps_routes)) as client,
    ):
        resp = client.post(
            "/api/add_app",
            json={"repo_url": f"file://{clone}"},
            cookies=cookies,
        )
    assert resp.status_code == 200, resp.text
    assert captured["installed_by"] is None, captured


@pytest.fixture
def cfg(tmp_path_factory: pytest.TempPathFactory) -> Any:
    c = _make_test_config(tmp_path_factory.mktemp("set-remote"), port=20400)
    init_db(c.db_path)
    return c


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


def _seed_git_app(cfg: Any, name: str, repo_path: str, repo_url: str | None = None) -> str:
    app_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            """INSERT INTO apps (app_id, name, version, repo_path, repo_url, local_port, status)
               VALUES (?, ?, '1.0', ?, ?, ?, 'stopped')""",
            (app_id, name, repo_path, repo_url, 20401),
        )
        db.commit()
    finally:
        db.close()
    return app_id


def test_set_app_remote_persists_normalized_url(cfg: Any, tmp_path: Path) -> None:
    """Posting a new upstream stores it on the row; a bare host gets https:// and
    the ``@ref`` suffix is preserved."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")

    app_id = _seed_git_app(cfg, "myapp", str(repo), repo_url="https://github.com/old/repo")
    cookies = auth_cookie(cfg)
    with TestClient(app=make_test_app(api_apps_routes)) as client:
        resp = client.post(
            f"/set_app_remote/{app_id}",
            json={"repo_url": "github.com/new/repo@dev"},
            cookies=cookies,
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["repo_url"] == "https://github.com/new/repo@dev"

    db = sqlite3.connect(cfg.db_path)
    try:
        stored = db.execute("SELECT repo_url FROM apps WHERE app_id = ?", (app_id,)).fetchone()[0]
    finally:
        db.close()
    assert stored == "https://github.com/new/repo@dev"


def test_set_app_remote_rejects_builtin_without_git(cfg: Any, tmp_path: Path) -> None:
    """An app whose repo_path has no .git (builtin/copied app) cannot have its
    upstream edited."""
    plain = tmp_path / "plain"
    plain.mkdir()
    app_id = _seed_git_app(cfg, "builtin", str(plain), repo_url=None)
    cookies = auth_cookie(cfg)
    with TestClient(app=make_test_app(api_apps_routes)) as client:
        resp = client.post(
            f"/set_app_remote/{app_id}",
            json={"repo_url": "https://github.com/new/repo"},
            cookies=cookies,
        )
    assert resp.status_code == 400, resp.text
    assert "no git repository" in resp.json()["error"].lower()


@pytest.mark.parametrize(
    "ssh_url",
    ["git@github.com:user/repo.git", "ssh://git@github.com/user/repo.git"],
)
def test_set_app_remote_rejects_ssh_url(cfg: Any, tmp_path: Path, ssh_url: str) -> None:
    """The remote-update box rejects SSH URLs with an HTTPS-pointing error and
    leaves the stored upstream unchanged."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    app_id = _seed_git_app(cfg, "myapp", str(repo), repo_url="https://github.com/old/repo")
    cookies = auth_cookie(cfg)
    with TestClient(app=make_test_app(api_apps_routes)) as client:
        resp = client.post(f"/set_app_remote/{app_id}", json={"repo_url": ssh_url}, cookies=cookies)
    assert resp.status_code == 400, resp.text
    assert "HTTPS" in resp.json()["error"]

    db = sqlite3.connect(cfg.db_path)
    try:
        stored = db.execute("SELECT repo_url FROM apps WHERE app_id = ?", (app_id,)).fetchone()[0]
    finally:
        db.close()
    assert stored == "https://github.com/old/repo"


@pytest.mark.parametrize(
    "ssh_url",
    ["git@github.com:user/repo.git", "ssh://git@github.com/user/repo.git"],
)
def test_clone_and_get_app_info_rejects_ssh_url(cfg: Any, ssh_url: str) -> None:
    """The deploy page (clone-and-inspect) rejects SSH URLs up front with a
    clear error — no clone attempt, no GitHub OAuth bounce."""
    cookies = auth_cookie(cfg)
    with TestClient(app=make_test_app(api_apps_routes)) as client:
        resp = client.post("/api/clone_and_get_app_info", json={"repo_url": ssh_url}, cookies=cookies)
    assert resp.status_code == 400, resp.text
    assert "HTTPS" in resp.json()["error"]


def test_git_pull_rejects_ssh_url(tmp_path: Path) -> None:
    """git_pull (the Update & Reload path) fails cleanly on an SSH upstream
    rather than mangling it into a broken HTTPS URL."""
    ok, err = git_pull(str(tmp_path), "myapp", repo_url="git@github.com:user/repo.git")
    assert ok is False
    assert err is not None and "HTTPS" in err


def test_set_app_remote_rejects_empty(cfg: Any, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    app_id = _seed_git_app(cfg, "myapp", str(repo), repo_url="https://github.com/old/repo")
    cookies = auth_cookie(cfg)
    with TestClient(app=make_test_app(api_apps_routes)) as client:
        resp = client.post(f"/set_app_remote/{app_id}", json={"repo_url": "  "}, cookies=cookies)
    assert resp.status_code == 400, resp.text


def test_git_pull_switches_branch_via_ref(tmp_path: Path) -> None:
    """git_pull given a repo_url with an ``@ref`` checks out that branch even
    when the clone is currently on a different one."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    (origin / "f.txt").write_text("main")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "main commit")
    _git(origin, "checkout", "-b", "feature")
    (origin / "f.txt").write_text("feature")
    _git(origin, "commit", "-am", "feature commit")
    _git(origin, "checkout", "main")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", f"file://{origin}", str(clone))
    # Clone starts on main.
    head = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=clone, capture_output=True, text=True
    ).stdout.strip()
    assert head == "main"

    ok, err = git_pull(str(clone), "myapp", repo_url=f"file://{origin}@feature")
    assert ok, err

    head = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=clone, capture_output=True, text=True
    ).stdout.strip()
    assert head == "feature"
    assert (clone / "f.txt").read_text() == "feature"


def test_git_pull_checks_out_tag_ref(tmp_path: Path) -> None:
    """A pinned ``@tag`` (or commit) ref — which has no ``origin/<ref>``
    branch — is checked out detached at the fetched ref rather than erroring."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    (origin / "f.txt").write_text("v1")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "v1")
    _git(origin, "tag", "v1.0")
    (origin / "f.txt").write_text("v2")
    _git(origin, "commit", "-am", "v2")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", f"file://{origin}", str(clone))

    ok, err = git_pull(str(clone), "myapp", repo_url=f"file://{origin}@v1.0")
    assert ok, err
    assert (clone / "f.txt").read_text() == "v1"


def test_git_pull_returns_to_default_branch_when_ref_cleared(tmp_path: Path) -> None:
    """With no ``@ref`` in the repo_url, git_pull switches back to origin's
    default branch instead of staying on whatever branch was checked out."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    (origin / "f.txt").write_text("main")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "main commit")
    _git(origin, "checkout", "-b", "feature")
    (origin / "f.txt").write_text("feature")
    _git(origin, "commit", "-am", "feature commit")
    # Leave origin's HEAD pointing at the default branch (main).
    _git(origin, "checkout", "main")

    clone = tmp_path / "clone"
    # Clone and pin the working copy to the feature branch, as if the app had
    # previously been installed with ``@feature``.
    _git(tmp_path, "clone", "--branch", "feature", f"file://{origin}", str(clone))
    head = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=clone, capture_output=True, text=True
    ).stdout.strip()
    assert head == "feature"

    # Clearing the @ref (plain URL) should bring it back to main.
    ok, err = git_pull(str(clone), "myapp", repo_url=f"file://{origin}")
    assert ok, err

    head = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=clone, capture_output=True, text=True
    ).stdout.strip()
    assert head == "main"
    assert (clone / "f.txt").read_text() == "main"


def test_reload_update_pins_resolved_default_branch(cfg: Any, tmp_path: Path) -> None:
    """A refless update records the branch it landed on back into repo_url, so
    the app pins to a concrete branch (visible, deterministic) rather than
    re-resolving the remote default on every future pull."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    (origin / "f.txt").write_text("main")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "main commit")
    _git(origin, "checkout", "-b", "feature")
    _git(origin, "commit", "--allow-empty", "-m", "feature commit")
    _git(origin, "checkout", "main")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "--branch", "feature", f"file://{origin}", str(clone))

    # Stored upstream has no @ref (operator cleared it).
    app_id = _seed_git_app(cfg, "myapp", str(clone), repo_url=f"file://{origin}")

    cookies = auth_cookie(cfg)
    with (
        mock.patch.object(apps_routes, "stop_app_process"),
        mock.patch.object(apps_routes, "reload_app_background"),
        TestClient(app=make_test_app(api_apps_routes)) as client,
    ):
        resp = client.post(f"/reload_app/{app_id}", json={"update": True}, cookies=cookies)
    assert resp.status_code == 200, resp.text

    db = sqlite3.connect(cfg.db_path)
    try:
        stored = db.execute("SELECT repo_url FROM apps WHERE app_id = ?", (app_id,)).fetchone()[0]
    finally:
        db.close()
    assert stored == f"file://{origin}@main", stored


def test_add_app_pins_default_branch_at_install(cfg: Any, tmp_path: Path) -> None:
    """Installing a refless upstream records the cloned default branch into
    repo_url, so the app is pinned and the branch is visible from first deploy
    — not only after the first Update & Reload."""
    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-b", "main")
    (clone / "openhost.toml").write_text(
        '[app]\nname = "myapp"\nversion = "0.1.0"\n[runtime.container]\nimage = "Dockerfile"\nport = 8080\n'
    )
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "init")

    manifest = parse_manifest(str(clone))

    captured: dict[str, Any] = {}

    def fake_insert_and_deploy(*args: Any, **kwargs: Any) -> str:
        captured["repo_url"] = kwargs.get("repo_url")
        return new_app_id()

    async def fake_clone(repo_url: str, return_to: str) -> tuple[Any, str, None, None]:
        return manifest, str(clone), None, None

    cookies = auth_cookie(cfg)
    with (
        mock.patch.object(apps_routes, "clone_with_github_fallback", side_effect=fake_clone),
        mock.patch.object(apps_routes, "validate_manifest", return_value=None),
        mock.patch.object(apps_routes, "move_clone_to_app_temp_dir", return_value=str(clone)),
        mock.patch.object(apps_routes, "insert_and_deploy", side_effect=fake_insert_and_deploy),
        TestClient(app=make_test_app(api_apps_routes)) as client,
    ):
        resp = client.post(
            "/api/add_app",
            json={"repo_url": "https://github.com/owner/repo"},
            cookies=cookies,
        )
    assert resp.status_code == 200, resp.text
    assert captured["repo_url"] == "https://github.com/owner/repo@main", captured


def test_add_app_non_git_clone_does_not_pin_or_error(cfg: Any, tmp_path: Path) -> None:
    """A ``file://`` URL pointing at a plain directory is copied verbatim, so the
    landed clone has no ``.git``. Install must not try to read a branch from it
    (which would raise InvalidGitRepositoryError → HTTP 500); repo_url stays
    refless."""
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "openhost.toml").write_text(
        '[app]\nname = "myapp"\nversion = "0.1.0"\n[runtime.container]\nimage = "Dockerfile"\nport = 8080\n'
    )
    assert not (clone / ".git").exists()

    manifest = parse_manifest(str(clone))

    captured: dict[str, Any] = {}

    def fake_insert_and_deploy(*args: Any, **kwargs: Any) -> str:
        captured["repo_url"] = kwargs.get("repo_url")
        return new_app_id()

    async def fake_clone(repo_url: str, return_to: str) -> tuple[Any, str, None, None]:
        return manifest, str(clone), None, None

    cookies = auth_cookie(cfg)
    with (
        mock.patch.object(apps_routes, "clone_with_github_fallback", side_effect=fake_clone),
        mock.patch.object(apps_routes, "validate_manifest", return_value=None),
        mock.patch.object(apps_routes, "move_clone_to_app_temp_dir", return_value=str(clone)),
        mock.patch.object(apps_routes, "insert_and_deploy", side_effect=fake_insert_and_deploy),
        TestClient(app=make_test_app(api_apps_routes)) as client,
    ):
        resp = client.post(
            "/api/add_app",
            json={"repo_url": f"file://{clone}"},
            cookies=cookies,
        )
    assert resp.status_code == 200, resp.text
    assert captured["repo_url"] == f"file://{clone}", captured
