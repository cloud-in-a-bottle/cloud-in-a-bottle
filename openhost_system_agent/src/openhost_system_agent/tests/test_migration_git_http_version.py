from __future__ import annotations

import pytest

from openhost_system_agent.migrations.versions import v0011_git_http_version
from openhost_system_agent.migrations.versions.v0011_git_http_version import Migration0011GitHttpVersion


def test_configures_git_http_version_for_root_and_host(monkeypatch: pytest.MonkeyPatch) -> None:
    run_calls: list[tuple[str, ...]] = []

    def fake_run(*cmd: str) -> None:
        run_calls.append(cmd)

    monkeypatch.setattr(v0011_git_http_version, "run", fake_run)

    Migration0011GitHttpVersion().up()

    assert run_calls == [
        (
            "sudo",
            "-u",
            "root",
            "-H",
            "git",
            "config",
            "--global",
            "--replace-all",
            "http.version",
            "HTTP/1.1",
        ),
        (
            "sudo",
            "-u",
            "host",
            "-H",
            "git",
            "config",
            "--global",
            "--replace-all",
            "http.version",
            "HTTP/1.1",
        ),
    ]


def test_migration_version_is_eleven() -> None:
    assert Migration0011GitHttpVersion.version == 11
