"""Edge-case coverage for the seamless-update updater (server, launcher, progress,
paths, token). Complements test_updater.py with adversarial / boundary inputs."""

from __future__ import annotations

import datetime as _dt
import json
import os
import socket
import ssl
import subprocess
import time
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


def test_read_entries_missing_file(data_dir: Path) -> None:
    # No log written yet.
    assert progress.read_entries() == []


def test_read_entries_empty_file(data_dir: Path) -> None:
    paths.progress_log_path().write_text("")
    assert progress.read_entries() == []


def test_read_entries_only_whitespace(data_dir: Path) -> None:
    paths.progress_log_path().write_text("\n  \n\t\n")
    assert progress.read_entries() == []


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


def test_read_entries_non_dict_json_skipped(data_dir: Path) -> None:
    # A JSON array / string / number on a line must be ignored (only dict entries).
    paths.progress_log_path().write_text('[1,2,3]\n"a string"\n42\n' + json.dumps({"phase": "x"}) + "\n")
    entries = progress.read_entries()
    assert [e["phase"] for e in entries] == ["x"]


def test_read_entries_unicode_message(data_dir: Path) -> None:
    progress.record("fetch", "Fetching\u2026 café 日本語")
    entries = progress.read_entries()
    assert "café" in entries[0]["message"]  # type: ignore[operator]


def test_read_entries_crlf_line_endings(data_dir: Path) -> None:
    paths.progress_log_path().write_bytes(
        (json.dumps({"phase": "fetch"}) + "\r\n" + json.dumps({"phase": "done"}) + "\r\n").encode()
    )
    entries = progress.read_entries()
    assert [e["phase"] for e in entries] == ["fetch", "done"]


# ─────────────────────────── progress: is_terminal edge cases ────────────────


def test_is_terminal_empty() -> None:
    assert progress.is_terminal([]) is False


def test_is_terminal_no_phase_key() -> None:
    assert progress.is_terminal([{"message": "x"}]) is False


def test_is_terminal_done() -> None:
    assert progress.is_terminal([{"phase": "fetch"}, {"phase": "done"}]) is True


def test_is_terminal_failed() -> None:
    assert progress.is_terminal([{"phase": "failed"}]) is True


def test_is_terminal_only_last_matters() -> None:
    # A "done" mid-log followed by more work is NOT terminal (last wins).
    assert progress.is_terminal([{"phase": "done"}, {"phase": "install"}]) is False


def test_is_terminal_unknown_phase() -> None:
    assert progress.is_terminal([{"phase": "banana"}]) is False


# ─────────────────────────── progress: record / reset ────────────────────────


def test_record_appends_in_order(data_dir: Path) -> None:
    for i in range(10):
        progress.record(f"phase{i}", f"msg{i}")
    entries = progress.read_entries()
    assert [e["phase"] for e in entries] == [f"phase{i}" for i in range(10)]


def test_record_with_ref(data_dir: Path) -> None:
    progress.record("checkout", "Checking out", ref="v1.2.3")
    assert progress.read_entries()[0]["ref"] == "v1.2.3"


def test_record_without_ref_is_null(data_dir: Path) -> None:
    progress.record("fetch", "Fetching")
    assert progress.read_entries()[0]["ref"] is None


def test_reset_truncates(data_dir: Path) -> None:
    progress.record("fetch", "old")
    progress.reset_progress()
    assert progress.read_entries() == []


def test_reset_then_record_starts_fresh(data_dir: Path) -> None:
    progress.record("fetch", "old")
    progress.reset_progress()
    progress.record("fetch", "new")
    entries = progress.read_entries()
    assert len(entries) == 1 and entries[0]["message"] == "new"


def test_record_never_raises_unwritable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(paths._DATA_DIR_ENV, "/proc/x/y/z/cannot")
    progress.record("fetch", "no raise")  # must not raise


def test_reset_never_raises_unwritable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(paths._DATA_DIR_ENV, "/proc/x/y/z/cannot")
    progress.reset_progress()  # must not raise


def test_record_message_with_newline_stays_one_entry(data_dir: Path) -> None:
    # A message containing a newline must be JSON-escaped so it stays a single
    # JSONL line (not split into two entries).
    progress.record("fetch", "line1\nline2")
    entries = progress.read_entries()
    assert len(entries) == 1
    assert entries[0]["message"] == "line1\nline2"


# ─────────────────────────── paths / token ───────────────────────────────────


def test_write_token_roundtrip(data_dir: Path) -> None:
    paths.write_token("abc")
    assert paths.token_path().read_text() == "abc"


def test_write_token_permissions_0600(data_dir: Path) -> None:
    paths.write_token("abc")
    assert (paths.token_path().stat().st_mode & 0o777) == 0o600


def test_write_token_overwrites(data_dir: Path) -> None:
    paths.write_token("first")
    paths.write_token("second")
    assert paths.token_path().read_text() == "second"


def test_write_token_creates_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # updater dir does NOT exist yet.
    monkeypatch.setenv(paths._DATA_DIR_ENV, str(tmp_path / "fresh"))
    paths.write_token("abc")
    assert paths.token_path().read_text() == "abc"


def test_clear_token_removes(data_dir: Path) -> None:
    paths.write_token("abc")
    paths.clear_token()
    assert not paths.token_path().exists()


def test_clear_token_idempotent(data_dir: Path) -> None:
    paths.clear_token()
    paths.clear_token()  # no raise even when absent


def test_token_with_urlsafe_chars(data_dir: Path) -> None:
    tok = "aB3-_xyz.token~value"
    paths.write_token(tok)
    assert paths.token_path().read_text() == tok


def test_ready_marker_paths(data_dir: Path) -> None:
    assert paths.ready_marker_path() == data_dir / "updater" / "serve.ready"


def test_data_dir_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(paths._DATA_DIR_ENV, str(tmp_path / "custom"))
    assert paths.data_dir() == tmp_path / "custom"


def test_cert_key_paths(data_dir: Path) -> None:
    assert paths.tls_cert_path().name == "openhost-tls-cert.pem"
    assert paths.tls_key_path().name == "openhost-tls-key.pem"


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


def test_server_correct_token_shows_logs(server_factory) -> None:  # type: ignore[no-untyped-def]
    port = server_factory("tok", [{"phase": "migrate", "message": "Migrating"}])
    status, body = _get(port, "/?token=tok")
    assert status == 200 and b"Updating this instance" in body


def test_server_wrong_token_shows_loading(server_factory) -> None:  # type: ignore[no-untyped-def]
    port = server_factory("tok", [])
    status, body = _get(port, "/?token=WRONG")
    assert status == 200 and b"This instance is updating" in body


def test_server_no_token_shows_loading(server_factory) -> None:  # type: ignore[no-untyped-def]
    port = server_factory("tok", [])
    status, body = _get(port, "/")
    assert status == 200 and b"This instance is updating" in body


def test_server_empty_token_param_shows_loading(server_factory) -> None:  # type: ignore[no-untyped-def]
    port = server_factory("tok", [])
    status, body = _get(port, "/?token=")
    assert b"This instance is updating" in body


def test_server_no_token_file_never_authes(server_factory) -> None:  # type: ignore[no-untyped-def]
    # No token file at all: even a matching-looking token can't auth.
    port = server_factory(None, [])
    status, body = _get(port, "/?token=anything")
    assert b"This instance is updating" in body


def test_server_updates_forbidden_without_token(server_factory) -> None:  # type: ignore[no-untyped-def]
    port = server_factory("tok", [{"phase": "done", "message": "d"}])
    status, _ = _get(port, "/updates")
    assert status == 403


def test_server_updates_forbidden_wrong_token(server_factory) -> None:  # type: ignore[no-untyped-def]
    port = server_factory("tok", [{"phase": "done", "message": "d"}])
    status, _ = _get(port, "/updates?token=nope")
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


def test_server_unknown_path_authed_shows_log_page(server_factory) -> None:  # type: ignore[no-untyped-def]
    # Any non-/updates path returns the update page (SPA-style catch-all).
    port = server_factory("tok", [])
    status, body = _get(port, "/some/deep/path?token=tok")
    assert status == 200 and b"Updating this instance" in body


def test_server_unknown_path_unauthed_shows_loading(server_factory) -> None:  # type: ignore[no-untyped-def]
    port = server_factory("tok", [])
    status, body = _get(port, "/random")
    assert b"This instance is updating" in body


def test_server_updates_reflects_live_append(server_factory) -> None:  # type: ignore[no-untyped-def]
    port = server_factory("tok", [{"phase": "fetch", "message": "f"}])
    s1, b1 = _get(port, "/updates?token=tok")
    assert len(json.loads(b1)["entries"]) == 1
    # Append while the server is live; next poll must reflect it.
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


def test_server_token_with_url_encoding(server_factory) -> None:  # type: ignore[no-untyped-def]
    # A token containing chars that need URL-encoding must match after decode.
    port = server_factory("a b+c", [])
    assert _get(port, "/updates?token=a%20b%2Bc")[0] == 200


# ─────────────────────────── ssl context edge cases ──────────────────────────


def test_make_ssl_context_missing_both(tmp_path: Path) -> None:
    assert server._make_ssl_context(tmp_path / "no.pem", tmp_path / "no.key") is None


def test_make_ssl_context_missing_key(tmp_path: Path) -> None:
    cert = tmp_path / "c.pem"
    key = tmp_path / "k.pem"
    _self_signed(cert, key)
    key.unlink()
    assert server._make_ssl_context(cert, key) is None


def test_make_ssl_context_garbage_cert(tmp_path: Path) -> None:
    cert = tmp_path / "c.pem"
    key = tmp_path / "k.pem"
    cert.write_text("not a cert")
    key.write_text("not a key")
    assert server._make_ssl_context(cert, key) is None


def test_make_ssl_context_valid(tmp_path: Path) -> None:
    cert = tmp_path / "c.pem"
    key = tmp_path / "k.pem"
    _self_signed(cert, key)
    assert server._make_ssl_context(cert, key) is not None


# ─────────────────────────── bind edge cases ─────────────────────────────────


def test_try_bind_ephemeral_ok() -> None:
    s = server._try_bind("127.0.0.1", 0)
    assert s is not None
    s.close()


def test_try_bind_conflict_returns_none() -> None:
    s = server._try_bind("127.0.0.1", 0)
    assert s is not None
    port = s.getsockname()[1]
    assert server._try_bind("127.0.0.1", port) is None
    s.close()


def test_try_bind_privileged_port_without_root_returns_none() -> None:
    # As non-root, binding :443 fails and returns None (not raises). Skip if root.
    if os.geteuid() == 0:
        pytest.skip("running as root can bind privileged ports")
    assert server._try_bind("0.0.0.0", 443) is None


# ─────────────────────────── compute_space readiness ─────────────────────────


def test_compute_space_ready_true_when_listening(monkeypatch: pytest.MonkeyPatch) -> None:
    lis = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lis.bind(("127.0.0.1", 0))
    lis.listen(1)
    monkeypatch.setattr(server, "_COMPUTE_SPACE_PORT", lis.getsockname()[1])
    try:
        assert server._compute_space_ready() is True
    finally:
        lis.close()


def test_compute_space_ready_false_when_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setattr(server, "_COMPUTE_SPACE_PORT", port)
    assert server._compute_space_ready() is False


# ─────────────────────────── acquire_ports lifecycle ─────────────────────────


def test_acquire_binds_immediately_when_free(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = object()
    monkeypatch.setattr(server, "_try_bind", lambda h, p: fake if p == 443 else None)
    monkeypatch.setattr(server, "_compute_space_ready", lambda: False)
    monkeypatch.setattr("openhost_system_agent.updater.server.time.sleep", lambda _: None)
    https, http = server._acquire_ports_during_downtime(ssl_ctx=object())  # type: ignore[arg-type]
    assert https is fake


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


def test_run_returns_when_no_ports(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_make_ssl_context", lambda *a: None)
    monkeypatch.setattr(server, "_acquire_ports_during_downtime", lambda ctx: (None, None))
    start = time.monotonic()
    server.run(data_dir / "c.pem", data_dir / "c.key")
    assert time.monotonic() - start < 3


def test_run_serves_then_releases_when_ready(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Bind a real ephemeral socket, then make compute_space "ready" so run()
    # serves briefly and then releases the socket.
    cert = data_dir / "openhost-tls-cert.pem"
    key = data_dir / "openhost-tls-key.pem"
    _self_signed(cert, key)
    real = server._try_bind("127.0.0.1", 0)
    assert real is not None
    monkeypatch.setattr(server, "_acquire_ports_during_downtime", lambda ctx: (real, None))
    monkeypatch.setattr(server, "_compute_space_ready", lambda: True)  # already back
    monkeypatch.setattr("openhost_system_agent.updater.server.time.sleep", lambda _: None)
    server.run(cert, key)
    # After run() returns, the listening socket must be closed (released).
    assert real.fileno() == -1


def test_run_writes_and_clears_ready_marker(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # run() touches the ready marker (so the launcher can proceed) and clears it
    # on exit. Force the no-ports path so it returns fast, but the marker is
    # touched inside _acquire via the real path — here we call the touch directly.
    server._touch_ready_marker()
    assert paths.ready_marker_path().exists()
    server._clear_ready_marker()
    assert not paths.ready_marker_path().exists()


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

    # Simulate the scope reaching its bind loop by touching the marker.
    def _run(cmd, **kw):  # type: ignore[no-untyped-def]
        paths.ready_marker_path().parent.mkdir(parents=True, exist_ok=True)
        paths.ready_marker_path().write_text("")
        return _Ok()

    monkeypatch.setattr("openhost_system_agent.updater.launcher.subprocess.run", _run)
    monkeypatch.setattr("openhost_system_agent.updater.launcher.time.sleep", lambda _: None)
    assert launcher.launch_updater() is True


def test_launcher_success_even_if_never_ready(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # If the marker never appears, launch still returns True (best-effort) so the
    # restart is not stalled — but only after the bounded wait.
    monkeypatch.setattr("openhost_system_agent.updater.launcher.shutil.which", lambda _: "/usr/bin/systemd-run")
    monkeypatch.setattr(launcher, "_reset_stale_scope", lambda: None)
    monkeypatch.setattr(launcher, "_READY_WAIT_SECONDS", 0.05)

    class _Ok:
        returncode = 0
        stderr = ""

    monkeypatch.setattr("openhost_system_agent.updater.launcher.subprocess.run", lambda *a, **k: _Ok())
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


def test_launcher_timeout_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openhost_system_agent.updater.launcher.shutil.which", lambda _: "/usr/bin/systemd-run")
    monkeypatch.setattr(launcher, "_reset_stale_scope", lambda: None)

    def _boom(*a, **k):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(cmd="systemd-run", timeout=15)

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
    assert "--scope" in cmd
    assert cmd[-2:] == ["updater", "serve"]
    # Must run the agent CLI module, in its own unit.
    assert any(a.startswith("--unit=") for a in cmd)


# ─────────────────────────── _read_token_file direct ─────────────────────────


def test_read_token_file_missing(data_dir: Path) -> None:
    assert server._read_token_file() is None


def test_read_token_file_empty_is_none(data_dir: Path) -> None:
    paths.write_token("")
    assert server._read_token_file() is None


def test_read_token_file_strips_whitespace(data_dir: Path) -> None:
    paths.token_path().write_text("  tok\n")
    assert server._read_token_file() == "tok"
