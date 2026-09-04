"""Tests for _run_system_agent's transient "command not found" retry.

Right after a self-update restart, the freshly-started compute_space immediately
runs `check_for_updates`, and sudo's PATH lookup can transiently miss the (present)
openhost_system_agent symlink for a beat. _run_system_agent retries that specific
error so a startup race doesn't surface as a scary "re-run ansible" failure."""

from __future__ import annotations

import subprocess

import pytest

from compute_space.core.system_agent import client as system_agent
from compute_space.core.system_agent.client import SystemAgentError
from compute_space.web.routes.api import settings as settings_api
from compute_space.web.routes.api import system as system_api


class _Result:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


_NOT_FOUND = "sudo: openhost_system_agent: command not found"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    # Don't actually wait between retries in tests.
    monkeypatch.setattr(system_agent.time, "sleep", lambda _: None)


def test_transient_not_found_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    # First two calls hit the startup race, third succeeds.
    seq = [_Result(1, stderr=_NOT_FOUND), _Result(1, stderr=_NOT_FOUND), _Result(0, stdout="ok")]
    calls = {"n": 0}

    def fake_run(*a, **k):  # type: ignore[no-untyped-def]
        r = seq[calls["n"]]
        calls["n"] += 1
        return r

    monkeypatch.setattr(system_agent.subprocess, "run", fake_run)
    assert system_agent._run_system_agent("status") == "ok"
    assert calls["n"] == 3  # retried past the two transient misses


def test_persistent_not_found_raises_ansible_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system_agent.subprocess, "run", lambda *a, **k: _Result(1, stderr=_NOT_FOUND))
    with pytest.raises(SystemAgentError) as e:
        system_agent._run_system_agent("status")
    # After exhausting retries, surface the actionable guidance.
    assert "not on sudo's PATH" in str(e.value)
    assert "ansible" in str(e.value)


def test_persistent_not_found_retries_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_run(*a, **k):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return _Result(1, stderr=_NOT_FOUND)

    monkeypatch.setattr(system_agent.subprocess, "run", fake_run)
    with pytest.raises(SystemAgentError):
        system_agent._run_system_agent("status")
    assert calls["n"] == system_agent._NOT_FOUND_RETRIES  # exactly N attempts, then give up


def test_other_error_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-"command not found" failure must raise immediately (no retry loop).
    calls = {"n": 0}

    def fake_run(*a, **k):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return _Result(1, stdout='{"error": "Working tree has uncommitted changes."}')

    monkeypatch.setattr(system_agent.subprocess, "run", fake_run)
    with pytest.raises(SystemAgentError) as e:
        system_agent._run_system_agent("update", "apply")
    assert "uncommitted changes" in str(e.value)
    assert calls["n"] == 1  # no retry for genuine errors


def test_binary_missing_filenotfound_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("sudo missing")

    monkeypatch.setattr(system_agent.subprocess, "run", boom)
    with pytest.raises(SystemAgentError) as e:
        system_agent._run_system_agent("status")
    assert "not found on PATH" in str(e.value)


def test_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(cmd="openhost_system_agent", timeout=5)

    monkeypatch.setattr(system_agent.subprocess, "run", boom)
    with pytest.raises(SystemAgentError) as e:
        system_agent._run_system_agent("status", timeout=5)
    assert "timed out" in str(e.value)


def test_stop_updater_sync_calls_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []

    def fake_run(*a, **k):  # type: ignore[no-untyped-def]
        calls.append(a)
        return _Result(0, stdout='{"ok": true}')

    monkeypatch.setattr(system_agent.subprocess, "run", fake_run)
    monkeypatch.delenv("OPENHOST_DATA_DIR", raising=False)
    system_agent.system_agent_stop_updater_sync()
    # Invoked the agent's `updater stop`.
    argv = calls[0][0]
    assert argv[:2] == ["sudo", "openhost_system_agent"]
    assert argv[-2:] == ["updater", "stop"]


def test_reset_restart_limit_sync_calls_narrow_agent_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(system_agent, "_run_system_agent", lambda *args, **kwargs: calls.append(args) or "")

    system_agent.system_agent_reset_restart_limit_sync()

    assert calls == [("service", "reset-start-limit")]


@pytest.mark.asyncio
async def test_manual_restart_prepares_before_triggering(monkeypatch: pytest.MonkeyPatch) -> None:
    steps: list[str] = []
    monkeypatch.setattr(settings_api, "system_agent_reset_restart_limit_sync", lambda: steps.append("reset"))
    monkeypatch.setattr(settings_api, "trigger_restart", lambda: steps.append("restart"))

    await settings_api.restart_compute_space.fn()

    assert steps == ["reset", "restart"]


@pytest.mark.asyncio
async def test_legacy_restart_endpoint_uses_safe_background_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    steps: list[str] = []
    monkeypatch.setattr(system_api, "system_agent_reset_restart_limit_sync", lambda: steps.append("reset"))
    monkeypatch.setattr(system_api, "trigger_restart", lambda: steps.append("restart"))

    response = await system_api.restart_router.fn()
    assert steps == ["reset"]
    assert response.background is not None
    await response.background()

    assert steps == ["reset", "restart"]


def test_health_exposes_process_generation() -> None:
    response = system_api.health.fn()
    assert response.generation == system_api.PROCESS_GENERATION
