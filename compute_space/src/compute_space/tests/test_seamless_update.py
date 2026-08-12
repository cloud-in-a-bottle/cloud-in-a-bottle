from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.exceptions import HTTPException
from litestar.plugins.jinja import JinjaTemplateEngine
from litestar.testing import TestClient

import compute_space.web.routes.api.settings as settings_mod
from compute_space.config import provide_config
from compute_space.config import set_active_config
from compute_space.core import seamless_update
from compute_space.core import update_progress
from compute_space.core.system_agent import SystemAgentError
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.web.app import _template_globals
from compute_space.web.routes.pages.settings import updating_page
from compute_space.web.templating import build_template_config
from openhost_system_agent.protocol import MigrationStatus
from openhost_system_agent.updater import paths as agent_paths
from openhost_system_agent.updater import progress as agent_progress

from ._litestar_helpers import auth_cookie
from .conftest import _make_test_config


async def _drain() -> None:
    for _ in range(6):
        await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def _fresh_apply_lock() -> None:
    """A successful handoff deliberately never releases the lock -- in production
    the process is stopped moments later. Tests need a fresh one each time."""
    settings_mod._apply_lock = asyncio.Lock()


@pytest.fixture(autouse=True)
def _no_walk_on_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """No apply unit is running unless a test says otherwise (it shells systemctl)."""
    monkeypatch.setattr(settings_mod, "apply_is_running", lambda: False)


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


# ─────────────── token minting / persistence ───────────────


def test_new_token_unique_and_urlsafe() -> None:
    tokens = {seamless_update.new_update_token() for _ in range(200)}
    assert len(tokens) == 200
    assert len(next(iter(tokens))) >= 32


@pytest.mark.asyncio
async def test_persist_token_calls_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def fake_set(token: str) -> None:
        seen.append(token)

    monkeypatch.setattr(seamless_update, "system_agent_set_update_token", fake_set)
    await seamless_update.persist_update_token("abc123")
    assert seen == ["abc123"]


# ─────────────── apply_update endpoint ───────────────
# The endpoint gate/lock/token-lifecycle tests live in test_settings_host_prep.py;
# here we cover only what that file doesn't: failure paths of the background
# apply task and their progress-log side effects.


@pytest.fixture
def progress_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(agent_paths.DATA_DIR_ENV, str(tmp_path))
    (tmp_path / "updater").mkdir(parents=True, exist_ok=True)
    return tmp_path


async def _apply_and_launch() -> object:
    """Call the handler, then run the launch the way Litestar does after sending.

    The launch is deliberately a response background task, not a create_task:
    the walk stops openhost within moments, so the browser must already have the
    token-carrying response by then.
    """
    response = await settings_mod.apply_update.fn()
    assert response.background is not None
    await response.background()
    return response


@pytest.mark.asyncio
async def test_apply_third_call_after_failure_allowed(
    monkeypatch: pytest.MonkeyPatch, token_calls: dict[str, list[str]], progress_env: Path
) -> None:
    # After a failed apply the lock frees, so a retry is accepted (not 409).
    async def failing() -> None:
        raise SystemAgentError("boom")

    async def status() -> MigrationStatus:
        return _status()

    monkeypatch.setattr(settings_mod, "system_agent_apply", failing)
    monkeypatch.setattr(settings_mod, "system_agent_status", status)
    await _apply_and_launch()
    assert not settings_mod._apply_lock.locked()
    assert token_calls["clear"] == ["x"]
    # Retry: a fresh apply is accepted.

    async def ok() -> None:
        return None

    monkeypatch.setattr(settings_mod, "system_agent_apply", ok)
    resp2 = await settings_mod.apply_update.fn()
    assert resp2.content.token
    # Launched successfully: the lock stays held, because the walk is now about
    # to stop this process and a second click must not race it.
    await resp2.background()
    assert settings_mod._apply_lock.locked()


@pytest.mark.asyncio
async def test_apply_generic_exception_also_recorded(
    monkeypatch: pytest.MonkeyPatch, token_calls: dict[str, list[str]], progress_env: Path
) -> None:
    # Not just SystemAgentError: any failure must clear the token and terminate
    # the progress log.
    async def failing() -> None:
        raise RuntimeError("totally unexpected")

    async def status() -> MigrationStatus:
        return _status()

    monkeypatch.setattr(settings_mod, "system_agent_apply", failing)
    monkeypatch.setattr(settings_mod, "system_agent_status", status)
    await _apply_and_launch()

    assert not settings_mod._apply_lock.locked()
    assert token_calls["clear"] == ["x"]
    view = update_progress.read_progress()
    assert view.terminal is True
    assert "totally unexpected" in str(view.entries[-1]["message"])


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


# ─────────────── /updates endpoint (compute_space) ───────────────


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


# ─────────────── update_progress helpers ───────────────


@pytest.mark.asyncio
async def test_record_apply_failure_terminates_nonterminal_log(progress_env: Path) -> None:
    agent_progress.record("fetch", "Fetching…")
    await update_progress.record_apply_failure("it broke")
    v = update_progress.read_progress()
    assert v.terminal is True and "it broke" in str(v.entries[-1]["message"])


@pytest.mark.asyncio
async def test_record_apply_failure_keeps_existing_terminal(progress_env: Path) -> None:
    agent_progress.record(agent_progress.PHASE_FAILED, "agent-side detail")
    await update_progress.record_apply_failure("vaguer compute_space message")
    v = update_progress.read_progress()
    assert [e["phase"] for e in v.entries] == [agent_progress.PHASE_FAILED]
    assert v.entries[-1]["message"] == "agent-side detail"


def test_mark_boot_complete_falls_back_to_agent(progress_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A log created by an older build is root-owned; the direct append fails and
    # the boot hook must route through the root agent instead of giving up.
    calls = {"agent": 0}
    monkeypatch.setattr(update_progress.agent_progress, "mark_boot_complete", lambda: False)
    monkeypatch.setattr(
        update_progress, "system_agent_mark_boot_complete_sync", lambda: calls.__setitem__("agent", calls["agent"] + 1)
    )
    update_progress.mark_boot_complete()
    assert calls["agent"] == 1


@pytest.mark.asyncio
async def test_record_apply_failure_falls_back_to_agent(progress_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_agent_fail(message: str) -> None:
        calls.append(message)

    monkeypatch.setattr(update_progress.agent_progress, "record_failure_if_not_terminal", lambda m: False)
    monkeypatch.setattr(update_progress, "system_agent_record_update_failure", fake_agent_fail)
    await update_progress.record_apply_failure("it broke")
    assert calls == ["it broke"]


# ─────────────── /updating page render ───────────────


@pytest.fixture
def cfg(tmp_path: Path) -> Iterator[Any]:
    config = _make_test_config(tmp_path, zone_domain="update-zone.example.com")
    init_db(config.db_path)
    yield config


def _build_app(cfg: Any, template_dir: Path | None = None) -> Litestar:
    """A minimal app with the real /updating route and Jinja globals installed.

    Uses the production template config so the test exercises the same frozen
    environment the router boots with.
    """
    web_dir = Path(__file__).resolve().parents[1] / "web"
    template_config = build_template_config(template_dir or web_dir / "templates")

    def _install_globals(app: Litestar) -> None:
        engine = app.template_engine
        if isinstance(engine, JinjaTemplateEngine):
            engine.engine.globals.update(_template_globals(cfg, web_dir / "static"))

    return Litestar(
        route_handlers=[updating_page],
        template_config=template_config,
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        on_startup=[_install_globals],
        openapi_config=None,
    )


def test_updating_page_renders_for_owner(cfg: Any) -> None:
    # The page is the user-visible centrepiece of the update flow; make sure the
    # template (include + static_url calls) actually renders.
    set_active_config(cfg)
    cookie = auth_cookie(cfg, username="owner")

    with TestClient(app=_build_app(cfg)) as client:
        client.cookies.update(cookie)
        resp = client.get("/updating")
    assert resp.status_code == 200
    assert "Updating this instance" in resp.text
    assert "update-progress.js" in resp.text
    assert "update-progress.css" in resp.text


def test_updating_page_survives_the_checkout_it_is_reporting_on(cfg: Any, tmp_path: Path) -> None:
    # The end this all exists for: the apply walk rewrites the template tree
    # while this process keeps serving. The page must keep rendering the markup
    # it booted with, not half of the next release's.
    template_dir = tmp_path / "templates"
    shutil.copytree(Path(__file__).resolve().parents[1] / "web" / "templates", template_dir)
    set_active_config(cfg)
    cookie = auth_cookie(cfg, username="owner")

    with TestClient(app=_build_app(cfg, template_dir=template_dir)) as client:
        client.cookies.update(cookie)
        assert "Updating this instance" in client.get("/updating").text

        # A checkout mid-update: the partial is rewritten to reference a global
        # this process never defined, and the page itself is dropped. The mtime
        # is pushed forward because Jinja detects staleness by mtime equality.
        partial = template_dir / "_update_progress_body.html"
        partial.write_text("{{ a_global_from_the_next_release() }}")
        stamp = partial.stat().st_mtime + 10
        os.utime(partial, (stamp, stamp))
        (template_dir / "updating.html").unlink()

        resp = client.get("/updating")

    assert resp.status_code == 200
    assert "Updating this instance" in resp.text


@pytest.mark.asyncio
async def test_apply_refuses_when_a_walk_is_already_running_on_the_host(
    monkeypatch: pytest.MonkeyPatch, token_calls: dict[str, list[str]], progress_env: Path
) -> None:
    # REGRESSION: the in-process lock only covers this process's lifetime, and the
    # walk stops and restarts us. A fresh process would hold a free lock while an
    # apply was still running, mint a new token, and strand the first owner's tab
    # on 403 for the rest of the update.
    async def status() -> MigrationStatus:
        return _status()

    async def apply_must_not_run() -> None:
        pytest.fail("must not launch a second walk")

    monkeypatch.setattr(settings_mod, "system_agent_status", status)
    monkeypatch.setattr(settings_mod, "system_agent_apply", apply_must_not_run)
    monkeypatch.setattr(settings_mod, "apply_is_running", lambda: True)

    with pytest.raises(HTTPException) as e:
        await settings_mod.apply_update.fn()

    assert e.value.status_code == 409
    assert token_calls["persist"] == []  # the in-flight update's token is untouched
    assert not settings_mod._apply_lock.locked()
