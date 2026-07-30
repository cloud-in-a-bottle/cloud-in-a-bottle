"""Generate router_config.toml for local ``openhost up``.

On server deployments, config is managed by ansible.
"""

import os
import sys
import tomllib
from pathlib import Path

from compute_space.config import DefaultConfig

_DEFAULT_DATA_DIR = os.path.expanduser("~/.openhost/local_compute_space")
_CONFIG_PATH = str(Path(_DEFAULT_DATA_DIR) / "config.toml")

# Bare `openhost up` (no --domain/--zone-domain) is local dev on http://localhost:8080; seed
# `localhost` so the router answers on it (and `*.localhost` for apps) instead of no domain at all.
_DEFAULT_LOCAL_DOMAIN = "localhost"


def _has_seed_domain(first_boot_path: str) -> bool:
    """True if ``first_boot.toml`` already carries a usable domain.  A blank one (left by an earlier
    run before a domain was resolved) counts as absent, so it can be re-seeded rather than wedging boot."""
    try:
        with open(first_boot_path, "rb") as f:
            return bool(str(tomllib.load(f).get("domain", "")).strip())
    except (OSError, tomllib.TOMLDecodeError):
        return False


def generate_config(
    domain: str,
    port: int = 8080,
    data_dir: str = _DEFAULT_DATA_DIR,
    email: str = "",
) -> str:

    # The domain seeds into the DB from first_boot.toml (written below) on first boot; config.toml
    # holds only operational config.
    domain = domain or _DEFAULT_LOCAL_DOMAIN
    content = DefaultConfig(
        host="0.0.0.0",
        port=port,
        data_root_dir=data_dir,
        acme_email=email or None,
        start_caddy=False,
    ).to_toml_str()

    first_boot_path = str(Path(_CONFIG_PATH).parent / "first_boot.toml")
    try:
        os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
        with open(_CONFIG_PATH, "w") as f:
            f.write(content)
        # Write the seed only if one isn't already present — first_boot.toml is consumed once and
        # then ignored, so re-running `openhost up` must not clobber a domain the operator may have
        # since changed (a blank seed from an aborted run is re-written, not preserved).
        if not _has_seed_domain(first_boot_path):
            with open(first_boot_path, "w") as f:
                f.write(f'domain = "{domain}"\ntls = false\n')
    except OSError as e:
        print(f"Error: could not write config {_CONFIG_PATH}: {e}", file=sys.stderr)
        raise SystemExit(1) from None
    return _CONFIG_PATH
