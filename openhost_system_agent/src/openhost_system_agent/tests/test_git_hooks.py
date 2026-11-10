import os
import shlex
import subprocess
from pathlib import Path

import pytest

from openhost_system_agent import apply_after_checkout
from openhost_system_agent import update
from openhost_system_agent.git_commands import HooklessRepo
from openhost_system_agent.git_commands import git_command


def test_system_update_git_entrypoints_disable_local_hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "global-config"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    repo_path = tmp_path / "repo"
    repo = HooklessRepo.init(repo_path, initial_branch="main")
    (repo_path / "data.txt").write_text("data")
    repo.git.add(".")
    repo.git.commit("-m", "initial")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "hook-ran"
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n")
    hook.chmod(0o755)
    repo.git.config("core.hooksPath", str(hooks))
    subprocess.run(["git", "checkout", "--detach", "HEAD"], cwd=repo_path, check=True, capture_output=True)
    assert marker.exists()
    marker.unlink()

    monkeypatch.setattr(update, "_PROJECT_DIR", repo_path)
    updater_repo = update._repo()
    updater_repo.git.checkout("main")
    updater_repo.git.checkout("--detach", "HEAD")
    result = apply_after_checkout._git(str(repo_path), "checkout", "main")
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    # Local config is preserved; suppression is applied independently to each command.
    result = subprocess.run(
        ["git", "config", "--local", "--get", "core.hooksPath"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == str(hooks)


def test_command_override_wins_over_environment_and_inherits_to_children(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": str(tmp_path / "untrusted"),
    }
    result = subprocess.run(
        git_command("config", "--get", "core.hooksPath"),
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "/dev/null"
    # A Git alias launches a child Git process, as recursive submodule commands do.
    result = subprocess.run(
        git_command("-c", "alias.child=!git config --get core.hooksPath", "child"),
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "/dev/null"
