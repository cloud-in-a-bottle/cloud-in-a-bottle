import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[4]
_SCRIPTS = _ROOT / "scripts"
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Bootstrap scripts target Linux")


@contextmanager
def _server(statuses: list[int], body: bytes) -> Iterator[tuple[str, list[str]]]:
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            status = statuses[min(len(requests) - 1, len(statuses) - 1)]
            response = body if status == 200 else b"download unavailable"
            self.send_response(status)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/archive", requests
        finally:
            server.shutdown()
            thread.join(timeout=5)


def _download(url: str, payload: bytes, destination: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPTS / "download_verified.sh"), url, hashlib.sha256(payload).hexdigest(), str(destination)],
        env={**os.environ, "NO_PROXY": "127.0.0.1"},
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_download_recovers_from_gateway_errors_and_reuses_verified_cache(tmp_path: Path) -> None:
    destination = tmp_path / "cache with spaces/archive"
    payload = b"complete pinned archive"
    with _server([504, 502, 200], payload) as (url, requests):
        result = _download(url, payload, destination)
        assert result.returncode == 0, result.stderr
        assert len(requests) == 3
        assert destination.read_bytes() == payload
        cached = _download(url, payload, destination)
        assert cached.returncode == 0, cached.stderr
        assert len(requests) == 3
    assert list(destination.parent.iterdir()) == [destination]


@pytest.mark.parametrize("status", [200, 404])
def test_failed_download_preserves_existing_file_and_removes_partial_output(tmp_path: Path, status: int) -> None:
    destination = tmp_path / "archive"
    destination.write_bytes(b"previous archive")
    with _server([status], b"wrong content") as (url, requests):
        result = _download(url, b"expected archive", destination)
    assert result.returncode != 0
    if status == 200:
        assert "SHA256 mismatch" in result.stderr
    assert len(requests) == 1
    assert destination.read_bytes() == b"previous archive"
    assert list(tmp_path.iterdir()) == [destination]


def test_corrupt_cache_is_replaced_by_a_verified_download(tmp_path: Path) -> None:
    destination = tmp_path / "archive"
    destination.write_bytes(b"corrupt cache")
    with _server([200], b"correct archive") as (url, requests):
        result = _download(url, b"correct archive", destination)
    assert result.returncode == 0, result.stderr
    assert len(requests) == 1
    assert destination.read_bytes() == b"correct archive"


def _installer(tmp_path: Path, version: str, arch: str = "x86_64") -> tuple[Path, dict[str, str]]:
    fixture = tmp_path / "installer"
    fixture.mkdir()
    script = fixture / "install_pixi.sh"
    shutil.copyfile(_SCRIPTS / "install_pixi.sh", script)
    archive = tmp_path / "fixture.tar.gz"
    executable = f"#!/bin/sh\necho 'pixi {version}'\n".encode()
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("pixi")
        member.size = len(executable)
        member.mode = 0o755
        tar.addfile(member, io.BytesIO(executable))
    # Exercise installation independently of the downloader, which is tested with
    # real HTTP above. Record its inputs to verify the requested release and pin.
    (fixture / "download_verified.sh").write_text(
        '#!/bin/bash\nset -eu\nprintf "%s\\n" "$1" "$2" > "$DOWNLOAD_ARGS"\n'
        'mkdir -p "$(dirname "$3")"\ncp "$TEST_ARCHIVE" "$3"\n'
    )
    uname = fixture / "uname"
    uname.write_text('#!/bin/sh\nif [ "$1" = -s ]; then echo Linux; else echo "$TEST_ARCH"; fi\n')
    uname.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fixture}:{os.environ['PATH']}",
        "TEST_ARCH": arch,
        "TEST_ARCHIVE": str(archive),
        "DOWNLOAD_ARGS": str(tmp_path / "download-args"),
        "PIXI_BIN_DIR": str(tmp_path / "bin"),
        "PIXI_CACHE_DIR": str(tmp_path / "cache"),
    }
    return script, env


@pytest.mark.parametrize("version", ["0.69.0", "0.70.2"])
@pytest.mark.parametrize("arch", ["x86_64", "aarch64", "arm64"])
def test_installer_keeps_version_and_architecture_pins(tmp_path: Path, version: str, arch: str) -> None:
    script, env = _installer(tmp_path, version, arch)
    result = subprocess.run(["bash", str(script), f"v{version}"], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    url, digest = (tmp_path / "download-args").read_text().splitlines()
    asset_arch = "aarch64" if arch == "arm64" else arch
    assert (
        url
        == f"https://github.com/prefix-dev/pixi/releases/download/v{version}/pixi-{asset_arch}-unknown-linux-musl.tar.gz"
    )
    assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
    binary = tmp_path / "bin/pixi"
    assert subprocess.check_output([str(binary), "--version"], text=True).strip() == f"pixi {version}"
    assert list(binary.parent.iterdir()) == [binary]


@pytest.mark.parametrize("failure", ["download", "wrong-version", "invalid-archive"])
def test_failed_install_keeps_existing_binary(tmp_path: Path, failure: str) -> None:
    script, env = _installer(tmp_path, "0.69.0")
    binary = tmp_path / "bin/pixi"
    binary.parent.mkdir()
    binary.write_text("existing binary")
    if failure == "download":
        (script.parent / "download_verified.sh").write_text("#!/bin/bash\nexit 22\n")
    elif failure == "invalid-archive":
        (tmp_path / "fixture.tar.gz").write_bytes(b"truncated archive")
    result = subprocess.run(["bash", str(script), "v0.70.2"], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert binary.read_text() == "existing binary"
    assert list(binary.parent.iterdir()) == [binary]


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="Requires the Ansible CLI")
@pytest.mark.parametrize("valid_checksum", [False, True])
def test_coredns_download_task_retries_and_checks_integrity(tmp_path: Path, valid_checksum: bool) -> None:
    tasks = yaml.safe_load((_ROOT / "ansible/tasks/coredns.yml").read_text())
    download = next(task for task in tasks if "get_url" in task)
    payload = b"complete archive"
    destination = tmp_path / "coredns.tgz"
    installed = tmp_path / "installed"
    with _server([504, 200], payload) as (url, requests):
        download["become"] = False
        download["get_url"].update(url=url, dest=str(destination))
        download["delay"] = 0
        play = [
            {
                "hosts": "localhost",
                "connection": "local",
                "become": False,
                "gather_facts": False,
                "vars": {
                    "ansible_python_interpreter": sys.executable,
                    "coredns_arch": "amd64",
                    "coredns_sha256": {
                        "amd64": hashlib.sha256(payload if valid_checksum else b"other archive").hexdigest()
                    },
                },
                "tasks": [download, {"copy": {"content": "installed", "dest": str(installed)}}],
            }
        ]
        playbook = tmp_path / "playbook.yml"
        playbook.write_text(yaml.safe_dump(play))
        result = subprocess.run(
            ["ansible-playbook", "-i", "localhost,", str(playbook)],
            env={
                **os.environ,
                "NO_PROXY": "127.0.0.1",
                "ANSIBLE_LOCAL_TEMP": str(tmp_path / "local"),
                "ANSIBLE_REMOTE_TEMP": str(tmp_path / "remote"),
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
    if valid_checksum:
        assert result.returncode == 0, result.stdout + result.stderr
        assert len(requests) == 2
        assert destination.read_bytes() == payload
        assert installed.exists()
    else:
        assert result.returncode != 0
        assert len(requests) > 2
        assert not destination.exists()
        assert not installed.exists()
