"""Hammer the updater's request handling 100x across varied states to prove that
during the downtime window EVERY request gets a proper, styled page or a valid
JSON/health response — never a raw failure/dead page (the only unavoidable gap is
the sub-second port switch, which is outside the server's request handling)."""

from __future__ import annotations

import datetime as _dt
import json
import socket
import ssl

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from openhost_system_agent.updater import paths
from openhost_system_agent.updater import progress
from openhost_system_agent.updater import server


def _self_signed(cert_path, key_path) -> None:  # type: ignore[no-untyped-def]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
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


# 100 iterations, each cycling through different states (token set/unset, empty
# log / mid-update / terminal) and a spread of request paths a browser or random
# visitor might hit during downtime.
_PATHS = [
    "/",
    "/updating?token={tok}",
    "/updating?token=WRONG",
    "/updating",
    "/settings",
    "/random/deep/path",
    "/favicon.ico",
    "/?token={tok}",
]

_STATES = [
    ("tok", []),
    ("tok", [{"phase": "fetch", "message": "Fetching"}]),
    ("tok", [{"phase": "migrate", "message": "Migrating"}, {"phase": "done", "message": "Done"}]),
    (None, []),  # no token file
    ("tok", [{"phase": "failed", "message": "boom"}]),
]


@pytest.mark.parametrize("iteration", range(100))
def test_updater_never_serves_failure_page(iteration: int, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv(paths._DATA_DIR_ENV, str(tmp_path))
    (tmp_path / "updater").mkdir(parents=True, exist_ok=True)

    token, entries = _STATES[iteration % len(_STATES)]
    if token is not None:
        paths.write_token(token)
    progress.reset_progress()
    for e in entries:
        progress.record(str(e["phase"]), str(e["message"]))

    cert = tmp_path / "openhost-tls-cert.pem"
    key = tmp_path / "openhost-tls-key.pem"
    _self_signed(cert, key)
    ctx = server._make_ssl_context(cert, key)
    assert ctx is not None

    sock = server._try_bind("127.0.0.1", 0)
    assert sock is not None
    port = int(sock.getsockname()[1])
    httpd = server._serve_on(sock, ctx)
    try:
        tok = token or "notoken"
        for template in _PATHS:
            path = template.format(tok=tok)
            status, body = _get(port, path)
            if path.split("?")[0] == "/health":
                continue
            # Every non-/updates path must return a real, styled updating page
            # (200) — never a 5xx, never an empty body.
            assert status == 200, f"iter={iteration} path={path} status={status}"
            assert b"Updating this instance" in body, f"iter={iteration} path={path} body missing heading"
            assert len(body) > 200, f"iter={iteration} path={path} body too small"

        # /health must always be 503 while the updater owns the port.
        hstatus, _ = _get(port, "/health")
        assert hstatus == 503

        # /updates must be valid JSON: 200 with the right shape when the token
        # matches, 403 otherwise. Never a 5xx / dead response.
        ustatus, ubody = _get(port, f"/updates?token={tok}")
        if token is not None and tok == token:
            assert ustatus == 200
            payload = json.loads(ubody)
            assert "entries" in payload and "terminal" in payload
        else:
            assert ustatus == 403

        # A wrong/absent token on /updates is always 403 (never a leak, never 5xx).
        wstatus, _ = _get(port, "/updates?token=definitely-wrong")
        assert wstatus == 403
    finally:
        httpd.shutdown()
        httpd.server_close()
