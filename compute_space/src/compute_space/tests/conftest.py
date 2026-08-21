import os
import signal
import socket
import sqlite3
import subprocess
import time
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
import requests

from compute_space import COMPUTE_SPACE_PACKAGE_DIR
from compute_space import OPENHOST_PROJECT_DIR
from compute_space.config import Config
from compute_space.config import DefaultConfig
from compute_space.config import set_active_config
from compute_space.core import mdns
from compute_space.core.domains import Domain
from compute_space.core.domains import primary_domain
from compute_space.core.domains import seed_domains
from compute_space.db.connection import init_db
from compute_space.db.schema import schema_path
from compute_space.tests.utils import kill_tree
from compute_space.tests.utils import make_router_env
from compute_space.tests.utils import managed_router
from compute_space.tests.utils import router_cmd
from compute_space.tests.utils import write_first_boot_beside

ROUTER_PORT = 18080
OWNER_PASSWORD = "testpass123"
TEST_ZONE_DOMAIN = "testzone.local"


@pytest.fixture(autouse=True, scope="session")
def _resolve_test_zone_to_localhost() -> Iterator[None]:
    """Resolve ``testzone.local`` (and subdomains) to 127.0.0.1 in the test process.

    This lets tests use real URLs like ``http://test-app.testzone.local:PORT/foo``
    instead of forging Host headers — needed because ``requests`` silently drops
    cookies on requests that override the Host header. Only patches lookups in
    the test process; the router subprocess is unaffected (it binds to 127.0.0.1
    and routes by the incoming ``Host`` header, which the URL sets correctly).
    """
    real_getaddrinfo = socket.getaddrinfo

    def patched(host: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(host, str) and (host == TEST_ZONE_DOMAIN or host.endswith("." + TEST_ZONE_DOMAIN)):
            host = "127.0.0.1"
        return real_getaddrinfo(host, *args, **kwargs)

    socket.getaddrinfo = patched
    try:
        yield
    finally:
        socket.getaddrinfo = real_getaddrinfo


class FakeMdnsSocket:
    """Stands in for the multicast socket, capturing sendto()."""

    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[object, ...]]] = []
        self.closed = False

    def sendto(self, data: bytes, addr: tuple[object, ...]) -> int:
        self.sent.append((data, addr))
        return len(data)

    def close(self) -> None:
        self.closed = True


def fake_mdns_responder(bases: tuple[str, ...], private_ip: str, private_ip6: str | None = None) -> mdns.MdnsResponder:
    """An unstarted responder over a fake socket — no real socket, no thread, no LAN traffic."""
    transport = mdns.Transport(sock=FakeMdnsSocket(), group=(mdns._MDNS_GROUP, mdns._MDNS_PORT))  # type: ignore[arg-type]
    return mdns.MdnsResponder(
        transports=(transport,),
        private_ip=private_ip,
        private_ip_prefix=None,
        private_ip6=private_ip6,
        private_ip6_prefix=64,
        bases=bases,
    )


@pytest.fixture(autouse=True)
def _no_real_mdns(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Never bind UDP :5353 or multicast onto the developer's LAN.

    Adding a `.local` domain reconciles the responder for real, which would otherwise join
    224.0.0.251 and leave a live thread answering LAN queries for the rest of the session.
    """
    monkeypatch.setattr(mdns, "start_mdns", fake_mdns_responder)
    yield
    if (responder := mdns.get_active_mdns()) is not None:
        responder.stop()
        mdns.set_active_mdns(None)


def open_db(config: Config) -> sqlite3.Connection:
    """A file-backed connection to the config's DB, configured like the app's ``get_db()`` — for
    tests that call the DB-backed stores directly (which take a connection, not a config)."""
    conn = sqlite3.connect(config.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def primary_of(config: Config) -> Domain:
    """The instance's primary domain, read live from the config's DB (the source of truth)."""
    with closing(open_db(config)) as db:
        return primary_domain(db)


def _make_test_config(tmp_path: Path, **overrides: Any) -> Config:
    """Build a DefaultConfig with temp dirs under tmp_path and register it active.  Unless
    ``seed_primary=False``, migrate the DB and seed ``zone_domain`` as the primary (the domain is
    DB-owned now, so most tests need a seeded primary)."""
    zone_domain = overrides.pop("zone_domain", "testzone.local")
    tls_enabled = overrides.pop("tls_enabled", False)
    seed_primary = overrides.pop("seed_primary", True)
    overrides.pop("domains", None)  # extra domains are seeded explicitly by tests that need them
    cfg = DefaultConfig(
        host="127.0.0.1",
        data_root_dir=str(tmp_path),
        apps_dir_override=str(OPENHOST_PROJECT_DIR / "apps"),
        port_range_start=overrides.pop("port_range_start", 19000),
        port_range_end=overrides.pop("port_range_end", 19099),
        start_caddy=overrides.pop("start_caddy", False),
        # Off by default in tests so existing test setup flows keep working;
        # the integration test that exercises the gate sets it to True
        # explicitly.
        claim_token_required=overrides.pop("claim_token_required", False),
        **overrides,
    )
    cfg.make_all_dirs()
    set_active_config(cfg)
    if seed_primary:
        init_db(cfg.db_path)
        with closing(open_db(cfg)) as db:
            seed_domains(db, Domain(name=zone_domain, tls=tls_enabled), [])
    return cfg


def _make_config_and_env(tmp_path: Path, **overrides: Any) -> tuple[Config, dict[str, str]]:
    """Create a test config + env dict for launching a router subprocess."""
    config = _make_test_config(tmp_path, **overrides)
    config_path = str(tmp_path / "config.toml")
    config.to_toml(config_path)
    write_first_boot_beside(config_path, primary_of(config))
    return config, make_router_env(config_path)


def _start_router_process(base_url: str, env: dict[str, str], startup_timeout: int = 30) -> subprocess.Popen[bytes]:
    """Start a router subprocess, wait for /health, return the Popen object."""
    proc = subprocess.Popen(
        router_cmd(),
        cwd=COMPUTE_SPACE_PACKAGE_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    deadline = time.time() + startup_timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{base_url}/health", timeout=2)
            if r.status_code == 200:
                return proc
        except (requests.ConnectionError, requests.ReadTimeout):
            pass
        time.sleep(0.3)
    out, err = proc.communicate(timeout=5)
    os.killpg(proc.pid, signal.SIGKILL)
    raise RuntimeError(f"Router failed to start.\nstdout: {out.decode()}\nstderr: {err.decode()}")


def _stop_router_process(proc: subprocess.Popen[Any]) -> None:
    """Stop a router subprocess."""
    kill_tree(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        proc.wait()


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite database loaded with the real production schema.

    FK enforcement is left off (sqlite default) so individual tests don't
    have to insert an apps row for every consumer/provider name they
    reference — these tests are about service / permission semantics, not
    referential integrity.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(schema_path()) as f:
        conn.executescript(f.read())
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(scope="module")
def config(tmp_path_factory: pytest.TempPathFactory) -> Config:
    """Create a test config with temp dirs, write to TOML, return the Config object."""
    tmpdir = tmp_path_factory.mktemp("router")
    return _make_test_config(tmpdir, port=ROUTER_PORT)


@pytest.fixture(scope="module")
def router_process(config: Config) -> subprocess.Popen[bytes]:
    """Start the router as a subprocess, wait for /health, tear down after."""
    with managed_router(config) as proc:
        yield proc


@pytest.fixture(scope="module")
def admin_session(router_process: subprocess.Popen[bytes], config: Config) -> requests.Session:
    """A requests.Session authenticated as the router owner.

    Uses the zone_domain in URLs (resolved to 127.0.0.1 via the autouse DNS
    fixture) so /setup's auth cookies get ``Domain=testzone.local`` and are
    automatically sent to app subdomains too.

    Once the setup POST returns, the setup-only Litestar app starts shutting
    down so start.py can boot the full app — poll a full-app-only path
    afterwards so callers see a ready router.
    """
    base_url = f"http://{primary_of(config).name}:{config.port}"
    s = requests.Session()
    r = s.post(
        f"{base_url}/setup",
        data={
            "password": OWNER_PASSWORD,
            "confirm_password": OWNER_PASSWORD,
        },
    )
    assert r.status_code == 200, f"Router setup failed: {r.status_code}"

    # /login lives only on the full app; use it as a "full app is up" probe.
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            probe = requests.get(f"{base_url}/login", timeout=1, allow_redirects=False)
            if probe.status_code in (200, 302):
                return s
        except requests.ConnectionError:
            pass
        time.sleep(0.2)
    raise RuntimeError("Full app did not come up after /setup within 30s")
