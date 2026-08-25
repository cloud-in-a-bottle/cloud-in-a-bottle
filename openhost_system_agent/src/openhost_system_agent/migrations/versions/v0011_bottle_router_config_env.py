"""Add a systemd drop-in advertising the router config path under BOTTLE_ROUTER_CONFIG.

The project was renamed OpenHost -> Cloud in a Bottle.  The router reads the
config-file path from ``BOTTLE_ROUTER_CONFIG`` (preferred), falling back to the
legacy ``OPENHOST_ROUTER_CONFIG``/``OPENHOST_CONFIG``.  The baseline unit already
sets ``OPENHOST_ROUTER_CONFIG``, so the router already resolves its config; this
drop-in additionally exposes the same value under the new name.

Rather than rewrite the main unit — or the shared ``build_openhost_service_unit``
builder, which lives in the v0002 baseline migration and so must not change — this
adds the new name through an additive systemd drop-in, the same file ansible
installs on fresh hosts (kept byte-identical; a test enforces it).  ``Environment=``
directives accumulate across drop-ins, so the new name layers on alongside the
legacy one without touching the baseline unit or any existing migration.  This is
the same pattern as v0010's journal-read drop-in, and it is the expected way to
change the unit going forward (see migrations/README.md, "Changing a systemd unit").

This migration writes the drop-in and reloads systemd so hosts provisioned before
the rename expose ``BOTTLE_ROUTER_CONFIG`` on their next self-update.  It does NOT
restart openhost: ``daemon-reload`` makes the drop-in authoritative for the next
start, and the apply walk restarts the service at the end anyway.  Idempotent:
writing the same file and reloading is safe to repeat.
"""

from __future__ import annotations

from openhost_system_agent.migrations.base import SystemMigration
from openhost_system_agent.migrations.helpers import run
from openhost_system_agent.migrations.helpers import write_file

BOTTLE_ROUTER_CONFIG_DROPIN_PATH = "/etc/systemd/system/openhost.service.d/20-bottle-router-config.conf"

# Additive drop-in: it layers a second ``Environment=`` (the new BOTTLE_ROUTER_CONFIG
# name) onto openhost.service without rewriting the main unit. Kept byte-identical
# with the ansible copy at ansible/files/openhost.service.d/20-bottle-router-config.conf
# (a test enforces this).
BOTTLE_ROUTER_CONFIG_DROPIN = """\
# Managed by OpenHost; do not edit by hand. Kept byte-identical between
# ansible/files/openhost.service.d/20-bottle-router-config.conf and the
# BOTTLE_ROUTER_CONFIG_DROPIN constant in openhost_system_agent's v0011 migration
# (a test enforces this).
#
# OpenHost -> Cloud in a Bottle rename: advertise the router config-file path under
# the new BOTTLE_ROUTER_CONFIG name. The main unit already sets the legacy
# OPENHOST_ROUTER_CONFIG and the router reads either (BOTTLE_ preferred), so this is
# additive — Environment= directives accumulate across drop-ins, layering the new
# name on without touching the baseline unit.
[Service]
Environment=BOTTLE_ROUTER_CONFIG=/home/host/.openhost/local_compute_space/config.toml
"""


class Migration0011BottleRouterConfigEnv(SystemMigration):
    version = 11

    def up(self) -> None:
        write_file(BOTTLE_ROUTER_CONFIG_DROPIN_PATH, BOTTLE_ROUTER_CONFIG_DROPIN, mode=0o644)
        # Make the drop-in authoritative for the next start. The apply walk restarts
        # openhost at the end, so we don't restart here.
        run("systemctl", "daemon-reload")
