"""Tests for the v10 migration that grants the host user journal read access.

Hosts detect host-level OOM kills by reading the kernel log from the system
journal, which needs the systemd-journal group.  The migration adds it through an
additive systemd drop-in (the same file ansible installs on fresh hosts) rather
than touching the main unit, so hosts provisioned before this change pick it up on
their next self-update.  It writes the drop-in and reloads systemd; we drive it
through fakes to assert exactly that.
"""

from __future__ import annotations

import pytest

from openhost_system_agent.migrations.versions import v0010_journal_read_for_oom
from openhost_system_agent.migrations.versions.v0010_journal_read_for_oom import Migration0010JournalReadForOom

_PREFIX = "openhost_system_agent.migrations.versions.v0010_journal_read_for_oom"


def test_writes_dropin_and_reloads(monkeypatch: pytest.MonkeyPatch) -> None:
    written: dict[str, object] = {}
    run_calls: list[tuple[str, ...]] = []

    def fake_write_file(path: str, content: str, *, mode: int = 0o600) -> None:
        written["path"] = path
        written["content"] = content
        written["mode"] = mode

    def fake_run(*cmd: str) -> None:
        run_calls.append(cmd)

    monkeypatch.setattr(f"{_PREFIX}.write_file", fake_write_file)
    monkeypatch.setattr(f"{_PREFIX}.run", fake_run)

    Migration0010JournalReadForOom().up()

    # Writes the additive drop-in (world-readable), not the main unit, so no
    # existing migration or the shared builder has to change.
    assert written["path"] == v0010_journal_read_for_oom.JOURNAL_READ_DROPIN_PATH
    assert str(written["path"]).endswith("/openhost.service.d/10-journal-read.conf")
    assert written["mode"] == 0o644
    assert written["content"] == v0010_journal_read_for_oom.JOURNAL_READ_DROPIN
    assert "SupplementaryGroups=systemd-journal\n" in str(written["content"])

    # Reloads systemd so the drop-in is authoritative for the next start; must NOT
    # restart openhost itself — the apply walk does that at the end.
    assert ("systemctl", "daemon-reload") in run_calls
    assert not any(c[:2] == ("systemctl", "restart") for c in run_calls)


def test_migration_version_is_ten() -> None:
    assert v0010_journal_read_for_oom.Migration0010JournalReadForOom.version == 10
