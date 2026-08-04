from __future__ import annotations

from collections.abc import Sequence

from openhost_system_agent.migrations.base import SystemMigration
from openhost_system_agent.migrations.versions.v0002_baseline import Migration0002Baseline
from openhost_system_agent.migrations.versions.v0003_remove_obsolete_hairpin_nat import (
    Migration0003RemoveObsoleteHairpinNat,
)
from openhost_system_agent.migrations.versions.v0004_pixi_version import Migration0004PixiVersion
from openhost_system_agent.migrations.versions.v0005_pixi_ownership_failsafe import Migration0005PixiOwnershipFailsafe
from openhost_system_agent.migrations.versions.v0006_journald_size_cap import Migration0006JournaldSizeCap
from openhost_system_agent.migrations.versions.v0007_seed_domains_and_scrub import Migration0007SeedDomainsAndScrub
from openhost_system_agent.migrations.versions.v0008_restart_on_failure import Migration0008RestartOnFailure
from openhost_system_agent.migrations.versions.v0009_seed_imbue_identity_and_scrub import (
    Migration0009SeedImbueIdentityAndScrub,
)

# Numbered migrations in apply order. Versions MUST start at 2 and be
# contiguous. v1 is the baseline produced by ansible setup.yml.
REGISTRY: list[SystemMigration] = [
    Migration0002Baseline(),
    Migration0003RemoveObsoleteHairpinNat(),
    Migration0004PixiVersion(),
    Migration0005PixiOwnershipFailsafe(),
    Migration0006JournaldSizeCap(),
    Migration0007SeedDomainsAndScrub(),
    Migration0008RestartOnFailure(),
    Migration0009SeedImbueIdentityAndScrub(),
]


def validate_registry(registry: Sequence[SystemMigration]) -> None:
    if not registry:
        return
    versions = [m.version for m in registry]
    expected = list(range(2, 2 + len(versions)))
    if versions != expected:
        raise RuntimeError(
            f"Migration registry is not strictly increasing and contiguous starting at 2: "
            f"got {versions}, expected {expected}"
        )


def latest_registry_version(registry: Sequence[SystemMigration]) -> int:
    if not registry:
        return 1
    return registry[-1].version
