"""Edge-case coverage for the seamless-update updater (server, launcher, progress,
paths, token). Complements test_updater.py with adversarial / boundary inputs."""

from __future__ import annotations

import datetime as _dt
import json
import os
import socket
import ssl
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from openhost_system_agent.cli import UpdaterCmd
from openhost_system_agent.updater import launcher
from openhost_system_agent.updater import paths
from openhost_system_agent.updater import progress
from openhost_system_agent.updater import server


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(paths._DATA_DIR_ENV, str(tmp_path))
    (tmp_path / "updater").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _self_signed(cert_path: Path, key_path: Path, cn: str = "localhost") -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
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


# ─────────────────────────── progress: read_entries edge cases ───────────────


def test_read_entries_blank_lines_between(data_dir: Path) -> None:
    paths.progress_log_path().write_text(
        json.dumps({"phase": "fetch"}) + "\n\n" + json.dumps({"phase": "done"}) + "\n"
    )
    entries = progress.read_entries()
    assert [e["phase"] for e in entries] == ["fetch", "done"]


def test_read_entries_trailing_partial_line(data_dir: Path) -> None:
    with open(paths.progress_log_path(), "w") as f:
        f.write(json.dumps({"phase": "fetch"}) + "\n")
        f.write('{"phase": "migr')  # cut off mid-write
    entries = progress.read_entries()
    assert len(entries) == 1


def test_read_entries_non_json_line_skipped(data_dir: Path) -> None:
    paths.progress_log_path().write_text("not json at all\n" + json.dumps({"phase": "fetch"}) + "\n")
    entries = progress.read_entries()
    assert [e["phase"] for e in entries] == ["fetch"]


# ─────────────────────────── progress: is_terminal edge cases ────────────────


def test_is_terminal_empty() -> None:
    assert progress.is_terminal([]) is False


def test_is_terminal_done() -> None:
    assert progress.is_terminal([{"phase": "fetch"}, {"phase": "done"}]) is True


def test_is_terminal_only_last_matters() -> None:
    # A "done" mid-log followed by more work is NOT terminal (last wins).
    assert progress.is_terminal([{"phase": "done"}, {"phase": "install"}]) is False


# ─────────────────────────── progress: record / reset ────────────────────────


def test_record_never_raises_unwritable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(paths._DATA_DIR_ENV, "/proc/x/y/z/cannot")
    progress.record("fetch", "no raise")  # must not raise


def test_record_message_with_newline_stays_one_entry(data_dir: Path) -> None:
    # A newline in the message must be JSON-escaped so it stays a single JSONL line.
    progress.record("fetch", "line1\nline2")
    entries = progress.read_entries()
    assert len(entries) == 1
    assert entries[0]["message"] == "line1\nline2"


# ─────────────────────────── paths / token ───────────────────────────────────


def test_write_token_permissions_0600(data_dir: Path) -> None:
    paths.write_token("abc")
    assert (paths.token_path().stat().st_mode & 0o777) == 0o600


def test_clear_token_removes(data_dir: Path) -> None:
    paths.write_token("abc")
    paths.clear_token()
    assert not paths.token_path().exists()


# ─────────────────────────── server: token auth over TLS ─────────────────────


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


@pytest.fixture
def server_factory(data_dir: Path):  # type: ignore[no-untyped-def]
    cert = data_dir / "openhost-tls-cert.pem"
    key = data_dir / "openhost-tls-key.pem"
    _self_signed(cert, key)
    ctx = server._make_ssl_context(cert, key)
    started: list[object] = []

    def make(token: str | None, entries: list[dict[str, object]]) -> int:
        if token is not None:
            paths.write_token(token)
        progress.reset_progress()
        for e in entries:
            progress.record(str(e.get("phase", "x")), str(e.get("message", "")), ref=e.get("ref"))  # type: ignore[arg-type]
        sock = server._try_bind("127.0.0.1", 0)
        assert sock is not None
        port = int(sock.getsockname()[1])  # read BEFORE _serve_on wraps/replaces the socket
        httpd = server._serve_on(sock, ctx)
        started.append(httpd)
        return port

    yield make
    for httpd in started:
        try:
            httpd.shutdown()  # type: ignore[attr-defined]
            httpd.server_close()  # type: ignore[attr-defined]
        except OSError:
            pass


def test_server_page_renders_with_token(server_factory) -> None:  # type: ignore[no-untyped-def]
    port = server_factory("tok", [{"phase": "migrate", "message": "Migrating"}])
    status, body = _get(port, "/?token=tok")
    assert status == 200 and b"Updating this instance" in body


def test_server_page_no_token_renders(server_factory) -> None:  # type: ignore[no-untyped-def]
    port = server_factory("tok", [])
    status, body = _get(port, "/")
    assert status == 200 and b"This instance is updating and will be back shortly" in body


def test_server_health_503_while_updating(server_factory) -> None:  # type: ignore[no-untyped-def]
    # /health is 503 while the updater owns the port (dashboard is NOT up yet).
    port = server_factory("tok", [])
    status, _ = _get(port, "/health")
    assert status == 503


def test_server_updates_forbidden_without_token(server_factory) -> None:  # type: ignore[no-untyped-def]
    port = server_factory("tok", [{"phase": "done", "message": "d"}])
    status, _ = _get(port, "/updates")
    assert status == 403


def test_server_updates_ok_with_token(server_factory) -> None:  # type: ignore[no-untyped-def]
    port = server_factory("tok", [{"phase": "migrate", "message": "m"}, {"phase": "done", "message": "d"}])
    status, body = _get(port, "/updates?token=tok")
    assert status == 200
    payload = json.loads(body)
    assert payload["terminal"] is True
    assert len(payload["entries"]) == 2


def test_server_updates_empty_progress(server_factory) -> None:  # type: ignore[no-untyped-def]
    port = server_factory("tok", [])
    status, body = _get(port, "/updates?token=tok")
    payload = json.loads(body)
    assert payload["entries"] == [] and payload["terminal"] is False


def test_server_updates_reflects_live_append(server_factory) -> None:  # type: ignore[no-untyped-def]
    port = server_factory("tok", [{"phase": "fetch", "message": "f"}])
    s1, b1 = _get(port, "/updates?token=tok")
    assert len(json.loads(b1)["entries"]) == 1
    progress.record("done", "complete")
    s2, b2 = _get(port, "/updates?token=tok")
    p2 = json.loads(b2)
    assert len(p2["entries"]) == 2 and p2["terminal"] is True


def test_server_token_rotation_mid_flight(server_factory) -> None:  # type: ignore[no-untyped-def]
    # If the token file changes, the server honors the NEW token (reads live).
    port = server_factory("tok1", [])
    assert _get(port, "/updates?token=tok1")[0] == 200
    paths.write_token("tok2")
    assert _get(port, "/updates?token=tok1")[0] == 403
    assert _get(port, "/updates?token=tok2")[0] == 200


# ─────────────────────────── ssl context edge cases ──────────────────────────


def test_make_ssl_context_missing_both(tmp_path: Path) -> None:
    assert server._make_ssl_context(tmp_path / "no.pem", tmp_path / "no.key") is None


# ─────────────────────────── bind edge cases ─────────────────────────────────


def test_try_bind_conflict_returns_none() -> None:
    s = server._try_bind("127.0.0.1", 0)
    assert s is not None
    port = s.getsockname()[1]
    assert server._try_bind("127.0.0.1", port) is None
    s.close()


def test_try_bind_privileged_port_without_root_returns_none() -> None:
    if os.geteuid() == 0:
        pytest.skip("running as root can bind privileged ports")
    assert server._try_bind("0.0.0.0", 443) is None


# ─────────────────────────── compute_space readiness ─────────────────────────


def test_compute_space_ready_false_when_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setattr(server, "_COMPUTE_SPACE_PORT", port)
    assert server._compute_space_ready() is False


# ─────────────────────────── acquire_ports lifecycle ─────────────────────────


def test_acquire_waits_for_downtime_before_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"polls": 0, "free": False}
    fake = object()

    def ready() -> bool:
        state["polls"] += 1
        if state["polls"] >= 4:
            state["free"] = True
        return not state["free"]

    monkeypatch.setattr(server, "_compute_space_ready", ready)
    monkeypatch.setattr(server, "_try_bind", lambda h, p: fake if state["free"] and p == 443 else None)
    monkeypatch.setattr("openhost_system_agent.updater.server.time.sleep", lambda _: None)
    https, _ = server._acquire_ports_during_downtime(ssl_ctx=object())  # type: ignore[arg-type]
    assert https is fake and state["polls"] >= 4


def test_acquire_returns_none_if_recovered_before_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    seq = iter([False, True])
    monkeypatch.setattr(server, "_compute_space_ready", lambda: next(seq, True))
    monkeypatch.setattr(server, "_try_bind", lambda h, p: None)
    monkeypatch.setattr("openhost_system_agent.updater.server.time.sleep", lambda _: None)
    https, http = server._acquire_ports_during_downtime(ssl_ctx=object())  # type: ignore[arg-type]
    assert https is None and http is None


def test_acquire_gives_up_after_bind_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_BIND_WAIT_SECONDS", 0.02)
    monkeypatch.setattr(server, "_compute_space_ready", lambda: False)
    monkeypatch.setattr(server, "_try_bind", lambda h, p: None)
    monkeypatch.setattr("openhost_system_agent.updater.server.time.sleep", lambda _: None)
    https, http = server._acquire_ports_during_downtime(ssl_ctx=object())  # type: ignore[arg-type]
    assert https is None and http is None


def test_acquire_closes_port80_when_giving_up(monkeypatch: pytest.MonkeyPatch) -> None:
    real80 = server._try_bind("127.0.0.1", 0)
    assert real80 is not None
    monkeypatch.setattr(server, "_BIND_WAIT_SECONDS", 0.02)
    monkeypatch.setattr(server, "_compute_space_ready", lambda: False)
    monkeypatch.setattr(server, "_try_bind", lambda h, p: None if p == 443 else real80)
    monkeypatch.setattr("openhost_system_agent.updater.server.time.sleep", lambda _: None)
    https, http = server._acquire_ports_during_downtime(ssl_ctx=object())  # type: ignore[arg-type]
    assert https is None and http is None
    assert real80.fileno() == -1  # closed, not leaked


def test_acquire_no_tls_uses_port80(monkeypatch: pytest.MonkeyPatch) -> None:
    # When there is no ssl_ctx, holding :80 alone is enough to cover downtime.
    fake80 = object()
    monkeypatch.setattr(server, "_compute_space_ready", lambda: False)
    monkeypatch.setattr(server, "_try_bind", lambda h, p: fake80 if p == 80 else None)
    monkeypatch.setattr("openhost_system_agent.updater.server.time.sleep", lambda _: None)
    https, http = server._acquire_ports_during_downtime(ssl_ctx=None)
    assert https is None and http is fake80


# ─────────────────────────── run() lifecycle ─────────────────────────────────


def test_run_serves_then_releases_when_ready(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cert = data_dir / "openhost-tls-cert.pem"
    key = data_dir / "openhost-tls-key.pem"
    _self_signed(cert, key)
    real = server._try_bind("127.0.0.1", 0)
    assert real is not None
    monkeypatch.setattr(server, "_acquire_ports_during_downtime", lambda ctx: (real, None))
    monkeypatch.setattr(server, "_compute_space_ready", lambda: True)  # already back
    monkeypatch.setattr("openhost_system_agent.updater.server.time.sleep", lambda _: None)
    server.run(cert, key)
    assert real.fileno() == -1  # released


# ─────────────────────────── launcher edge cases ─────────────────────────────


def test_launcher_no_systemd_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openhost_system_agent.updater.launcher.shutil.which", lambda _: None)
    assert launcher.launch_updater() is False


def test_launcher_success_waits_for_ready(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openhost_system_agent.updater.launcher.shutil.which", lambda _: "/usr/bin/systemd-run")
    monkeypatch.setattr(launcher, "_reset_stale_scope", lambda: None)

    class _Ok:
        returncode = 0
        stderr = ""

    def _run(cmd, **kw):  # type: ignore[no-untyped-def]
        paths.ready_marker_path().parent.mkdir(parents=True, exist_ok=True)
        paths.ready_marker_path().write_text("")
        return _Ok()

    monkeypatch.setattr("openhost_system_agent.updater.launcher.subprocess.run", _run)
    monkeypatch.setattr("openhost_system_agent.updater.launcher.time.sleep", lambda _: None)
    assert launcher.launch_updater() is True


def test_launcher_systemd_run_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openhost_system_agent.updater.launcher.shutil.which", lambda _: "/usr/bin/systemd-run")
    monkeypatch.setattr(launcher, "_reset_stale_scope", lambda: None)

    class _Fail:
        returncode = 1
        stderr = "unit exists"

    monkeypatch.setattr("openhost_system_agent.updater.launcher.subprocess.run", lambda *a, **k: _Fail())
    assert launcher.launch_updater() is False


def test_launcher_oserror_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openhost_system_agent.updater.launcher.shutil.which", lambda _: "/usr/bin/systemd-run")
    monkeypatch.setattr(launcher, "_reset_stale_scope", lambda: None)

    def _boom(*a, **k):  # type: ignore[no-untyped-def]
        raise OSError("nope")

    monkeypatch.setattr("openhost_system_agent.updater.launcher.subprocess.run", _boom)
    assert launcher.launch_updater() is False


def test_launcher_command_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openhost_system_agent.updater.launcher.shutil.which", lambda _: "/usr/bin/systemd-run")
    monkeypatch.setattr(launcher, "_reset_stale_scope", lambda: None)
    captured: list[list[str]] = []

    class _Ok:
        returncode = 0
        stderr = ""

    def _run(cmd, **kw):  # type: ignore[no-untyped-def]
        captured.append(cmd)
        return _Ok()

    monkeypatch.setattr("openhost_system_agent.updater.launcher.subprocess.run", _run)
    monkeypatch.setattr("openhost_system_agent.updater.launcher.time.sleep", lambda _: None)
    monkeypatch.setattr(launcher, "_READY_WAIT_SECONDS", 0.01)
    launcher.launch_updater()
    cmd = captured[0]
    assert cmd[0] == "systemd-run"
    # Transient service (no --scope) so systemd-run returns immediately.
    assert "--scope" not in cmd
    joined = " ".join(cmd)
    assert "-c" in cmd
    assert "updater" in joined and "serve" in joined
    assert any(a.startswith("--unit=") for a in cmd)


# ─────────────────────────── _read_token_file direct ─────────────────────────


def test_read_token_file_empty_is_none(data_dir: Path) -> None:
    paths.write_token("")
    assert server._read_token_file() is None


# ─────────────────────────── set-token resets progress ───────────────────────


def test_set_token_resets_stale_progress(data_dir: Path) -> None:
    # A prior run's terminal "done" must be cleared when a new token is set, so
    # the /updating page's first poll doesn't see stale terminal state and bounce.
    progress.record("done", "old run")
    assert progress.is_terminal(progress.read_entries()) is True

    UpdaterCmd().set_token("freshtoken")

    assert progress.read_entries() == []  # log cleared
    assert paths.token_path().read_text() == "freshtoken"


# ─────────────────────────── stop_updater (handoff release) ──────────────────


def test_stop_updater_calls_systemctl(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):  # type: ignore[no-untyped-def]
        calls.append(cmd)

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr("openhost_system_agent.updater.launcher.subprocess.run", fake_run)
    launcher.stop_updater()
    assert calls[0][:2] == ["systemctl", "stop"]
    assert calls[0][2] == launcher._SCOPE_UNIT


def test_stop_updater_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise OSError("systemctl gone")

    monkeypatch.setattr("openhost_system_agent.updater.launcher.subprocess.run", boom)
    launcher.stop_updater()  # must not raise


def test_try_bind_no_fd_leak_on_conflict() -> None:
    # Repeated failed binds (as during the handoff retry loop) must not leak fds.
    s = server._try_bind("127.0.0.1", 0)
    assert s is not None
    port = s.getsockname()[1]
    before = len(_open_fds())
    for _ in range(50):
        assert server._try_bind("127.0.0.1", port) is None  # always conflicts
    after = len(_open_fds())
    s.close()
    assert after - before < 5


def _open_fds() -> list[str]:
    try:
        return os.listdir("/proc/self/fd")
    except OSError:
        return []
