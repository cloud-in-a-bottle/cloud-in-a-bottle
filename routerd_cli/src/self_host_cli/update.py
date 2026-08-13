"""``openhost update`` -- update Cloud in a Bottle code.

Pulls latest code via git fetch + reset, then runs ``pixi install`` to
sync the env to the new lockfile.
"""

import argparse
import subprocess
import sys

from compute_space import OPENHOST_PROJECT_DIR
from self_host_cli.down import _ROUTER_PID
from self_host_cli.down import _is_alive
from self_host_cli.down import _read_pid


def _is_git_repo() -> bool:
    """Check if the project directory is a git checkout."""
    return (OPENHOST_PROJECT_DIR / ".git").is_dir()


def _update_code() -> None:
    """Pull latest code from git and sync dependencies."""
    print("Checking for code updates...", flush=True)
    project_dir = str(OPENHOST_PROJECT_DIR)

    if not _is_git_repo():
        print(
            "  Not a git repository. Clone openhost with git to use updates.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # Check for uncommitted changes
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        cwd=project_dir,
    )
    if status.stdout.strip():
        print(
            "  Warning: working tree has uncommitted changes. "
            "Skipping code update.\n"
            "  Stash or commit your changes first.",
        )
        return

    # Fetch latest
    subprocess.run(
        ["git", "fetch", "origin"],
        capture_output=True,
        cwd=project_dir,
    )

    # Determine current branch
    branch_result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        cwd=project_dir,
    )
    branch = branch_result.stdout.strip() or "main"

    # Check if HEAD already matches the remote
    local_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        cwd=project_dir,
    ).stdout.strip()
    remote_rev = subprocess.run(
        ["git", "rev-parse", f"origin/{branch}"],
        capture_output=True,
        text=True,
        cwd=project_dir,
    ).stdout.strip()

    if local_rev == remote_rev:
        short = local_rev[:7]
        print(f"  Already up to date ({branch} @ {short}).")
        return

    # Show new commits on origin (if any; may be empty after a force push)
    subprocess.run(
        ["git", "log", f"HEAD..origin/{branch}", "--oneline"],
        cwd=project_dir,
    )

    # Hard-reset to match origin (handles both fast-forward and force-push)
    print(f"  Resetting to origin/{branch}...")
    reset = subprocess.run(
        ["git", "reset", "--hard", f"origin/{branch}"],
        capture_output=True,
        text=True,
        cwd=project_dir,
    )
    if reset.returncode != 0:
        print(f"  Error: git reset failed:\n{reset.stderr}", file=sys.stderr)
        raise SystemExit(1)

    # Sync dependencies
    print("  Running pixi install...")
    sync = subprocess.run(
        ["pixi", "install"],
        capture_output=True,
        text=True,
        cwd=project_dir,
    )
    if sync.returncode != 0:
        print(f"  Warning: pixi install failed:\n{sync.stderr}", file=sys.stderr)

    current = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        cwd=project_dir,
    ).stdout.strip()
    print(f"  Code updated ({branch} @ {current}).")


def _check_router_not_running() -> None:
    """Warn if the router appears to be running."""
    pid = _read_pid(_ROUTER_PID)
    if pid is not None and _is_alive(pid):
        print(
            f"Warning: router appears to be running (pid {pid}). "
            "Consider running 'openhost down' first, then updating.",
        )


def run_update(args: argparse.Namespace) -> None:
    _check_router_not_running()
    _update_code()
    print()
    print("Run 'openhost up' to start with the updated configuration.")
