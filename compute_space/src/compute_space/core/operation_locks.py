from __future__ import annotations

import asyncio
from collections.abc import Callable
from collections.abc import Coroutine
from typing import Any

from compute_space.core.logging import logger

operation_lock = asyncio.Lock()

_operation_tasks: set[asyncio.Task[Any]] = set()
_detached_tasks: set[asyncio.Task[Any]] = set()


def _log_detached_failure(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        if exc := task.exception():
            logger.opt(exception=exc).error("Exclusive operation failed")


def _operation_done(task: asyncio.Task[Any]) -> None:
    _operation_tasks.discard(task)
    if task in _detached_tasks:
        _detached_tasks.discard(task)
        _log_detached_failure(task)


def detach_operation(task: asyncio.Task[Any]) -> None:
    """Mark an owner whose request stopped awaiting it, so later failures are logged."""
    if task.done():
        _log_detached_failure(task)
    else:
        _detached_tasks.add(task)


async def wait_for_operations() -> None:
    """Wait for retained operation owners before process teardown."""
    while _operation_tasks:
        await asyncio.gather(*tuple(_operation_tasks), return_exceptions=True)


async def start_exclusive_operation[T](
    operation: Callable[[], Coroutine[Any, Any, T]],
) -> asyncio.Task[T] | None:
    """Start an operation immediately, or return None without waiting if one is active.

    The retained task owns lock release, so cancellation of the requesting task
    cannot release the lock while off-loop work is still running.
    """
    if operation_lock.locked():
        return None
    await operation_lock.acquire()

    async def run_as_owner() -> T:
        try:
            return await operation()
        finally:
            operation_lock.release()

    try:
        task = asyncio.create_task(run_as_owner())
    except BaseException:
        operation_lock.release()
        raise
    _operation_tasks.add(task)
    task.add_done_callback(_operation_done)
    return task
