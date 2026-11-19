"""Launch the real router with test-only patches applied first.

Tests start routers as subprocesses (see ``utils.router_cmd``), where an in-process
monkeypatch can't reach, so patches that a router needs under test are applied here
before handing off to the production entrypoint.  Keep test concerns in this file
rather than adding test hooks to production code.
"""

import os

from compute_space.core import containers
from compute_space.web.start import main

# Set by the root conftest under pytest-xdist: podman names are host-global while each
# test router has its own DB, so routers on different workers must not share
# `openhost-{app}` names.
TEST_CONTAINER_PREFIX_ENV = "OPENHOST_TEST_CONTAINER_PREFIX"


def apply_test_patches() -> None:
    """Patches shared by router subprocesses and the pytest process itself."""
    if prefix := os.environ.get(TEST_CONTAINER_PREFIX_ENV):
        containers.APP_NAME_PREFIX = prefix


if __name__ == "__main__":
    apply_test_patches()
    main()
