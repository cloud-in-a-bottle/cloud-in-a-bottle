"""Phase 3: the generated Caddyfile serves each configured domain on its own terms
— https (with the acquired cert or Caddy's internal CA) for TLS domains, plain http
with no redirect for mDNS `.local` domains — so http `.local` and https external run
at once.  Where the `caddy` binary is available we adapt the output to prove it's
syntactically valid, not just string-matched."""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
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

    def poll(self) -> int:
        return 0

    def wait(self, timeout: float | None = None) -> int:
        return 0


def test_restart_serializes_concurrent_callers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Three daemon threads (deferred domain reload, acquisition completion, TLS renewal) can restart
    # Caddy at once; the spawn critical section must run one-at-a-time, else a second `caddy run`
    # races the first onto :443.
    active = 0
    max_active = 0
    counter_lock = threading.Lock()  # guards the probe counters, not the code under test

    def fake_spawn(_path: Path) -> _FakeProc:
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)  # widen the window so an unserialized restart would overlap here
        with counter_lock:
            active -= 1
        return _FakeProc()

    monkeypatch.setattr(caddy, "_spawn_caddy", fake_spawn)
    cp = CaddyProcess(proc=_FakeProc(), caddyfile_path=tmp_path / "Caddyfile")  # type: ignore[arg-type]

    threads = [threading.Thread(target=cp.restart) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

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
    """Reports still-running (poll() is None) so reload() takes the graceful-reload path."""

    pid = 1

    def poll(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


def _completed(cmd: list[str], returncode: int) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(cmd, returncode, b"", b"boom")


def test_reload_uses_admin_api_without_respawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A graceful reload keeps the running process (no respawn = no dropped connections); it just
    # shells out to `caddy reload` against the admin socket.
    spawned: list[Path] = []
    monkeypatch.setattr(caddy, "_spawn_caddy", lambda p: spawned.append(p) or _AliveProc())  # type: ignore[arg-type,func-returns-value]
    calls: list[list[str]] = []
    monkeypatch.setattr(caddy.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _completed(cmd, 0))
    cp = CaddyProcess(proc=_AliveProc(), caddyfile_path=tmp_path / "Caddyfile", admin_addr="unix//x.sock")  # type: ignore[arg-type]

    cp.reload()

    assert calls and calls[0][:2] == ["caddy", "reload"]
    assert "--address" in calls[0] and "unix//x.sock" in calls[0]
    assert spawned == []  # graceful — the process was never respawned


def test_reload_falls_back_to_cold_restart_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[Path] = []
    monkeypatch.setattr(caddy, "_spawn_caddy", lambda p: spawned.append(p) or _AliveProc())  # type: ignore[arg-type,func-returns-value]
    monkeypatch.setattr(caddy.subprocess, "run", lambda cmd, **kw: _completed(cmd, 1))  # reload fails
    cp = CaddyProcess(proc=_AliveProc(), caddyfile_path=tmp_path / "Caddyfile", admin_addr="unix//x.sock")  # type: ignore[arg-type]

    cp.reload()

    assert len(spawned) == 1  # reload failed → cold restart respawned Caddy


def test_reload_falls_back_to_cold_restart_on_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A hung `caddy reload` raises TimeoutExpired; reload() must cold-restart rather than let it
    # propagate uncaught and leave Caddy serving the stale config.
    spawned: list[Path] = []
    monkeypatch.setattr(caddy, "_spawn_caddy", lambda p: spawned.append(p) or _AliveProc())  # type: ignore[arg-type,func-returns-value]

    def _hang(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(caddy.subprocess, "run", _hang)
    cp = CaddyProcess(proc=_AliveProc(), caddyfile_path=tmp_path / "Caddyfile", admin_addr="unix//x.sock")  # type: ignore[arg-type]

    cp.reload()  # must not raise

    assert len(spawned) == 1  # timeout → cold restart respawned Caddy


def test_reload_cold_restarts_when_admin_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # With no admin endpoint there's nothing to reload through, so reload() must cold-restart and
    # never invoke `caddy reload`.
    ran: list[list[str]] = []
    monkeypatch.setattr(caddy.subprocess, "run", lambda cmd, **kw: ran.append(cmd) or _completed(cmd, 0))
    spawned: list[Path] = []
    monkeypatch.setattr(caddy, "_spawn_caddy", lambda p: spawned.append(p) or _AliveProc())  # type: ignore[arg-type,func-returns-value]
    cp = CaddyProcess(proc=_AliveProc(), caddyfile_path=tmp_path / "Caddyfile")  # type: ignore[arg-type]  # admin_addr=None

    cp.reload()

    assert ran == []  # never shelled out to `caddy reload`
    assert len(spawned) == 1  # cold restart instead


# ── _spawn_caddy bind-retry (self-update handoff) ────────────────────────────


class _RunningProc:
    """Caddy that binds successfully: wait() blocks (raises TimeoutExpired)."""

    pid = 100

    def poll(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired(cmd="caddy", timeout=timeout or 0)


class _DeadProc:
    """Caddy that exited immediately (bind conflict): wait() returns non-zero."""

    pid = 101
    returncode = 1

    def poll(self) -> int:
        return 1

    def wait(self, timeout: float | None = None) -> int:
        return 1


_ADDR_IN_USE_LINE = "Error: loading initial config: ... listen tcp :443: bind: address already in use"


class _FakeThread:
    """Stand-in for the log-streaming thread; join() is a no-op in tests."""

    def join(self, timeout: float | None = None) -> None:
        pass


def test_spawn_caddy_returns_on_successful_bind(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    spawns: list[int] = []
    monkeypatch.setattr(caddy, "_spawn_caddy_once", lambda p: (spawns.append(1) or _RunningProc(), [], _FakeThread()))  # type: ignore[func-returns-value,arg-type]
    monkeypatch.setattr(caddy.time, "sleep", lambda _: None)
    proc = caddy._spawn_caddy(tmp_path / "Caddyfile")
    assert isinstance(proc, _RunningProc)
    assert len(spawns) == 1  # bound first try, no retry


def test_spawn_caddy_retries_until_ports_free(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # First two spawns hit a bind conflict (updater still holds :443), third binds.
    seq = [
        (_DeadProc(), [_ADDR_IN_USE_LINE], _FakeThread()),
        (_DeadProc(), [_ADDR_IN_USE_LINE], _FakeThread()),
        (_RunningProc(), [], _FakeThread()),
    ]
    calls = {"n": 0}

    def fake_once(_p):  # type: ignore[no-untyped-def]
        triple = seq[calls["n"]]
        calls["n"] += 1
        return triple

    monkeypatch.setattr(caddy, "_spawn_caddy_once", fake_once)
    monkeypatch.setattr(caddy.time, "sleep", lambda _: None)
    proc = caddy._spawn_caddy(tmp_path / "Caddyfile")
    assert isinstance(proc, _RunningProc)
    assert calls["n"] == 3  # retried past the two conflicts


def test_spawn_caddy_gives_up_after_window(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Ports never free (persistent bind conflict): after the retry window, return
    # the dead proc so the caller sees the failure rather than a false "Caddy up".
    monkeypatch.setattr(caddy, "_spawn_caddy_once", lambda p: (_DeadProc(), [_ADDR_IN_USE_LINE], _FakeThread()))  # type: ignore[arg-type]
    monkeypatch.setattr(caddy.time, "sleep", lambda _: None)
    monkeypatch.setattr(caddy, "_CADDY_BIND_RETRY_SECONDS", 0.0)
    proc = caddy._spawn_caddy(tmp_path / "Caddyfile")
    assert isinstance(proc, _DeadProc)


def test_spawn_caddy_fails_fast_on_non_bind_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A config/syntax error (NOT a bind conflict) must not be retried — return the
    # dead proc immediately instead of spinning the whole retry window.
    calls = {"n": 0}

    def fake_once(_p):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return _DeadProc(), ["Error: adapting config: unexpected token"], _FakeThread()

    monkeypatch.setattr(caddy, "_spawn_caddy_once", fake_once)
    monkeypatch.setattr(caddy.time, "sleep", lambda _: None)
    proc = caddy._spawn_caddy(tmp_path / "Caddyfile")
    assert isinstance(proc, _DeadProc)
    assert calls["n"] == 1  # no retry on a non-bind failure
