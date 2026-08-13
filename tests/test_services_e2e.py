"""E2E test of the v2 service interface on a full local stack.

Starts the router directly (HTTP-only, ``*.localhost`` zone), deploys the secrets app
from GitHub and the local test_app via rootless podman, then exercises a cross-app
service call: test-app fetches a secret from the secrets app through the router's
service proxy, including the permission-denied and grant flows.

Prerequisites:
    - Rootless podman working (``podman info`` succeeds; on macOS ``podman machine`` running)
    - Network access to github.com (the secrets app is cloned as part of the test)
    - On Linux, the openhost0 dummy interface + host_containers_internal_ip setup from
      ansible/tasks/containers.yml (see the CI test-containers job), so containers can
      reach the router; macOS needs nothing (gvproxy maps host.containers.internal)

Run:
    pixi run -e dev pytest tests/test_services_e2e.py -v -s -x --run-containers --timeout=900
"""

import os

import pytest

from compute_space import OPENHOST_PROJECT_DIR
from compute_space.tests.local_stack import LocalStack
from compute_space.tests.local_stack import clone_and_get_app_info
from compute_space.tests.local_stack import complete_setup
from compute_space.tests.local_stack import create_bare_git_repo
from compute_space.tests.local_stack import deploy_app
from compute_space.tests.local_stack import make_local_stack_config
from compute_space.tests.utils import managed_router

ROUTER_PORT = 28180

SECRETS_REPO_URL = "https://github.com/imbue-openhost/secrets"
SECRETS_SERVICE_URL = "github.com/imbue-openhost/openhost/services/secrets"
TEST_APP_REPO_URL = f"file://{OPENHOST_PROJECT_DIR / 'apps' / 'test_app'}"

SECRET_KEY = "TEST_SECRET"
SECRET_VALUE = "s3cret-value-123"

requires_containers = pytest.mark.requires_containers


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    config = make_local_stack_config(
        data_root_dir=str(tmp_path_factory.mktemp("services_e2e")),
        port=ROUTER_PORT,
        zone_name="svczone",
        port_range_start=29100,
        port_range_end=29199,
        default_apps=[],
    )
    local_stack = LocalStack(config=config)
    with managed_router(config):
        yield local_stack
    local_stack.remove_deployed_app_containers()


@pytest.fixture(scope="module")
def owner(stack):
    return complete_setup(stack)


@pytest.fixture(scope="module")
def secrets_app(stack, owner):
    """Deploy the secrets app (service provider) from GitHub."""
    return deploy_app(owner, stack, SECRETS_REPO_URL, timeout=600)


@pytest.fixture(scope="module")
def test_app(stack, owner):
    """Deploy the local test_app (service consumer)."""
    return deploy_app(owner, stack, TEST_APP_REPO_URL)


def call_service_via_test_app(owner, stack, payload):
    """Ask test-app to make a v2 service call and return the proxied result."""
    r = owner.post(f"{stack.app_url('test-app')}/call-service", json=payload, timeout=30)
    assert r.status_code == 200, f"test-app /call-service failed: {r.status_code}: {r.text[:300]}"
    return r.json()


def fetch_secret_via_test_app(owner, stack, keys):
    return call_service_via_test_app(owner, stack, {"shortname": "secrets", "path": "get", "payload": {"keys": keys}})


@requires_containers
class TestSecretsServiceE2E:
    def test_secrets_app_serves_owner_ui(self, stack, owner, secrets_app):
        r = owner.get(f"{stack.app_url('secrets')}/api/secrets", timeout=10)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_owner_sets_secret(self, stack, owner, secrets_app):
        r = owner.post(
            f"{stack.app_url('secrets')}/api/secrets",
            json={"key": SECRET_KEY, "value": SECRET_VALUE, "description": "e2e test secret"},
            timeout=10,
        )
        assert r.status_code in (200, 201), f"set secret failed: {r.status_code}: {r.text[:300]}"
        r = owner.get(f"{stack.app_url('secrets')}/api/secrets", timeout=10)
        assert SECRET_KEY in [s["key"] for s in r.json()]

    def test_fetch_denied_without_grant(self, stack, owner, secrets_app, test_app):
        result = fetch_secret_via_test_app(owner, stack, [SECRET_KEY])
        assert result["service_status"] == 403
        body = result["service_body"]
        assert body["error"] == "permission_required"
        assert body["required_grant"]["grant"] == {"key": SECRET_KEY}
        # the router decorates global-scope denials with an owner-facing approval URL
        assert "/approve-permissions-v2" in body["required_grant"]["grant_url"]

    def test_fetch_succeeds_after_grant(self, stack, owner, secrets_app, test_app):
        r = owner.post(
            f"{stack.router_url}/api/permissions/v2/grant_global_scoped",
            json={"app_id": test_app, "service_url": SECRETS_SERVICE_URL, "grant": {"key": SECRET_KEY}},
            timeout=10,
        )
        assert r.status_code == 200, f"grant failed: {r.status_code}: {r.text[:300]}"

        result = fetch_secret_via_test_app(owner, stack, [SECRET_KEY])
        # the secrets app returns 201 (litestar's POST default) though its openapi spec says 200
        assert result["service_status"] in (200, 201), f"service call failed: {result}"
        assert result["service_body"]["secrets"] == {SECRET_KEY: SECRET_VALUE}

    def test_granted_but_missing_key_reported(self, stack, owner, secrets_app, test_app):
        missing_key = "NO_SUCH_SECRET"
        r = owner.post(
            f"{stack.router_url}/api/permissions/v2/grant_global_scoped",
            json={"app_id": test_app, "service_url": SECRETS_SERVICE_URL, "grant": {"key": missing_key}},
            timeout=10,
        )
        assert r.status_code == 200

        result = fetch_secret_via_test_app(owner, stack, [SECRET_KEY, missing_key])
        assert result["service_status"] in (200, 201)
        assert result["service_body"]["secrets"] == {SECRET_KEY: SECRET_VALUE}
        assert result["service_body"]["missing"] == [missing_key]

    def test_undeclared_shortname_rejected(self, stack, owner, test_app):
        result = call_service_via_test_app(owner, stack, {"shortname": "not-declared", "path": "get", "payload": {}})
        assert result["service_status"] == 404
        assert result["service_body"]["error"] == "shortname_not_declared"

    def test_add_app_reuses_existing_clone_dir(self, stack, owner):
        """add_app reuses a pre-existing clone_dir from clone_and_get_app_info instead of
        re-cloning, and the result is a real git clone (has .git), not a copy.

        TEST_APP_REPO_URL has no .git dir itself, so clone_and_read_manifest would take
        the shutil.copytree fallback path instead of a real clone -- build a bare repo so
        this actually exercises the git-clone path, not just the plain-directory-copy path.
        """
        bare_path = os.path.join(stack.config.temporary_data_dir, "test-app-clonedir.git")
        create_bare_git_repo(str(OPENHOST_PROJECT_DIR / "apps" / "test_app"), bare_path)
        repo_url = f"file://{bare_path}"

        clone_dir, app_name = clone_and_get_app_info(owner, stack, repo_url)
        assert app_name == "test-app"

        deployed_app_name = "test-app-clonedir"
        deploy_app(owner, stack, repo_url, app_name=deployed_app_name, clone_dir=clone_dir)

        repo_dir = os.path.join(stack.config.temporary_data_dir, "app_temp_data", deployed_app_name, "repo")
        assert os.path.isdir(os.path.join(repo_dir, ".git")), (
            ".git directory not found — app may have been copied instead of cloned"
        )
