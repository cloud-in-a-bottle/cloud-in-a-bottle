from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC
from datetime import datetime
from datetime import time
from pathlib import Path

import pytest

import compute_space.core.auto_update.runner as runner_mod
import compute_space.core.system_agent.apply as apply_mod
from compute_space.core.auto_update.config import AutoUpdateConfig
from compute_space.core.auto_update.config import AutoUpdateLastRun
from compute_space.core.auto_update.config import read_auto_update_config
from compute_space.core.auto_update.config import read_last_run
from compute_space.core.auto_update.config import record_last_run
from compute_space.core.auto_update.config import write_auto_update_config
from compute_space.core.auto_update.schedule import due_slot
from compute_space.core.settings_store import set_setting
from compute_space.core.system_agent.client import SystemAgentError
from compute_space.db import get_db
from compute_space.db.connection import init_db
from openhost_system_agent.protocol import FetchResult
from openhost_system_agent.protocol import MigrationStatus
from openhost_system_agent.protocol import RemoteInfo
from openhost_system_agent.protocol import UpdateChannel

_NOW = datetime(2026, 10, 9, 4, 2, tzinfo=UTC)
_AT_4 = time(4, 0)


# ─────────────── schedule ───────────────


def test_due_shortly_after_the_scheduled_time() -> None:
    assert due_slot(_NOW, _AT_4, None) == datetime(2026, 10, 9, 4, 0, tzinfo=UTC)


def test_not_due_before_the_scheduled_time() -> None:
    assert due_slot(datetime(2026, 10, 9, 3, 59, tzinfo=UTC), _AT_4, None) is None


def test_missed_window_waits_for_the_next_day() -> None:
    assert due_slot(datetime(2026, 10, 9, 4, 30, tzinfo=UTC), _AT_4, None) is None


def test_not_due_twice_for_one_slot() -> None:
    assert due_slot(_NOW, _AT_4, datetime(2026, 10, 9, 4, 0, 30, tzinfo=UTC)) is None
    # Yesterday's attempt doesn't count against today's slot.
    assert due_slot(_NOW, _AT_4, datetime(2026, 10, 8, 4, 0, 30, tzinfo=UTC)) is not None


def test_slot_just_before_midnight_is_due_just_after() -> None:
    now = datetime(2026, 10, 9, 0, 1, tzinfo=UTC)
    assert due_slot(now, time(23, 58), None) == datetime(2026, 10, 8, 23, 58, tzinfo=UTC)


# ─────────────── config ───────────────


@pytest.fixture
def migrated_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    init_db(str(tmp_path / "router.db"))
    with closing(get_db()) as db:
        yield db


def test_fresh_instance_defaults_to_enabled(migrated_db: sqlite3.Connection) -> None:
    config = read_auto_update_config(migrated_db, _NOW)
    assert config.enabled is True


def test_default_time_is_4am_server_time(migrated_db: sqlite3.Connection) -> None:
    config = read_auto_update_config(migrated_db, _NOW)
    local = datetime.combine(_NOW.date(), config.time_utc, tzinfo=UTC).astimezone()
    assert (local.hour, local.minute) == (4, 0)


def test_existing_instance_migrated_to_v15_stays_disabled(tmp_path: Path) -> None:
    # An instance that predates auto-updates reaches v15 through the migration, not schema.sql.
    db_path = str(tmp_path / "router.db")
    with closing(sqlite3.connect(db_path)) as db:
        db.executescript((Path(__file__).parent / "snapshots" / "empty" / "v0001.sql").read_text())
    init_db(db_path)
    with closing(get_db()) as db:
        assert read_auto_update_config(db, _NOW).enabled is False


def test_config_roundtrip(migrated_db: sqlite3.Connection) -> None:
    write_auto_update_config(migrated_db, AutoUpdateConfig(enabled=False, time_utc=time(9, 30)))
    assert read_auto_update_config(migrated_db, _NOW) == AutoUpdateConfig(enabled=False, time_utc=time(9, 30))


def test_invalid_enabled_value_fails_loudly(migrated_db: sqlite3.Connection) -> None:
    set_setting(migrated_db, "auto_update_enabled", "yes")
    with pytest.raises(ValueError):
        read_auto_update_config(migrated_db, _NOW)


# ─────────────── runner ───────────────


def _status(ok: bool = True, reason: str = "") -> MigrationStatus:
    return MigrationStatus(ok=ok, reason=reason, message="msg", current_host_version=1, expected_version=1)


@pytest.fixture(autouse=True)
def _fresh_apply_lock() -> None:
    if apply_mod.apply_lock.locked():
        apply_mod.apply_lock.release()


class FakeAgent:
    """On the tags channel and behind the latest release unless a test changes it."""

    def __init__(self) -> None:
        self.channel = UpdateChannel.TAGS
        self.fetch_state = "BEHIND_REMOTE"
        self.status = _status()
        self.applied = 0

    async def get_remote(self) -> RemoteInfo:
        return RemoteInfo(url="https://example.com/r", ref="v1", channel=self.channel)

    async def fetch(self) -> FetchResult:
        return FetchResult(state=self.fetch_state)

    async def get_status(self) -> MigrationStatus:
        return self.status

    async def apply(self) -> None:
        self.applied += 1


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch, migrated_db: sqlite3.Connection) -> FakeAgent:
    fake = FakeAgent()
    monkeypatch.setattr(runner_mod, "system_agent_get_remote", fake.get_remote)
    monkeypatch.setattr(runner_mod, "system_agent_fetch", fake.fetch)
    monkeypatch.setattr(runner_mod, "system_agent_status", fake.get_status)
    monkeypatch.setattr(apply_mod, "system_agent_status", fake.get_status)
    monkeypatch.setattr(apply_mod, "system_agent_apply", fake.apply)
    monkeypatch.setattr(apply_mod, "apply_is_running", lambda: False)
    write_auto_update_config(migrated_db, AutoUpdateConfig(enabled=True, time_utc=_AT_4))
    return fake


def _last_result(db: sqlite3.Connection) -> str | None:
    last = read_last_run(db)
    return last.result if last else None


@pytest.mark.asyncio
async def test_runner_launches_update_when_due(agent: FakeAgent, migrated_db: sqlite3.Connection) -> None:
    await runner_mod.run_auto_update_if_due(_NOW)
    assert agent.applied == 1
    assert _last_result(migrated_db) == "Started an update to the latest release."
    # The second tick in the same window does nothing, including after the restart.
    if apply_mod.apply_lock.locked():
        apply_mod.apply_lock.release()
    await runner_mod.run_auto_update_if_due(_NOW.replace(minute=3))
    assert agent.applied == 1


@pytest.mark.asyncio
async def test_runner_does_nothing_when_disabled(agent: FakeAgent, migrated_db: sqlite3.Connection) -> None:
    write_auto_update_config(migrated_db, AutoUpdateConfig(enabled=False, time_utc=_AT_4))
    await runner_mod.run_auto_update_if_due(_NOW)
    assert agent.applied == 0
    assert read_last_run(migrated_db) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", [UpdateChannel.BRANCH, UpdateChannel.PINNED])
async def test_runner_skips_off_the_tags_channel(
    agent: FakeAgent, migrated_db: sqlite3.Connection, channel: UpdateChannel
) -> None:
    agent.channel = channel
    await runner_mod.run_auto_update_if_due(_NOW)
    assert agent.applied == 0
    assert "only run when following tagged releases" in str(_last_result(migrated_db))


@pytest.mark.asyncio
async def test_runner_skips_dirty_tree(agent: FakeAgent, migrated_db: sqlite3.Connection) -> None:
    agent.fetch_state = "DIRTY"
    await runner_mod.run_auto_update_if_due(_NOW)
    assert agent.applied == 0
    assert "uncommitted local changes" in str(_last_result(migrated_db))


@pytest.mark.asyncio
async def test_runner_up_to_date(agent: FakeAgent, migrated_db: sqlite3.Connection) -> None:
    agent.fetch_state = "UP_TO_DATE"
    await runner_mod.run_auto_update_if_due(_NOW)
    assert agent.applied == 0
    assert _last_result(migrated_db) == "Already up to date."


@pytest.mark.asyncio
async def test_runner_applies_pending_migrations_when_up_to_date(
    agent: FakeAgent, migrated_db: sqlite3.Connection
) -> None:
    agent.fetch_state = "UP_TO_DATE"
    agent.status = _status(ok=False, reason="behind")
    await runner_mod.run_auto_update_if_due(_NOW)
    assert agent.applied == 1


@pytest.mark.asyncio
async def test_runner_records_blocked_apply_and_releases_lock(
    agent: FakeAgent, migrated_db: sqlite3.Connection
) -> None:
    agent.status = _status(ok=False, reason="ahead")
    await runner_mod.run_auto_update_if_due(_NOW)
    assert agent.applied == 0
    assert _last_result(migrated_db) == "Skipped: msg"
    assert not apply_mod.apply_lock.locked()


@pytest.mark.asyncio
async def test_runner_records_agent_failure(
    agent: FakeAgent, migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing() -> RemoteInfo:
        raise SystemAgentError("agent unreachable")

    monkeypatch.setattr(runner_mod, "system_agent_get_remote", failing)
    await runner_mod.run_auto_update_if_due(_NOW)
    assert _last_result(migrated_db) == "Failed to check for updates: agent unreachable"


@pytest.mark.asyncio
async def test_runner_skips_while_another_update_holds_the_lock(
    agent: FakeAgent, migrated_db: sqlite3.Connection
) -> None:
    await apply_mod.apply_lock.acquire()
    await runner_mod.run_auto_update_if_due(_NOW)
    assert agent.applied == 0
    assert _last_result(migrated_db) == "Skipped: an update is already in progress."


def test_last_run_roundtrip(migrated_db: sqlite3.Connection) -> None:
    record_last_run(migrated_db, AutoUpdateLastRun(at=_NOW, result="ok"))
    assert read_last_run(migrated_db) == AutoUpdateLastRun(at=_NOW, result="ok")
