import asyncio
import sqlite3
import threading
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import closing
from contextlib import contextmanager

from compute_space.core.logging import logger
from compute_space.db.connection import open_db

_mutation_lock = threading.Lock()
_pending: set[asyncio.Task[object]] = set()


@contextmanager
def checkout_operation() -> Iterator[None]:
    """Serialize name ownership and checkout mutations across API and default-app workers."""
    with _mutation_lock:
        yield


def _run[T](db_path: str, operation: Callable[[sqlite3.Connection], T]) -> T:
    # The worker owns the connection and lock even if its request is cancelled.
    with checkout_operation(), closing(open_db(db_path)) as db:
        return operation(db)


def _finished(task: asyncio.Task[object]) -> None:
    _pending.discard(task)
    if not task.cancelled() and (error := task.exception()) is not None:
        logger.error("Checkout operation failed: {}", error)


async def run_checkout_operation[T](db_path: str, operation: Callable[[sqlite3.Connection], T]) -> T:
    task = asyncio.create_task(asyncio.to_thread(_run, db_path, operation))
    _pending.add(task)
    task.add_done_callback(_finished)
    return await asyncio.shield(task)
