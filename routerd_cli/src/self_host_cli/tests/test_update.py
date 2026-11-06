"""Tests for the ``openhost update`` command.

All external calls (git, pixi) are mocked so tests run offline
and without side effects.
"""

from argparse import Namespace
from unittest.mock import MagicMock

import pytest

from self_host_cli.update import _check_router_not_running
from self_host_cli.update import run_update

# ---------------------------------------------------------------------------
# Router running guard
# ---------------------------------------------------------------------------


class TestRouterRunningGuard:
    def test_no_pidfile(self, monkeypatch):
        monkeypatch.setattr("self_host_cli.update._read_pid", lambda _: None)
        # Should not raise
        _check_router_not_running()

    def test_pid_not_alive(self, monkeypatch):
        monkeypatch.setattr("self_host_cli.update._read_pid", lambda _: 99999)
        monkeypatch.setattr("self_host_cli.update._is_alive", lambda _: False)
        _check_router_not_running()

    def test_router_running_warns(self, monkeypatch, capsys):
        monkeypatch.setattr("self_host_cli.update._read_pid", lambda _: 12345)
        monkeypatch.setattr("self_host_cli.update._is_alive", lambda _: True)
        _check_router_not_running()
        out = capsys.readouterr().out
        assert "router appears to be running" in out


# ---------------------------------------------------------------------------
# Code update
# ---------------------------------------------------------------------------


class TestUpdateCode:
    def test_no_git_repo_exits(self, monkeypatch):
        monkeypatch.setattr("self_host_cli.update._is_git_repo", lambda: False)
        args = Namespace()
        with pytest.raises(SystemExit, match="1"):
            run_update(args)

    def test_dirty_tree_skips(self, monkeypatch, capsys):
        monkeypatch.setattr("self_host_cli.update._is_git_repo", lambda: True)

        def fake_run(cmd, **kwargs):
            assert cmd[:3] == ["git", "-c", "core.hooksPath=/dev/null"]
            cmd = [cmd[0], *cmd[3:]]
            result = MagicMock()
            result.returncode = 0
            # git status --porcelain returns dirty output
            if cmd[:2] == ["git", "status"]:
                result.stdout = " M dirty_file.py\n"
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("self_host_cli.update.subprocess.run", fake_run)
        args = Namespace()
        run_update(args)
        out = capsys.readouterr().out
        assert "uncommitted changes" in out

    def test_already_up_to_date(self, monkeypatch, capsys):
        monkeypatch.setattr("self_host_cli.update._is_git_repo", lambda: True)

        same_hash = "abc1234abc1234abc1234abc1234abc1234abc12"

        def fake_run(cmd, **kwargs):
            assert cmd[:3] == ["git", "-c", "core.hooksPath=/dev/null"]
            cmd = [cmd[0], *cmd[3:]]
            result = MagicMock()
            result.returncode = 0
            if cmd[:2] == ["git", "status"]:
                result.stdout = ""  # clean tree
            elif cmd[:2] == ["git", "fetch"]:
                result.stdout = ""
            elif cmd[:2] == ["git", "rev-parse"] and "--abbrev-ref" in cmd:
                result.stdout = "main"
            elif cmd[:2] == ["git", "rev-parse"]:
                # Both HEAD and origin/main return the same hash
                result.stdout = same_hash
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("self_host_cli.update.subprocess.run", fake_run)
        args = Namespace()
        run_update(args)
        out = capsys.readouterr().out
        assert "Already up to date" in out

    def test_resets_and_syncs(self, monkeypatch, capsys):
        monkeypatch.setattr("self_host_cli.update._is_git_repo", lambda: True)

        commands_run = []
        call_count = {"rev_parse_plain": 0}

        def fake_run(cmd, **kwargs):
            if cmd[0] == "git":
                assert cmd[:3] == ["git", "-c", "core.hooksPath=/dev/null"]
                cmd = [cmd[0], *cmd[3:]]
            commands_run.append(cmd[:3])
            result = MagicMock()
            result.returncode = 0
            if cmd[:2] == ["git", "status"]:
                result.stdout = ""
            elif cmd[:2] == ["git", "fetch"]:
                result.stdout = ""
            elif "--abbrev-ref" in cmd:
                result.stdout = "main"
            elif "--short" in cmd:
                result.stdout = "def5678"
            elif cmd[:2] == ["git", "rev-parse"]:
                # First call (HEAD) returns old hash, second (origin/main) returns new
                call_count["rev_parse_plain"] += 1
                if call_count["rev_parse_plain"] == 1:
                    result.stdout = "aaa0000aaa0000aaa0000"
                else:
                    result.stdout = "bbb1111bbb1111bbb1111"
            elif cmd[:2] == ["git", "log"]:
                result.stdout = ""
            elif cmd[:2] == ["git", "reset"]:
                result.stdout = ""
            elif cmd[:2] == ["pixi", "install"]:
                result.stdout = ""
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr("self_host_cli.update.subprocess.run", fake_run)
        args = Namespace()
        run_update(args)

        # Verify git reset --hard and pixi install were called
        flat = [c[:2] for c in commands_run]
        assert ["git", "reset"] in flat
        assert ["pixi", "install"] in flat
        out = capsys.readouterr().out
        assert "Code updated" in out
