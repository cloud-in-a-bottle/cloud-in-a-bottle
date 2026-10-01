"""Real-process regressions for router output backpressure and forced shutdown."""

import signal
import sys
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from threading import Timer

import pytest
import requests

from compute_space.tests import conftest
from compute_space.tests import utils

_ROUTER = """
import signal
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

signal.signal(signal.SIGTERM, signal.SIG_IGN)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/noisy':
            sys.stderr.write('stderr\\n' * 32768)
            sys.stderr.flush()
            sys.stdout.write('stdout\\n' * 32768)
            sys.stdout.flush()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'ok')

    def log_message(self, *args):
        pass

HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
"""


@contextmanager
def _isolated_router(base_url, env):
    proc = conftest._start_router_process(base_url, env)
    try:
        yield proc
    finally:
        conftest._stop_router_process(proc)


@pytest.mark.parametrize("managed", [False, True], ids=["isolated", "managed"])
def test_noisy_router_exits_even_when_sigterm_is_ignored(tmp_path, unused_tcp_port, monkeypatch, managed):
    config, env = conftest._make_config_and_env(tmp_path, port=unused_tcp_port, default_apps=[])
    command = [sys.executable, "-u", "-c", _ROUTER, str(unused_tcp_port)]
    monkeypatch.setattr(conftest, "router_cmd", lambda: command)
    monkeypatch.setattr(utils, "router_cmd", lambda: command)
    base_url = f"http://127.0.0.1:{unused_tcp_port}"
    router = utils.managed_router(config) if managed else _isolated_router(base_url, env)
    rescued = Event()
    watchdog = None
    try:
        with router as proc:
            # Bound the regression itself when run against the broken cleanup.
            def rescue():
                rescued.set()
                proc.kill()

            watchdog = Timer(15, rescue)
            watchdog.daemon = True
            watchdog.start()
            assert requests.get(f"{base_url}/noisy", timeout=3).status_code == 200
        assert not rescued.is_set(), "Router cleanup needed the test's emergency SIGKILL"
        assert proc.returncode == -signal.SIGKILL
    finally:
        if watchdog is not None:
            watchdog.cancel()

    log_dir = Path(config.temporary_data_dir) if managed else tmp_path
    log = (log_dir / "router.log").read_text()
    assert "stderr\n" * 32768 in log
    assert "stdout\n" * 32768 in log


def test_router_startup_failure_preserves_output(tmp_path, unused_tcp_port, monkeypatch):
    monkeypatch.setattr(
        conftest, "router_cmd", lambda: [sys.executable, "-c", "import sys; sys.stderr.write('startup failed\\n')"]
    )
    with pytest.raises(RuntimeError, match="startup failed"):
        conftest._start_router_process(
            f"http://127.0.0.1:{unused_tcp_port}",
            {"OPENHOST_ROUTER_CONFIG": str(tmp_path / "config.toml")},
            startup_timeout=1,
        )
