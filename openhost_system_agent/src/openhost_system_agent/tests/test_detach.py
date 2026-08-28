"""Tests for handing the apply walk to systemd as its own unit.

The walk cannot stop openhost while it is a child of openhost's cgroup: the stop
would kill it mid-flight, leaving migrations half-applied and nothing to start the
service again. Most of what is asserted here is a regression found by running real
updates on live hosts, which unit tests with a mocked subprocess could not see.
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


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


def _noting(events: list[str], what: str, result: object = None) -> object:
    """A stub that records it was called."""

    def _call(*_a: object, **_k: object) -> object:
        events.append(what)
        return result

    return _call


def _recorder(calls: list[list[str]], result: subprocess.CompletedProcess[str] | None = None) -> object:
    def _run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return result or _ok()

    return _run


@pytest.fixture(autouse=True)
def _systemd_run_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the host tooling exists; the sandbox has no systemd."""
    monkeypatch.setattr("openhost_system_agent.detach.shutil.which", lambda name: f"/usr/bin/{name}")


@pytest.fixture
def progress_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(updater_paths.DATA_DIR_ENV, str(tmp_path))
    return tmp_path


class _Repo:
    """Read-only stand-in for the parts of gitpython the walk touches."""

    def __init__(self, order: list[str] | None = None, dirty: bool = False) -> None:
        self.git = self
        self._order = order if order is not None else []
        self._dirty = dirty

    def _note(self, what: str) -> None:
        self._order.append(what)

    def is_dirty(self, **_kwargs: object) -> bool:
        self._note("is_dirty")
        return self._dirty

    def fetch(self, *_args: str, **_kwargs: object) -> None:
        self._note("fetch")

    def checkout(self, *_args: str) -> None:
        self._note("checkout")

    def clean(self, *_args: str) -> None:
        self._note("clean")


# ── the handoff ──────────────────────────────────────────────────────


def test_detach_launches_a_transient_unit_with_everything_it_needs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setenv("HOME", "/root")
    monkeypatch.setenv(updater_paths.DATA_DIR_ENV, "/custom/data")
    monkeypatch.setattr("openhost_system_agent.detach.subprocess.run", _recorder(calls))

    detach_mod.detach_apply()

    cmd = calls[0]
    assert cmd[0] == "systemd-run"
    assert f"--unit={detach_mod.APPLY_UNIT}" in cmd
    # The marker stops the detached copy from handing itself off forever.
    assert f"--setenv={detach_mod.DETACHED_ENV}=1" in cmd
    # REGRESSION: transient units get no HOME, and without one the walk died on its
    # first git call ("fatal: $HOME not set") on every host.
    assert "--setenv=HOME=/root" in cmd
    # Without this the detached walk resolves a different progress log and token.
    assert f"--setenv={updater_paths.DATA_DIR_ENV}=/custom/data" in cmd
    # OpenHost -> Cloud in a Bottle rename: both env-var names are exported so the
    # detached walk resolves them whichever it reads.
    assert f"--setenv={detach_mod.DETACHED_ENV_NEW}=1" in cmd
    assert f"--setenv={updater_paths.DATA_DIR_ENV_NEW}=/custom/data" in cmd
    # A wedged walk would otherwise hold the instance down indefinitely.
    assert f"--property=RuntimeMaxSec={detach_mod._RUNTIME_MAX_SECONDS}" in cmd

    # The failsafe: however the walk ends, openhost is started again. REGRESSION:
    # it must reset the start-rate-limit first, or an exhausted budget refuses the
    # failsafe itself and the instance stays down until someone SSHes in.
    stop_post = [c for c in cmd if c.startswith("--property=ExecStopPost=")]
    assert len(stop_post) == 1
    assert "--property=ExecStopPost=/bin/sh" in stop_post[0]
    assert stop_post[0].index("reset-failed") < stop_post[0].index("start --no-block")


def test_detach_falls_back_to_root_home(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setattr("openhost_system_agent.detach.subprocess.run", _recorder(calls))

    detach_mod.detach_apply()

    assert "--setenv=HOME=/root" in calls[0]


def test_detach_reports_an_apply_already_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "openhost_system_agent.detach.subprocess.run",
        lambda *a, **k: _fail("Failed to start transient service unit: Unit openhost-apply.service already exists."),
    )

    with pytest.raises(ApplyAlreadyRunningError):
        detach_mod.detach_apply()


def test_detach_refuses_without_systemd_run(monkeypatch: pytest.MonkeyPatch) -> None:
    # No inline fallback on purpose: with nothing outside openhost's cgroup to
    # survive the stop, stopping the service would strand the instance.
    ran: list[list[str]] = []
    monkeypatch.setattr("openhost_system_agent.detach.shutil.which", lambda _n: None)
    monkeypatch.setattr("openhost_system_agent.detach.subprocess.run", _recorder(ran))

    with pytest.raises(RuntimeError, match="systemd-run"):
        detach_mod.detach_apply()
    assert ran == []


# ── start_apply ──────────────────────────────────────────────────────


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
    with patch.object(update_mod, "apply_update") as walk:
        update_mod.start_apply()

    assert walk.called


def test_start_apply_records_failure_when_the_handoff_fails(
    monkeypatch: pytest.MonkeyPatch, progress_dir: Path
) -> None:
    # Or the /updating page polls forever on an update that never started.
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
    # The in-flight apply owns the log; blanking it would strand the page watching.
    updater_progress.record("migrate", "Applying system migrations…")
    monkeypatch.setattr(update_mod, "is_detached", lambda: False)
    monkeypatch.setattr(update_mod, "apply_is_running", lambda: True)
    monkeypatch.setattr(update_mod, "detach_apply", lambda: pytest.fail("must not launch a second walk"))

    with pytest.raises(ApplyAlreadyRunningError):
        update_mod.start_apply()

    assert [e["phase"] for e in updater_progress.read_entries()] == ["migrate"]


# ── ordering: the reason the change exists ───────────────────────────


def test_walk_order_preflight_then_stop_then_tree(monkeypatch: pytest.MonkeyPatch, progress_dir: Path) -> None:
    # Nothing of ours may be reading the DB or the data dir once the tree changes,
    # and the fetch/dirty checks only read the repo -- so they run with the router
    # still up, where a dirty tree or an unreachable remote costs no downtime.
    order: list[str] = []
    monkeypatch.setattr(update_mod, "launch_updater", _noting(order, "launch_updater", True))
    monkeypatch.setattr(update_mod, "stop_openhost", lambda: order.append("stop_openhost"))
    monkeypatch.setattr(update_mod, "_repo", lambda: _Repo(order))
    monkeypatch.setattr(update_mod, "_get_sorted_tags", lambda _r: ["v1"])
    monkeypatch.setattr(update_mod, "_next_step", lambda _r: "v1")
    monkeypatch.setattr(update_mod, "_resolve_ref_sha", lambda _r, ref: ref)
    monkeypatch.setattr("openhost_system_agent.update.os.execv", lambda *_a: order.append("execv"))

    update_mod.apply_update()

    assert order.index("is_dirty") < order.index("stop_openhost")
    assert order.index("fetch") < order.index("stop_openhost")
    assert order.index("launch_updater") < order.index("stop_openhost")
    assert order.index("stop_openhost") < order.index("checkout")
    assert order[-1] == "execv"


def test_dirty_tree_never_takes_the_service_down(monkeypatch: pytest.MonkeyPatch, progress_dir: Path) -> None:
    monkeypatch.setattr(update_mod, "_repo", lambda: _Repo(dirty=True))
    monkeypatch.setattr(update_mod, "launch_updater", lambda: pytest.fail("no downtime is needed to fail this"))
    monkeypatch.setattr(update_mod, "stop_openhost", lambda: pytest.fail("must not stop the service"))

    with pytest.raises(RuntimeError, match="uncommitted"):
        update_mod.apply_update()

    assert updater_progress.read_entries()[-1]["phase"] == updater_progress.Phase.FAILED


def test_failed_stop_cleans_up_the_updater(monkeypatch: pytest.MonkeyPatch, progress_dir: Path) -> None:
    # The service never went down, so there is no downtime to cover. The updater
    # only binds once it has seen :8080 offline, so left running it would idle and
    # then take 80/443 during a later, unrelated restart.
    events: list[str] = []

    def _stop() -> None:
        raise RuntimeError("Interactive authentication required.")

    monkeypatch.setattr(update_mod, "launch_updater", _noting(events, "launch", True))
    monkeypatch.setattr(update_mod, "stop_openhost", _stop)
    monkeypatch.setattr(update_mod, "stop_updater", lambda: events.append("stop_updater"))
    monkeypatch.setattr(update_mod, "_repo", lambda: _Repo())
    monkeypatch.setattr(update_mod, "_get_sorted_tags", lambda _r: ["v1"])
    monkeypatch.setattr(update_mod, "_next_step", lambda _r: "v1")

    with pytest.raises(RuntimeError, match="Interactive authentication"):
        update_mod.apply_update()

    assert events == ["launch", "stop_updater"]
    assert updater_progress.read_entries()[-1]["phase"] == updater_progress.Phase.FAILED


def test_successful_stop_leaves_the_updater_serving(monkeypatch: pytest.MonkeyPatch, progress_dir: Path) -> None:
    # The mirror case: once the service IS down the updater must keep serving until
    # the new compute_space takes the ports back, even if the walk then fails.
    repo = _Repo()
    monkeypatch.setattr(repo, "checkout", lambda *_a: (_ for _ in ()).throw(RuntimeError("git exploded")))
    monkeypatch.setattr(update_mod, "launch_updater", lambda: True)
    monkeypatch.setattr(update_mod, "stop_openhost", lambda: None)
    monkeypatch.setattr(update_mod, "stop_updater", lambda: pytest.fail("the downtime still needs covering"))
    monkeypatch.setattr(update_mod, "_repo", lambda: repo)
    monkeypatch.setattr(update_mod, "_get_sorted_tags", lambda _r: ["v1"])
    monkeypatch.setattr(update_mod, "_next_step", lambda _r: "v1")
    monkeypatch.setattr(update_mod, "_resolve_ref_sha", lambda _r, ref: ref)

    with pytest.raises(RuntimeError, match="git exploded"):
        update_mod.apply_update()


# ── stop/start/probe semantics ───────────────────────────────────────


def test_stop_openhost_tolerates_an_uninstalled_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    # A baseline host has no openhost.service until this walk's migrations install
    # it, so "not loaded" must not abort the update.
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


def test_start_openhost_restarts_and_clears_the_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    # `restart`, not `start`: if anything started the service mid-walk, a `start` is
    # a silent no-op and the freshly installed code never boots.
    calls: list[list[str]] = []
    monkeypatch.setattr("openhost_system_agent.detach.subprocess.run", _recorder(calls))

    detach_mod.start_openhost()

    assert [c[1] for c in calls] == ["reset-failed", "restart"]


def test_start_openhost_raises_when_systemd_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    def _run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return _ok() if cmd[1] == "reset-failed" else _fail("Job failed")

    monkeypatch.setattr("openhost_system_agent.detach.subprocess.run", _run)
    with pytest.raises(RuntimeError, match="systemctl restart"):
        detach_mod.start_openhost()


def test_apply_is_running_reads_active_state(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    for state, expected in (("active", True), ("activating", True), ("inactive", False), ("", False)):
        monkeypatch.setattr("openhost_system_agent.detach.subprocess.run", _recorder(calls, _ok(state + "\n")))
        assert detach_mod.apply_is_running() is expected, state
    # `is-active` on a --collect-ed unit makes PID 1 log a failed transient-file
    # open on every call, and this runs on the request path and at every boot.
    assert "is-active" not in calls[0]


def test_apply_is_running_false_when_systemctl_is_unusable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unknown state must not block updates; systemd-run's name collision is the
    # real guard.
    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("no systemctl")

    monkeypatch.setattr("openhost_system_agent.detach.subprocess.run", _boom)
    assert detach_mod.apply_is_running() is False


# ── --wait: keeping an exit code for scripts ─────────────────────────


def test_wait_for_apply_returns_when_the_unit_goes_away(monkeypatch: pytest.MonkeyPatch) -> None:
    states = iter([True, True, False])
    monkeypatch.setattr(detach_mod, "apply_is_running", lambda: next(states, False))
    assert detach_mod.wait_for_apply(timeout=10, poll=0) is True


def test_wait_for_apply_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(detach_mod, "apply_is_running", lambda: True)
    assert detach_mod.wait_for_apply(timeout=0.01, poll=0) is False


@pytest.mark.parametrize(
    ("phase", "expected_error"),
    [
        # A successful walk ends on the NON-terminal "restarting" (only the freshly
        # started compute_space appends "done"), so that must not read as failure.
        (updater_progress.Phase.RESTARTING, None),
        (updater_progress.Phase.FAILED, "Dependency install failed"),
    ],
)
def test_start_apply_wait_surfaces_the_walks_outcome(
    monkeypatch: pytest.MonkeyPatch, progress_dir: Path, phase: str, expected_error: str | None
) -> None:
    def _walk() -> None:
        updater_progress.record(phase, "Dependency install failed (exit 7).")

    monkeypatch.setattr(update_mod, "is_detached", lambda: False)
    monkeypatch.setattr(update_mod, "apply_is_running", lambda: False)
    monkeypatch.setattr(update_mod, "detach_apply", _walk)
    monkeypatch.setattr(update_mod, "wait_for_apply", lambda: True)

    if expected_error is None:
        update_mod.start_apply(wait=True)
    else:
        with pytest.raises(RuntimeError, match=expected_error):
            update_mod.start_apply(wait=True)


def test_start_apply_wait_raises_when_the_unit_never_finishes(
    monkeypatch: pytest.MonkeyPatch, progress_dir: Path
) -> None:
    monkeypatch.setattr(update_mod, "is_detached", lambda: False)
    monkeypatch.setattr(update_mod, "apply_is_running", lambda: False)
    monkeypatch.setattr(update_mod, "detach_apply", lambda: None)
    monkeypatch.setattr(update_mod, "wait_for_apply", lambda: False)

    with pytest.raises(RuntimeError, match="did not finish"):
        update_mod.start_apply(wait=True)


def test_start_apply_does_not_wait_by_default(monkeypatch: pytest.MonkeyPatch, progress_dir: Path) -> None:
    # The dashboard path must never block: the walk stops the process that would.
    monkeypatch.setattr(update_mod, "is_detached", lambda: False)
    monkeypatch.setattr(update_mod, "apply_is_running", lambda: False)
    monkeypatch.setattr(update_mod, "detach_apply", lambda: None)
    monkeypatch.setattr(update_mod, "wait_for_apply", lambda: pytest.fail("must not wait"))

    update_mod.start_apply()
