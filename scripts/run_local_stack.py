"""Run a full OpenHost stack locally for browser testing.

HTTP-only, bound to loopback, with a ``*.localhost`` zone domain — browsers and the OS
resolver send any ``*.localhost`` name to loopback, so no DNS or /etc/hosts setup is
needed.  Apps run in rootless podman containers exactly as on a real server.

Usage:
    pixi run -e dev python scripts/run_local_stack.py [--fresh] [--port 8080] [--default-apps]

Then open http://home.localhost:8080/ in a browser.  On first run, /setup asks you to
pick an owner password.  Deployed apps are served at http://<app>.home.localhost:8080/.

Data persists in --data-dir across restarts; use --fresh to start over.  App containers
are not children of the router and keep running after it exits (the router re-adopts them
on restart); use ``podman ps`` / ``podman rm -f openhost-<app>`` to stop them manually.
"""

import argparse
import shutil
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from compute_space import COMPUTE_SPACE_PACKAGE_DIR
from compute_space import OPENHOST_PROJECT_DIR
from compute_space.core.domains import primary_domain
from compute_space.db import get_db
from compute_space.tests.local_stack import make_local_stack_config
from compute_space.tests.utils import make_router_env
from compute_space.tests.utils import write_first_boot_beside


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--zone-name", default="home", help="zone is <zone-name>.localhost:<port>")
    parser.add_argument("--data-dir", default="~/.openhost-local-stack")
    parser.add_argument("--fresh", action="store_true", help="wipe the data dir before starting")
    parser.add_argument(
        "--default-apps",
        action="store_true",
        help="deploy the standard default apps (secrets, filestash, catalog, oauth, backup) at setup",
    )
    args = parser.parse_args()

    # resolve() so symlinked paths like /tmp -> /private/tmp become the real path:
    # podman machine on macOS only shares resolved paths (/Users, /private, /var/folders)
    # with the VM, and bind-mount sources must be visible there.
    data_dir = Path(args.data_dir).expanduser().resolve()
    if args.fresh and data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    config = make_local_stack_config(
        data_root_dir=str(data_dir),
        port=args.port,
        zone_name=args.zone_name,
        default_apps=None if args.default_apps else [],
        # vendored builtins (e.g. file_browser in default_apps) live in the repo's apps/
        apps_dir_override=str(OPENHOST_PROJECT_DIR / "apps"),
    )
    config_path = str(data_dir / "config.toml")
    config.to_toml(config_path)
    with closing(get_db()) as db:
        primary = primary_domain(db)
    write_first_boot_beside(config_path, primary)

    zone_domain = primary.name
    print(f"data dir:  {data_dir}")
    print(f"zone:      {zone_domain}")
    print()
    print(f"  first run:  http://{zone_domain}/setup   (pick an owner password)")
    print(f"  dashboard:  http://{zone_domain}/dashboard")
    print(f"  apps:       http://<app-name>.{zone_domain}/")
    print(flush=True)

    proc = subprocess.run(
        [sys.executable, "-m", "compute_space"],
        cwd=str(COMPUTE_SPACE_PACKAGE_DIR),
        env=make_router_env(config_path),
    )
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
