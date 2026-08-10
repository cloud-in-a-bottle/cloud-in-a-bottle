"""Base classes for versioned migrations.

Transaction contract
--------------------

Each :class:`Migration` owns its own transaction. :meth:`Migration.apply`
is the public entry point the runner calls; it wraps :meth:`up` in a
single ``BEGIN EXCLUSIVE`` + version-bump + ``COMMIT`` so "migration
applied" and "schema_version bumped" are atomic.

Migration authors MUST NOT call ``db.commit()`` or issue ``BEGIN`` /
``COMMIT`` / ``ROLLBACK`` inside :meth:`up` — doing so breaks the
atomicity guarantee by ending the transaction that :meth:`apply` opened.
This rule is documented and trusted rather than enforced; we don't try
to police it.

Why ``apply`` and ``up`` are separate methods
---------------------------------------------

There are two kinds of migrations and they need transactions opened
differently:

* A Python migration runs one ``db.execute(...)`` at a time. The runner
  can safely bracket the whole sequence with an explicit
  ``db.execute("BEGIN EXCLUSIVE")`` ... ``db.execute("COMMIT")`` pair,
  and ``up()`` just emits the per-statement calls in between. That is
  what :meth:`Migration.apply` does.

* A SQL-file migration is opaque — the file may contain any number of
  statements and we do not parse it. The only built-in way to run a
  multi-statement SQL string is :meth:`sqlite3.Connection.executescript`,
  and *executescript issues an implicit* ``COMMIT`` *before running
  the script*. That implicit commit would close out a ``BEGIN EXCLUSIVE``
  the runner had opened, defeating the atomicity guarantee.

So SQL-file migrations have to embed their own ``BEGIN EXCLUSIVE`` /
``COMMIT`` *inside the string passed to executescript*. The executescript
call's implicit pre-commit hits nothing (we are in autocommit mode);
then the script itself opens, runs, and closes a transaction in one
call. That is what :meth:`SqlFileMigration.apply` does.

We used to have a hand-rolled SQL statement splitter to keep SQL files
on the Python-execute path, but custom parsers have custom bugs;
deleting it and trusting executescript is the simpler contract.

Both paths share the same outward behaviour (atomic apply + stamp), so
the runner just calls ``migration.apply(db)`` without caring which
subclass it is.

The caller of :meth:`apply` MUST open the DB in autocommit mode
(``sqlite3.connect(..., isolation_level=None)``). Python's default
isolation level wraps DML statements in implicit transactions, which
would race with our explicit ``BEGIN EXCLUSIVE``.
"""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path
from typing import ClassVar

# DDL for the single-row schema-version metadata table. Kept here so both
# the runner and the legacy migrate() import the exact same string.
SCHEMA_VERSION_DDL = (
    "CREATE TABLE IF NOT EXISTS schema_version (id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL)"
)


class Migration:
    """Base class for a single numbered schema migration.

    Subclasses set ``version`` (the target version after the migration
    runs — strictly greater than the source version) and implement
    :meth:`up`.

    Do NOT commit inside :meth:`up`. See the module docstring for the
    transaction contract.
    """

    version: ClassVar[int] = 0

    # Opt out of the PRAGMA foreign_keys tx-safety lint for migrations that
    # deliberately toggle FKs across a table swap (see TestPragmaHeuristic).
    allow_pragma_foreign_keys: ClassVar[bool] = False

    def up(self, db: sqlite3.Connection) -> None:
        raise NotImplementedError

    def apply(self, db: sqlite3.Connection) -> None:
        """Atomically apply ``up(db)`` and bump schema_version in one tx.

        The caller must already hold the process-level migration lock.
        ``db`` must be in autocommit mode (``isolation_level=None``) so
        that the explicit ``BEGIN EXCLUSIVE`` below is not short-circuited
        by Python's implicit-transaction handling.
        """
        db.execute("BEGIN EXCLUSIVE")
        try:
            self.up(db)
            db.execute(
                "INSERT OR REPLACE INTO schema_version (id, version) VALUES (1, ?)",
                (self.version,),
            )
            db.execute("COMMIT")
        except Exception:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise


class SqlFileMigration(Migration):
    """Migration whose body is a sibling ``.sql`` file.

    ``sql_file`` is resolved relative to the directory of the subclass's
    source file, so the ``.sql`` lives next to its Python wrapper.

    :meth:`up` is unused on this subclass — :meth:`apply` reads the file
    and drives everything via a single :meth:`sqlite3.Connection.executescript`
    call. The .sql MUST NOT contain ``BEGIN``, ``COMMIT`` or ``ROLLBACK``;
    :meth:`apply` wraps the file in its own ``BEGIN EXCLUSIVE`` ... ``COMMIT``.
    """

    sql_file: ClassVar[str] = ""

    def up(self, db: sqlite3.Connection) -> None:
        # Not used — apply() handles everything via executescript.
        raise NotImplementedError("SqlFileMigration drives execution through apply(), not up()")

    def apply(self, db: sqlite3.Connection) -> None:
        # Why BEGIN EXCLUSIVE lives inside the script (not wrapping an
        # outer db.execute("BEGIN EXCLUSIVE") around db.executescript):
        # sqlite3.Connection.executescript() issues an implicit COMMIT
        # of any open transaction *before* running its script. Opening
        # the tx from Python then calling executescript would close
        # that tx immediately and we'd lose atomicity. Putting the
        # BEGIN/COMMIT inside the script text is the only way to give
        # executescript a multi-statement body that runs under our own
        # transaction.
        #
        # ``version`` is a trusted int (ClassVar on a registered
        # subclass), not user input, so interpolating it into the SQL
        # string is safe. executescript does not accept bound params.
        sql = self._load_sql()
        wrapped = (
            "BEGIN EXCLUSIVE;\n"
            f"{sql}\n"
            f"INSERT OR REPLACE INTO schema_version (id, version) VALUES (1, {int(self.version)});\n"
            "COMMIT;\n"
        )
        try:
            db.executescript(wrapped)
        except Exception:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def _load_sql(self) -> str:
        module_file = inspect.getfile(self.__class__)
        sql_path = Path(module_file).resolve().parent / self.sql_file
        return sql_path.read_text()
