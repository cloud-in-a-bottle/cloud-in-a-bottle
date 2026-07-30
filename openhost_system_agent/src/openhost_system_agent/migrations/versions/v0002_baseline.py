"""Baseline migration: ensure every host matches the current ansible setup.

Safe to run on systems already provisioned by ansible — every step is
idempotent. Skips dev tools, dotfiles, and ACME keys.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from openhost_system_agent.migrations.base import SystemMigration
from openhost_system_agent.migrations.helpers import ensure_line
from openhost_system_agent.migrations.helpers import get_host_uid
from openhost_system_agent.migrations.helpers import run
from openhost_system_agent.migrations.helpers import set_sshd_option
from openhost_system_agent.migrations.helpers import write_file

OPENHOST_SERVICE_PATH = "/etc/systemd/system/openhost.service"

# Fixed path for the reclaim script the service runs before ExecStart.
RECLAIM_SCRIPT_PATH = "/usr/local/bin/openhost-reclaim-pixi"

# The reclaim script: hand the host's OpenHost trees back to the host user. Run
# as root (the unit's ExecStartPre uses the `+` prefix) before the host-user
# `git`/`pixi run`, so files a root-run update left behind can't brick the
# service. A standalone script — not an inline ExecStartPre snippet — so no
# $VAR reaches systemd (which would substitute it from the unit environment
# before /bin/sh runs). Depends on nothing from the (possibly broken) pixi env.
# Kept byte-identical with ansible/files/openhost-reclaim-pixi (a test enforces
# this).
RECLAIM_SCRIPT = """#!/bin/sh
# Reclaim ownership of the host's OpenHost trees for the host user, so files a
# root-run update left behind can't brick the host-user service. Run as root
# before the host `git`/`pixi run`. Managed by OpenHost; keep byte-identical
# with RECLAIM_SCRIPT in the openhost_system_agent baseline (v0002_baseline.py).
# Best-effort: one overall timeout, failure swallowed, so it can't block startup.
# shellcheck disable=SC2016  # $dir is expanded by the inner `sh -c`, not here.
timeout 80 sh -c '
for dir in /home/host/openhost /home/host/.pixi; do
    if [ -e "$dir" ]; then
        chown -Rh host:host "$dir"
    fi
done
' || :
"""

# The unit line that invokes the reclaim script before ExecStart. Prefixes
# (order-insensitive): `+` runs it as root (needed to chown root-owned files
# even though the unit is User=host); `-` makes it best-effort so a chown
# failure can never block startup — the failsafe must not brick the service it
# protects. Kept in sync with ansible/templates/openhost.service.j2.
RECLAIM_EXEC_START_PRE = f"ExecStartPre=-+{RECLAIM_SCRIPT_PATH}\n"


def build_openhost_service_unit(host_uid: int) -> str:
    """Render the openhost.service unit. Shared so migrations that rewrite it
    stay consistent with the baseline (and with ansible's template)."""
    return (
        "[Unit]\n"
        "Description=OpenHost Compute Space\n"
        f"After=network-online.target user@{host_uid}.service\n"
        f"Wants=network-online.target user@{host_uid}.service\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "User=host\n"
        "WorkingDirectory=/home/host/openhost\n"
        "Environment=PATH=/home/host/.pixi/bin:/home/host/openhost/.pixi/envs/default/bin:"
        "/usr/local/bin:/usr/bin:/bin\n"
        "Environment=OPENHOST_ROUTER_CONFIG=/home/host/.openhost/local_compute_space/config.toml\n"
        f"Environment=XDG_RUNTIME_DIR=/run/user/{host_uid}\n"
        f"Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{host_uid}/bus\n"
        + RECLAIM_EXEC_START_PRE
        + "ExecStart=/home/host/.pixi/bin/pixi run python -m compute_space\n"
        "Restart=no\n"
        "RestartForceExitStatus=42\n"
        "SuccessExitStatus=42\n"
        "RestartSec=3\n"
        "TimeoutStopSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


class Migration0002Baseline(SystemMigration):
    version = 2

    def up(self) -> None:
        self._apt_packages()
        self._fuse_config()
        self._container_support()
        self._sysctls()
        self._container_registries()
        self._dummy_interface()
        self._user_container_config()
        self._harden_ssh()
        self._systemd_service()

    def _apt_packages(self) -> None:
        run("apt-get", "update", "-qq")
        run(
            "apt-get",
            "install",
            "-y",
            "-qq",
            "git",
            "curl",
            "wget",
            "openssh-server",
            "ca-certificates",
            "gnupg",
            "fuse3",
            "uidmap",
            "passt",
            "golang-github-containers-common",
        )
        subprocess.run(["apt-get", "remove", "-y", "-qq", "podman", "crun"], check=False)

    def _fuse_config(self) -> None:
        ensure_line("/etc/fuse.conf", "user_allow_other")

    def _container_support(self) -> None:
        if not Path("/var/lib/systemd/linger/host").exists():
            run("loginctl", "enable-linger", "host")

    def _sysctls(self) -> None:
        write_file(
            "/etc/sysctl.d/90-openhost-podman.conf",
            "# Managed by OpenHost; do not edit by hand.\n"
            "net.ipv4.ip_unprivileged_port_start = 25\n"
            "kernel.apparmor_restrict_unprivileged_userns = 0\n",
            mode=0o644,
        )
        run("sysctl", "-p", "/etc/sysctl.d/90-openhost-podman.conf")

    def _container_registries(self) -> None:
        Path("/etc/containers").mkdir(parents=True, exist_ok=True)
        write_file(
            "/etc/containers/registries.conf",
            "# Managed by OpenHost; do not edit by hand.\n"
            'unqualified-search-registries = ["docker.io"]\n'
            'short-name-mode = "permissive"\n',
            mode=0o644,
        )

    def _dummy_interface(self) -> None:
        result = subprocess.run(["ip", "link", "show", "openhost0"], capture_output=True)
        if result.returncode != 0:
            run("ip", "link", "add", "openhost0", "type", "dummy")
            run("ip", "addr", "add", "10.200.0.1/32", "dev", "openhost0")
            run("ip", "link", "set", "openhost0", "up")

        write_file(
            "/etc/systemd/network/10-openhost0.netdev",
            "[NetDev]\nName=openhost0\nKind=dummy\n",
            mode=0o644,
        )
        write_file(
            "/etc/systemd/network/10-openhost0.network",
            "[Match]\nName=openhost0\n\n[Network]\nAddress=10.200.0.1/32\n",
            mode=0o644,
        )

    def _user_container_config(self) -> None:
        conf_dir = Path("/home/host/.config/containers")
        conf_dir.mkdir(parents=True, exist_ok=True)
        write_file(
            str(conf_dir / "containers.conf"),
            '# Managed by OpenHost; do not edit by hand.\n[containers]\nhost_containers_internal_ip = "10.200.0.1"\n',
            mode=0o644,
        )
        for p in [conf_dir, conf_dir / "containers.conf"]:
            run("chown", "host:host", str(p))

    def _harden_ssh(self) -> None:
        set_sshd_option("PasswordAuthentication", "no")
        set_sshd_option("KbdInteractiveAuthentication", "no")
        set_sshd_option("PermitRootLogin", "prohibit-password")
        run("systemctl", "reload", "ssh")

    def _systemd_service(self) -> None:
        write_file(RECLAIM_SCRIPT_PATH, RECLAIM_SCRIPT, mode=0o755)
        unit = build_openhost_service_unit(get_host_uid())
        write_file(OPENHOST_SERVICE_PATH, unit, mode=0o644)
        run("systemctl", "daemon-reload")
        run("systemctl", "enable", "openhost")
