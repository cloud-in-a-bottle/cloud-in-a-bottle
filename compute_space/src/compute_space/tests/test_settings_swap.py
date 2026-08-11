"""Tests for the /api/settings/swap get + set handlers.

Like test_settings_host_prep.py, we call the Litestar handlers' underlying
coroutines via ``handler.fn(...)`` and drive the system agent through fakes, so
the routing layer and the privileged agent are both out of scope.
"""

from __future__ import annotations

import pytest
from litestar.exceptions import HTTPException

import compute_space.web.routes.api.settings as settings_mod
from compute_space.core.system_agent import SystemAgentError
from openhost_system_agent.protocol import SwapStatus


@pytest.mark.asyncio
async def test_get_swap_returns_status(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get() -> SwapStatus:
        return SwapStatus(size_bytes=16 * 1024**3, path="/swapfile", active=True)

    monkeypatch.setattr(settings_mod, "system_agent_get_swap", fake_get)

    result = await settings_mod.get_swap.fn()

    assert result.size_bytes == 16 * 1024**3
    assert result.active is True


@pytest.mark.asyncio
async def test_get_swap_agent_error_is_500(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get() -> SwapStatus:
        raise SystemAgentError("agent down")

    monkeypatch.setattr(settings_mod, "system_agent_get_swap", fake_get)

    with pytest.raises(HTTPException) as excinfo:
        await settings_mod.get_swap.fn()
    assert excinfo.value.status_code == 500
    assert "agent down" in (excinfo.value.detail or "")


@pytest.mark.asyncio
async def test_set_swap_applies_valid_size(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {"size": None}

    async def fake_set(size_gib: int) -> SwapStatus:
        captured["size"] = size_gib
        return SwapStatus(size_bytes=size_gib * 1024**3, path="/swapfile", active=True)

    monkeypatch.setattr(settings_mod, "system_agent_set_swap", fake_set)

    result = await settings_mod.set_swap.fn(settings_mod.SetSwapRequest(size_gib=32))

    assert captured["size"] == 32
    assert result.size_bytes == 32 * 1024**3


@pytest.mark.asyncio
async def test_set_swap_rejects_out_of_range_before_calling_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(size_gib: int) -> SwapStatus:
        raise AssertionError("agent must not run for an out-of-range size")

    monkeypatch.setattr(settings_mod, "system_agent_set_swap", boom)

    with pytest.raises(HTTPException) as excinfo:
        await settings_mod.set_swap.fn(settings_mod.SetSwapRequest(size_gib=99999))
    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_set_swap_allows_zero_to_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_set(size_gib: int) -> SwapStatus:
        return SwapStatus(size_bytes=0, path="/swapfile", active=False)

    monkeypatch.setattr(settings_mod, "system_agent_set_swap", fake_set)

    result = await settings_mod.set_swap.fn(settings_mod.SetSwapRequest(size_gib=0))

    assert result.size_bytes == 0
    assert result.active is False


@pytest.mark.asyncio
async def test_set_swap_agent_error_is_500(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_set(size_gib: int) -> SwapStatus:
        raise SystemAgentError("mkswap failed")

    monkeypatch.setattr(settings_mod, "system_agent_set_swap", fake_set)

    with pytest.raises(HTTPException) as excinfo:
        await settings_mod.set_swap.fn(settings_mod.SetSwapRequest(size_gib=8))
    assert excinfo.value.status_code == 500
    assert "mkswap failed" in (excinfo.value.detail or "")
