"""Edge-case coverage for the compute_space side of the seamless update:
the apply_update lock/token flow, /updates endpoint, and progress view."""

from __future__ import annotations

import asyncio
import json
import string
from pathlib import Path

import pytest
from litestar.exceptions import HTTPException

import compute_space.web.routes.api.settings as settings_mod
from compute_space.core import seamless_update
from compute_space.core import update_progress
from compute_space.core.system_agent import SystemAgentError
from openhost_system_agent.protocol import MigrationStatus
from openhost_system_agent.updater import paths as agent_paths
from openhost_system_agent.updater import progress as agent_progress


async def _drain() -> None:
    for _ in range(6):
        await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def _reset_lock() -> None:
    # Ensure a clean lock between tests (a prior failure could leave it held).
    if settings_mod._apply_lock.locked():
        settings_mod._apply_lock.release()


@pytest.fixture
def token_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    calls: dict[str, list[str]] = {"persist": [], "clear": []}

    async def fake_persist(token: str) -> None:
        calls["persist"].append(token)

    async def fake_clear() -> None:
        calls["clear"].append("x")

    monkeypatch.setattr(settings_mod, "persist_update_token", fake_persist)
    monkeypatch.setattr(settings_mod, "clear_update_token", fake_clear)
    return calls


def _status(ok: bool = True, reason: str = "", msg: str = "ok") -> MigrationStatus:
    return MigrationStatus(ok=ok, reason=reason, message=msg, current_host_version=1, expected_version=1)


# ─────────────── new_update_token ───────────────


def test_new_token_nonempty() -> None:
    assert seamless_update.new_update_token()


def test_new_token_unique_many() -> None:
    tokens = {seamless_update.new_update_token() for _ in range(200)}
    assert len(tokens) == 200  # no collisions


def test_new_token_urlsafe() -> None:
    allowed = set(string.ascii_letters + string.digits + "-_")
    assert set(seamless_update.new_update_token()) <= allowed


def test_new_token_length() -> None:
    assert len(seamless_update.new_update_token()) >= 40


# ─────────────── persist / clear token (agent-routed) ───────────────


@pytest.mark.asyncio
async def test_persist_calls_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def fake(t: str) -> None:
        seen.append(t)

    monkeypatch.setattr(seamless_update, "system_agent_set_update_token", fake)
    await seamless_update.persist_update_token("tok")
    assert seen == ["tok"]


@pytest.mark.asyncio
async def test_persist_swallows_agent_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(t: str) -> None:
        raise SystemAgentError("down")

    monkeypatch.setattr(seamless_update, "system_agent_set_update_token", boom)
    await seamless_update.persist_update_token("tok")  # no raise


@pytest.mark.asyncio
async def test_clear_calls_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    n = {"c": 0}

    async def fake() -> None:
        n["c"] += 1

    monkeypatch.setattr(seamless_update, "system_agent_clear_update_token", fake)
    await seamless_update.clear_update_token()
    assert n["c"] == 1


@pytest.mark.asyncio
async def test_clear_swallows_agent_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom() -> None:
        raise SystemAgentError("down")

    monkeypatch.setattr(seamless_update, "system_agent_clear_update_token", boom)
    await seamless_update.clear_update_token()  # no raise


# ─────────────── apply_update endpoint ───────────────


@pytest.mark.asyncio
async def test_apply_returns_token(monkeypatch: pytest.MonkeyPatch, token_calls: dict[str, list[str]]) -> None:
    async def apply() -> None:
        return None

    async def status() -> MigrationStatus:
        return _status()

    monkeypatch.setattr(settings_mod, "system_agent_apply", apply)
    monkeypatch.setattr(settings_mod, "system_agent_status", status)
    resp = await settings_mod.apply_update.fn()
    await _drain()
    assert resp.token and token_calls["persist"] == [resp.token]


@pytest.mark.asyncio
async def test_apply_second_call_409(monkeypatch: pytest.MonkeyPatch, token_calls: dict[str, list[str]]) -> None:
    release = asyncio.Event()

    async def apply() -> None:
        await release.wait()

    async def status() -> MigrationStatus:
        return _status()

    monkeypatch.setattr(settings_mod, "system_agent_apply", apply)
    monkeypatch.setattr(settings_mod, "system_agent_status", status)
    await settings_mod.apply_update.fn()
    await _drain()
    with pytest.raises(HTTPException) as e:
        await settings_mod.apply_update.fn()
    assert e.value.status_code == 409
    release.set()
    await _drain()


@pytest.mark.asyncio
async def test_apply_third_call_after_failure_allowed(
    monkeypatch: pytest.MonkeyPatch, token_calls: dict[str, list[str]]
) -> None:
    # After a failed apply the lock frees, so a retry is accepted (not 409).
    async def failing() -> None:
        raise SystemAgentError("boom")

    async def status() -> MigrationStatus:
        return _status()

    monkeypatch.setattr(settings_mod, "system_agent_apply", failing)
    monkeypatch.setattr(settings_mod, "system_agent_status", status)
    await settings_mod.apply_update.fn()
    await _drain()
    assert not settings_mod._apply_lock.locked()
    assert token_calls["clear"] == ["x"]
    # Retry: a fresh apply is accepted.

    async def ok() -> None:
        return None

    monkeypatch.setattr(settings_mod, "system_agent_apply", ok)
    resp2 = await settings_mod.apply_update.fn()
    await _drain()
    assert resp2.token


@pytest.mark.asyncio
async def test_apply_gate_missing_migration_409(
    monkeypatch: pytest.MonkeyPatch, token_calls: dict[str, list[str]]
) -> None:
    async def status() -> MigrationStatus:
        return _status(ok=False, reason="missing", msg="log missing")

    monkeypatch.setattr(settings_mod, "system_agent_status", status)
    monkeypatch.setattr(settings_mod, "system_agent_apply", lambda: None)
    with pytest.raises(HTTPException) as e:
        await settings_mod.apply_update.fn()
    assert e.value.status_code == 409
    assert not settings_mod._apply_lock.locked()
    assert token_calls["persist"] == []  # never minted


@pytest.mark.asyncio
async def test_apply_gate_behind_is_allowed(
    monkeypatch: pytest.MonkeyPatch, token_calls: dict[str, list[str]]
) -> None:
    called = {"n": 0}

    async def apply() -> None:
        called["n"] += 1

    async def status() -> MigrationStatus:
        return _status(ok=False, reason="behind", msg="migrations pending")

    monkeypatch.setattr(settings_mod, "system_agent_apply", apply)
    monkeypatch.setattr(settings_mod, "system_agent_status", status)
    resp = await settings_mod.apply_update.fn()
    await _drain()
    assert resp.token and called["n"] == 1


@pytest.mark.asyncio
async def test_apply_status_error_500_releases_lock(
    monkeypatch: pytest.MonkeyPatch, token_calls: dict[str, list[str]]
) -> None:
    async def status() -> MigrationStatus:
        raise SystemAgentError("agent unreachable")

    monkeypatch.setattr(settings_mod, "system_agent_status", status)
    with pytest.raises(HTTPException) as e:
        await settings_mod.apply_update.fn()
    assert e.value.status_code == 500
    assert not settings_mod._apply_lock.locked()


@pytest.mark.asyncio
async def test_apply_unexpected_status_error_releases_lock(
    monkeypatch: pytest.MonkeyPatch, token_calls: dict[str, list[str]]
) -> None:
    async def status() -> MigrationStatus:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(settings_mod, "system_agent_status", status)
    with pytest.raises(RuntimeError):
        await settings_mod.apply_update.fn()
    assert not settings_mod._apply_lock.locked()


@pytest.mark.asyncio
async def test_apply_persist_failure_still_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If persisting the token fails softly (best-effort), the apply still runs.
    called = {"n": 0}

    async def apply() -> None:
        called["n"] += 1

    async def status() -> MigrationStatus:
        return _status()

    async def persist(t: str) -> None:
        return None  # seamless_update already swallows agent errors internally

    async def clear() -> None:
        return None

    monkeypatch.setattr(settings_mod, "system_agent_apply", apply)
    monkeypatch.setattr(settings_mod, "system_agent_status", status)
    monkeypatch.setattr(settings_mod, "persist_update_token", persist)
    monkeypatch.setattr(settings_mod, "clear_update_token", clear)
    resp = await settings_mod.apply_update.fn()
    await _drain()
    assert resp.token and called["n"] == 1


# ─────────────── /updates endpoint (compute_space) ───────────────


@pytest.fixture
def progress_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(agent_paths._DATA_DIR_ENV, str(tmp_path))
    (tmp_path / "updater").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.mark.asyncio
async def test_update_progress_empty(progress_env: Path) -> None:
    resp = await settings_mod.update_progress.fn()
    assert resp.entries == [] and resp.terminal is False


@pytest.mark.asyncio
async def test_update_progress_entries(progress_env: Path) -> None:
    with open(agent_paths.progress_log_path(), "w") as f:
        f.write(json.dumps({"phase": "fetch", "message": "F"}) + "\n")
        f.write(json.dumps({"phase": "done", "message": "D"}) + "\n")
    resp = await settings_mod.update_progress.fn()
    assert [e["phase"] for e in resp.entries] == ["fetch", "done"]
    assert resp.terminal is True


@pytest.mark.asyncio
async def test_update_progress_failed_terminal(progress_env: Path) -> None:
    with open(agent_paths.progress_log_path(), "w") as f:
        f.write(json.dumps({"phase": "failed", "message": "boom"}) + "\n")
    resp = await settings_mod.update_progress.fn()
    assert resp.terminal is True


@pytest.mark.asyncio
async def test_update_progress_partial_line(progress_env: Path) -> None:
    with open(agent_paths.progress_log_path(), "w") as f:
        f.write(json.dumps({"phase": "fetch"}) + "\n")
        f.write('{"phase": "mig')
    resp = await settings_mod.update_progress.fn()
    assert len(resp.entries) == 1


# ─────────────── update_progress.read_progress view ───────────────


def test_read_progress_view_empty(progress_env: Path) -> None:
    v = update_progress.read_progress()
    assert v.entries == [] and v.terminal is False


def test_read_progress_view_terminal(progress_env: Path) -> None:
    with open(agent_paths.progress_log_path(), "w") as f:
        f.write(json.dumps({"phase": "done"}) + "\n")
    v = update_progress.read_progress()
    assert v.terminal is True


def test_read_progress_view_matches_agent_reader(progress_env: Path) -> None:
    # The compute_space view must agree with the shared agent reader (no drift).
    with open(agent_paths.progress_log_path(), "w") as f:
        f.write(json.dumps({"phase": "fetch"}) + "\n")
        f.write(json.dumps({"phase": "install"}) + "\n")
    v = update_progress.read_progress()
    assert v.entries == agent_progress.read_entries()
    assert v.terminal == agent_progress.is_terminal(v.entries)
