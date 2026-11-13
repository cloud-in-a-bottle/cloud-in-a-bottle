from __future__ import annotations

import itertools
import subprocess
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC
from datetime import datetime
from datetime import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from litestar.testing import TestClient

import compute_space.core.auto_update.apps as apps_mod
import compute_space.core.auto_update.runner as runner_mod
from compute_space.config import Config
from compute_space.core import archive_backend
from compute_space.core.app_id import new_app_id
from compute_space.core.auto_update.app_config import AppAutoUpdateLastRun
from compute_space.core.auto_update.app_config import AppUpdateOutcome
from compute_space.core.auto_update.app_config import AppUpdateResult
from compute_space.core.auto_update.app_config import read_app_auto_update_enabled
from compute_space.core.auto_update.app_config import read_app_last_run
from compute_space.core.auto_update.app_config import record_app_last_run
from compute_space.core.auto_update.app_config import write_app_auto_update_enabled
from compute_space.core.auto_update.config import AutoUpdateConfig
from compute_space.core.auto_update.config import AutoUpdateLastRun
from compute_space.core.auto_update.config import AutoUpdateOutcome
from compute_space.core.auto_update.config import record_last_run
from compute_space.core.auto_update.config import write_auto_update_config
from compute_space.core.settings_store import set_setting
from compute_space.db import get_db
from compute_space.tests._litestar_helpers import auth_cookie
from compute_space.tests._litestar_helpers import make_test_app
from compute_space.web.routes.api.settings import api_settings_routes

from .conftest import _make_test_config

_MANIFEST = """\
[app]
name = "{name}"
version = "{version}"

[runtime.container]
image = "Dockerfile"
port = 5000

{extra}
"""

_NEW_PERMISSION = """\
[[services.v2.consumes]]
service = "github.com/x/secrets"
shortname = "secrets"
version = ">=0.1.0"
grants = [{ key = "API_KEY" }]
"""


_ports = itertools.count(20951)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    data = tmp_path / "data"
    data.mkdir()
    return _make_test_config(data, port=20950)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def _commit_manifest(repo: Path, name: str, version: str, extra: str = "") -> str:
    (repo / "cloudinabottle.toml").write_text(_MANIFEST.format(name=name, version=version, extra=extra))
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", version)
    return _git(repo, "rev-parse", "HEAD")


class FakeApp:
    """A running app whose checkout is at v1, with v2 committed but not yet pulled. The fake pull moves to v2."""

    def __init__(self, cfg: Config, tmp_path: Path, name: str, v2_extra: str = "", status: str = "running") -> None:
        self.repo = tmp_path / name
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        self.v1 = _commit_manifest(self.repo, name, "1.0.0")
        self.v1_raw = (self.repo / "cloudinabottle.toml").read_text()
        self.v2 = _commit_manifest(self.repo, name, "1.1.0", v2_extra)
        # Stands in for the remote's newest commit, which the fake pull checks out.
        _git(self.repo, "tag", "upstream", self.v2)
        _git(self.repo, "reset", "-q", "--hard", self.v1)
        self.app_id = new_app_id()
        self.repo_url = f"https://github.com/x/{name}@main"
        with closing(get_db()) as db:
            db.execute(
                "INSERT INTO apps (app_id, name, version, repo_path, repo_url, local_port, status, manifest_raw) "
                "VALUES (?, ?, '1.0.0', ?, ?, ?, ?, ?)",
                (self.app_id, name, str(self.repo), self.repo_url, next(_ports), status, self.v1_raw),
            )
            db.commit()

    def head(self) -> str:
        return _git(self.repo, "rev-parse", "HEAD")


@pytest.fixture
def side_effects(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Stub the network and container parts of an update: the pull checks out the newest commit, and the rebuild
    succeeds unless a test sets ``rebuild_error``."""
    state: dict[str, Any] = {"pulls": 0, "reloads": [], "stops": 0, "rebuild_error": None, "pull_error": None}

    def fake_pull(repo_path: str, app_name: str, **_: Any) -> tuple[bool, str | None]:
        state["pulls"] += 1
        if state["pull_error"] is not None:
            return False, state["pull_error"]
        _git(Path(repo_path), "reset", "-q", "--hard", "upstream")
        return True, None

    def fake_stop(_row: Any) -> None:
        state["stops"] += 1

    def fake_reload(app_id: str, _repo_path: str, _config: Config) -> None:
        state["reloads"].append(app_id)
        with closing(get_db()) as db:
            if state["rebuild_error"] is None:
                db.execute("UPDATE apps SET status = 'running' WHERE app_id = ?", (app_id,))
            else:
                db.execute(
                    "UPDATE apps SET status = 'error', error_message = ? WHERE app_id = ?",
                    (state["rebuild_error"], app_id),
                )
            db.commit()

    monkeypatch.setattr(apps_mod, "git_pull", fake_pull)
    monkeypatch.setattr(apps_mod, "stop_app_process", fake_stop)
    monkeypatch.setattr(apps_mod, "reload_app_background", fake_reload)
    monkeypatch.setattr(archive_backend, "is_archive_dir_healthy", lambda _c, _db: True)
    yield state


def _status(app: FakeApp) -> str:
    with closing(get_db()) as db:
        return str(db.execute("SELECT status FROM apps WHERE app_id = ?", (app.app_id,)).fetchone()["status"])


# ─────────────── one app ───────────────


@pytest.mark.asyncio
async def test_update_without_review_is_applied(cfg: Config, tmp_path: Path, side_effects: dict[str, Any]) -> None:
    # Only the version changed, which never needs the owner's approval.
    app = FakeApp(cfg, tmp_path, "plain")
    result = await apps_mod.auto_update_app(app.app_id, cfg)
    assert result.outcome == AppUpdateOutcome.UPDATED
    assert app.head() == app.v2
    assert side_effects["reloads"] == [app.app_id]
    assert _status(app) == "running"


@pytest.mark.asyncio
async def test_update_keeps_tracking_default_branch(cfg: Config, tmp_path: Path, side_effects: dict[str, Any]) -> None:
    app = FakeApp(cfg, tmp_path, "refless")
    repo_url = "https://github.com/x/refless"
    with closing(get_db()) as db:
        db.execute("UPDATE apps SET repo_url = ? WHERE app_id = ?", (repo_url, app.app_id))
        db.commit()

    result = await apps_mod.auto_update_app(app.app_id, cfg)

    assert result.outcome == AppUpdateOutcome.UPDATED
    with closing(get_db()) as db:
        assert (
            db.execute("SELECT repo_url FROM apps WHERE app_id = ?", (app.app_id,)).fetchone()["repo_url"] == repo_url
        )


@pytest.mark.asyncio
async def test_update_needing_approval_is_left_and_rolled_back(
    cfg: Config, tmp_path: Path, side_effects: dict[str, Any]
) -> None:
    app = FakeApp(cfg, tmp_path, "perms", v2_extra=_NEW_PERMISSION)
    result = await apps_mod.auto_update_app(app.app_id, cfg)
    assert result.outcome == AppUpdateOutcome.NEEDS_APPROVAL
    assert result.detail is not None and "github.com/x/secrets" in result.detail
    # The checkout matches the running version again, and the app wasn't touched.
    assert app.head() == app.v1
    assert side_effects["reloads"] == []
    assert side_effects["stops"] == 0
    assert _status(app) == "running"


@pytest.mark.asyncio
async def test_already_up_to_date(cfg: Config, tmp_path: Path, side_effects: dict[str, Any]) -> None:
    app = FakeApp(cfg, tmp_path, "current")
    _git(app.repo, "reset", "-q", "--hard", app.v2)
    result = await apps_mod.auto_update_app(app.app_id, cfg)
    assert result.outcome == AppUpdateOutcome.UP_TO_DATE
    assert side_effects["reloads"] == []


@pytest.mark.asyncio
async def test_failed_rebuild_is_reported(cfg: Config, tmp_path: Path, side_effects: dict[str, Any]) -> None:
    side_effects["rebuild_error"] = "Container build failed"
    app = FakeApp(cfg, tmp_path, "broken")
    result = await apps_mod.auto_update_app(app.app_id, cfg)
    assert result.outcome == AppUpdateOutcome.FAILED
    assert result.detail == "Container build failed"


@pytest.mark.asyncio
async def test_failed_pull_leaves_app_running(cfg: Config, tmp_path: Path, side_effects: dict[str, Any]) -> None:
    side_effects["pull_error"] = "could not resolve host"
    app = FakeApp(cfg, tmp_path, "offline")
    app.repo_url = "https://example.com/x/offline"
    with closing(get_db()) as db:
        db.execute("UPDATE apps SET repo_url = ? WHERE app_id = ?", (app.repo_url, app.app_id))
        db.commit()
    result = await apps_mod.auto_update_app(app.app_id, cfg)
    assert result.outcome == AppUpdateOutcome.FAILED
    assert result.detail is not None and "could not resolve host" in result.detail
    assert app.head() == app.v1
    assert _status(app) == "running"


@pytest.mark.asyncio
async def test_failed_log_write_rolls_back_without_stopping_app(
    cfg: Config, tmp_path: Path, side_effects: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app = FakeApp(cfg, tmp_path, "unwritable")

    def fail_log(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(apps_mod, "archive_old_log", fail_log)
    result = await apps_mod.auto_update_app(app.app_id, cfg)

    assert result.outcome == AppUpdateOutcome.FAILED
    assert result.detail is not None and "disk full" in result.detail
    assert app.head() == app.v1
    assert _status(app) == "running"
    assert side_effects["stops"] == 0
    assert side_effects["reloads"] == []


@pytest.mark.asyncio
async def test_stopped_app_is_skipped(cfg: Config, tmp_path: Path, side_effects: dict[str, Any]) -> None:
    app = FakeApp(cfg, tmp_path, "stopped", status="stopped")
    result = await apps_mod.auto_update_app(app.app_id, cfg)
    assert result.outcome == AppUpdateOutcome.SKIPPED
    assert side_effects["pulls"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["stopped", "building"])
async def test_app_leaving_running_during_the_update_is_skipped(
    cfg: Config, tmp_path: Path, side_effects: dict[str, Any], monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    app = FakeApp(cfg, tmp_path, "midway")
    pull = apps_mod.git_pull

    def pull_then_owner_acts(repo_path: str, app_name: str, **kwargs: Any) -> tuple[bool, str | None]:
        # The owner stops (or reloads) the app while its update is pulling.
        with closing(get_db()) as db:
            db.execute("UPDATE apps SET status = ? WHERE app_id = ?", (status, app.app_id))
            db.commit()
        return pull(repo_path, app_name, **kwargs)

    monkeypatch.setattr(apps_mod, "git_pull", pull_then_owner_acts)
    result = await apps_mod.auto_update_app(app.app_id, cfg)

    assert result.outcome == AppUpdateOutcome.SKIPPED
    assert result.detail == f"not running ({status})"
    assert app.head() == app.v1
    assert _status(app) == status
    assert side_effects["stops"] == 0
    assert side_effects["reloads"] == []


@pytest.mark.asyncio
async def test_local_changes_are_skipped(cfg: Config, tmp_path: Path, side_effects: dict[str, Any]) -> None:
    app = FakeApp(cfg, tmp_path, "edited")
    (app.repo / "local.txt").write_text("mine")
    result = await apps_mod.auto_update_app(app.app_id, cfg)
    assert result.outcome == AppUpdateOutcome.SKIPPED
    assert result.detail == "has local changes"
    assert side_effects["pulls"] == 0


@pytest.mark.asyncio
async def test_commit_pin_is_skipped(cfg: Config, tmp_path: Path, side_effects: dict[str, Any]) -> None:
    app = FakeApp(cfg, tmp_path, "pinned")
    with closing(get_db()) as db:
        db.execute(
            "UPDATE apps SET repo_url = ? WHERE app_id = ?", (f"https://github.com/x/pinned@{app.v1}", app.app_id)
        )
        db.commit()
    result = await apps_mod.auto_update_app(app.app_id, cfg)
    assert result.outcome == AppUpdateOutcome.SKIPPED
    assert side_effects["pulls"] == 0


# ─────────────── a whole run ───────────────


@pytest.mark.asyncio
async def test_run_updates_every_app_and_records_outcomes(
    cfg: Config, tmp_path: Path, side_effects: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = FakeApp(cfg, tmp_path, "a-plain")
    perms = FakeApp(cfg, tmp_path, "b-perms", v2_extra=_NEW_PERMISSION)
    crashing = FakeApp(cfg, tmp_path, "c-crashing")
    later = FakeApp(cfg, tmp_path, "d-later")

    real = apps_mod.auto_update_app

    async def crash_one(app_id: str, config: Config) -> AppUpdateResult:
        if app_id == crashing.app_id:
            raise RuntimeError("boom")
        return await real(app_id, config)

    monkeypatch.setattr(apps_mod, "auto_update_app", crash_one)
    await apps_mod.run_app_auto_updates()

    with closing(get_db()) as db:
        last = read_app_last_run(db)
    assert last is not None
    outcomes = {r.app_name: (r.outcome, r.detail) for r in last.results}
    assert outcomes["a-plain"][0] == AppUpdateOutcome.UPDATED
    assert outcomes["b-perms"][0] == AppUpdateOutcome.NEEDS_APPROVAL
    assert outcomes["c-crashing"] == (AppUpdateOutcome.FAILED, "boom")
    # One app's failure didn't stop the next.
    assert outcomes["d-later"][0] == AppUpdateOutcome.UPDATED
    assert plain.head() == plain.v2 and perms.head() == perms.v1 and later.head() == later.v2


# ─────────────── settings ───────────────


def test_app_auto_update_defaults_off(cfg: Config) -> None:
    with closing(get_db()) as db:
        assert read_app_auto_update_enabled(db) is False


def test_app_auto_update_setting_roundtrip(cfg: Config) -> None:
    with closing(get_db()) as db:
        write_app_auto_update_enabled(db, True)
        assert read_app_auto_update_enabled(db) is True


def test_invalid_app_auto_update_value_fails_loudly(cfg: Config) -> None:
    with closing(get_db()) as db:
        set_setting(db, "app_auto_update_enabled", "yes")
        with pytest.raises(ValueError):
            read_app_auto_update_enabled(db)


def test_last_run_roundtrip(cfg: Config) -> None:
    run = AppAutoUpdateLastRun(
        at=datetime(2026, 11, 6, 4, 0, tzinfo=UTC),
        results=[
            AppUpdateResult(app_id="a1", app_name="one", outcome=AppUpdateOutcome.UPDATED, detail="aaa -> bbb"),
            AppUpdateResult(app_id="a2", app_name="two", outcome=AppUpdateOutcome.UP_TO_DATE),
        ],
    )
    with closing(get_db()) as db:
        record_app_last_run(db, run)
        assert read_app_last_run(db) == run


# ─────────────── the scheduled window ───────────────


@pytest.fixture
def window(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records what the scheduled window ran, in order, with the window due now."""
    ran: list[str] = []

    async def apps() -> None:
        ran.append("apps")

    async def instance() -> None:
        ran.append("instance")

    monkeypatch.setattr(runner_mod, "run_app_auto_updates", apps)
    monkeypatch.setattr(runner_mod, "run_auto_update", instance)
    monkeypatch.setattr(runner_mod, "_update_due", datetime.now(UTC) - timedelta(minutes=1))
    monkeypatch.setattr(runner_mod, "_app_update_due", datetime.now(UTC) - timedelta(minutes=1))
    return ran


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("instance_on", "apps_on", "expected"),
    [(True, True, ["apps", "instance"]), (False, True, ["apps"]), (True, False, ["instance"]), (False, False, [])],
)
async def test_window_runs_apps_before_the_instance(
    window: list[str], instance_on: bool, apps_on: bool, expected: list[str]
) -> None:
    with closing(get_db()) as db:
        write_auto_update_config(db, AutoUpdateConfig(enabled=instance_on, time_utc=time(4, 0)))
        write_app_auto_update_enabled(db, apps_on)
    await runner_mod._run_if_due()
    assert window == expected


@pytest.mark.asyncio
async def test_failed_app_run_still_updates_the_instance(window: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_apps() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(runner_mod, "run_app_auto_updates", failing_apps)
    with closing(get_db()) as db:
        write_auto_update_config(db, AutoUpdateConfig(enabled=True, time_utc=time(4, 0)))
        write_app_auto_update_enabled(db, True)
    await runner_mod._run_if_due()
    assert window == ["instance"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("instance_missed", "apps_missed", "expected"),
    [(True, False, ["instance"]), (False, True, ["apps"]), (True, True, ["apps", "instance"]), (False, False, [])],
)
async def test_restart_catches_up_only_what_missed_the_window(
    window: list[str], monkeypatch: pytest.MonkeyPatch, instance_missed: bool, apps_missed: bool, expected: list[str]
) -> None:
    # Just restarted: the due times come from each updater's own last run.
    monkeypatch.setattr(runner_mod, "_update_due", None)
    monkeypatch.setattr(runner_mod, "_app_update_due", None)
    now = datetime.now(UTC)
    with closing(get_db()) as db:
        write_auto_update_config(db, AutoUpdateConfig(enabled=True, time_utc=time(4, 0)))
        write_app_auto_update_enabled(db, True)
        record_last_run(
            db,
            AutoUpdateLastRun(
                at=now - timedelta(days=2) if instance_missed else now, outcome=AutoUpdateOutcome.UP_TO_DATE
            ),
        )
        record_app_last_run(db, AppAutoUpdateLastRun(at=now - timedelta(days=2) if apps_missed else now, results=[]))
    await runner_mod._run_if_due()
    await runner_mod._run_if_due()
    assert window == expected
    assert runner_mod._update_due is not None and runner_mod._update_due > now
    assert runner_mod._app_update_due is not None and runner_mod._app_update_due > now


@pytest.mark.asyncio
async def test_disabled_updater_does_not_hold_back_the_other(
    window: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # App updates are off and were never run; the instance's missed window still catches up.
    monkeypatch.setattr(runner_mod, "_update_due", None)
    monkeypatch.setattr(runner_mod, "_app_update_due", None)
    with closing(get_db()) as db:
        write_auto_update_config(db, AutoUpdateConfig(enabled=True, time_utc=time(4, 0)))
        record_last_run(
            db, AutoUpdateLastRun(at=datetime.now(UTC) - timedelta(days=2), outcome=AutoUpdateOutcome.UP_TO_DATE)
        )
    await runner_mod._run_if_due()
    assert window == ["instance"]


# ─────────────── settings API ───────────────


def test_settings_api_toggles_and_reports_last_run(cfg: Config) -> None:
    with closing(get_db()) as db:
        record_app_last_run(
            db,
            AppAutoUpdateLastRun(
                at=datetime(2026, 11, 6, 4, 0, tzinfo=UTC),
                results=[
                    AppUpdateResult(
                        app_id="a1", app_name="perms", outcome=AppUpdateOutcome.NEEDS_APPROVAL, detail="approve x"
                    )
                ],
            ),
        )
    with TestClient(app=make_test_app(api_settings_routes)) as client:
        client.cookies.update(auth_cookie(cfg))
        assert client.get("/api/settings/app-auto-update").json()["enabled"] is False
        body = client.post("/api/settings/app-auto-update", json={"enabled": True}).json()
    assert body["enabled"] is True
    assert body["last_run_results"] == [{"app_name": "perms", "outcome": "needs_approval", "detail": "approve x"}]


@pytest.mark.parametrize(
    ("was_on", "turn_on", "reschedules"),
    [(False, True, True), (True, False, False), (True, True, False), (False, False, False)],
)
def test_only_turning_app_updates_on_reschedules(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, was_on: bool, turn_on: bool, reschedules: bool
) -> None:
    # Catch-ups for missed windows are pending; only turning app updates on should move the apps' one, and nothing
    # here should move the instance's.
    missed = datetime.now(UTC) - timedelta(hours=1)
    monkeypatch.setattr(runner_mod, "_update_due", missed)
    monkeypatch.setattr(runner_mod, "_app_update_due", missed)
    with closing(get_db()) as db:
        write_app_auto_update_enabled(db, was_on)
    with TestClient(app=make_test_app(api_settings_routes)) as client:
        client.cookies.update(auth_cookie(cfg))
        assert client.post("/api/settings/app-auto-update", json={"enabled": turn_on}).status_code == 200
    assert runner_mod._update_due == missed
    assert runner_mod._app_update_due is not None
    assert (runner_mod._app_update_due > datetime.now(UTC)) is reschedules
    if not reschedules:
        assert runner_mod._app_update_due == missed
