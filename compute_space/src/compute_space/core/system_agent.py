from __future__ import annotations

import json
import subprocess
import time

import cattrs

from compute_space.core.util import async_wrap
from openhost_system_agent.protocol import DiffResult
from openhost_system_agent.protocol import FetchResult
from openhost_system_agent.protocol import MigrationStatus
from openhost_system_agent.protocol import RemoteInfo


class SystemAgentError(Exception):
    pass


# sudo prints `sudo: openhost_system_agent: command not found` when the symlink
# at /usr/local/bin/openhost_system_agent isn't resolvable. That is usually a
# genuinely-missing symlink (needs an ansible re-deploy), but it also shows up
# TRANSIENTLY right after a self-update restart: the freshly-started
# compute_space immediately runs its "check for updates", and for a brief window
# sudo's PATH lookup can miss the (present) symlink before the environment
# settles. So we retry this specific error a few times — it self-heals within a
# second or two — and only surface the "re-run ansible" guidance if it persists.
_CMD_NOT_FOUND = "openhost_system_agent: command not found"
_NOT_FOUND_RETRIES = 5
_NOT_FOUND_RETRY_DELAY = 0.5


def _run_system_agent(*args: str, timeout: int = 300) -> str:
    """Run the agent, raising SystemAgentError on failure. Returns stdout.

    Retries transient post-restart "command not found" errors a few times (see
    _CMD_NOT_FOUND above) before giving up; all other failures raise immediately.
    """
    last_not_found: str | None = None
    for attempt in range(_NOT_FOUND_RETRIES):
        try:
            result = subprocess.run(
                ["sudo", "openhost_system_agent", *args],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as e:
            raise SystemAgentError("openhost_system_agent not found on PATH") from e
        except subprocess.TimeoutExpired as e:
            raise SystemAgentError(f"openhost_system_agent timed out after {timeout}s") from e

        if result.returncode == 0:
            return result.stdout

        try:
            body = json.loads(result.stdout)
            error = body.get("error", result.stderr)
        except (json.JSONDecodeError, ValueError):
            error = result.stderr or result.stdout

        if _CMD_NOT_FOUND in str(error):
            # Transient startup race — wait and retry (unless this was the last
            # attempt, in which case fall through to the persistent-failure path).
            last_not_found = str(error).strip()
            if attempt < _NOT_FOUND_RETRIES - 1:
                time.sleep(_NOT_FOUND_RETRY_DELAY)
                continue
            raise SystemAgentError(
                f"{last_not_found}\n"
                "\n"
                "The openhost_system_agent binary is not on sudo's PATH. "
                "Re-running the ansible deploy against this host will reinstall it — "
                "see https://github.com/imbue-openhost/openhost/blob/main/ansible/readme.md"
            )
        raise SystemAgentError(str(error))

    # Unreachable: the loop either returns, retries, or raises. Guard for safety.
    raise SystemAgentError(last_not_found or "openhost_system_agent failed")


def _call_system_agent_sync[ResultT](result_type: type[ResultT], *args: str, timeout: int = 300) -> ResultT:
    stdout = _run_system_agent(*args, timeout=timeout)
    try:
        raw = json.loads(stdout)
    except (json.JSONDecodeError, ValueError) as e:
        raise SystemAgentError(f"Invalid JSON from system agent: {stdout}") from e

    try:
        return cattrs.structure(raw, result_type)
    except (cattrs.ClassValidationError, KeyError, TypeError) as e:
        raise SystemAgentError(f"Unexpected response shape from system agent: {e}") from e


@async_wrap
def system_agent_fetch() -> FetchResult:
    return _call_system_agent_sync(FetchResult, "update", "fetch")


@async_wrap
def system_agent_show_diff() -> DiffResult:
    return _call_system_agent_sync(DiffResult, "update", "show-diff")


@async_wrap
def system_agent_apply() -> None:
    # On success the agent restarts openhost, which kills this process before
    # it returns, so there is nothing to parse. Only failures return here (and
    # raise); the restarted compute_space reads the migration log for results.
    _run_system_agent("update", "apply", timeout=600)


@async_wrap
def system_agent_set_remote(url: str) -> RemoteInfo:
    return _call_system_agent_sync(RemoteInfo, "update", "set-remote", url, timeout=120)


@async_wrap
def system_agent_get_remote() -> RemoteInfo:
    return _call_system_agent_sync(RemoteInfo, "update", "get-remote")


@async_wrap
def system_agent_status() -> MigrationStatus:
    return _call_system_agent_sync(MigrationStatus, "status")


@async_wrap
def system_agent_set_update_token(token: str) -> None:
    # Written via the (root) agent so it lands in the root-managed updater dir
    # regardless of directory ownership, and is readable by the root updater.
    _run_system_agent("updater", "set-token", token, timeout=30)


@async_wrap
def system_agent_clear_update_token() -> None:
    _run_system_agent("updater", "clear-token", timeout=30)


def system_agent_stop_updater_sync() -> None:
    """Stop the detached updater (releasing 80/443), synchronously.

    Called from compute_space startup right before Caddy binds 80/443, so the
    updater lets go first and Caddy can't lose the handoff race. Best-effort:
    startup must never fail because the (cosmetic) updater couldn't be stopped.
    """
    try:
        _run_system_agent("updater", "stop", timeout=30)
    except SystemAgentError:
        pass
