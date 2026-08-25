from __future__ import annotations

# Env-var naming contract.  The project was renamed OpenHost -> Cloud in a
# Bottle; every OPENHOST_* variable is now also exposed under BOTTLE_*.  The
# legacy names are kept indefinitely for backward compatibility.
#
# This lives in its own dependency-free module so that both ``data`` and
# ``containers`` can use it without an import cycle: ``data`` imports gateway
# constants from ``containers``, so ``containers`` must not import from ``data``.
LEGACY_ENV_PREFIX = "OPENHOST_"
ENV_PREFIX = "BOTTLE_"


def add_bottle_env_aliases(env: dict[str, str]) -> dict[str, str]:
    """Return ``env`` with a ``BOTTLE_``-prefixed twin for every ``OPENHOST_`` var.

    The rename from OpenHost to Cloud in a Bottle keeps the legacy
    ``OPENHOST_*`` names for compatibility while exposing the same values
    under ``BOTTLE_*``.  An already-present ``BOTTLE_*`` entry is never
    clobbered, so an explicit new-style value wins over the auto-alias.
    """
    aliased = dict(env)
    for key, value in env.items():
        if key.startswith(LEGACY_ENV_PREFIX):
            twin = ENV_PREFIX + key[len(LEGACY_ENV_PREFIX) :]
            aliased.setdefault(twin, value)
    return aliased
