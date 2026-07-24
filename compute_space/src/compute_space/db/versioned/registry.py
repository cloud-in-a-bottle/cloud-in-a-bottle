"""Hand-maintained list of numbered migrations.

Append a new :class:`Migration` subclass to :data:`REGISTRY` each time a
new schema change is added. The runner validates that versions are
strictly increasing and contiguous starting at 2.
"""

from __future__ import annotations

from compute_space.db.versioned.base import Migration
from compute_space.db.versioned.migrations.v0002_noop import Migration0002Noop
from compute_space.db.versioned.migrations.v0003_drop_password_needs_set import Migration0003DropPasswordNeedsSet
from compute_space.db.versioned.migrations.v0004_apps_removing_status import Migration0004AppsRemovingStatus
from compute_space.db.versioned.migrations.v0005_archive_backend import Migration0005ArchiveBackend
from compute_space.db.versioned.migrations.v0006_apps_installed_by import Migration0006AppsInstalledBy
from compute_space.db.versioned.migrations.v0007_app_ids import Migration0007AppIds
from compute_space.db.versioned.migrations.v0008_drop_v1_service_tables import Migration0008DropV1ServiceTables
from compute_space.db.versioned.migrations.v0009_session_auth import Migration0009SessionAuth
from compute_space.db.versioned.migrations.v0010_app_links import Migration0010AppLinks
from compute_space.db.versioned.migrations.v0011_cpu_cores import Migration0011CpuCores
from compute_space.db.versioned.migrations.v0012_archive_local_backend import Migration0012ArchiveLocalBackend

# Numbered migrations in apply order. Versions MUST start at 2 and be
# contiguous. v0 (legacy) and v1 (baseline produced by the existing
# ``migrate()`` function) are handled out of band by the runner.
REGISTRY: list[Migration] = [
    Migration0002Noop(),
    Migration0003DropPasswordNeedsSet(),
    Migration0004AppsRemovingStatus(),
    Migration0005ArchiveBackend(),
    Migration0006AppsInstalledBy(),
    Migration0007AppIds(),
    Migration0008DropV1ServiceTables(),
    Migration0009SessionAuth(),
    Migration0010AppLinks(),
    Migration0011CpuCores(),
    Migration0012ArchiveLocalBackend(),
]
