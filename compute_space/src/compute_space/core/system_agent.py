from __future__ import annotations

import json
import subprocess

import cattrs

from compute_space.core.util import async_wrap
from openhost_system_agent.protocol import DiffResult
from openhost_system_agent.protocol import FetchResult
from openhost_system_agent.protocol import MigrationStatus
from openhost_system_agent.protocol import RemoteInfo


class SystemAgentError(Exception):
    pass


def _run_system_agent(*args: str, timeout: int = 300) -> str:
    """Run the agent, raising SystemAgentError on failure. Returns stdout."""
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

    if result.returncode != 0:
        try:
            body = json.loads(result.stdout)
            error = body.get("error", result.stderr)
        except (json.JSONDecodeError, ValueError):
            error = result.stderr or result.stdout
        # sudo prints `sudo: openhost_system_agent: command not found` when
        # the symlink at /usr/local/bin/openhost_system_agent is missing.
        # ansible installs it (install_openhost_units.yml); pre-symlink
        # installs hit this and need to re-run ansible to fix it.
        if "openhost_system_agent: command not found" in str(error):
            raise SystemAgentError(
                f"{str(error).strip()}\n"
                "\n"
                "The openhost_system_agent binary is not on sudo's PATH. "
                "Re-running the ansible deploy against this host will reinstall it — "
                "see https://github.com/imbue-openhost/openhost/blob/main/ansible/readme.md"
            )
        raise SystemAgentError(str(error))

    return result.stdout


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
