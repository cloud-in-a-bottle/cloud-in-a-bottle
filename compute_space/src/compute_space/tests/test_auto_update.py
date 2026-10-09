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
import compute_space.core.system_agent.client as client_mod
from compute_space.core.auto_update.config import AutoUpdateConfig
from compute_space.core.auto_update.config import AutoUpdateLastRun
from compute_space.core.auto_update.config import AutoUpdateOutcome
from compute_space.core.auto_update.config import read_auto_update_config
from compute_space.core.auto_update.config import read_last_run
from compute_space.core.auto_update.config import record_last_run
from compute_space.core.auto_update.config import write_auto_update_config
from compute_space.core.auto_update.runner import is_due
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
    assert is_due(_NOW, _AT_4)


def test_not_due_before_the_scheduled_time() -> None:
    assert not is_due(datetime(2026, 10, 9, 3, 59, tzinfo=UTC), _AT_4)


def test_due_for_one_check_interval() -> None:
    assert is_due(datetime(2026, 10, 9, 4, 59, tzinfo=UTC), _AT_4)
    assert not is_due(datetime(2026, 10, 9, 5, 0, tzinfo=UTC), _AT_4)


def test_slot_just_before_midnight_is_due_just_after() -> None:
    assert is_due(datetime(2026, 10, 9, 0, 1, tzinfo=UTC), time(23, 58))


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
    if client_mod.apply_lock.locked():
        client_mod.apply_lock.release()


class FakeAgent:
    """On the tags channel and behind the latest release unless a test changes it."""

    def __init__(self) -> None:
        self.channel = UpdateChannel.TAGS
        self.fetch_state = "BEHIND_REMOTE"
        self.status = _status()
        self.apply_error: Exception | None = None
        self.applied = 0

    async def get_remote(self) -> RemoteInfo:
        return RemoteInfo(url="https://example.com/r", ref="v1", channel=self.channel)

    async def fetch(self) -> FetchResult:
        return FetchResult(state=self.fetch_state)

    async def get_status(self) -> MigrationStatus:
        return self.status

    async def start_apply(self) -> None:
        if self.apply_error is not None:
            raise self.apply_error
        self.applied += 1


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch, migrated_db: sqlite3.Connection) -> FakeAgent:
    fake = FakeAgent()
    monkeypatch.setattr(runner_mod, "system_agent_get_remote", fake.get_remote)
    monkeypatch.setattr(runner_mod, "system_agent_fetch", fake.fetch)
    monkeypatch.setattr(runner_mod, "system_agent_status", fake.get_status)
    monkeypatch.setattr(client_mod, "_start_apply", fake.start_apply)
    write_auto_update_config(migrated_db, AutoUpdateConfig(enabled=True, time_utc=_AT_4))
    return fake


def _outcome(db: sqlite3.Connection) -> AutoUpdateOutcome | None:
    last = read_last_run(db)
    return last.outcome if last else None


@pytest.mark.asyncio
async def test_runner_launches_update_when_due(agent: FakeAgent, migrated_db: sqlite3.Connection) -> None:
    await runner_mod.run_auto_update_if_due(_NOW)
    assert agent.applied == 1
    assert _outcome(migrated_db) == AutoUpdateOutcome.STARTED
    # Held until the walk stops this process.
    assert client_mod.apply_lock.locked()


@pytest.mark.asyncio
async def test_runner_does_nothing_when_not_due(agent: FakeAgent, migrated_db: sqlite3.Connection) -> None:
    await runner_mod.run_auto_update_if_due(datetime(2026, 10, 9, 12, 0, tzinfo=UTC))
    assert agent.applied == 0
    assert read_last_run(migrated_db) is None


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
    assert _outcome(migrated_db) == AutoUpdateOutcome.NOT_ON_TAGS


@pytest.mark.asyncio
async def test_runner_skips_dirty_tree(agent: FakeAgent, migrated_db: sqlite3.Connection) -> None:
    agent.fetch_state = "DIRTY"
    await runner_mod.run_auto_update_if_due(_NOW)
    assert agent.applied == 0
    assert _outcome(migrated_db) == AutoUpdateOutcome.DIRTY


@pytest.mark.asyncio
async def test_runner_up_to_date(agent: FakeAgent, migrated_db: sqlite3.Connection) -> None:
    agent.fetch_state = "UP_TO_DATE"
    await runner_mod.run_auto_update_if_due(_NOW)
    assert agent.applied == 0
    assert _outcome(migrated_db) == AutoUpdateOutcome.UP_TO_DATE


@pytest.mark.asyncio
async def test_runner_applies_pending_migrations_when_up_to_date(
    agent: FakeAgent, migrated_db: sqlite3.Connection
) -> None:
    agent.fetch_state = "UP_TO_DATE"
    agent.status = _status(ok=False, reason="behind")
    await runner_mod.run_auto_update_if_due(_NOW)
    assert agent.applied == 1


@pytest.mark.asyncio
async def test_runner_records_broken_migration_state(agent: FakeAgent, migrated_db: sqlite3.Connection) -> None:
    agent.status = _status(ok=False, reason="ahead")
    await runner_mod.run_auto_update_if_due(_NOW)
    assert agent.applied == 0
    last = read_last_run(migrated_db)
    assert last is not None and last.describe() == "Failed: msg"


@pytest.mark.asyncio
async def test_runner_records_agent_failure(
    agent: FakeAgent, migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing() -> RemoteInfo:
        raise SystemAgentError("agent unreachable")

    monkeypatch.setattr(runner_mod, "system_agent_get_remote", failing)
    await runner_mod.run_auto_update_if_due(_NOW)
    last = read_last_run(migrated_db)
    assert last is not None and last.describe() == "Failed: could not check for updates: agent unreachable"


@pytest.mark.asyncio
async def test_runner_records_launch_failure_and_releases_lock(
    agent: FakeAgent, migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def noop(*_: object) -> None:
        return None

    monkeypatch.setattr(client_mod, "record_apply_failure", noop)
    monkeypatch.setattr(client_mod, "system_agent_clear_update_token", noop)
    agent.apply_error = SystemAgentError("boom")
    await runner_mod.run_auto_update_if_due(_NOW)
    assert _outcome(migrated_db) == AutoUpdateOutcome.FAILED
    assert not client_mod.apply_lock.locked()


@pytest.mark.asyncio
async def test_runner_skips_while_another_update_holds_the_lock(
    agent: FakeAgent, migrated_db: sqlite3.Connection
) -> None:
    await client_mod.apply_lock.acquire()
    await runner_mod.run_auto_update_if_due(_NOW)
    assert agent.applied == 0
    assert _outcome(migrated_db) == AutoUpdateOutcome.ALREADY_RUNNING


def test_last_run_roundtrip(migrated_db: sqlite3.Connection) -> None:
    failed = AutoUpdateLastRun(at=_NOW, outcome=AutoUpdateOutcome.FAILED, detail="boom")
    record_last_run(migrated_db, failed)
    assert read_last_run(migrated_db) == failed
    # A later run without a detail doesn't inherit the old one.
    ok = AutoUpdateLastRun(at=_NOW, outcome=AutoUpdateOutcome.UP_TO_DATE)
    record_last_run(migrated_db, ok)
    assert read_last_run(migrated_db) == ok
