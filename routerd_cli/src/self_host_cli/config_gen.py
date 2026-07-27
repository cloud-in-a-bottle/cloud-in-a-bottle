"""Generate router_config.toml for local ``openhost up``.

On server deployments, config is managed by ansible.
"""

import os
import sys
from pathlib import Path

from compute_space.config import DefaultConfig

_DEFAULT_DATA_DIR = os.path.expanduser("~/.openhost/local_compute_space")
_CONFIG_PATH = str(Path(_DEFAULT_DATA_DIR) / "config.toml")


def generate_config(
    domain: str,
    port: int = 8080,
    data_dir: str = _DEFAULT_DATA_DIR,
    email: str = "",
) -> str:

    # The domain is no longer written into config.toml — it seeds into the DB from first_boot.toml
    # on first boot (after that the DB is authoritative).  config.toml holds only operational config.
    content = DefaultConfig(
        host="0.0.0.0",
        port=port,
        data_root_dir=data_dir,
        acme_email=email or None,
        tls_enabled=False,
        start_caddy=False,
    ).to_toml_str()

    first_boot_path = str(Path(_CONFIG_PATH).parent / "first_boot.toml")
    try:
        os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
        with open(_CONFIG_PATH, "w") as f:
            f.write(content)
        # Write the seed only if absent — first_boot.toml is consumed once and then ignored, so
        # re-running `openhost up` must not clobber a domain the operator may have since changed.
        if not os.path.exists(first_boot_path):
            with open(first_boot_path, "w") as f:
                f.write(f'domain = "{domain}"\ntls = false\n')
    except OSError as e:
        print(f"Error: could not write config {_CONFIG_PATH}: {e}", file=sys.stderr)
        raise SystemExit(1) from None
    return _CONFIG_PATH
