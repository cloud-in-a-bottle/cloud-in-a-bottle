from __future__ import annotations

import pytest

from compute_space.core import seamless_update
from compute_space.core.system_agent import SystemAgentError


def test_new_update_token_is_unguessable_and_unique() -> None:
    a = seamless_update.new_update_token()
    b = seamless_update.new_update_token()
    assert a and b and a != b
    # token_urlsafe(32) yields ~43 url-safe chars.
    assert len(a) >= 32


@pytest.mark.asyncio
async def test_persist_update_token_calls_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def fake_set(token: str) -> None:
        seen.append(token)

    monkeypatch.setattr(seamless_update, "system_agent_set_update_token", fake_set)
    await seamless_update.persist_update_token("abc123")
    assert seen == ["abc123"]


@pytest.mark.asyncio
async def test_persist_update_token_swallows_agent_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(token: str) -> None:
        raise SystemAgentError("agent down")

    monkeypatch.setattr(seamless_update, "system_agent_set_update_token", boom)
    # Best-effort: a failure to persist must not propagate (update still proceeds).
    await seamless_update.persist_update_token("abc123")


@pytest.mark.asyncio
async def test_clear_update_token_calls_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"n": 0}

    async def fake_clear() -> None:
        called["n"] += 1

    monkeypatch.setattr(seamless_update, "system_agent_clear_update_token", fake_clear)
    await seamless_update.clear_update_token()
    assert called["n"] == 1


@pytest.mark.asyncio
async def test_clear_update_token_swallows_agent_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom() -> None:
        raise SystemAgentError("agent down")

    monkeypatch.setattr(seamless_update, "system_agent_clear_update_token", boom)
    await seamless_update.clear_update_token()
