"""Compute space self-update: status checking, orchestration, restart."""

import asyncio
import subprocess
from enum import StrEnum
from pathlib import Path

import git

from compute_space.core.git_ops import count_commits_vs_remote
from compute_space.core.git_ops import fetch
from compute_space.core.git_ops import hard_checkout_ref
from compute_space.core.git_ops import is_dirty
from compute_space.core.git_ops import validate_repo
from compute_space.core.logging import logger
from compute_space.core.util import async_wrap


class GitState(StrEnum):
    NO_REMOTE = "NO_REMOTE"
    DIRTY = "DIRTY"
    AHEAD_OF_REMOTE = "AHEAD_OF_REMOTE"
    BEHIND_REMOTE = "BEHIND_REMOTE"
    UP_TO_DATE = "UP_TO_DATE"


async def check_git_state(repo_path: Path) -> GitState:
    """
    Raises:
        ValueError: if the local repository is not in a clean state for performing updates
    """
    try:
        await validate_repo(repo_path)
    except (git.NoSuchPathError, git.InvalidGitRepositoryError) as e:
        raise ValueError(str(e)) from e

    try:
        await fetch(repo_path)
        ahead, behind = await count_commits_vs_remote(repo_path)
    except LookupError:
        return GitState.NO_REMOTE
    except ValueError:
        raise

    if await is_dirty(repo_path):
        return GitState.DIRTY

    if ahead > 0:
        return GitState.AHEAD_OF_REMOTE
    if behind > 0:
        return GitState.BEHIND_REMOTE
    else:
        assert ahead == 0 and behind == 0
        return GitState.UP_TO_DATE


async def hard_checkout_and_validate(repo_path: Path, ref: str) -> None:
    await fetch(repo_path)
    await hard_checkout_ref(repo_path, ref)
    await run_pixi_install(repo_path)


@async_wrap
def run_pixi_install(repo_path: Path) -> None:
    """Run pixi install to update dependencies."""
    try:
        result = subprocess.run(
            ["/home/host/.pixi/bin/pixi", "install"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            output = f"Stdout:\n{result.stdout}\n\nStderr:\n{result.stderr}"
            raise RuntimeError(f"pixi install failed. exit code: {result.returncode}\n{output}")
    except FileNotFoundError as e:
        raise RuntimeError("pixi command not found. Is pixi installed and on the PATH?") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("pixi install timed out after 5 minutes") from e


RESTART_EXIT_CODE = 42

_shutdown_event: asyncio.Event | None = None


def initialize_shutdown_event(event: asyncio.Event) -> None:
    """Called once at startup to wire the shutdown trigger."""
    global _shutdown_event  # noqa: PLW0603
    _shutdown_event = event


def is_shutdown_pending() -> bool:
    return _shutdown_event is not None and _shutdown_event.is_set()


async def wait_for_shutdown() -> None:
    """Block until the shutdown event is set. No-op if not initialized."""
    if _shutdown_event is not None:
        await _shutdown_event.wait()


def trigger_restart() -> None:
    logger.info(f"Scheduling graceful shutdown for restart (exit code {RESTART_EXIT_CODE})")
    if _shutdown_event is None:
        raise RuntimeError("shutdown event not initialized — call initialize_shutdown_event() at startup")
    _shutdown_event.set()
