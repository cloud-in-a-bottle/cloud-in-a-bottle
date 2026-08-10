"""Tests for _run_system_agent's transient "command not found" retry.

Right after a self-update restart, the freshly-started compute_space immediately
runs `check_for_updates`, and sudo's PATH lookup can transiently miss the (present)
openhost_system_agent symlink for a beat. _run_system_agent retries that specific
error so a startup race doesn't surface as a scary "re-run ansible" failure."""

from __future__ import annotations

import subprocess

import pytest

from compute_space.core import system_agent
from compute_space.core.system_agent import SystemAgentError


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


def test_success_first_try(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_run(*a, **k):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return _Result(0, stdout='{"ok": true}')

    monkeypatch.setattr(system_agent.subprocess, "run", fake_run)
    assert system_agent._run_system_agent("status") == '{"ok": true}'
    assert calls["n"] == 1  # no retry on success


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


def test_not_found_in_json_error_field_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    # The error can also arrive inside the agent's JSON {"error": ...}; still retryable.
    seq = [
        _Result(1, stdout=f'{{"error": "{_NOT_FOUND}"}}'),
        _Result(0, stdout="recovered"),
    ]
    calls = {"n": 0}

    def fake_run(*a, **k):  # type: ignore[no-untyped-def]
        r = seq[calls["n"]]
        calls["n"] += 1
        return r

    monkeypatch.setattr(system_agent.subprocess, "run", fake_run)
    assert system_agent._run_system_agent("status") == "recovered"
    assert calls["n"] == 2


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
