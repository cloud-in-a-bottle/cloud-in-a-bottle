"""Tests for handing the apply walk to systemd as its own unit.

The walk cannot stop openhost while it is a child of openhost's cgroup: the
stop's SIGTERM would kill it mid-flight, leaving migrations half-applied and
nothing to start the service again. So `update apply` re-launches itself as
openhost-apply.service first. These tests pin that handoff, the failsafe
property that guarantees the service comes back, and the ordering that makes
migrations run against a stopped router.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import openhost_system_agent.detach as detach_mod
import openhost_system_agent.update as update_mod
from openhost_system_agent.detach import ApplyAlreadyRunningError
from openhost_system_agent.updater import paths as updater_paths
from openhost_system_agent.updater import progress as updater_progress


def _ok(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _fail(stderr: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


def _recorder(calls: list[list[str]]) -> object:
    def _run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _ok()

    return _run


@pytest.fixture
def progress_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(updater_paths.DATA_DIR_ENV, str(tmp_path))
    return tmp_path


# ── the handoff ──────────────────────────────────────────────────────


def test_detach_launches_a_transient_unit_with_the_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def _run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _ok()

    monkeypatch.setattr(detach_mod, "systemd_run_available", lambda: True)
    monkeypatch.setattr("openhost_system_agent.detach.subprocess.run", _run)

    detach_mod.detach_apply()

    cmd = calls[0]
    assert cmd[0] == "systemd-run"
    assert f"--unit={detach_mod.APPLY_UNIT}" in cmd
    # The marker is what stops the detached copy from detaching again forever.
    assert f"--setenv={detach_mod.DETACHED_ENV}=1" in cmd


def test_detach_sets_the_execstoppost_failsafe(monkeypatch: pytest.MonkeyPatch) -> None:
    # The single most important property here: however the walk dies -- crash,
    # OOM, SIGKILL, timeout -- systemd still starts openhost, so no exit path can
    # leave the instance stopped.
    calls: list[list[str]] = []
    monkeypatch.setattr(detach_mod, "systemd_run_available", lambda: True)
    monkeypatch.setattr("openhost_system_agent.detach.subprocess.run", _recorder(calls))

    detach_mod.detach_apply()

    stop_post = [c for c in calls[0] if c.startswith("--property=ExecStopPost=")]
    assert len(stop_post) == 1
    # Absolute path (systemd requires one) and a non-blocking start (so it cannot
    # wait on a job from inside the unit's own teardown).
    assert stop_post[0].endswith(f"systemctl start --no-block {detach_mod.OPENHOST_UNIT}")
    assert "--property=ExecStopPost=/" in stop_post[0]


def test_detach_forwards_the_data_dir_override(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without this the detached walk would resolve a different progress log and
    # token than the compute_space that launched it.
    calls: list[list[str]] = []
    monkeypatch.setenv(updater_paths.DATA_DIR_ENV, "/custom/data")
    monkeypatch.setattr(detach_mod, "systemd_run_available", lambda: True)
    monkeypatch.setattr("openhost_system_agent.detach.subprocess.run", _recorder(calls))

    detach_mod.detach_apply()

    assert f"--setenv={updater_paths.DATA_DIR_ENV}=/custom/data" in calls[0]


def test_detach_reports_an_apply_already_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(detach_mod, "systemd_run_available", lambda: True)
    monkeypatch.setattr(
        "openhost_system_agent.detach.subprocess.run",
        lambda *a, **k: _fail("Failed to start transient service unit: Unit openhost-apply.service already exists."),
    )

    with pytest.raises(ApplyAlreadyRunningError):
        detach_mod.detach_apply()


def test_detach_refuses_without_systemd_run(monkeypatch: pytest.MonkeyPatch) -> None:
    # No inline fallback on purpose: with nothing outside openhost's cgroup to
    # survive the stop, stopping the service would strand the instance.
    monkeypatch.setattr(detach_mod, "systemd_run_available", lambda: False)
    ran: list[list[str]] = []
    monkeypatch.setattr("openhost_system_agent.detach.subprocess.run", _recorder(ran))

    with pytest.raises(RuntimeError, match="systemd-run"):
        detach_mod.detach_apply()
    assert ran == []


# ── start_apply: the entry point ─────────────────────────────────────


def test_start_apply_detaches_and_does_not_run_the_walk(monkeypatch: pytest.MonkeyPatch, progress_dir: Path) -> None:
    monkeypatch.setattr(update_mod, "is_detached", lambda: False)
    monkeypatch.setattr(update_mod, "apply_is_running", lambda: False)
    detached: list[bool] = []
    monkeypatch.setattr(update_mod, "detach_apply", lambda: detached.append(True))
    with patch.object(update_mod, "apply_update") as walk:
        update_mod.start_apply()

    assert detached == [True]
    assert not walk.called  # the walk belongs to the detached copy


def test_start_apply_runs_the_walk_when_already_detached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_mod, "is_detached", lambda: True)
    monkeypatch.setattr(update_mod, "detach_apply", lambda: pytest.fail("must not detach twice"))
    monkeypatch.setattr(update_mod, "apply_is_running", lambda: pytest.fail("must not re-check"))
    with patch.object(update_mod, "apply_update") as walk:
        update_mod.start_apply()

    assert walk.called


def test_start_apply_records_failure_when_the_handoff_fails(
    monkeypatch: pytest.MonkeyPatch, progress_dir: Path
) -> None:
    # A launch failure must still leave the log terminal, or the /updating page
    # would spin forever on an update that never started.
    monkeypatch.setattr(update_mod, "is_detached", lambda: False)
    monkeypatch.setattr(update_mod, "apply_is_running", lambda: False)

    def _boom() -> None:
        raise RuntimeError("systemd said no")

    monkeypatch.setattr(update_mod, "detach_apply", _boom)

    with pytest.raises(RuntimeError, match="systemd said no"):
        update_mod.start_apply()

    entries = updater_progress.read_entries()
    assert updater_progress.is_terminal(entries) is True
    assert "systemd said no" in str(entries[-1]["message"])


def test_start_apply_leaves_the_log_alone_when_one_is_already_running(
    monkeypatch: pytest.MonkeyPatch, progress_dir: Path
) -> None:
    # The in-flight apply owns the log. Recording a terminal failure here would
    # blank it out from under the page that is watching it.
    updater_progress.record("migrate", "Applying system migrations…")
    monkeypatch.setattr(update_mod, "is_detached", lambda: False)
    monkeypatch.setattr(update_mod, "apply_is_running", lambda: True)
    monkeypatch.setattr(update_mod, "detach_apply", lambda: pytest.fail("must not launch a second walk"))

    with pytest.raises(ApplyAlreadyRunningError):
        update_mod.start_apply()

    entries = updater_progress.read_entries()
    assert [e["phase"] for e in entries] == ["migrate"]
    assert updater_progress.is_terminal(entries) is False


# ── ordering: the reason the change exists ───────────────────────────


def test_walk_stops_openhost_before_touching_the_working_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, progress_dir: Path
) -> None:
    # The point of the whole change: nothing of ours may be reading the DB, the
    # data dir or the pixi env by the time the checkout and migrations run.
    order: list[str] = []

    class _Repo:
        def __init__(self) -> None:
            self.git = self

        def is_dirty(self, **_kwargs: object) -> bool:
            return False

        def fetch(self, *_args: str) -> None:
            order.append("fetch")

        def checkout(self, *_args: str) -> None:
            order.append("checkout")

        def clean(self, *_args: str) -> None:
            order.append("clean")

    def _launch() -> bool:
        order.append("launch_updater")
        return True

    monkeypatch.setattr(update_mod, "launch_updater", _launch)
    monkeypatch.setattr(update_mod, "stop_openhost", lambda: order.append("stop_openhost"))
    monkeypatch.setattr(update_mod, "_repo", lambda: _Repo())
    monkeypatch.setattr(update_mod, "_get_sorted_tags", lambda _r: ["v1"])
    monkeypatch.setattr(update_mod, "_next_step", lambda _r: "v1")
    monkeypatch.setattr(update_mod, "_resolve_ref_sha", lambda _r, ref: ref)
    monkeypatch.setattr("openhost_system_agent.update.os.execv", lambda *_a: order.append("execv"))

    update_mod.apply_update()

    # Downtime is covered first, then the service goes away, and only then does
    # anything touch the tree.
    assert order[:2] == ["launch_updater", "stop_openhost"]
    assert order.index("stop_openhost") < order.index("checkout")
    assert order[-1] == "execv"


# ── stop/start semantics ─────────────────────────────────────────────


def test_stop_openhost_tolerates_an_uninstalled_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    # A baseline host has no openhost.service until the migrations in this very
    # walk install it, so "not loaded" must not abort the update.
    monkeypatch.setattr(
        "openhost_system_agent.detach.subprocess.run",
        lambda *a, **k: _fail("Failed to stop openhost.service: Unit openhost.service not loaded."),
    )
    detach_mod.stop_openhost()


def test_stop_openhost_raises_on_a_real_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # Carrying on here would run migrations against a live router.
    monkeypatch.setattr(
        "openhost_system_agent.detach.subprocess.run", lambda *a, **k: _fail("Interactive authentication required.")
    )
    with pytest.raises(RuntimeError, match="systemctl stop"):
        detach_mod.stop_openhost()


def test_start_openhost_raises_when_systemd_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openhost_system_agent.detach.subprocess.run", lambda *a, **k: _fail("Job failed"))
    with pytest.raises(RuntimeError, match="systemctl start"):
        detach_mod.start_openhost()


def test_apply_is_running_reads_unit_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openhost_system_agent.detach.subprocess.run", lambda *a, **k: _ok())
    assert detach_mod.apply_is_running() is True

    monkeypatch.setattr("openhost_system_agent.detach.subprocess.run", lambda *a, **k: _fail("inactive"))
    assert detach_mod.apply_is_running() is False


def test_apply_is_running_false_when_systemctl_is_unusable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unknown state must not block updates; systemd-run's own name collision is
    # still there as the real guard.
    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("no systemctl")

    monkeypatch.setattr("openhost_system_agent.detach.subprocess.run", _boom)
    assert detach_mod.apply_is_running() is False
