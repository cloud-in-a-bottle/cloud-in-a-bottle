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

from compute_space.config import Domain
from compute_space.core import caddy as caddy_mod
from compute_space.core.caddy import CaddyProcess
from compute_space.core.caddy import generate_caddyfile

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

    monkeypatch.setattr(caddy_mod, "_spawn_caddy", fake_spawn)
    cp = CaddyProcess(proc=_FakeProc(), caddyfile_path=tmp_path / "Caddyfile")  # type: ignore[arg-type]

    threads = [threading.Thread(target=cp.restart) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert max_active == 1  # never two spawns in flight at once
