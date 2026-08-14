# openhost_test_harness

Test scaffolding for Cloud in a Bottle apps, running the **real Cloud in a Bottle router** locally.
The harness starts an HTTP-only router on a `*.localhost` zone (resolves to loopback on
Linux and macOS, no DNS setup), deploys your app through the real install path with rootless
podman, and gives your tests authenticated access. Routing, auth, identity env vars, and the
v2 service interface behave exactly as on a real server.

## Install (in an app repo)

```toml
[dependency-groups]
dev = [
    "openhost[test-harness] @ git+https://github.com/imbue-openhost/openhost@main",
]
```

Requirements:
- python >= 3.12
- rootless podman (on macOS, a running `podman machine`)
- Linux only: app containers reach the router through a dummy interface (pasta shares the
  host's IP with containers, so `host.containers.internal` can't otherwise reach
  loopback-bound host services). One-time setup:

  ```bash
  sudo ip link add openhost0 type dummy
  sudo ip addr add 10.200.0.1/32 dev openhost0
  sudo ip link set openhost0 up
  mkdir -p ~/.config/containers
  printf '[containers]\nhost_containers_internal_ip = "10.200.0.1"\n' >> ~/.config/containers/containers.conf
  ```

  The interface doesn't survive reboots; see `ansible/tasks/containers.yml` in the openhost
  repo for the persistent systemd-networkd version. macOS needs nothing (gvproxy maps
  `host.containers.internal` to the host loopback).

## Use

```python
# tests/conftest.py
import pytest
from openhost_test_harness import OpenhostStack

@pytest.fixture(scope="session")
def stack():
    with OpenhostStack() as s:  # app_dir found by walking up from the cwd to the nearest openhost.toml
        yield s
```

```python
# tests/test_thing.py
def test_index(stack):
    r = stack.owner_session.get(f"{stack.url}/")
    assert r.status_code == 200
```

The app under test deploys from a snapshot of your git working tree (tracked +
untracked files, minus gitignored ones), so uncommitted changes are what runs — the
router would otherwise clone the repo at HEAD and silently test stale code.

- `stack.url` — your app through the router (subdomain routing, real auth)
- `stack.owner_session` — a `requests.Session` authenticated as the zone owner; its cookie
  is scoped to the zone domain so it works on `stack.url` and every other app URL.
  Unauthenticated requests to `stack.url` redirect to `/login`, like production.
- `stack.app_url` — direct to your app's container, bypassing the router (for tests that
  forge `X-OpenHost-*` headers or check unauthenticated behavior)
- `stack.url_for(app_name)` — any other deployed app, through the router
- `stack.router_url` — the router itself (dashboard, owner APIs)

### Testing owner routes in a browser (playwright)

`stack.url` goes through the real router, so an unauthenticated `page.goto(stack.url)`
redirects to `/login`. `stack.playwright_login(page)` drives the real login form (navigates
to `/login`, submits the owner password, waits for the redirect) and returns the page
logged in, so it can then reach owner-only pages. Playwright and pytest-playwright come
with the `test-harness` extra; pass pytest-playwright's `page` fixture:

```python
# tests/test_pages.py
from playwright.sync_api import expect
from openhost_test_harness import OpenhostStack

def test_dashboard_renders(stack: OpenhostStack, page) -> None:
    stack.playwright_login(page)
    page.goto(stack.url)                             # authenticated, no /login redirect
    expect(page.get_by_role("heading", name="My App")).to_be_visible()
```

To test the unauthenticated experience instead, use pytest-playwright's plain `page`
fixture (no cookies) and assert the `/login` redirect, or hit `stack.app_url` to bypass
the router entirely.

## Testing the service interface

### Your app consumes a service

Deploy a real provider next to it and grant permissions:

```python
def test_my_app_reads_secrets(stack):
    stack.deploy_app("https://github.com/imbue-openhost/secrets")
    stack.grant(stack.app_id, "github.com/imbue-openhost/openhost/services/secrets", {"key": "DB_URL"})
    ...  # exercise your app; its service calls go through the real router proxy
```

`OpenhostStack(grant_manifest_permissions=True)` is the default, so grants declared in your
app's `[[services.v2.consumes]]` are approved at install; pass `False` to test the
permission-denied flow.

If your app needs a provider at **startup** (e.g. it reads config from the secrets service
before binding its port), deploy and seed that provider via the `pre_deploy` hook, which runs
after the router is up but before the app under test deploys — like an owner preparing the
server before installing the app:

```python
def _seed(s: OpenhostStack) -> None:
    s.deploy_app("https://github.com/imbue-openhost/secrets")
    s.owner_session.post(f"{s.url_for('secrets')}/api/secrets", json={"key": "DB_URL", "value": "..."})

@pytest.fixture(scope="session")
def stack():
    with OpenhostStack(pre_deploy=_seed) as s:
        yield s
```

### Your app provides a service

Use a synthetic consumer to call your service through the real proxy:

```python
def test_my_service(stack):
    consumer = stack.deploy_service_consumer("github.com/me/my-service", shortname="svc", version=">=0.1.0")
    result = consumer.call("get", payload={"keys": ["FOO"]})   # routed via /api/services/v2/call/svc/get
    assert result.status == 403                                 # no grant yet — provider-side denial
    stack.grant(consumer.app_id, "github.com/me/my-service", {"key": "FOO"})
    assert consumer.call("get", payload={"keys": ["FOO"]}).status == 200
```

The consumer is a generated stdlib-only app; the router injects `X-OpenHost-Permissions` and
`X-OpenHost-Consumer-*` headers into proxied calls exactly as in production.

Or test against a real consumer app — deploy it like any other app, grant it access, and
drive it through the router:

```python
def test_my_service_with_real_consumer(stack):
    consumer_app_id = stack.deploy_app("https://github.com/me/my-consumer-app")
    stack.grant(consumer_app_id, "github.com/me/my-service", {"key": "FOO"})
    r = stack.owner_session.get(f"{stack.url_for('my-consumer-app')}/page-that-uses-my-service")
    assert r.status_code == 200
```

## Self-tests

```
pixi run -e dev pytest openhost_app_test_harness -x --run-containers
```
