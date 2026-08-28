from __future__ import annotations

import json
import subprocess
import time

import cattrs

from compute_space.core.logging import logger
from compute_space.core.util import async_wrap
from openhost_system_agent.protocol import DiffResult
from openhost_system_agent.protocol import FetchResult
from openhost_system_agent.protocol import MigrationStatus
from openhost_system_agent.protocol import RemoteInfo
from openhost_system_agent.updater.paths import data_dir_env_value
from openhost_system_agent.updater.paths import data_dir_setenv_pairs


class SystemAgentError(Exception):
    pass


# The agent resolves the shared updater paths (progress log, token, certs) from
# the data-dir env var (BOTTLE_DATA_DIR, legacy OPENHOST_DATA_DIR), but sudo's
# env_reset strips it. Forward it explicitly (via `sudo env`) so a non-default
# data_root_dir keeps compute_space and the agent pointed at the same files
# instead of silently diverging to the agent's default. Both names are forwarded.
def _agent_argv(*args: str) -> list[str]:
    data_dir = data_dir_env_value()
    if data_dir:
        return ["sudo", "env", *data_dir_setenv_pairs(data_dir), "openhost_system_agent", *args]
    return ["sudo", "openhost_system_agent", *args]


# Missing/unresolvable agent symlink. Usually needs an ansible re-deploy, but it
# also appears transiently right after a self-update restart before sudo's PATH
# lookup settles, so we retry before surfacing the "re-run ansible" guidance.
# (`sudo env` reports it as "No such file or directory" instead of sudo's form.)
_CMD_NOT_FOUND_PATTERNS = (
    "openhost_system_agent: command not found",
    "openhost_system_agent': No such file or directory",
)
_NOT_FOUND_RETRIES = 5
_NOT_FOUND_RETRY_DELAY = 0.5


def _is_cmd_not_found(error: str) -> bool:
    return any(p in error for p in _CMD_NOT_FOUND_PATTERNS)


def _run_system_agent(*args: str, timeout: int = 300) -> str:
    """Run the agent, raising SystemAgentError on failure. Returns stdout.

    Retries transient post-restart "command not found" errors (see
    _CMD_NOT_FOUND_PATTERNS); all other failures raise immediately.
    """
    last_not_found: str | None = None
    for attempt in range(_NOT_FOUND_RETRIES):
        try:
            result = subprocess.run(
                _agent_argv(*args),
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

        if _is_cmd_not_found(str(error)):
            last_not_found = str(error).strip()
            if attempt < _NOT_FOUND_RETRIES - 1:
                time.sleep(_NOT_FOUND_RETRY_DELAY)
                continue
            raise SystemAgentError(
                f"{last_not_found}\n"
                "\n"
                "The openhost_system_agent binary is not on sudo's PATH. "
                "Re-running the ansible deploy against this host will reinstall it — "
                "see https://github.com/cloud-in-a-bottle/cloud-in-a-bottle/blob/main/ansible/readme.md"
            )
        raise SystemAgentError(str(error))

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
    # The agent hands the walk to openhost-apply.service and returns immediately,
    # so this call is just the handoff — no output worth parsing. The walk then
    # stops openhost (killing this process), and the freshly started
    # compute_space reads the progress log for results. A failure here means the
    # walk never started, which the caller turns into a terminal log entry.
    _run_system_agent("update", "apply", timeout=60)


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
    _run_system_agent("updater", "set-token", token, timeout=30)


@async_wrap
def system_agent_clear_update_token() -> None:
    _run_system_agent("updater", "clear-token", timeout=30)


def system_agent_mark_boot_complete_sync() -> None:
    """Finalize the update progress log via the root agent (see mark_boot_complete)."""
    _run_system_agent("updater", "mark-booted", timeout=30)


@async_wrap
def system_agent_record_update_failure(message: str) -> None:
    """Record a terminal 'failed' progress entry via the root agent."""
    _run_system_agent("updater", "fail", message, timeout=30)


def system_agent_stop_updater_sync() -> None:
    """Stop the detached updater (releasing 80/443), synchronously. Best-effort.

    Failure is logged, not raised: Caddy still has its own bind-retry window, but
    if that also loses the race the instance has no TLS terminator — leave a trace
    of why the ports were never released.
    """
    try:
        _run_system_agent("updater", "stop", timeout=30)
    except SystemAgentError:
        logger.exception("failed to stop the detached updater; Caddy must win 80/443 via bind-retry")
