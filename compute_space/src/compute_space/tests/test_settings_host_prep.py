"""Tests for /api/settings/check_for_updates and the HTTP 409 gate on
/api/settings/apply_update.

The handlers are Litestar route handlers — to exercise them in isolation
we call the underlying coroutine via ``handler.fn(...)``.
On the error path the handler raises ``HTTPException``; we inspect its
``status_code`` / ``detail`` directly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from litestar.exceptions import HTTPException

import compute_space.web.routes.api.settings as settings_mod
from compute_space.core.system_agent.client import SystemAgentError
from openhost_system_agent.protocol import FetchResult
from openhost_system_agent.protocol import MigrationStatus


@pytest.fixture(autouse=True)
def _fresh_apply_lock() -> None:
    """A successful handoff deliberately never releases the lock -- in production
    the process is stopped moments later. Tests need a fresh one each time."""
    settings_mod._apply_lock = asyncio.Lock()


@pytest.fixture(autouse=True)
def _no_walk_on_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """No apply unit is running unless a test says otherwise (it shells systemctl)."""
    monkeypatch.setattr(settings_mod, "apply_is_running", lambda: False)


async def _run_launch(response: object) -> None:
    """Run the launch the way Litestar does: after the response has been written.

    apply_update attaches it as a response background task rather than a
    create_task, because the walk stops openhost within moments of being launched
    and the browser must already hold the token-carrying response by then.
    """
    background = response.background  # type: ignore[attr-defined]
    assert background is not None, "apply_update must attach the launch to the response"
    await background()
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.fixture
def token_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Stub the (agent-routed) token persist/clear calls and record them.

    apply_update persists the token via the root agent; in tests we don't have a
    real agent, so record the calls to assert token lifecycle without touching
    the filesystem or invoking sudo.
    """
    calls: dict[str, list[str]] = {"persist": [], "clear": []}

    async def fake_persist(token: str) -> None:
        calls["persist"].append(token)

    async def fake_clear() -> None:
        calls["clear"].append("cleared")

    monkeypatch.setattr(settings_mod, "persist_update_token", fake_persist)
    monkeypatch.setattr(settings_mod, "clear_update_token", fake_clear)
    return calls


@pytest.mark.asyncio
async def test_check_for_updates_up_to_date(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch() -> FetchResult:
        return FetchResult(state="UP_TO_DATE")

    async def fake_status() -> MigrationStatus:
        return MigrationStatus(ok=True, reason="", message="ok", current_host_version=1, expected_version=1)

    monkeypatch.setattr(settings_mod, "system_agent_fetch", fake_fetch)
    monkeypatch.setattr(settings_mod, "system_agent_status", fake_status)

    result = await settings_mod.check_for_updates.fn()

    assert result.state == "UP_TO_DATE"
    assert result.error is None


@pytest.mark.asyncio
async def test_check_for_updates_update_available(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch() -> FetchResult:
        return FetchResult(state="BEHIND_REMOTE")

    async def fake_status() -> MigrationStatus:
        return MigrationStatus(ok=True, reason="", message="ok", current_host_version=1, expected_version=1)

    monkeypatch.setattr(settings_mod, "system_agent_fetch", fake_fetch)
    monkeypatch.setattr(settings_mod, "system_agent_status", fake_status)

    result = await settings_mod.check_for_updates.fn()

    assert result.state == "UPDATE_AVAILABLE"
    assert result.error is None


@pytest.mark.asyncio
async def test_check_for_updates_migration_behind_is_update_available(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch() -> FetchResult:
        return FetchResult(state="BEHIND_REMOTE")

    async def fake_status() -> MigrationStatus:
        return MigrationStatus(
            ok=False, reason="behind", message="run system migrations", current_host_version=1, expected_version=2
        )

    monkeypatch.setattr(settings_mod, "system_agent_fetch", fake_fetch)
    monkeypatch.setattr(settings_mod, "system_agent_status", fake_status)

    result = await settings_mod.check_for_updates.fn()

    # A "behind" host is applied by the Update button, so we must not surface the
    # CLI-oriented status message (which tells the owner to SSH in and run
    # `sudo openhost_system_agent update apply`) as a scary error in the UI.
    assert result.state == "UPDATE_AVAILABLE"
    assert result.error is None


@pytest.mark.asyncio
async def test_check_for_updates_migration_missing_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch() -> FetchResult:
        return FetchResult(state="UP_TO_DATE")

    async def fake_status() -> MigrationStatus:
        return MigrationStatus(
            ok=False, reason="missing", message="missing migration log", current_host_version=0, expected_version=2
        )

    monkeypatch.setattr(settings_mod, "system_agent_fetch", fake_fetch)
    monkeypatch.setattr(settings_mod, "system_agent_status", fake_status)

    result = await settings_mod.check_for_updates.fn()

    assert result.state == "ERROR"
    assert result.error == "missing migration log"


@pytest.mark.asyncio
async def test_check_for_updates_agent_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch() -> FetchResult:
        raise SystemAgentError("agent down")

    monkeypatch.setattr(settings_mod, "system_agent_fetch", fake_fetch)

    result = await settings_mod.check_for_updates.fn()

    assert result.state == "ERROR"
    assert "agent down" in (result.error or "")


@pytest.mark.asyncio
async def test_apply_update_refuses_with_409_when_not_prepared(
    monkeypatch: pytest.MonkeyPatch, token_calls: dict[str, list[str]]
) -> None:
    async def boom() -> None:
        raise AssertionError("system_agent_apply must not run when the gate fires")

    async def fake_status() -> MigrationStatus:
        return MigrationStatus(
            ok=False, reason="missing", message="migration log missing", current_host_version=0, expected_version=2
        )

    monkeypatch.setattr(settings_mod, "system_agent_apply", boom)
    monkeypatch.setattr(settings_mod, "system_agent_status", fake_status)

    with pytest.raises(HTTPException) as excinfo:
        await settings_mod.apply_update.fn()
    assert excinfo.value.status_code == 409
    # The gate must not have left the serialization lock held, nor minted a token.
    assert not settings_mod._apply_lock.locked()
    assert token_calls["persist"] == []


@pytest.mark.asyncio
async def test_apply_update_proceeds_when_migration_behind(
    monkeypatch: pytest.MonkeyPatch, token_calls: dict[str, list[str]]
) -> None:
    called = {"n": 0}

    async def fake_apply() -> None:
        called["n"] += 1

    async def fake_status() -> MigrationStatus:
        return MigrationStatus(
            ok=False, reason="behind", message="migrations needed", current_host_version=1, expected_version=2
        )

    monkeypatch.setattr(settings_mod, "system_agent_apply", fake_apply)
    monkeypatch.setattr(settings_mod, "system_agent_status", fake_status)

    resp = await settings_mod.apply_update.fn()
    assert resp.content.token  # a token is minted for the browser
    await _run_launch(resp)
    assert called["n"] == 1


@pytest.mark.asyncio
async def test_apply_update_persists_minted_token(
    monkeypatch: pytest.MonkeyPatch, token_calls: dict[str, list[str]]
) -> None:
    async def fake_apply() -> None:
        return None

    async def fake_status() -> MigrationStatus:
        return MigrationStatus(ok=True, reason="", message="ok", current_host_version=1, expected_version=1)

    monkeypatch.setattr(settings_mod, "system_agent_apply", fake_apply)
    monkeypatch.setattr(settings_mod, "system_agent_status", fake_status)

    resp = await settings_mod.apply_update.fn()
    await _run_launch(resp)

    # The token returned to the browser is the same one persisted for the updater.
    assert token_calls["persist"] == [resp.content.token]


@pytest.mark.asyncio
async def test_apply_update_rejects_concurrent_call(
    monkeypatch: pytest.MonkeyPatch, token_calls: dict[str, list[str]]
) -> None:
    release = asyncio.Event()
    called = {"n": 0}

    async def fake_apply() -> None:
        called["n"] += 1
        await release.wait()

    async def fake_status() -> MigrationStatus:
        return MigrationStatus(ok=True, reason="", message="ok", current_host_version=1, expected_version=1)

    monkeypatch.setattr(settings_mod, "system_agent_apply", fake_apply)
    monkeypatch.setattr(settings_mod, "system_agent_status", fake_status)

    # First call hands off; the lock is held from the moment it is taken.
    resp = await settings_mod.apply_update.fn()
    assert resp.content.token
    assert settings_mod._apply_lock.locked()

    # Second call is rejected while the first apply holds the lock.
    with pytest.raises(HTTPException) as excinfo:
        await settings_mod.apply_update.fn()
    assert excinfo.value.status_code == 409

    launch = asyncio.create_task(_run_launch(resp))
    release.set()
    await launch
    assert called["n"] == 1
    # A successful handoff keeps the lock: the walk is about to stop this
    # process, and a second click must not race it.
    assert settings_mod._apply_lock.locked()


@pytest.mark.asyncio
async def test_apply_update_releases_lock_on_unexpected_precheck_error(
    monkeypatch: pytest.MonkeyPatch, token_calls: dict[str, list[str]]
) -> None:
    # A non-SystemAgentError raised during the precheck must still release the
    # lock (via finally), or updates would be wedged forever.
    async def boom_status() -> MigrationStatus:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(settings_mod, "system_agent_status", boom_status)

    with pytest.raises(RuntimeError):
        await settings_mod.apply_update.fn()
    assert not settings_mod._apply_lock.locked()
    # Nothing was persisted since we failed before minting.
    assert token_calls["persist"] == []


@pytest.mark.asyncio
async def test_apply_update_releases_lock_and_clears_token_on_failure(
    monkeypatch: pytest.MonkeyPatch, token_calls: dict[str, list[str]], tmp_path: Path
) -> None:
    # Isolate the failure-path progress write from the real data dir.
    monkeypatch.setenv("OPENHOST_DATA_DIR", str(tmp_path))

    async def failing_apply() -> None:
        raise SystemAgentError("apply blew up")

    async def fake_status() -> MigrationStatus:
        return MigrationStatus(ok=True, reason="", message="ok", current_host_version=1, expected_version=1)

    monkeypatch.setattr(settings_mod, "system_agent_apply", failing_apply)
    monkeypatch.setattr(settings_mod, "system_agent_status", fake_status)

    resp = await settings_mod.apply_update.fn()
    await _run_launch(resp)

    # A failed launch must free the lock (so the owner can retry) and clear the
    # token (so a later visitor doesn't see a stale progress log).
    assert not settings_mod._apply_lock.locked()
    assert token_calls["clear"] == ["cleared"]
