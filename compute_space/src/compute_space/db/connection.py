import contextlib
import sqlite3
import uuid
from collections.abc import Generator
from collections.abc import Iterator

from compute_space.db.versioned import apply_migrations

_db_path: str | None = None


@contextlib.contextmanager
def make_atomic_with_savepoint(db: sqlite3.Connection) -> Iterator[None]:
    """Create a savepoint and roll back to it if an exception is raised.

    Generally transactions make sqlite ops atomic, but for helper funcs that are called within a transaction,
    we can use savepoints to make them atomic without affecting the outer transaction.
    Then you don't have to wonder if the caller is using a transaction or not, to be sure that the helper func is atomic.
    """
    name = f"sp_{uuid.uuid4().hex}"
    db.execute(f"SAVEPOINT {name}")
    try:
        yield
        db.execute(f"RELEASE SAVEPOINT {name}")
    except BaseException:
        db.execute(f"ROLLBACK TO SAVEPOINT {name}")
        db.execute(f"RELEASE SAVEPOINT {name}")
        raise


def init_db(db_path: str) -> None:
    """Configure the SQLite path used by ``get_db()`` and run migrations."""
    global _db_path
    _db_path = db_path
    apply_migrations(db_path)


def get_db() -> sqlite3.Connection:
    """Return a fresh SQLite connection.

    Each call opens a new connection; CPython closes the underlying handle when
    the caller's last reference drops.  No cross-call sharing — callers that
    need a single transaction across multiple helpers must pass their conn
    explicitly.
    """
    if _db_path is None:
        raise RuntimeError("init_db() must be called before get_db()")
    return open_db(_db_path)


def open_db(db_path: str) -> sqlite3.Connection:
    """Open a configured connection for an operation with an explicit database path."""
    db = sqlite3.connect(db_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def provide_db() -> Generator[sqlite3.Connection, None, None]:
    """Litestar dependency: hand a fresh SQLite connection to a route, and close
    it once the handler returns.  Generator form so Litestar runs the post-yield
    cleanup deterministically instead of relying on GC of ``parsed_kwargs``."""
    db = get_db()
    try:
        yield db
    finally:
        db.close()
