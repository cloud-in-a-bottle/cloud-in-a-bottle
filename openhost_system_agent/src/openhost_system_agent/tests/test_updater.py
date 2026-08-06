from __future__ import annotations

import datetime as _dt
import json
import socket
import ssl
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from openhost_system_agent.updater import launcher
from openhost_system_agent.updater import paths
from openhost_system_agent.updater import progress
from openhost_system_agent.updater import server


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(paths._DATA_DIR_ENV, str(tmp_path))
    return tmp_path


# ── paths ───────────────────────────────────────────────────────────────────


def test_paths_honor_env(data_dir: Path) -> None:
    assert paths.data_dir() == data_dir
    assert paths.updater_dir() == data_dir / "updater"
    assert paths.progress_log_path() == data_dir / "updater" / "progress.jsonl"
    assert paths.token_path() == data_dir / "updater" / "token"
    assert paths.tls_cert_path() == data_dir / "openhost-tls-cert.pem"
    assert paths.tls_key_path() == data_dir / "openhost-tls-key.pem"


# ── progress log ─────────────────────────────────────────────────────────────


def test_progress_reset_and_record(data_dir: Path) -> None:
    progress.reset_progress()
    progress.record("fetch", "Fetching")
    progress.record("migrate", "Migrating", ref="v1.2.3")
    progress.record(progress.PHASE_DONE, "Done")

    lines = paths.progress_log_path().read_text().strip().splitlines()
    assert len(lines) == 3
    entries = [json.loads(x) for x in lines]
    assert entries[0]["phase"] == "fetch"
    assert entries[1]["ref"] == "v1.2.3"
    assert entries[2]["phase"] == "done"
    # Every entry has a timestamp.
    assert all(e.get("ts") for e in entries)


def test_progress_reset_truncates_stale(data_dir: Path) -> None:
    progress.record("fetch", "old run")
    progress.reset_progress()
    assert paths.progress_log_path().read_text() == ""


def test_progress_record_never_raises_on_bad_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    # Point at an unwritable path; record must swallow the error, not raise.
    monkeypatch.setenv(paths._DATA_DIR_ENV, "/proc/nonexistent/cannot/create")
    progress.record("fetch", "should not raise")  # no exception = pass


# ── server: progress tailing + terminal detection ────────────────────────────


def test_tail_progress_skips_partial_lines(data_dir: Path) -> None:
    progress.reset_progress()
    progress.record("fetch", "one")
    # Append a half-written final line (no newline / invalid json).
    with open(paths.progress_log_path(), "a") as f:
        f.write('{"phase": "migrate"')  # truncated, no closing brace
    entries = server._tail_progress()
    assert len(entries) == 1
    assert entries[0]["phase"] == "fetch"


def test_is_terminal(data_dir: Path) -> None:
    assert server._is_terminal([]) is False
    assert server._is_terminal([{"phase": "fetch"}]) is False
    assert server._is_terminal([{"phase": "done"}]) is True
    assert server._is_terminal([{"phase": "failed"}]) is True


# ── server: token gating over real TLS ───────────────────────────────────────


def _self_signed(cert_path: Path, key_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=1))
        .not_valid_after(_dt.datetime.now(_dt.UTC) + _dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )


@pytest.fixture
def running_server(data_dir: Path) -> Iterator[int]:
    (data_dir / "updater").mkdir(parents=True, exist_ok=True)
    paths.token_path().write_text("goodtoken")
    progress.reset_progress()
    progress.record("migrate", "Applying migrations")
    progress.record(progress.PHASE_DONE, "Complete")

    cert = data_dir / "openhost-tls-cert.pem"
    key = data_dir / "openhost-tls-key.pem"
    _self_signed(cert, key)
    ctx = server._make_ssl_context(cert, key)
    sock = server._try_bind("127.0.0.1", 0)  # ephemeral port
    assert sock is not None
    port = sock.getsockname()[1]
    httpd = server._serve_on(sock, ctx)
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(port: int, path: str) -> tuple[int, bytes]:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection(("127.0.0.1", port), timeout=5)
    conn = ctx.wrap_socket(raw, server_hostname="localhost")
    conn.sendall(f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
    data = b""
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    conn.close()
    header, _, body = data.partition(b"\r\n\r\n")
    status = int(header.split(b" ")[1])
    return status, body


def test_server_authed_shows_log_page(running_server: int) -> None:
    status, body = _get(running_server, "/?token=goodtoken")
    assert status == 200
    assert b"Updating this instance" in body


def test_server_unauthed_shows_loading_page(running_server: int) -> None:
    status, body = _get(running_server, "/")
    assert status == 200
    assert b"This instance is updating" in body


def test_server_updates_authed_returns_progress(running_server: int) -> None:
    status, body = _get(running_server, "/updates?token=goodtoken")
    assert status == 200
    payload = json.loads(body)
    assert payload["terminal"] is True
    assert payload["entries"][0]["phase"] == "migrate"


def test_server_updates_wrong_token_forbidden(running_server: int) -> None:
    status, _ = _get(running_server, "/updates?token=wrong")
    assert status == 403


def test_server_updates_no_token_forbidden(running_server: int) -> None:
    status, _ = _get(running_server, "/updates")
    assert status == 403


# ── server: bind + readiness helpers ─────────────────────────────────────────


def test_try_bind_conflict_returns_none() -> None:
    first = server._try_bind("127.0.0.1", 0)
    assert first is not None
    port = first.getsockname()[1]
    # Binding the SAME concrete port again should fail (SO_REUSEADDR doesn't let
    # two live listeners share it).
    second = server._try_bind("127.0.0.1", port)
    first.close()
    assert second is None


def test_compute_space_ready_detects_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    monkeypatch.setattr(server, "_COMPUTE_SPACE_PORT", port)
    try:
        assert server._compute_space_ready() is True
    finally:
        listener.close()


def test_compute_space_not_ready_when_nothing_listening(monkeypatch: pytest.MonkeyPatch) -> None:
    # Grab a port then close it so nothing is listening there.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setattr(server, "_COMPUTE_SPACE_PORT", port)
    assert server._compute_space_ready() is False


def test_make_ssl_context_missing_files_returns_none(tmp_path: Path) -> None:
    assert server._make_ssl_context(tmp_path / "no.pem", tmp_path / "no.key") is None


# ── run / acquire_ports_during_downtime lifecycle ────────────────────────────


def test_acquire_waits_until_downtime_then_binds(monkeypatch: pytest.MonkeyPatch) -> None:
    # At launch compute_space is UP and the ports are held by Caddy (bind fails).
    # The updater must keep waiting, NOT exit. Once compute_space goes down and a
    # bind succeeds, it returns the socket.
    state = {"up": True, "bind_ok": False, "polls": 0}

    def fake_ready() -> bool:
        state["polls"] += 1
        # After a few polls, simulate the restart taking compute_space down and
        # freeing the port.
        if state["polls"] >= 3:
            state["up"] = False
            state["bind_ok"] = True
        return bool(state["up"])

    fake_sock = object()

    def fake_bind(host: str, port: int):  # type: ignore[no-untyped-def]
        return fake_sock if state["bind_ok"] else None

    monkeypatch.setattr(server, "_compute_space_ready", fake_ready)
    monkeypatch.setattr(server, "_try_bind", fake_bind)
    monkeypatch.setattr("openhost_system_agent.updater.server.time.sleep", lambda _: None)

    https, http = server._acquire_ports_during_downtime(ssl_ctx=object())  # type: ignore[arg-type]
    # It waited through "still up" polls and only bound once downtime began.
    assert https is fake_sock
    assert state["polls"] >= 3


def test_acquire_gives_up_if_downtime_seen_but_never_binds(monkeypatch: pytest.MonkeyPatch) -> None:
    # compute_space goes down but we never manage to grab the port (Caddy rebinds
    # faster than we retry). After the bind-wait window we give up, not spin.
    monkeypatch.setattr(server, "_BIND_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(server, "_compute_space_ready", lambda: False)  # always "down"
    monkeypatch.setattr(server, "_try_bind", lambda *a: None)  # never succeeds
    monkeypatch.setattr("openhost_system_agent.updater.server.time.sleep", lambda _: None)

    https, http = server._acquire_ports_during_downtime(ssl_ctx=object())  # type: ignore[arg-type]
    assert https is None and http is None


def test_acquire_closes_partial_bind_on_giveup(monkeypatch: pytest.MonkeyPatch) -> None:
    # We grab port 80 but never 443 (443 requires TLS which we hold). On giving up
    # we must close the 80 socket rather than leak it. Uses a real ephemeral
    # socket for 80 and forces 443 to keep failing.
    real80 = server._try_bind("127.0.0.1", 0)
    assert real80 is not None

    def fake_bind(host: str, port: int):  # type: ignore[no-untyped-def]
        return None if port == 443 else real80

    monkeypatch.setattr(server, "_BIND_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(server, "_compute_space_ready", lambda: False)
    monkeypatch.setattr(server, "_try_bind", fake_bind)
    monkeypatch.setattr("openhost_system_agent.updater.server.time.sleep", lambda _: None)

    https, http = server._acquire_ports_during_downtime(ssl_ctx=object())  # type: ignore[arg-type]
    assert https is None and http is None
    # The port-80 socket must have been closed (fileno() == -1 once closed).
    assert real80.fileno() == -1


def test_acquire_returns_none_if_recovered_before_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    # Downtime is observed once, then compute_space comes back before we grab a
    # port — nothing left to cover.
    seq = iter([False, True, True, True, True])

    def fake_ready() -> bool:
        try:
            return next(seq)
        except StopIteration:
            return True

    monkeypatch.setattr(server, "_compute_space_ready", fake_ready)
    monkeypatch.setattr(server, "_try_bind", lambda *a: None)
    monkeypatch.setattr("openhost_system_agent.updater.server.time.sleep", lambda _: None)

    https, http = server._acquire_ports_during_downtime(ssl_ctx=object())  # type: ignore[arg-type]
    assert https is None and http is None


def test_run_returns_when_no_ports_acquired(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_make_ssl_context", lambda *a: None)
    monkeypatch.setattr(server, "_acquire_ports_during_downtime", lambda ctx: (None, None))
    start = time.monotonic()
    server.run(data_dir / "c.pem", data_dir / "c.key")
    assert time.monotonic() - start < 2


# ── token persistence ────────────────────────────────────────────────────────


def test_write_and_clear_token(data_dir: Path) -> None:
    paths.write_token("mytoken")
    p = paths.token_path()
    assert p.read_text() == "mytoken"
    assert (p.stat().st_mode & 0o777) == 0o600
    paths.clear_token()
    assert not p.exists()
    # Idempotent.
    paths.clear_token()


# ── launcher ─────────────────────────────────────────────────────────────────


def test_launch_updater_no_systemd_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openhost_system_agent.updater.launcher.shutil.which", lambda _: None)
    assert launcher.launch_updater() is False


def test_launch_updater_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openhost_system_agent.updater.launcher.shutil.which", lambda _: "/usr/bin/systemd-run")
    monkeypatch.setattr(launcher, "_reset_stale_scope", lambda: None)
    calls: list[list[str]] = []

    class _Ok:
        returncode = 0
        stderr = ""

    def _run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return _Ok()

    monkeypatch.setattr("openhost_system_agent.updater.launcher.subprocess.run", _run)
    monkeypatch.setattr("openhost_system_agent.updater.launcher.time.sleep", lambda _: None)
    assert launcher.launch_updater() is True
    assert calls[0][0] == "systemd-run"
    assert "--scope" in calls[0]
    assert calls[0][-2:] == ["updater", "serve"]


def test_launch_updater_systemd_run_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openhost_system_agent.updater.launcher.shutil.which", lambda _: "/usr/bin/systemd-run")
    monkeypatch.setattr(launcher, "_reset_stale_scope", lambda: None)

    class _Fail:
        returncode = 1
        stderr = "boom"

    monkeypatch.setattr("openhost_system_agent.updater.launcher.subprocess.run", lambda *a, **k: _Fail())
    assert launcher.launch_updater() is False


def test_launch_updater_never_raises_on_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openhost_system_agent.updater.launcher.shutil.which", lambda _: "/usr/bin/systemd-run")
    monkeypatch.setattr(launcher, "_reset_stale_scope", lambda: None)

    def _boom(*a, **k):  # type: ignore[no-untyped-def]
        raise OSError("nope")

    monkeypatch.setattr("openhost_system_agent.updater.launcher.subprocess.run", _boom)
    assert launcher.launch_updater() is False
