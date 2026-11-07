from __future__ import annotations

import datetime
import os
import time
from collections.abc import Sequence

from loguru import logger

from openhost_system_agent.migrations.base import SystemMigration
from openhost_system_agent.migrations.helpers import configure_migration_data_dir
from openhost_system_agent.migrations.helpers import require_router_stopped
from openhost_system_agent.migrations.migration_log import MIGRATIONS_PATH
from openhost_system_agent.migrations.migration_log import MigrationLogEntry
from openhost_system_agent.migrations.migration_log import append_entry
from openhost_system_agent.migrations.migration_log import current_host_version
from openhost_system_agent.migrations.migration_log import read_log
from openhost_system_agent.migrations.registry import REGISTRY
from openhost_system_agent.migrations.registry import latest_registry_version
from openhost_system_agent.migrations.registry import validate_registry


def apply_system_migrations(
    migrations_path: str = MIGRATIONS_PATH,
    registry: Sequence[SystemMigration] | None = None,
) -> list[int]:
    if registry is None:
        registry = REGISTRY
    validate_registry(registry)
    highest = latest_registry_version(registry)

    if os.geteuid() != 0:
        raise RuntimeError("System migrations must be run as root")

    entries = read_log(migrations_path)
    current = current_host_version(entries)

    if current == 0:
        raise RuntimeError(
            f"No migration history found at {migrations_path}. "
            "Run ansible setup.yml to bootstrap this host before applying migrations."
        )

    if current > highest:
        raise RuntimeError(
            f"Host is at system version {current} but this code only knows up to "
            f"version {highest}. Upgrade the code before running migrations."
        )

    if current < highest:
        configure_migration_data_dir()

    applied: list[int] = []
    for migration in registry:
        if migration.version <= current:
            continue
        require_router_stopped()
        source = current
        t0 = time.perf_counter()
        logger.info(f"Applying system migration v{source} → v{migration.version} ({type(migration).__name__})")
        try:
            migration.up()
        except Exception as e:
            logger.error(f"System migration v{source} → v{migration.version} ({type(migration).__name__}) failed")
            append_entry(
                migrations_path,
                MigrationLogEntry(
                    version=migration.version,
                    timestamp=datetime.datetime.now(datetime.UTC).isoformat(),
                    success=False,
                    error=str(e),
                ),
            )
            raise
        duration = time.perf_counter() - t0
        append_entry(
            migrations_path,
            MigrationLogEntry(
                version=migration.version,
                timestamp=datetime.datetime.now(datetime.UTC).isoformat(),
                success=True,
                error=None,
            ),
        )
        logger.info(
            f"Applied system migration v{source} → v{migration.version} "
            f"({type(migration).__name__}) in {duration:.3f}s"
        )
        current = migration.version
        applied.append(migration.version)

    if not applied:
        logger.info(f"System is at version {current} (up to date)")

    return applied
