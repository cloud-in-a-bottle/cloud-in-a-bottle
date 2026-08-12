import pytest

from openhost_system_agent.tests.test_migration_container import _DOCKERFILE
from openhost_system_agent.tests.test_migration_container import _IMAGE_NAME
from openhost_system_agent.tests.test_migration_container import _REPO_ROOT
from openhost_system_agent.tests.test_migration_container import _podman


@pytest.fixture(scope="session", autouse=True)
def _migration_image() -> object:
    """Build the test image once for the whole test session; reuse it across containers.

    Must live in conftest.py, not a test module. Importing from a sibling test file rebuilds
    the image. The only persisting cache is conftest.py.
    """
    if _podman("image", "exists", _IMAGE_NAME, check=False).returncode != 0:
        _podman("build", "-t", _IMAGE_NAME, "-f", str(_DOCKERFILE), str(_REPO_ROOT), timeout=600)
    yield
    _podman("rmi", "-f", _IMAGE_NAME, check=False, timeout=30)
