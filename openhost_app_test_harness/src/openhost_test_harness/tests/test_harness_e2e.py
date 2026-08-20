"""Self-test: the harness against its fixture provider app, on a real local stack.

Run:
    pixi run -e dev pytest openhost_app_test_harness -x --run-containers --timeout=900
"""

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
import requests
from playwright.sync_api import Page
from playwright.sync_api import expect

from openhost_test_harness import OpenhostStack
from openhost_test_harness import ServiceConsumer

PROVIDER_APP_DIR = Path(__file__).parent / "provider_app"
ECHO_SERVICE = "github.com/example/echo"

requires_containers = pytest.mark.requires_containers


@pytest.fixture(scope="module")
def stack() -> Iterator[OpenhostStack]:
    with OpenhostStack(app_dir=PROVIDER_APP_DIR) as s:
        yield s


@pytest.fixture(scope="module")
def consumer(stack: OpenhostStack) -> ServiceConsumer:
    return stack.deploy_service_consumer(ECHO_SERVICE, shortname="echo", version=">=0.1.0")


@requires_containers
class TestHarness:
    def test_app_through_router(self, stack: OpenhostStack) -> None:
        r = stack.owner_session.get(f"{stack.url}/", timeout=10)
        assert r.status_code == 200
        assert r.json() == {"app": "echo-provider"}

    def test_router_requires_auth(self, stack: OpenhostStack) -> None:
        r = requests.get(f"{stack.url}/", allow_redirects=False, timeout=10)
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]

    def test_playwright_login(self, stack: OpenhostStack, page: Page) -> None:
        """playwright_login drives the real /login form so browser tests reach owner routes."""
        page.goto(stack.url)
        expect(page).to_have_url(re.compile(r"/login"))  # gated before login

        stack.playwright_login(page)
        page.goto(stack.url)
        expect(page).not_to_have_url(re.compile(r"/login"))
        assert "echo-provider" in page.content()

    def test_app_url_bypasses_router(self, stack: OpenhostStack) -> None:
        """Direct container access, e.g. for provider tests that forge identity headers."""
        r = requests.post(
            f"{stack.app_url}/svc/echo",
            json={"hello": "world"},
            headers={"X-OpenHost-Permissions": '[{"grant": "forged", "scope": "global"}]'},
            timeout=10,
        )
        assert r.status_code == 200
        echoed = r.json()
        assert echoed["body"] == {"hello": "world"}
        assert echoed["permissions"] == [{"grant": "forged", "scope": "global"}]

    def test_service_call_without_grants(self, stack: OpenhostStack, consumer: ServiceConsumer) -> None:
        result = consumer.call("echo", payload={"n": 1})
        assert result.status == 200
        assert result.body["method"] == "POST"
        assert result.body["path"] == "/svc/echo"
        assert result.body["body"] == {"n": 1}
        assert result.body["permissions"] == []
        assert result.body["consumer"] == consumer.name

    def test_service_call_sees_grant(self, stack: OpenhostStack, consumer: ServiceConsumer) -> None:
        stack.grant(consumer.app_id, ECHO_SERVICE, {"key": "FOO"})
        result = consumer.call("echo")
        assert result.status == 200
        assert result.body["permissions"] == [{"grant": {"key": "FOO"}, "scope": "global"}]

    def test_app_logs(self, stack: OpenhostStack) -> None:
        logs = stack.app_logs()
        assert "Echo provider listening" in logs or "Build complete" in logs
