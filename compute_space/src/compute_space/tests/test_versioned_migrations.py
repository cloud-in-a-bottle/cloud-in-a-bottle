"""Tests for the versioned migration framework itself.

Scope: framework plumbing — base class, registry validation, runner
behaviour, fresh init, concurrency, the PRAGMA heuristic. The
migrations themselves are covered by the snapshot tests in
``test_migration_snapshots.py``.

Covers:
  - schema_version metadata table (REQ-VER-1..5)
  - Refusal to start on a pre-versioned-migrations (v0) DB
  - Migration base class + SqlFileMigration (REQ-MF-*)
  - Registry validation: contiguous, starts at 2 (REQ-REG-*)
  - Runner behaviour: lock, apply, atomic version bump, rollback (REQ-RUN-*)
  - Fresh-DB init via schema.sql, stamps to highest (REQ-INIT-*)
  - Concurrency: two concurrent startups (REQ-TEST-6)
  - PRAGMA foreign_keys heuristic warning (REQ-TEST-7)
"""

from __future__ import annotations

import importlib
import inspect
import re
import sqlite3
import subprocess
import sys
import time
import warnings as _warnings
from pathlib import Path

import pytest
from loguru import logger

from compute_space.db.versioned import REGISTRY
from compute_space.db.versioned import Migration
from compute_space.db.versioned import SqlFileMigration
from compute_space.db.versioned import apply_migrations
from compute_space.db.versioned import highest_registered_version
from compute_space.db.versioned import read_version
from compute_space.db.versioned import validate_registry

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Registry validation (REQ-REG-2)
# --------------------------------------------------------------------------- #


class _FakeMigration(Migration):
    def __init__(self, version: int):
        self.version = version  # type: ignore[misc]

    def up(self, db: sqlite3.Connection) -> None:  # pragma: no cover - unused
        pass


class TestRegistryValidation:
    def test_empty_registry_is_valid(self):
        validate_registry([])

    def test_single_v2_is_valid(self):
        validate_registry([_FakeMigration(2)])

    def test_gap_is_rejected(self):
        with pytest.raises(RuntimeError, match="not strictly increasing"):
            validate_registry([_FakeMigration(2), _FakeMigration(4)])

    def test_duplicate_is_rejected(self):
        with pytest.raises(RuntimeError, match="not strictly increasing"):
            validate_registry([_FakeMigration(2), _FakeMigration(2)])

    def test_wrong_start_is_rejected(self):
        with pytest.raises(RuntimeError, match="not strictly increasing"):
            validate_registry([_FakeMigration(3)])

    def test_out_of_order_is_rejected(self):
        with pytest.raises(RuntimeError, match="not strictly increasing"):
            validate_registry([_FakeMigration(3), _FakeMigration(2)])


# --------------------------------------------------------------------------- #
# Fresh-DB init (REQ-INIT-1, REQ-INIT-2)
# --------------------------------------------------------------------------- #


class TestFreshInit:
    def test_empty_file_goes_through_fresh_path(self, tmp_path):
        db_path = str(tmp_path / "fresh.db")
        apply_migrations(db_path)

        db = sqlite3.connect(db_path)
        try:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            assert "apps" in tables
            assert "schema_version" in tables
            assert read_version(db) == highest_registered_version(REGISTRY)
        finally:
            db.close()

    def test_fresh_init_does_not_replay_migrations(self, tmp_path):
        """After a fresh init, a second run must be a pure no-op."""
        db_path = str(tmp_path / "fresh.db")

        class SpyMigration(Migration):
            version = 2
            calls: list[int] = []

            def up(self, db):
                SpyMigration.calls.append(1)

        # Fresh init with the spy registered.
        apply_migrations(db_path, registry=[SpyMigration()])
        # Fresh path stamps to highest (= 2), so migration should NOT have run.
        assert SpyMigration.calls == []

        # Second run: version already 2, nothing to do.
        apply_migrations(db_path, registry=[SpyMigration()])
        assert SpyMigration.calls == []

        db = sqlite3.connect(db_path)
        try:
            assert read_version(db) == 2
        finally:
            db.close()


# --------------------------------------------------------------------------- #
# Version gating (REQ-VER-4)
# --------------------------------------------------------------------------- #


class TestVersionGating:
    def test_db_ahead_of_code_aborts(self, tmp_path):
        db_path = str(tmp_path / "future.db")
        apply_migrations(db_path)  # fresh init

        # Hand-stamp a version higher than any registered.
        db = sqlite3.connect(db_path)
        try:
            db.execute("UPDATE schema_version SET version = 99 WHERE id = 1")
            db.commit()
        finally:
            db.close()

        with pytest.raises(RuntimeError, match="newer than the highest version"):
            apply_migrations(db_path)


# --------------------------------------------------------------------------- #
# Legacy v0 refusal
# --------------------------------------------------------------------------- #


class TestLegacyV0Refusal:
    def test_tables_but_no_stamp_refuses_to_start(self, tmp_path):
        """A pre-versioned-migrations DB (tables present, no schema_version row)
        must raise a clear error directing the operator to upgrade through an
        earlier release first."""
        db_path = str(tmp_path / "v0.db")
        db = sqlite3.connect(db_path)
        # Any table is enough to make _has_any_tables true. We don't need to
        # reproduce the real v0 shape — the runner only checks "tables exist
        # AND no schema_version row" to detect this case.
        db.executescript("CREATE TABLE apps (id INTEGER PRIMARY KEY);")
        db.commit()
        db.close()

        with pytest.raises(RuntimeError, match=r"schema_version"):
            apply_migrations(db_path)


# --------------------------------------------------------------------------- #
# Numbered migration plumbing (REQ-MF-*, REQ-RUN-*)
# --------------------------------------------------------------------------- #


class TestNumberedMigrationRunner:
    def _v1_db(self, path: str) -> None:
        """Stamp the DB to v1 precisely, regardless of the live REGISTRY.

        Tests in this class register their own v2+ migrations; they need
        the DB to start at v1 so their migrations have room to run.
        """
        apply_migrations(path, registry=[])

    def test_migration_applied_atomically(self, tmp_path):
        db_path = str(tmp_path / "mig.db")
        self._v1_db(db_path)

        class AddTableMig(Migration):
            version = 2

            def up(self, db):
                db.execute("CREATE TABLE added (x INTEGER NOT NULL)")
                db.execute("INSERT INTO added (x) VALUES (42)")

        apply_migrations(db_path, registry=[AddTableMig()])

        db = sqlite3.connect(db_path)
        try:
            assert read_version(db) == 2
            row = db.execute("SELECT x FROM added").fetchone()
            assert row[0] == 42
        finally:
            db.close()

    def test_failing_migration_rolls_back(self, tmp_path):
        """REQ-MF-4: failing migration leaves DB + version unchanged."""
        db_path = str(tmp_path / "rollback.db")
        self._v1_db(db_path)

        class BadMig(Migration):
            version = 2

            def up(self, db):
                db.execute("CREATE TABLE will_be_gone (x INTEGER)")
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            apply_migrations(db_path, registry=[BadMig()])

        db = sqlite3.connect(db_path)
        try:
            assert read_version(db) == 1
            row = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='will_be_gone'").fetchone()
            assert row is None, "Failed migration's DDL must be rolled back"
        finally:
            db.close()

        # Re-running cleanly retries from last successful version (REQ-RUN-5).
        class GoodMig(Migration):
            version = 2

            def up(self, db):
                db.execute("CREATE TABLE ok (x INTEGER)")

        apply_migrations(db_path, registry=[GoodMig()])
        db = sqlite3.connect(db_path)
        try:
            assert read_version(db) == 2
        finally:
            db.close()

    def test_migrations_apply_in_order(self, tmp_path):
        db_path = str(tmp_path / "ordered.db")
        self._v1_db(db_path)

        order: list[int] = []

        def make(ver):
            class M(Migration):
                version = ver

                def up(self, db):
                    order.append(ver)
                    db.execute(f"CREATE TABLE step_{ver} (x INTEGER)")

            return M()

        apply_migrations(db_path, registry=[make(2), make(3), make(4)])
        assert order == [2, 3, 4]

        db = sqlite3.connect(db_path)
        try:
            assert read_version(db) == 4
        finally:
            db.close()

    def test_info_log_per_applied_migration(self, tmp_path):
        """REQ-RUN-4: one INFO line per applied migration with version + duration."""
        db_path = str(tmp_path / "log.db")
        self._v1_db(db_path)

        captured: list[str] = []
        sink_id = logger.add(lambda msg: captured.append(str(msg)), level="INFO")
        try:

            class MigA(Migration):
                version = 2

                def up(self, db):
                    db.execute("CREATE TABLE t_a (x INTEGER)")

            class MigB(Migration):
                version = 3

                def up(self, db):
                    db.execute("CREATE TABLE t_b (x INTEGER)")

            apply_migrations(db_path, registry=[MigA(), MigB()])
        finally:
            logger.remove(sink_id)

        blob = "\n".join(captured)
        assert "v1 \u2192 v2" in blob
        assert "v2 \u2192 v3" in blob
        # Each applied-migration line includes a duration in seconds.
        assert re.search(r"in \d+\.\d+s", blob)


# --------------------------------------------------------------------------- #
# SqlFileMigration (REQ-MF-3)
# --------------------------------------------------------------------------- #


class TestSqlFileMigration:
    def test_runs_sibling_sql_file(self, tmp_path):
        # Build a throwaway package so inspect.getfile() can resolve the
        # subclass's source path.
        pkg_dir = tmp_path / "migpkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "m0002.sql").write_text(
            "CREATE TABLE from_sql_file (id INTEGER PRIMARY KEY);\nINSERT INTO from_sql_file (id) VALUES (7);\n"
        )
        (pkg_dir / "m0002.py").write_text(
            "from compute_space.db.versioned import SqlFileMigration\n"
            "class M0002(SqlFileMigration):\n"
            "    version = 2\n"
            "    sql_file = 'm0002.sql'\n"
        )
        sys.path.insert(0, str(tmp_path))
        try:
            mod = importlib.import_module("migpkg.m0002")
        finally:
            sys.path.pop(0)
        M0002 = mod.M0002

        db_path = str(tmp_path / "sqlfile.db")
        apply_migrations(db_path, registry=[])  # v1 baseline
        apply_migrations(db_path, registry=[M0002()])

        db = sqlite3.connect(db_path)
        try:
            row = db.execute("SELECT id FROM from_sql_file").fetchone()
            assert row[0] == 7
            assert read_version(db) == 2
        finally:
            db.close()

    def test_sqlfile_syntax_error_rolls_back(self, tmp_path):
        """A ``.sql`` file with a syntax error must leave DB + version unchanged.

        ``SqlFileMigration.apply`` wraps the file in ``BEGIN EXCLUSIVE`` ...
        ``COMMIT`` and hands it to :meth:`sqlite3.Connection.executescript`;
        if any statement fails, the wrapper rolls back so the migration is
        all-or-nothing.
        """
        pkg_dir = tmp_path / "sqlfail_pkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "m.sql").write_text(
            "CREATE TABLE ok_before (x INTEGER);\nTHIS IS NOT VALID SQL;\nCREATE TABLE ok_after (x INTEGER);\n"
        )
        (pkg_dir / "m.py").write_text(
            "from compute_space.db.versioned import SqlFileMigration\n"
            "class Bad(SqlFileMigration):\n"
            "    version = 2\n"
            "    sql_file = 'm.sql'\n"
        )
        sys.path.insert(0, str(tmp_path))
        try:
            mod = importlib.import_module("sqlfail_pkg.m")
        finally:
            sys.path.pop(0)
        Bad = mod.Bad

        db_path = str(tmp_path / "sqlfail.db")
        apply_migrations(db_path, registry=[])  # v1 baseline
        with pytest.raises(sqlite3.Error):
            apply_migrations(db_path, registry=[Bad()])

        db = sqlite3.connect(db_path)
        try:
            for tbl in ("ok_before", "ok_after"):
                row = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)).fetchone()
                assert row is None, f"{tbl} must have been rolled back"
            assert read_version(db) == 1
        finally:
            db.close()

        # Subsequent cleanly retryable run (swap in a valid migration at v2).
        (pkg_dir / "good.sql").write_text("CREATE TABLE good (x INTEGER);\n")
        (pkg_dir / "good.py").write_text(
            "from compute_space.db.versioned import SqlFileMigration\n"
            "class Good(SqlFileMigration):\n"
            "    version = 2\n"
            "    sql_file = 'good.sql'\n"
        )
        sys.path.insert(0, str(tmp_path))
        try:
            good_mod = importlib.import_module("sqlfail_pkg.good")
        finally:
            sys.path.pop(0)
        apply_migrations(db_path, registry=[good_mod.Good()])

        db = sqlite3.connect(db_path)
        try:
            assert read_version(db) == 2
            assert db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='good'").fetchone() is not None
        finally:
            db.close()


# --------------------------------------------------------------------------- #
# Concurrency (REQ-TEST-6, REQ-RUN-1)
# --------------------------------------------------------------------------- #


class TestConcurrency:
    def test_two_concurrent_processes_serialize(self, tmp_path):
        """Two OS processes call apply_migrations against the same DB at once.

        fcntl.flock locks are per open-file-description, so this exercises
        real cross-process serialization — stronger than a thread-level
        test. Only one process applies the migration; the other waits
        for the lock, observes the already-bumped version, and returns
        a no-op.
        """
        db_path = str(tmp_path / "race.db")
        # Bring DB to v1 precisely so the workers' CounterMig(v=2) has room to run.
        apply_migrations(db_path, registry=[])

        counter_path = tmp_path / "counter.txt"
        counter_path.write_text("")

        worker_script = tmp_path / "worker.py"
        worker_script.write_text(
            "import sys, time\n"
            "from compute_space.db.versioned import Migration, apply_migrations\n"
            "\n"
            "DB_PATH = sys.argv[1]\n"
            "COUNTER = sys.argv[2]\n"
            "\n"
            "class CounterMig(Migration):\n"
            "    version = 2\n"
            "    def up(self, db):\n"
            "        with open(COUNTER, 'a') as f:\n"
            "            f.write('x')\n"
            "        # Hold the exclusive lock long enough for the other\n"
            "        # process to contend and queue behind us.\n"
            "        time.sleep(0.5)\n"
            "        db.execute('CREATE TABLE race_done (x INTEGER)')\n"
            "\n"
            "apply_migrations(DB_PATH, registry=[CounterMig()])\n"
        )

        def spawn():
            return subprocess.Popen(
                [sys.executable, str(worker_script), db_path, str(counter_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        p1 = spawn()
        # Small nudge so p1 almost certainly wins the flock race. The
        # assertion below still holds either way — only one applies.
        time.sleep(0.1)
        p2 = spawn()

        out1, err1 = p1.communicate(timeout=30)
        out2, err2 = p2.communicate(timeout=30)

        assert p1.returncode == 0, f"worker 1 crashed: {err1.decode()}"
        assert p2.returncode == 0, f"worker 2 crashed: {err2.decode()}"

        # Exactly one process ran CounterMig.up() — the other saw
        # version==2 already and no-op'd.
        assert counter_path.read_text() == "x", (
            f"Exactly one worker must apply the migration; got counter={counter_path.read_text()!r}"
        )

        db = sqlite3.connect(db_path)
        try:
            assert read_version(db) == 2
            # The applied migration's table must exist.
            row = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='race_done'").fetchone()
            assert row is not None
        finally:
            db.close()


# --------------------------------------------------------------------------- #
# PRAGMA foreign_keys heuristic (REQ-TEST-7)
# --------------------------------------------------------------------------- #


_PRAGMA_FK_RE = re.compile(r"PRAGMA\s+foreign_keys", re.IGNORECASE)

# Inline opt-out: a source containing this marker acknowledges its
# PRAGMA foreign_keys toggle as a deliberate, non-tx-safe op.
_PRAGMA_FK_OPT_OUT = "migration-lint: allow-pragma-foreign-keys"


def _scan_migration_for_unsafe_ops(migration: Migration) -> list[str]:
    """Flag ops known to confuse SQLite's transactional rollback.

    Currently: ``PRAGMA foreign_keys`` toggles inside the migration body
    (the PRAGMA is a no-op inside a tx, which tends to silently defeat
    the author's intent). Extend as we encounter more gotchas.
    """
    findings: list[str] = []
    if isinstance(migration, SqlFileMigration):
        sql_path = Path(inspect.getfile(migration.__class__)).resolve().parent / migration.sql_file
        if sql_path.exists():
            text = sql_path.read_text()
            if _PRAGMA_FK_RE.search(text) and _PRAGMA_FK_OPT_OUT not in text:
                findings.append(f"{sql_path.name}: PRAGMA foreign_keys inside SQL migration body")
    # Also scan the Python source of custom up() methods.
    try:
        src = inspect.getsource(type(migration))
    except (OSError, TypeError):
        src = ""
    if _PRAGMA_FK_RE.search(src) and _PRAGMA_FK_OPT_OUT not in src:
        findings.append(
            f"{type(migration).__module__}.{type(migration).__name__}: PRAGMA foreign_keys inside Python migration"
        )
    return findings


class TestPragmaHeuristic:
    def test_registered_migrations_have_no_unsafe_pragma_toggles(self):
        """Scan REGISTRY for PRAGMA foreign_keys toggles, which are
        known to silently no-op inside a transaction. Best-effort; emits a
        UserWarning rather than failing hard so migration authors can opt in
        with an inline justification comment.
        """
        findings: list[str] = []
        for mig in REGISTRY:
            findings.extend(_scan_migration_for_unsafe_ops(mig))
        for f in findings:
            _warnings.warn(f"Non-tx-safe op in migration: {f}", stacklevel=2)
        # The scan itself must run without error. Findings are advisory.

    def test_scanner_catches_pragma_in_sql_file(self, tmp_path):
        """Positive control: the heuristic actually flags a bad migration."""
        pkg_dir = tmp_path / "badpkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "bad.sql").write_text(
            "PRAGMA foreign_keys=OFF;\nCREATE TABLE t (x INTEGER);\nPRAGMA foreign_keys=ON;\n"
        )
        (pkg_dir / "bad.py").write_text(
            "from compute_space.db.versioned import SqlFileMigration\n"
            "class Bad(SqlFileMigration):\n"
            "    version = 2\n"
            "    sql_file = 'bad.sql'\n"
        )
        sys.path.insert(0, str(tmp_path))
        try:
            mod = importlib.import_module("badpkg.bad")
        finally:
            sys.path.pop(0)
        Bad = mod.Bad

        findings = _scan_migration_for_unsafe_ops(Bad())
        assert findings, "Scanner must flag PRAGMA foreign_keys in a migration SQL file"
        assert "PRAGMA foreign_keys" in findings[0]
