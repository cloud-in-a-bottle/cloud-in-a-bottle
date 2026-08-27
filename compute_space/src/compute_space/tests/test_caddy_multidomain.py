"""Phase 3: the generated Caddyfile serves each configured domain on its own terms
— https (with the acquired cert or Caddy's internal CA) for TLS domains, plain http
with no redirect for mDNS `.local` domains — so http `.local` and https external run
at once.  Where the `caddy` binary is available we adapt the output to prove it's
syntactically valid, not just string-matched."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from compute_space.core import caddy
from compute_space.core.caddy import CaddyProcess
from compute_space.core.caddy import generate_caddyfile
from compute_space.core.caddy import unix_admin_address
from compute_space.core.domains import Domain

PUBLIC = Domain("host.example.com", tls=True)
PUBLIC2 = Domain("host.example.org", tls=True)
LOCAL = Domain("myhost.local", tls=False, mdns=True)
CERT = Path("/data/cert.pem")
KEY = Path("/data/key.pem")


def _cert_for(cert_domain: str | None):  # type: ignore[no-untyped-def]
    """Resolver that hands out the file cert for `cert_domain` only (mimics the primary having an
    acquired cert while other domains don't yet)."""

    def resolve(name: str):  # type: ignore[no-untyped-def]
        return (CERT, KEY) if name == cert_domain else None

    return resolve


def _gen(domains: tuple[Domain, ...], cert_domain: str | None = "host.example.com") -> str:
    return generate_caddyfile(domains, 8080, _cert_for(cert_domain))


def test_primary_tls_domain_uses_file_cert() -> None:
    cf = _gen((PUBLIC,))
    assert "https://host.example.com, https://*.host.example.com {" in cf
    assert f"tls {CERT} {KEY}" in cf
    assert "reverse_proxy localhost:8080" in cf


def test_reverse_proxy_retries_upstream() -> None:
    # The upstream is retried for a few seconds so a request landing before the
    # router's loopback listener is up (post-restart / update handoff) doesn't 502.
    cf = _gen((PUBLIC,))
    assert "lb_try_duration" in cf


def test_http3_disabled() -> None:
    # The update-downtime server covers only TCP 80/443, not HTTP/3's UDP :443, so
    # Caddy must serve h1/h2 only (no h3 alt-svc) or browsers hit ERR_QUIC_PROTOCOL_ERROR.
    cf = _gen((PUBLIC,))
    assert "protocols h1 h2" in cf


def test_tls_domain_redirect_is_scoped_not_global() -> None:
    cf = _gen((PUBLIC,))
    # per-domain http site, not a bare `:80 {` catch-all
    assert ":80 {" not in cf
    assert "http://host.example.com, http://*.host.example.com {" in cf
    assert "redir https://{host}{uri} permanent" in cf


def test_local_domain_served_plain_http_without_redirect() -> None:
    cf = _gen((PUBLIC, LOCAL))
    assert "http://myhost.local, http://*.myhost.local {" in cf
    # the .local http block reverse-proxies and does NOT redirect to https
    local_block = cf.split("http://myhost.local")[1].split("}")[0]
    assert "reverse_proxy localhost:8080" in local_block
    assert "redir" not in local_block


def test_second_public_domain_uses_internal_ca() -> None:
    cf = _gen((PUBLIC, PUBLIC2))
    # only the primary (cert_domain) gets the file cert; the extra domain self-signs
    assert f"tls {CERT} {KEY}" in cf
    assert "tls internal" in cf
    assert "https://host.example.org, https://*.host.example.org {" in cf


def test_auto_https_disable_redirects_when_any_tls() -> None:
    # `disable_redirects` (not `off`) so `tls internal` can still issue
    assert "auto_https disable_redirects" in _gen((PUBLIC, LOCAL))


def test_auto_https_off_when_no_tls_domain() -> None:
    assert "auto_https off" in _gen((LOCAL,), cert_domain=None)


# --- validate with the real caddy binary where present ----------------------------

_caddy = shutil.which("caddy")


@pytest.mark.skipif(_caddy is None, reason="caddy binary not on PATH")
@pytest.mark.parametrize(
    "domains,cert_domain",
    [
        ((PUBLIC,), "host.example.com"),
        ((PUBLIC, LOCAL), "host.example.com"),
        ((PUBLIC, PUBLIC2), "host.example.com"),
        ((LOCAL,), None),
    ],
)
def test_generated_caddyfile_is_valid(tmp_path: Path, domains: tuple[Domain, ...], cert_domain: str | None) -> None:
    cf = generate_caddyfile(domains, 8080, _cert_for(cert_domain))
    path = tmp_path / "Caddyfile"
    path.write_text(cf)
    result = subprocess.run(
        [_caddy, "adapt", "--config", str(path), "--adapter", "caddyfile"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"caddy rejected the config:\n{result.stderr}\n---\n{cf}"


class _FakeProc:
    """Reports already-exited so restart() skips terminate and goes straight to (the stubbed) spawn."""

    pid = 1
    returncode = 0

    async def wait(self) -> int:
        return 0


@pytest.mark.asyncio
async def test_restart_serializes_concurrent_callers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The domain API, cert acquisition and TLS renewal can all restart Caddy at once; the spawn
    # critical section must run one-at-a-time, else a second `caddy run` races the first onto :443.
    active = 0
    max_active = 0

    async def fake_spawn(_path: Path) -> _FakeProc:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)  # widen the window so an unserialized restart would overlap here
        active -= 1
        return _FakeProc()

    monkeypatch.setattr(caddy, "_spawn_caddy", fake_spawn)
    cp = CaddyProcess(proc=_FakeProc(), caddyfile_path=tmp_path / "Caddyfile")  # type: ignore[arg-type]

    await asyncio.gather(*(cp.restart() for _ in range(5)))

    assert max_active == 1  # never two spawns in flight at once


# --- admin API + graceful reload --------------------------------------------------


def test_admin_off_by_default() -> None:
    assert "admin off" in _gen((PUBLIC,))


def test_admin_directive_emitted_when_addr_given() -> None:
    cf = generate_caddyfile((PUBLIC,), 8080, _cert_for("host.example.com"), admin_addr="unix//run/caddy-admin.sock")
    assert "admin unix//run/caddy-admin.sock" in cf
    assert "admin off" not in cf


def test_unix_admin_address_format() -> None:
    assert unix_admin_address(Path("/opt/openhost/openhost_data/caddy-admin.sock")) == (
        "unix//opt/openhost/openhost_data/caddy-admin.sock"
    )


class _AliveProc:
    """Reports still-running (returncode is None) so reload() takes the graceful-reload path."""

    pid = 1
    returncode = None

    async def wait(self) -> int:
        return 0

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


def _spawn_recorder(spawned: list[Path]):  # type: ignore[no-untyped-def]
    async def fake_spawn(path: Path) -> _AliveProc:
        spawned.append(path)
        return _AliveProc()

    return fake_spawn


def _reload_recorder(reloads: list[tuple[Path, str]], returncode: int = 0):  # type: ignore[no-untyped-def]
    async def fake_reload(caddyfile_path: Path, admin_addr: str) -> tuple[int, bytes]:
        reloads.append((caddyfile_path, admin_addr))
        return returncode, b"boom"

    return fake_reload


@pytest.mark.asyncio
async def test_reload_uses_admin_api_without_respawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A graceful reload keeps the running process (no respawn = no dropped connections); it just
    # shells out to `caddy reload` against the admin socket.
    spawned: list[Path] = []
    reloads: list[tuple[Path, str]] = []
    monkeypatch.setattr(caddy, "_spawn_caddy", _spawn_recorder(spawned))
    monkeypatch.setattr(caddy, "_run_caddy_reload", _reload_recorder(reloads))
    cp = CaddyProcess(proc=_AliveProc(), caddyfile_path=tmp_path / "Caddyfile", admin_addr="unix//x.sock")  # type: ignore[arg-type]

    await cp.reload()

    assert reloads == [(tmp_path / "Caddyfile", "unix//x.sock")]
    assert spawned == []  # graceful — the process was never respawned


@pytest.mark.asyncio
async def test_reload_falls_back_to_cold_restart_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[Path] = []
    monkeypatch.setattr(caddy, "_spawn_caddy", _spawn_recorder(spawned))
    monkeypatch.setattr(caddy, "_run_caddy_reload", _reload_recorder([], returncode=1))  # reload fails
    cp = CaddyProcess(proc=_AliveProc(), caddyfile_path=tmp_path / "Caddyfile", admin_addr="unix//x.sock")  # type: ignore[arg-type]

    await cp.reload()

    assert len(spawned) == 1  # reload failed → cold restart respawned Caddy


@pytest.mark.asyncio
async def test_reload_falls_back_to_cold_restart_on_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A hung `caddy reload` raises TimeoutError; reload() must cold-restart rather than let it
    # propagate uncaught and leave Caddy serving the stale config.
    spawned: list[Path] = []
    monkeypatch.setattr(caddy, "_spawn_caddy", _spawn_recorder(spawned))

    async def _hang(caddyfile_path: Path, admin_addr: str) -> tuple[int, bytes]:
        raise TimeoutError

    monkeypatch.setattr(caddy, "_run_caddy_reload", _hang)
    cp = CaddyProcess(proc=_AliveProc(), caddyfile_path=tmp_path / "Caddyfile", admin_addr="unix//x.sock")  # type: ignore[arg-type]

    await cp.reload()  # must not raise

    assert len(spawned) == 1  # timeout → cold restart respawned Caddy


@pytest.mark.asyncio
async def test_reload_cold_restarts_when_admin_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # With no admin endpoint there's nothing to reload through, so reload() must cold-restart and
    # never invoke `caddy reload`.
    reloads: list[tuple[Path, str]] = []
    spawned: list[Path] = []
    monkeypatch.setattr(caddy, "_run_caddy_reload", _reload_recorder(reloads))
    monkeypatch.setattr(caddy, "_spawn_caddy", _spawn_recorder(spawned))
    cp = CaddyProcess(proc=_AliveProc(), caddyfile_path=tmp_path / "Caddyfile")  # type: ignore[arg-type]  # admin_addr=None

    await cp.reload()

    assert reloads == []  # never shelled out to `caddy reload`
    assert len(spawned) == 1  # cold restart instead


# ── _spawn_caddy bind-retry (self-update handoff) ────────────────────────────


class _RunningProc:
    """Caddy that bound successfully: still running after the settle window."""

    pid = 100
    returncode = None


class _DeadProc:
    """Caddy that exited immediately (bind conflict)."""

    pid = 101
    returncode = 1


_ADDR_IN_USE_LINE = "Error: loading initial config: ... listen tcp :443: bind: address already in use"


async def _done_task() -> asyncio.Task[None]:
    """Stand-in for the log-streaming task, already finished so the drain returns at once."""

    async def _noop() -> None:
        return None

    task = asyncio.create_task(_noop())
    await task
    return task


@pytest.mark.asyncio
async def test_spawn_caddy_retries_until_ports_free(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # First two spawns hit a bind conflict (updater still holds :443), third binds.
    seq = [(_DeadProc(), [_ADDR_IN_USE_LINE]), (_DeadProc(), [_ADDR_IN_USE_LINE]), (_RunningProc(), [])]
    calls = {"n": 0}

    async def fake_once(_p):  # type: ignore[no-untyped-def]
        proc, recent = seq[calls["n"]]
        calls["n"] += 1
        return proc, recent, await _done_task()

    monkeypatch.setattr(caddy, "_spawn_caddy_once", fake_once)
    monkeypatch.setattr(caddy, "_CADDY_BIND_RETRY_INTERVAL", 0)
    proc = await caddy._spawn_caddy(tmp_path / "Caddyfile")
    assert isinstance(proc, _RunningProc)
    assert calls["n"] == 3  # retried past the two conflicts


@pytest.mark.asyncio
async def test_spawn_caddy_gives_up_after_window(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Ports never free (persistent bind conflict): after the retry window, return
    # the dead proc so the caller sees the failure rather than a false "Caddy up".
    async def fake_once(_p):  # type: ignore[no-untyped-def]
        return _DeadProc(), [_ADDR_IN_USE_LINE], await _done_task()

    monkeypatch.setattr(caddy, "_spawn_caddy_once", fake_once)
    monkeypatch.setattr(caddy, "_CADDY_BIND_RETRY_INTERVAL", 0)
    monkeypatch.setattr(caddy, "_CADDY_BIND_RETRY_SECONDS", 0.0)
    proc = await caddy._spawn_caddy(tmp_path / "Caddyfile")
    assert isinstance(proc, _DeadProc)


@pytest.mark.asyncio
async def test_spawn_caddy_fails_fast_on_non_bind_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A config/syntax error (NOT a bind conflict) must not be retried — return the
    # dead proc immediately instead of spinning the whole retry window.
    calls = {"n": 0}

    async def fake_once(_p):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return _DeadProc(), ["Error: adapting config: unexpected token"], await _done_task()

    monkeypatch.setattr(caddy, "_spawn_caddy_once", fake_once)
    monkeypatch.setattr(caddy, "_CADDY_BIND_RETRY_INTERVAL", 0)
    proc = await caddy._spawn_caddy(tmp_path / "Caddyfile")
    assert isinstance(proc, _DeadProc)
    assert calls["n"] == 1  # no retry on a non-bind failure
