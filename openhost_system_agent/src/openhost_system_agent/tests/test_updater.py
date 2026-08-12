from __future__ import annotations

import datetime as _dt
import json
import os
import socket
import ssl
import subprocess
import time
from collections.abc import Callable
from collections.abc import Iterator
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
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path))
    (tmp_path / "updater").mkdir(parents=True, exist_ok=True)
    return tmp_path


# ── shared TLS + HTTP helpers ─────────────────────────────────────────────────


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


def _request(port: int, path: str, extra_headers: str = "") -> tuple[int, dict[str, str], bytes]:
    """GET over TLS against the updater; returns status, headers, body."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection(("127.0.0.1", port), timeout=5)
    conn = ctx.wrap_socket(raw, server_hostname="localhost")
    conn.sendall(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n{extra_headers}Connection: close\r\n\r\n".encode())
    data = b""
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    conn.close()
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode(errors="replace").split("\r\n")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name:
            headers[name.strip().lower()] = value.strip()
    return int(lines[0].split(" ")[1]), headers, body


def _get(port: int, path: str, extra_headers: str = "") -> tuple[int, bytes]:
    status, _headers, body = _request(port, path, extra_headers)
    return status, body


def _open_fds() -> list[str]:
    try:
        return os.listdir("/proc/self/fd")
    except OSError:
        return []


# ── progress log: record / reset ───────────────────────────────────────────────


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


# ── progress log: read_entries tolerance ───────────────────────────────────────


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
    assert entries[0]["phase"] == "fetch"


def test_read_entries_non_json_line_skipped(data_dir: Path) -> None:
    paths.progress_log_path().write_text("not json at all\n" + json.dumps({"phase": "fetch"}) + "\n")
    entries = progress.read_entries()
    assert [e["phase"] for e in entries] == ["fetch"]


# ── progress log: is_terminal ───────────────────────────────────────────────────


def test_is_terminal(data_dir: Path) -> None:
    assert progress.is_terminal([]) is False
    assert progress.is_terminal([{"phase": "fetch"}]) is False
    assert progress.is_terminal([{"phase": "fetch"}, {"phase": "done"}]) is True
    assert progress.is_terminal([{"phase": "failed"}]) is True
    # Only the LAST entry matters: a "done" mid-log followed by work is not terminal.
    assert progress.is_terminal([{"phase": "done"}, {"phase": "install"}]) is False
    # "restarting" is deliberately NOT terminal: only the freshly booted
    # compute_space appends "done", so the page can't leave for a dashboard that
    # is about to die.
    assert progress.is_terminal([{"phase": progress.PHASE_RESTARTING}]) is False


def test_mark_boot_complete_appends_done_after_restarting(data_dir: Path) -> None:
    progress.record(progress.PHASE_RESTARTING, "Update complete. Restarting…")
    assert progress.mark_boot_complete() is True
    entries = progress.read_entries()
    assert entries[-1]["phase"] == progress.PHASE_DONE
    assert entries[-1]["message"] == "Instance is back online."


def test_mark_boot_complete_noop_when_already_terminal(data_dir: Path) -> None:
    # A finished run must not gain a second terminal entry on the next boot.
    progress.record(progress.PHASE_DONE, "Instance is back online.")
    progress.mark_boot_complete()
    assert [e["phase"] for e in progress.read_entries()] == [progress.PHASE_DONE]


def test_mark_boot_complete_noop_on_an_empty_log(data_dir: Path) -> None:
    progress.reset_progress()
    assert progress.mark_boot_complete() is True
    assert progress.read_entries() == []


def test_record_failure_skips_after_restarting(data_dir: Path) -> None:
    # A successful apply ends the log with "restarting" right before the restart
    # kills compute_space; the resulting exception must NOT be logged as a failure.
    progress.record(progress.PHASE_RESTARTING, "Update complete. Restarting…")
    assert progress.record_failure_if_not_terminal("Update failed: killed by restart") is True
    assert progress.read_entries()[-1]["phase"] == progress.PHASE_RESTARTING


def test_record_failure_records_when_mid_apply(data_dir: Path) -> None:
    progress.record("install", "Installing…")
    progress.record_failure_if_not_terminal("Update failed: pixi install error")
    assert progress.read_entries()[-1]["phase"] == progress.PHASE_FAILED


# ── server: token gating + page rendering over real TLS ────────────────────────


@pytest.fixture
def server_factory(data_dir: Path) -> Iterator[Callable[[str | None, list[dict[str, object]]], int]]:
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


def test_server_health_returns_503_while_updating(
    server_factory: Callable[[str | None, list[dict[str, object]]], int],
) -> None:
    # /health must NOT report OK while the updater owns the port, or the page
    # would think the real dashboard is back and redirect to a still-down route.
    port = server_factory("tok", [])
    status, _ = _get(port, "/health")
    assert status == 503


def test_server_xhr_gets_503_not_html(
    server_factory: Callable[[str | None, list[dict[str, object]]], int],
) -> None:
    # Non-navigation requests (fetch/XHR, e.g. the dashboard's /api/* calls racing
    # in after the redirect) must get a 503 JSON, never the HTML page — otherwise
    # the caller does JSON.parse("<!doctype ...") and errors.
    port = server_factory("tok", [])
    status, body = _get(port, "/api/settings/update", extra_headers="Sec-Fetch-Dest: empty\r\n")
    assert status == 503
    assert b"<!doctype" not in body.lower()
    assert b'"error"' in body


def test_server_navigation_gets_page(
    server_factory: Callable[[str | None, list[dict[str, object]]], int],
) -> None:
    port = server_factory("tok", [])
    status, body = _get(port, "/settings", extra_headers="Sec-Fetch-Dest: document\r\n")
    assert status == 200
    assert b"Updating this instance" in body


def test_server_headerless_request_gets_503(
    server_factory: Callable[[str | None, list[dict[str, object]]], int],
) -> None:
    # No Sec-Fetch-Dest (non-browser client) is treated as non-navigation -> 503,
    # not HTML, so a monitor/script never parses the page as data.
    port = server_factory("tok", [])
    status, body = _get(port, "/")
    assert status == 503
    assert b"<!doctype" not in body.lower()


def test_server_sends_connection_close(
    server_factory: Callable[[str | None, list[dict[str, object]]], int],
) -> None:
    # Connection: close so the browser doesn't reuse the updater's socket for
    # requests that should reach the new compute_space after the handoff.
    port = server_factory("tok", [])
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    conn = ctx.wrap_socket(socket.create_connection(("127.0.0.1", port), timeout=5), server_hostname="localhost")
    conn.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
    raw = conn.recv(4096)
    conn.close()
    assert b"Connection: close" in raw or b"connection: close" in raw


def test_server_updates_forbidden_without_or_wrong_token(
    server_factory: Callable[[str | None, list[dict[str, object]]], int],
) -> None:
    port = server_factory("tok", [{"phase": "done", "message": "d"}])
    assert _get(port, "/updates")[0] == 403
    assert _get(port, "/updates?token=wrong")[0] == 403


def test_server_updates_authed_returns_progress(
    server_factory: Callable[[str | None, list[dict[str, object]]], int],
) -> None:
    port = server_factory("tok", [{"phase": "migrate", "message": "m"}, {"phase": "done", "message": "d"}])
    status, body = _get(port, "/updates?token=tok")
    assert status == 200
    payload = json.loads(body)
    assert payload["terminal"] is True
    assert len(payload["entries"]) == 2
    assert payload["entries"][0]["phase"] == "migrate"


def test_server_token_rotation_mid_flight(
    server_factory: Callable[[str | None, list[dict[str, object]]], int],
) -> None:
    # If the token file changes, the server honors the NEW token (reads live).
    port = server_factory("tok1", [])
    assert _get(port, "/updates?token=tok1")[0] == 200
    paths.write_token("tok2")
    assert _get(port, "/updates?token=tok1")[0] == 403
    assert _get(port, "/updates?token=tok2")[0] == 200


# ── server: bind helpers ────────────────────────────────────────────────────────


def test_try_bind_conflict_returns_none() -> None:
    first = server._try_bind("127.0.0.1", 0)
    assert first is not None
    port = first.getsockname()[1]
    # Binding the SAME concrete port again should fail (SO_REUSEADDR doesn't let
    # two live listeners share it).
    second = server._try_bind("127.0.0.1", port)
    first.close()
    assert second is None


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


# ── server: _acquire_ports_during_downtime lifecycle ────────────────────────────


def test_acquire_waits_until_downtime_then_binds(monkeypatch: pytest.MonkeyPatch) -> None:
    # At launch compute_space is UP and the ports are held by Caddy (bind fails).
    # The updater must keep waiting, NOT exit. Once compute_space goes down and a
    # bind succeeds, it returns the socket.
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
    # Downtime is observed once, then compute_space comes back before we grab a
    # port — nothing left to cover.
    seq = iter([False, True])
    monkeypatch.setattr(server, "_compute_space_ready", lambda: next(seq, True))
    monkeypatch.setattr(server, "_try_bind", lambda h, p: None)
    monkeypatch.setattr("openhost_system_agent.updater.server.time.sleep", lambda _: None)
    https, http = server._acquire_ports_during_downtime(ssl_ctx=object())  # type: ignore[arg-type]
    assert https is None and http is None


def test_acquire_gives_up_after_bind_window(monkeypatch: pytest.MonkeyPatch) -> None:
    # compute_space goes down but we never manage to grab the port (Caddy rebinds
    # faster than we retry). After the bind-wait window we give up, not spin.
    monkeypatch.setattr(server, "_BIND_WAIT_SECONDS", 0.02)
    monkeypatch.setattr(server, "_compute_space_ready", lambda: False)
    monkeypatch.setattr(server, "_try_bind", lambda h, p: None)
    monkeypatch.setattr("openhost_system_agent.updater.server.time.sleep", lambda _: None)
    https, http = server._acquire_ports_during_downtime(ssl_ctx=object())  # type: ignore[arg-type]
    assert https is None and http is None


def test_acquire_closes_partial_bind_on_giveup(monkeypatch: pytest.MonkeyPatch) -> None:
    # We grab port 80 but never 443 (443 requires TLS which we hold). On giving up
    # we must close the 80 socket rather than leak it.
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


# ── server: run() lifecycle ─────────────────────────────────────────────────────


def test_run_serves_then_releases_when_ready(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cert = data_dir / "openhost-tls-cert.pem"
    key = data_dir / "openhost-tls-key.pem"
    _self_signed(cert, key)
    real = server._try_bind("127.0.0.1", 0)
    assert real is not None
    port = int(real.getsockname()[1])
    monkeypatch.setattr(server, "_acquire_ports_during_downtime", lambda ctx: (real, None))
    monkeypatch.setattr(server, "_compute_space_ready", lambda: True)  # already back
    monkeypatch.setattr("openhost_system_agent.updater.server.time.sleep", lambda _: None)
    server.run(cert, key)
    # The port must actually be free again (the wrapped listener was closed);
    # checking fileno() on `real` would be vacuous — wrap_socket detaches it.
    rebound = server._try_bind("127.0.0.1", port)
    assert rebound is not None
    rebound.close()


def test_run_returns_when_no_ports_acquired(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_make_ssl_context", lambda *a: None)
    monkeypatch.setattr(server, "_acquire_ports_during_downtime", lambda ctx: (None, None))
    start = time.monotonic()
    server.run(data_dir / "c.pem", data_dir / "c.key")
    assert time.monotonic() - start < 2


def test_page_is_snapshotted_not_reread_per_request(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The page files live in the source tree the update walk rewrites. Reading
    # them per request could serve a file caught mid-write (git writes
    # non-atomically) or drift if the tree moves again while we're still up.
    body = tmp_path / "_update_progress_body.html"
    body.write_text("<h1>Updating this instance</h1>", encoding="utf-8")
    monkeypatch.setattr(server, "_BODY_PATH", body)
    monkeypatch.setattr(server, "_page_snapshot", None)

    assert b"Updating this instance" in server.snapshot_page()

    body.write_text("<h1>truncated mid-checkou", encoding="utf-8")
    assert b"Updating this instance" in server._page()

    body.unlink()
    assert b"Updating this instance" in server._page()


# ── token persistence ───────────────────────────────────────────────────────────


def test_write_and_clear_token(data_dir: Path) -> None:
    paths.write_token("mytoken")
    p = paths.token_path()
    assert p.read_text() == "mytoken"
    assert (p.stat().st_mode & 0o777) == 0o600
    paths.clear_token()
    assert not p.exists()
    # Idempotent.
    paths.clear_token()


def test_set_token_resets_stale_progress(data_dir: Path) -> None:
    # A prior run's terminal "done" must be cleared when a new token is set, so
    # the /updating page's first poll doesn't see stale terminal state and bounce.
    progress.record("done", "old run")
    assert progress.is_terminal(progress.read_entries()) is True

    UpdaterCmd().set_token("freshtoken")

    assert progress.read_entries() == []  # log cleared
    assert paths.token_path().read_text() == "freshtoken"


# ── launcher ─────────────────────────────────────────────────────────────────────


def test_launch_updater_no_systemd_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openhost_system_agent.updater.launcher.shutil.which", lambda _: None)
    assert launcher.launch_updater() is False


def test_launch_updater_success(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openhost_system_agent.updater.launcher.shutil.which", lambda _: "/usr/bin/systemd-run")
    monkeypatch.setattr(launcher, "stop_updater", lambda: None)

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


def test_launch_updater_systemd_run_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openhost_system_agent.updater.launcher.shutil.which", lambda _: "/usr/bin/systemd-run")
    monkeypatch.setattr(launcher, "stop_updater", lambda: None)

    class _Fail:
        returncode = 1
        stderr = "unit exists"

    monkeypatch.setattr("openhost_system_agent.updater.launcher.subprocess.run", lambda *a, **k: _Fail())
    assert launcher.launch_updater() is False


def test_launch_updater_never_raises_on_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openhost_system_agent.updater.launcher.shutil.which", lambda _: "/usr/bin/systemd-run")
    monkeypatch.setattr(launcher, "stop_updater", lambda: None)

    def _boom(*a, **k):  # type: ignore[no-untyped-def]
        raise OSError("nope")

    monkeypatch.setattr("openhost_system_agent.updater.launcher.subprocess.run", _boom)
    assert launcher.launch_updater() is False


def test_launch_updater_command_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openhost_system_agent.updater.launcher.shutil.which", lambda _: "/usr/bin/systemd-run")
    monkeypatch.setattr(launcher, "stop_updater", lambda: None)
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
    # Transient service (no --scope) so systemd-run returns immediately instead of
    # blocking on the long-lived server, with its own --unit.
    assert "--scope" not in cmd
    assert any(a.startswith("--unit=") for a in cmd)
    # Runs the CLI entrypoint via `python -c` (not `-m`, which mis-dispatches
    # under __main__), invoking `updater serve`.
    joined = " ".join(cmd)
    assert "-c" in cmd
    assert "updater" in joined and "serve" in joined


# ── launcher: stop_updater (handoff release) ────────────────────────────────────


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
    assert calls[0][2] == launcher._UPDATER_UNIT


def test_stop_updater_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise OSError("systemctl gone")

    monkeypatch.setattr("openhost_system_agent.updater.launcher.subprocess.run", boom)
    launcher.stop_updater()  # must not raise


def test_updater_unit_gets_a_restart_policy(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    # The updater is the only thing serving 80/443 for the length of the apply
    # now, and systemd-run units default to Restart=no. Without a policy a crash
    # would leave the instance unreachable until the walk finished.
    calls: list[list[str]] = []

    def _run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(launcher, "_systemd_run_available", lambda: True)
    monkeypatch.setattr("openhost_system_agent.updater.launcher.subprocess.run", _run)
    monkeypatch.setattr(launcher, "_READY_WAIT_SECONDS", 0.01)

    launcher.launch_updater()

    systemd_run = next(c for c in calls if c and c[0] == "systemd-run")
    assert "--property=Restart=on-failure" in systemd_run


def test_mark_boot_complete_finalizes_an_interrupted_walk(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # REGRESSION: a walk killed mid-flight (OOM, SIGKILL, reboot) leaves the log
    # on a non-terminal phase. Nothing else ever finalizes it, and the page only
    # stops polling on a terminal entry -- so the owner's tab spun forever against
    # an instance that was already healthy.
    progress.reset_progress()
    progress.record("migrate", "Applying system migrations…")
    monkeypatch.setattr("openhost_system_agent.updater.progress.apply_is_running", lambda: False)

    assert progress.mark_boot_complete() is True

    entries = progress.read_entries()
    assert progress.is_terminal(entries) is True
    assert entries[-1]["phase"] == progress.PHASE_FAILED
    assert "interrupted" in str(entries[-1]["message"]).lower()


def test_mark_boot_complete_leaves_a_live_walks_log_alone(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An operator starting openhost mid-walk boots us while the apply is still
    # running; its log must not be terminated under it.
    progress.reset_progress()
    progress.record("migrate", "Applying system migrations…")
    monkeypatch.setattr("openhost_system_agent.updater.progress.apply_is_running", lambda: True)

    assert progress.mark_boot_complete() is True

    entries = progress.read_entries()
    assert [e["phase"] for e in entries] == ["migrate"]
    assert progress.is_terminal(entries) is False


def test_mark_boot_complete_still_completes_a_normal_restart(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    progress.reset_progress()
    progress.record(progress.PHASE_RESTARTING, "Update complete. Starting…")
    monkeypatch.setattr(
        "openhost_system_agent.updater.progress.apply_is_running",
        lambda: pytest.fail("a restarting log needs no unit check"),
    )

    assert progress.mark_boot_complete() is True
    assert progress.read_entries()[-1]["phase"] == progress.PHASE_DONE


def test_updater_503s_carry_retry_after(
    server_factory: Callable[[str | None, list[dict[str, object]]], int],
) -> None:
    # Every app on the instance answers 503 for the length of the apply, so API
    # clients and monitors need a hint rather than a bare refusal. All three 503
    # paths must carry it: /health, a non-document GET, and an unauthorized log read.
    port = server_factory("tok", [])
    for path, extra in (("/health", ""), ("/", "Sec-Fetch-Dest: empty\r\n"), ("/updates", "")):
        status, headers, _body = _request(port, path, extra)
        assert status in (403, 503), f"{path} -> {status}"
        if status == 503:
            assert headers.get("retry-after") == str(server._RETRY_AFTER_SECONDS), path


def test_updater_gives_up_when_the_service_never_goes_down(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # If the apply died before its stop (or the stop failed), compute_space stays
    # up and there is no downtime to cover. The updater must exit rather than idle:
    # a lingering one would grab 80/443 during the NEXT unrelated restart and serve
    # an update page for an update that is not happening.
    monkeypatch.setattr(server, "_compute_space_ready", lambda: True)
    monkeypatch.setattr(server, "_DOWNTIME_WAIT_SECONDS", 0.05)
    monkeypatch.setattr("openhost_system_agent.updater.server.time.sleep", lambda _s: None)
    monkeypatch.setattr(server, "_try_bind", lambda *_a: pytest.fail("must not bind while the router is up"))

    https_sock, http_sock = server._acquire_ports_during_downtime(None)

    assert https_sock is None and http_sock is None


def test_updater_clears_its_ready_marker_when_it_acquires_nothing(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The marker means "poised to bind"; leaving it behind after giving up misleads
    # the next launcher, which waits on it.
    monkeypatch.setattr(server, "_make_ssl_context", lambda *_a: None)
    monkeypatch.setattr(server, "_acquire_ports_during_downtime", lambda _ctx: (None, None))
    paths.ready_marker_path().parent.mkdir(parents=True, exist_ok=True)
    paths.ready_marker_path().write_text("")

    server.run(data_dir / "c.pem", data_dir / "c.key")

    assert not paths.ready_marker_path().exists()
