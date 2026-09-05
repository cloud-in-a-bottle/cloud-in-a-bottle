"""Unit tests for compute_space.core.containers.

These tests mock ``subprocess`` so they run without a live podman daemon —
end-to-end tests that actually exercise podman live under the
``@requires_containers`` marker.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from compute_space.core import containers
from compute_space.core.containers import DEFAULT_CAPABILITIES
from compute_space.core.containers import _bind_mount_arg
from compute_space.core.containers import build_image
from compute_space.core.containers import get_docker_logs
from compute_space.core.containers import image_exists
from compute_space.core.containers import is_container_running
from compute_space.core.containers import remove_image
from compute_space.core.containers import run_container
from compute_space.core.containers import stop_container
from compute_space.core.manifest import AppManifest
from compute_space.core.manifest import PortMapping
from compute_space.core.manifest import parse_manifest_from_string


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_subprocess_run(monkeypatch: pytest.MonkeyPatch, handler):
    """Replace subprocess.run globally with a callable that records + returns."""
    monkeypatch.setattr(subprocess, "run", handler)


# ---------------------------------------------------------------------------
# build_image
# ---------------------------------------------------------------------------


def test_build_image_uses_podman_build(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return _FakeCompleted(0, stdout="")

    _patch_subprocess_run(monkeypatch, fake_run)

    tag = build_image("myapp", "/tmp/repo", "Dockerfile", temp_data_dir=None)
    assert tag == "openhost-myapp:latest"
    assert calls[0] == [
        "podman",
        "build",
        "-t",
        "openhost-myapp:latest",
        "-f",
        "/tmp/repo/Dockerfile",
        "/tmp/repo",
    ]


def test_build_image_applies_memory_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return _FakeCompleted(0, stdout="")

    _patch_subprocess_run(monkeypatch, fake_run)

    build_image("myapp", "/tmp/repo", "Dockerfile", temp_data_dir=None, memory_mb=512)
    # --memory caps build RAM; --memory-swap set equal to --memory gives the
    # build zero swap (the combined memory+swap ceiling == the memory limit).
    # Both go to `podman build`, before the build context path.
    assert calls[0] == [
        "podman",
        "build",
        "-t",
        "openhost-myapp:latest",
        "-f",
        "/tmp/repo/Dockerfile",
        "--memory=512m",
        "--memory-swap=512m",
        "/tmp/repo",
    ]


def test_build_image_omits_memory_flag_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return _FakeCompleted(0, stdout="")

    _patch_subprocess_run(monkeypatch, fake_run)

    build_image("myapp", "/tmp/repo", "Dockerfile", temp_data_dir=None)
    # No memory limit → neither --memory nor --memory-swap is passed.
    assert not any(arg.startswith("--memory") for arg in calls[0])


@pytest.mark.parametrize(
    "fragment",
    [
        "error: content digest sha256:deadbeef: not found",
        "Error: storage-driver errored: something happened",
        "Error: layer not known: sha256:whatever",
        # Overlay short-name "link" corruption (empty link file): buildah
        # fails mounting the next build layer.  Observed on rootless podman
        # after a concurrent initial build.
        "Error: identifier is not a container: preparing container for next step: "
        'mounting new container "e156": readlink '
        "/home/host/.local/share/containers/storage/overlay/l: invalid argument",
        # Same corruption surfacing at container start (bare overlay dir).
        "Error: readlink /home/host/.local/share/containers/storage/overlay: invalid argument",
    ],
)
def test_build_image_detects_every_known_cache_corrupt_fragment(
    monkeypatch: pytest.MonkeyPatch, fragment: str
) -> None:
    """Every substring in _BUILD_CACHE_CORRUPT_FRAGMENTS must trigger the
    BUILD_CACHE_CORRUPT marker via the non-streaming build code path
    (no temp_data_dir) so the dashboard's 'drop cache' toast offers a
    remediation, not a blind retry."""

    def fake_run(cmd, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        return _FakeCompleted(1, stderr=fragment)

    _patch_subprocess_run(monkeypatch, fake_run)

    with pytest.raises(RuntimeError) as exc_info:
        build_image("myapp", "/tmp/repo", "Dockerfile", temp_data_dir=None)
    assert str(exc_info.value).startswith(containers.BUILD_CACHE_CORRUPT_MARKER)


@pytest.mark.parametrize(
    "fragment",
    [
        "error: content digest sha256:deadbeef: not found",
        "Error: storage-driver errored: something happened",
        "Error: layer not known: sha256:whatever",
        "Error: identifier is not a container: preparing container for next step: "
        'mounting new container "e156": readlink '
        "/home/host/.local/share/containers/storage/overlay/l: invalid argument",
        "Error: readlink /home/host/.local/share/containers/storage/overlay: invalid argument",
    ],
)
def test_build_image_streaming_path_detects_cache_corrupt(
    tmp_path, monkeypatch: pytest.MonkeyPatch, fragment: str
) -> None:
    """The streaming build path (temp_data_dir set) assembles build_output
    incrementally from Popen.stdout and then runs the same substring check.
    It needs its own regression coverage because the non-streaming code
    path uses a different subprocess API.
    """

    class _FakePopen:
        def __init__(self, *a, **_kw):  # type: ignore[no-untyped-def]
            # Every line of the "build" ends with a newline; the code under
            # test iterates over proc.stdout and appends to build_output.
            self.stdout = iter([fragment + "\n"])
            self.returncode = 1
            self.pid = 12345

        def wait(self, timeout: int | None = None) -> int:
            return self.returncode

        def kill(self) -> None:
            pass

        # Popen is used as a context manager in build_image's streaming
        # path so child processes are reaped and pipes closed even if
        # the body raises.  Mirror that here so the test fake behaves
        # like the real thing.
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_exc):  # type: ignore[no-untyped-def]
            return False

    monkeypatch.setattr("compute_space.core.containers.subprocess.Popen", _FakePopen)

    temp_data_dir = str(tmp_path / "temp")
    os.makedirs(os.path.join(temp_data_dir, "app_temp_data", "myapp"), exist_ok=True)

    with pytest.raises(RuntimeError) as exc_info:
        build_image("myapp", "/tmp/repo", "Dockerfile", temp_data_dir=temp_data_dir)
    assert str(exc_info.value).startswith(containers.BUILD_CACHE_CORRUPT_MARKER)


def test_build_image_generic_failure_does_not_use_cache_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(cmd, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        long_line = "x" * 200
        return _FakeCompleted(1, stderr=f"Step 1/2\n{long_line}\n\n")

    _patch_subprocess_run(monkeypatch, fake_run)

    with pytest.raises(RuntimeError) as exc_info:
        build_image("myapp", "/tmp/repo", "Dockerfile", temp_data_dir=None)
    msg = str(exc_info.value)
    assert containers.BUILD_CACHE_CORRUPT_MARKER not in msg
    assert msg == f"Container build failed (exit code 1):\n...{'x' * 150}"

    class _FakePopen:
        def __init__(self, *a, **_kw):  # type: ignore[no-untyped-def]
            self.stdout = iter(["step 1\n", "step 2\n", "syntax error on line 5\n"])
            self.returncode = 2
            self.pid = 12345

        def wait(self, timeout: int | None = None) -> int:
            return self.returncode

        def kill(self) -> None:
            pass

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_exc):  # type: ignore[no-untyped-def]
            return False

    monkeypatch.setattr("compute_space.core.containers.subprocess.Popen", _FakePopen)
    temp_data_dir = str(tmp_path / "temp")
    os.makedirs(os.path.join(temp_data_dir, "app_temp_data", "myapp"), exist_ok=True)

    with pytest.raises(RuntimeError) as exc_info:
        build_image("myapp", "/tmp/repo", "Dockerfile", temp_data_dir=temp_data_dir)
    assert str(exc_info.value) == "Container build failed (exit code 2):\nsyntax error on line 5"


def test_build_image_non_streaming_path_inspects_stdout_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The non-streaming build path (temp_data_dir=None) concatenates
    stdout and stderr before running the cache-corrupt check so it
    catches corruption indicators regardless of which channel podman
    emits them on.  Pin this by putting the corruption marker in
    stdout only — a regression that inspected only stderr would miss
    it and produce a generic build-failure error instead of the
    'Drop Cache' remediation marker.
    """

    def fake_run(cmd, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        return _FakeCompleted(
            1,
            stdout="error: content digest sha256:deadbeef: not found\n",
            stderr="",
        )

    _patch_subprocess_run(monkeypatch, fake_run)

    with pytest.raises(RuntimeError) as exc_info:
        build_image("myapp", "/tmp/repo", "Dockerfile", temp_data_dir=None)
    assert str(exc_info.value).startswith(containers.BUILD_CACHE_CORRUPT_MARKER)


def test_build_image_streaming_path_reaps_child_on_timeout(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression guard for the zombie-safety fix: when proc.wait(timeout=
    300) raises TimeoutExpired in the streaming path, build_image must
    call proc.kill() followed by a bounded proc.wait().  A kill without
    a subsequent wait leaves a zombie child hanging off the long-running
    router process.
    """

    kill_calls: list[int] = []
    wait_calls: list[int] = []

    class _HangingPopen:
        """Fakes a podman-build that hangs: iterating stdout finishes
        cleanly but proc.wait() raises TimeoutExpired."""

        def __init__(self, *_a, **_kw) -> None:
            self.stdout = iter(["building...\n"])
            self.returncode = -9
            self.pid = 99999

        def wait(self, timeout: int | None = None) -> int:
            wait_calls.append(timeout or 0)
            if timeout == 5:
                # The bounded wait inside the except block: simulate a
                # well-behaved kill that does reap.
                return self.returncode
            # The first wait (the 300s build wait) is the one that times out.
            raise subprocess.TimeoutExpired(cmd=["podman", "build"], timeout=timeout or 0)

        def kill(self) -> None:
            kill_calls.append(self.pid)

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_exc):  # type: ignore[no-untyped-def]
            return False

    monkeypatch.setattr("compute_space.core.containers.subprocess.Popen", _HangingPopen)

    temp_data_dir = str(tmp_path / "temp")
    os.makedirs(os.path.join(temp_data_dir, "app_temp_data", "myapp"), exist_ok=True)

    with pytest.raises(subprocess.TimeoutExpired):
        build_image("myapp", "/tmp/repo", "Dockerfile", temp_data_dir=temp_data_dir)

    # Must have invoked kill and then a bounded wait with timeout=5.
    assert kill_calls == [99999], f"expected one kill, got {kill_calls}"
    assert 5 in wait_calls, f"expected bounded wait(timeout=5) after kill, got {wait_calls}"


@pytest.mark.parametrize(
    "innocuous_output",
    [
        # Normal build progress mentioning a layer digest — must NOT
        # be classified as cache corruption.
        "Copying blob sha256:abcdef1234567890 done",
        "STEP 1/3: FROM sha256:deadbeef",
        # File-not-found error unrelated to any digest — must NOT be
        # classified as cache corruption just because it contains
        # ": not found".
        "error: /app/build.sh: not found",
        "COPY failed: file /src/missing.txt: not found in build context",
        # Registry pull failure: mentions a sha256 digest AND ": not
        # found" but the failure mode is "base image not published to
        # the registry", not "local cache corrupt".  Retrying the pull
        # would help; dropping the local cache would not.
        "Error: initializing source docker://registry.example.com/unknown@sha256:abc: image not found",
        "Error: pulling image sha256:abc123: manifest not found in registry",
        # readlink/EINVAL unrelated to overlay storage — the overlay
        # matcher must require "overlay" on the same line, so a bare
        # readlink failure in the app's own build script must NOT trip it.
        "readlink /app/config/link: invalid argument",
        # Mentions overlay but is normal driver progress, not a readlink
        # EINVAL — must not match.
        "Mounted overlay lowerdir for layer sha256:abc successfully",
    ],
)
def test_build_image_does_not_misclassify_innocuous_output_as_cache_corrupt(
    monkeypatch: pytest.MonkeyPatch, innocuous_output: str
) -> None:
    """Regression guard: the cache-corruption matcher must require BOTH
    a sha256 digest AND a ": not found" suffix on the same line.  A
    naive substring match would trigger on normal layer status output
    (which mentions sha256 digests) or on unrelated file-not-found
    errors, causing the dashboard to prompt a misleading "Drop Cache
    & Rebuild" and masking the actual build error."""

    def fake_run(cmd, capture_output, text, timeout):  # type: ignore[no-untyped-def]
        return _FakeCompleted(1, stderr=innocuous_output)

    _patch_subprocess_run(monkeypatch, fake_run)

    with pytest.raises(RuntimeError) as exc_info:
        build_image("myapp", "/tmp/repo", "Dockerfile", temp_data_dir=None)
    msg = str(exc_info.value)
    assert containers.BUILD_CACHE_CORRUPT_MARKER not in msg, (
        f"Expected generic build failure, got cache-corrupt marker for output: {innocuous_output!r}"
    )


# ---------------------------------------------------------------------------
# run_container
# ---------------------------------------------------------------------------


def _basic_manifest(**overrides) -> AppManifest:  # type: ignore[no-untyped-def]
    kwargs = dict(
        name="myapp",
        version="0.1.0",
        container_image="Dockerfile",
        container_port=8080,
        memory_mb=256,
        cpu_cores=0.5,
    )
    kwargs.update(overrides)
    return AppManifest(**kwargs)  # type: ignore[arg-type]


def _run_and_capture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    manifest: AppManifest,
    tmp_path,
    port_mappings: list[PortMapping] | None = None,
    env_vars: dict[str, str] | None = None,
) -> list[str]:
    """Invoke run_container with mocked subprocess and return the built argv."""
    runs: list[list[str]] = []

    def fake_run(cmd, capture_output=False, text=False, timeout=60, **_):  # type: ignore[no-untyped-def]
        runs.append(list(cmd))
        # First call is the pre-run "podman rm -f" — no output needed.
        # Second call is the actual "podman run" — return a fake container id.
        if cmd[:2] == ["podman", "run"]:
            return _FakeCompleted(0, stdout="container-id-xyz\n")
        return _FakeCompleted(0)

    _patch_subprocess_run(monkeypatch, fake_run)

    data_dir = str(tmp_path / "persistent")
    temp_data_dir = str(tmp_path / "temp")
    archive_dir = str(tmp_path / "archive")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(temp_data_dir, exist_ok=True)
    os.makedirs(archive_dir, exist_ok=True)
    os.makedirs(os.path.join(temp_data_dir, "app_temp_data", manifest.name), exist_ok=True)

    run_container(
        manifest.name,
        "openhost-myapp:latest",
        manifest,
        local_port=9001,
        env_vars=env_vars or {},
        data_dir=data_dir,
        temp_data_dir=temp_data_dir,
        archive_dir=archive_dir,
        port_mappings=port_mappings,
    )
    # The "run" call is the one after the pre-cleanup "rm".
    run_cmds = [c for c in runs if c[:2] == ["podman", "run"]]
    assert len(run_cmds) == 1
    return run_cmds[0]


def test_run_container_has_hardening_flags(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _basic_manifest()
    argv = _run_and_capture(monkeypatch, manifest=manifest, tmp_path=tmp_path)
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges=true" in argv
    assert "--add-host=host.docker.internal:host-gateway" in argv
    assert "--add-host=host.containers.internal:host-gateway" in argv


def test_run_container_points_dns_at_gateway_for_pasta_apps(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Pasta apps resolve via the container-facing CoreDNS view on the gateway so
    # `<app>.<zone>` hairpins to Caddy instead of looping in the container netns.
    monkeypatch.setattr(containers, "is_bindable", lambda ip: True)
    manifest = _basic_manifest()
    argv = _run_and_capture(monkeypatch, manifest=manifest, tmp_path=tmp_path)
    dns_idx = argv.index("--dns")
    assert argv[dns_idx + 1] == "10.200.0.1"


def test_run_container_skips_dns_when_gateway_is_absent(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No openhost0 dummy interface (dev, CI, macOS) means no container-facing
    # CoreDNS view to point at, and a container aimed at a resolver that isn't
    # there gets no DNS at all — leave podman's default resolver in place.
    monkeypatch.setattr(containers, "is_bindable", lambda ip: False)
    manifest = _basic_manifest()
    argv = _run_and_capture(monkeypatch, manifest=manifest, tmp_path=tmp_path)
    assert "--dns" not in argv


def test_run_container_network_host_app_gets_no_dns_override(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    # network_host apps share the host netns and use the host's resolver
    # directly; they must not get the gateway --dns override.
    manifest = _basic_manifest(network_host=True)
    argv = _run_and_capture(monkeypatch, manifest=manifest, tmp_path=tmp_path)
    assert "--network=host" in argv
    assert "--dns" not in argv


def test_run_container_mounts_use_idmap_option(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _basic_manifest(app_data=True, app_temp_data=True, app_archive=True)
    argv = _run_and_capture(monkeypatch, manifest=manifest, tmp_path=tmp_path)
    # Every -v argument value should have :idmap (or :ro,idmap) as its
    # options suffix so container-root writes land on disk under the host
    # user, not under the mapped subuid.
    volume_args = [arg for prev, arg in zip(argv, argv[1:], strict=False) if prev == "-v"]
    assert volume_args  # The test manifest requested three mounts.
    for v in volume_args:
        assert v.endswith(":idmap") or v.endswith(":ro,idmap"), v


def test_run_container_adds_manifest_capabilities(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _basic_manifest(capabilities=["NET_ADMIN", "NET_RAW"])
    argv = _run_and_capture(monkeypatch, manifest=manifest, tmp_path=tmp_path)
    # --cap-drop=ALL must come before the --cap-add entries.
    drop_idx = argv.index("--cap-drop=ALL")
    for cap in ("NET_ADMIN", "NET_RAW"):
        pair_idx = argv.index(cap)
        assert argv[pair_idx - 1] == "--cap-add"
        assert pair_idx > drop_idx, "cap-drop must come before cap-add"


def test_run_container_grants_docker_default_capabilities_by_default(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Apps with no manifest capabilities still get Docker's default set
    (CHOWN, DAC_OVERRIDE, SETUID, etc.) so debian-packaged daemons that
    assume container-root can override DAC on image-layer files — tor,
    postgres, redis, nginx, rabbitmq — keep working under podman without
    the manifest needing to enumerate every capability Docker grants
    implicitly."""
    manifest = _basic_manifest()  # no explicit capabilities
    argv = _run_and_capture(monkeypatch, manifest=manifest, tmp_path=tmp_path)

    drop_idx = argv.index("--cap-drop=ALL")
    for cap in sorted(DEFAULT_CAPABILITIES):
        pair_idx = argv.index(cap)
        assert argv[pair_idx - 1] == "--cap-add", f"{cap}: expected --cap-add, got {argv[pair_idx - 1]!r}"
        assert pair_idx > drop_idx, f"{cap}: --cap-drop=ALL must precede --cap-add"


def test_run_container_does_not_duplicate_baseline_caps_from_manifest(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest that redundantly lists a baseline capability (e.g.
    CHOWN) must not produce a second --cap-add entry for it — the
    operator-visible argv should stay clean and match what podman
    sees."""
    # Pick a capability that's in both the baseline and the allowlist.
    redundant = next(iter(DEFAULT_CAPABILITIES))
    manifest = _basic_manifest(capabilities=[redundant, "NET_ADMIN"])
    argv = _run_and_capture(monkeypatch, manifest=manifest, tmp_path=tmp_path)

    # The redundant cap appears exactly once (from the baseline loop);
    # NET_ADMIN appears exactly once (from the manifest loop).
    assert argv.count(redundant) == 1, f"expected one {redundant}, got {argv.count(redundant)}"
    assert argv.count("NET_ADMIN") == 1


def test_run_container_adds_manifest_devices(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``manifest.devices`` entries must be passed to podman as
    ``--device <entry>`` pairs.  Symmetric to the capabilities test:
    the manifest validator restricts devices to SAFE_DEVICE_PATHS at
    parse time, and run_container then forwards every entry unchanged.
    A regression that silently dropped device passthrough would break
    VPN-style apps (/dev/net/tun) and FUSE filesystems silently, so
    pin the exact argv wiring."""
    manifest = _basic_manifest(devices=["/dev/net/tun", "/dev/fuse"])
    argv = _run_and_capture(monkeypatch, manifest=manifest, tmp_path=tmp_path)

    for device in ("/dev/net/tun", "/dev/fuse"):
        pair_idx = argv.index(device)
        assert argv[pair_idx - 1] == "--device", (
            f"device {device!r} must appear as the value of a --device flag, "
            f"got argv[{pair_idx - 1}]={argv[pair_idx - 1]!r}"
        )


def test_run_container_removed_access_vm_data_flag_does_not_mount_vm_data(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = parse_manifest_from_string(
        """\
[app]
name = "myapp"
version = "0.1.0"

[runtime.container]
image = "Dockerfile"
port = 8080

[data]
access_vm_data = true
"""
    )
    argv = _run_and_capture(monkeypatch, manifest=manifest, tmp_path=tmp_path)

    volume_args = [arg for prev, arg in zip(argv, argv[1:], strict=False) if prev == "-v"]
    assert not any("/data/vm_data" in v for v in volume_args)


def test_run_container_app_archive_mounts_per_app_subdir(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Apps opting in via [data].app_archive get a per-app bind mount at /data/app_archive/<name>/ (host source: operator-configured archive_dir, container path same regardless of backing). A regression mounting the parent instead would expose every app's archive contents to every app."""
    manifest = _basic_manifest(app_data=True, app_archive=True)
    argv = _run_and_capture(monkeypatch, manifest=manifest, tmp_path=tmp_path)

    volume_args = [arg for prev, arg in zip(argv, argv[1:], strict=False) if prev == "-v"]
    archive_mounts = [v for v in volume_args if "/data/app_archive" in v]
    assert len(archive_mounts) == 1, archive_mounts

    assert archive_mounts[0].endswith(f":/data/app_archive/{manifest.name}:idmap"), archive_mounts[0]

    assert f"archive/{manifest.name}:" in archive_mounts[0], archive_mounts[0]


def test_run_container_app_archive_off_by_default(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Apps that don't opt into app_archive get NO archive bind mount; a regression that always mounted the archive tier would surface operator-configured backings to apps that never asked for it."""
    manifest = _basic_manifest(app_data=True)
    argv = _run_and_capture(monkeypatch, manifest=manifest, tmp_path=tmp_path)

    volume_args = [arg for prev, arg in zip(argv, argv[1:], strict=False) if prev == "-v"]
    assert not any("/data/app_archive" in v for v in volume_args)


def test_run_container_access_all_app_data_skips_archive_when_missing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-app access is permissive when the JuiceFS mount is unavailable."""
    runs: list[list[str]] = []

    def fake_run(cmd, capture_output=False, text=False, timeout=60, **_):  # type: ignore[no-untyped-def]
        runs.append(list(cmd))
        if cmd[:2] == ["podman", "run"]:
            return _FakeCompleted(0, stdout="container-id-xyz\n")
        return _FakeCompleted(0)

    _patch_subprocess_run(monkeypatch, fake_run)

    manifest = _basic_manifest(access_all_app_data=True)
    data_dir = str(tmp_path / "persistent")
    temp_data_dir = str(tmp_path / "temp")
    archive_dir = str(tmp_path / "archive-not-mounted")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(temp_data_dir, exist_ok=True)
    os.makedirs(os.path.join(temp_data_dir, "app_temp_data", manifest.name), exist_ok=True)

    run_container(
        manifest.name,
        "openhost-myapp:latest",
        manifest,
        local_port=9001,
        env_vars={},
        data_dir=data_dir,
        temp_data_dir=temp_data_dir,
        archive_dir=archive_dir,
    )
    run_cmds = [c for c in runs if c[:2] == ["podman", "run"]]
    argv = run_cmds[0]
    volume_args = [arg for prev, arg in zip(argv, argv[1:], strict=False) if prev == "-v"]
    assert not any("/data/app_archive" in v for v in volume_args)


def test_run_container_access_all_app_data_mounts_parent_dirs(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cross-app access mounts every app-owned data parent, not per-app subdirs."""
    manifest = _basic_manifest(access_all_app_data=True)
    argv = _run_and_capture(monkeypatch, manifest=manifest, tmp_path=tmp_path)

    volume_args = [arg for prev, arg in zip(argv, argv[1:], strict=False) if prev == "-v"]
    targets = [v.rsplit(":", 2)[1] for v in volume_args]
    assert "/data/app_data" in targets
    assert "/data/app_temp_data" in targets
    assert "/data/app_archive" in targets
    assert "/data/vm_data" not in targets
    assert f"/data/app_data/{manifest.name}" not in targets
    assert f"/data/app_archive/{manifest.name}" not in targets


def test_run_container_app_data_opt_out(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Apps can explicitly opt out of app_data with ``app_data = false``."""
    manifest = _basic_manifest(app_data=False)
    argv = _run_and_capture(monkeypatch, manifest=manifest, tmp_path=tmp_path)

    volume_args = [arg for prev, arg in zip(argv, argv[1:], strict=False) if prev == "-v"]
    assert not any(f"/data/app_data/{manifest.name}" in v for v in volume_args)


def test_run_container_app_archive_env_var_translated_to_container_path(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``OPENHOST_APP_ARCHIVE_DIR`` in env_vars must be translated from the host path to the in-container path before being stamped on the podman run argv — same host-vs-container translation pattern as ``OPENHOST_APP_DATA_DIR`` and ``OPENHOST_APP_TEMP_DIR``."""
    manifest = _basic_manifest(app_data=True, app_archive=True)
    env_vars = {"OPENHOST_APP_ARCHIVE_DIR": "/some/host/archive/myapp"}
    argv = _run_and_capture(monkeypatch, manifest=manifest, tmp_path=tmp_path, env_vars=env_vars)

    e_values = [arg for prev, arg in zip(argv, argv[1:], strict=False) if prev == "-e"]
    archive_env = [e for e in e_values if e.startswith("OPENHOST_APP_ARCHIVE_DIR=")]
    assert len(archive_env) == 1, archive_env
    assert archive_env[0] == f"OPENHOST_APP_ARCHIVE_DIR=/data/app_archive/{manifest.name}"


def test_run_container_stamps_bottle_and_openhost_env_twins(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every app env var is stamped under both the legacy OPENHOST_ name and
    the new BOTTLE_ name (OpenHost -> Cloud in a Bottle rename).  The BOTTLE_
    twin of a path var must carry the already-translated in-container value,
    not the host path — i.e. aliasing happens after path translation."""
    manifest = _basic_manifest(app_data=True, app_archive=True)
    env_vars = {
        "OPENHOST_APP_TOKEN": "tok123",
        "OPENHOST_APP_ARCHIVE_DIR": "/some/host/archive/myapp",
    }
    argv = _run_and_capture(monkeypatch, manifest=manifest, tmp_path=tmp_path, env_vars=env_vars)

    e_values = [arg for prev, arg in zip(argv, argv[1:], strict=False) if prev == "-e"]

    # Non-path var: legacy + new twin, same value.
    assert "OPENHOST_APP_TOKEN=tok123" in e_values
    assert "BOTTLE_APP_TOKEN=tok123" in e_values

    # Path var: the BOTTLE_ twin carries the translated container path.
    assert f"OPENHOST_APP_ARCHIVE_DIR=/data/app_archive/{manifest.name}" in e_values
    assert f"BOTTLE_APP_ARCHIVE_DIR=/data/app_archive/{manifest.name}" in e_values


def test_run_container_port_mappings_bind_tcp_and_udp_on_all_interfaces(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manifests with [[ports]] should publish TCP and UDP on 0.0.0.0 for
    every entry.  Port mappings are user-visible and binding to the wrong
    interface or protocol would silently break apps."""
    manifest = _basic_manifest()
    argv = _run_and_capture(
        monkeypatch,
        manifest=manifest,
        tmp_path=tmp_path,
        port_mappings=[
            PortMapping(label="wg", container_port=51820, host_port=51820),
            PortMapping(label="dns", container_port=5300, host_port=5300),
        ],
    )

    # Collect every -p value.
    p_values = [arg for prev, arg in zip(argv, argv[1:], strict=False) if prev == "-p"]
    # The main loopback-only HTTP mapping must always be present, and the
    # two [[ports]] entries must each expand to a tcp + udp publish on
    # 0.0.0.0.
    assert "127.0.0.1:9001:8080" in p_values
    assert "0.0.0.0:51820:51820/tcp" in p_values
    assert "0.0.0.0:51820:51820/udp" in p_values
    assert "0.0.0.0:5300:5300/tcp" in p_values
    assert "0.0.0.0:5300:5300/udp" in p_values


# ---------------------------------------------------------------------------
# stop, remove, status, cache drop
# ---------------------------------------------------------------------------


def test_stop_container_calls_podman(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **_):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        return _FakeCompleted(0)

    _patch_subprocess_run(monkeypatch, fake_run)

    stop_container("abc123")
    assert calls == [
        ["podman", "stop", "-t", "10", "abc123"],
        ["podman", "rm", "-f", "abc123"],
    ]


def test_stop_container_escalates_to_kill_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **_):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        if cmd[:2] == ["podman", "stop"]:
            return _FakeCompleted(125)
        return _FakeCompleted(0)

    _patch_subprocess_run(monkeypatch, fake_run)

    stop_container("abc123")
    assert calls == [
        ["podman", "stop", "-t", "10", "abc123"],
        ["podman", "kill", "abc123"],
        ["podman", "rm", "-f", "abc123"],
    ]


def test_stop_container_escalates_to_kill_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        if cmd[:2] == ["podman", "stop"]:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 20))
        return _FakeCompleted(0)

    _patch_subprocess_run(monkeypatch, fake_run)

    stop_container("abc123")
    assert calls == [
        ["podman", "stop", "-t", "10", "abc123"],
        ["podman", "kill", "abc123"],
        ["podman", "rm", "-f", "abc123"],
    ]


def test_remove_image_calls_podman_rmi(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **_):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        return _FakeCompleted(0)

    _patch_subprocess_run(monkeypatch, fake_run)

    remove_image("myapp")
    assert calls == [["podman", "rmi", "openhost-myapp:latest"]]


def test_is_container_running_returns_false_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **_):  # type: ignore[no-untyped-def]
        return _FakeCompleted(1, stderr="no such container")

    _patch_subprocess_run(monkeypatch, fake_run)
    assert is_container_running("bogus") is False


@pytest.mark.parametrize("returncode, expected", [(0, True), (1, False)])
def test_image_exists_uses_podman_exit_status(
    monkeypatch: pytest.MonkeyPatch, returncode: int, expected: bool
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], returncode),
    )

    assert image_exists("openhost-notes:latest") is expected


def test_image_exists_returns_false_when_podman_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", unavailable)

    assert image_exists("openhost-notes:latest") is False


def test_is_container_running_returns_true_when_podman_reports_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: podman inspect exits 0 with ``running``."""

    def fake_run(cmd, **_):  # type: ignore[no-untyped-def]
        return _FakeCompleted(0, stdout="running\n")

    _patch_subprocess_run(monkeypatch, fake_run)
    assert is_container_running("real-container") is True


def test_is_container_running_returns_false_for_non_running_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any podman state other than ``running`` (paused, created, exited)
    collapses to False.  Callers only care about the running/not-running
    distinction.
    """

    def fake_run(cmd, **_):  # type: ignore[no-untyped-def]
        return _FakeCompleted(0, stdout="paused\n")

    _patch_subprocess_run(monkeypatch, fake_run)
    assert is_container_running("paused-container") is False


# NOTE: build-cache drop is exercised in test_build_cache.py with the
# same subprocess mock plus kwarg assertions.  Not duplicated here.


# ---------------------------------------------------------------------------
# get_docker_logs
# ---------------------------------------------------------------------------


def _make_build_log(tmp_path, app_name: str, contents: str) -> str:
    """Write a build log file at the canonical path for an app and return it."""
    log_dir = os.path.join(str(tmp_path), "app_temp_data", app_name)
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "docker.log")
    with open(log_file, "w") as f:
        f.write(contents)
    return log_file


def test_get_docker_logs_returns_build_log_when_no_container(tmp_path) -> None:
    """When the app has no running container, get_docker_logs should still
    return the build log contents (and skip the podman logs call)."""
    _make_build_log(tmp_path, "notes", "build-line-1\nbuild-line-2\n")

    output = get_docker_logs("notes", str(tmp_path), container_id=None)
    assert "build-line-1" in output
    assert "build-line-2" in output
    # No "Container logs" header should appear since no container id.
    assert "=== Container logs ===" not in output


def test_get_docker_logs_returns_empty_when_no_build_log_and_no_container(tmp_path) -> None:
    """Fresh app that failed before any _append_log call has neither
    a build log file nor a container id.  The function's os.path.exists
    guard must short-circuit both branches and return "" rather than
    trying to read a missing file or call podman inspect."""
    # No _make_build_log call — the build log file does not exist.
    output = get_docker_logs("brand-new", str(tmp_path), container_id=None)
    assert output == ""


def test_get_docker_logs_appends_podman_logs_when_container_id_set(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With a container id, get_docker_logs should also run ``podman logs
    --tail <N>`` and concatenate the output under a 'Container logs' header."""
    _make_build_log(tmp_path, "notes", "=== Build complete ===\n")

    captured: list[list[str]] = []

    def fake_run(cmd, capture_output=False, text=False, timeout=10, **_):  # type: ignore[no-untyped-def]
        captured.append(list(cmd))
        return _FakeCompleted(0, stdout="app stdout line\n")

    _patch_subprocess_run(monkeypatch, fake_run)

    output = get_docker_logs("notes", str(tmp_path), container_id="abc123", tail=500)

    assert captured == [["podman", "logs", "--tail", "500", "abc123"]]
    assert "=== Build complete ===" in output
    assert "=== Container logs ===" in output
    assert "app stdout line" in output


def test_get_docker_logs_strips_ansi_escape_sequences(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Color codes and carriage returns from podman logs should be
    stripped before we hand the text back to the dashboard (which
    otherwise renders them as garbage text)."""
    _make_build_log(tmp_path, "notes", "")

    def fake_run(cmd, capture_output=False, text=False, timeout=10, **_):  # type: ignore[no-untyped-def]
        return _FakeCompleted(
            0,
            stdout="\x1b[31mred text\x1b[0m plain\r\n",
        )

    _patch_subprocess_run(monkeypatch, fake_run)

    output = get_docker_logs("notes", str(tmp_path), container_id="abc123")
    assert "\x1b[" not in output
    assert "\r" not in output
    assert "red text plain" in output


# ---------------------------------------------------------------------------
# _bind_mount_arg
# ---------------------------------------------------------------------------


def test_bind_mount_arg_rw_appends_idmap() -> None:
    """Read-write mounts must carry the bare ``:idmap`` option so
    container-root writes land on disk under the unprivileged host
    user rather than the mapped subuid."""
    assert _bind_mount_arg("/srv/data", "/data") == "/srv/data:/data:idmap"


def test_bind_mount_arg_ro_emits_ro_idmap() -> None:
    """Read-only mounts must combine ``ro`` with ``idmap``.  The
    ordering (``ro,idmap``) matters for operator readability of
    ``podman ps`` output; pin the canonical rendering here even though
    podman parses the options order-independently."""
    assert _bind_mount_arg("/srv/data", "/data", read_only=True) == "/srv/data:/data:ro,idmap"


def test_bind_mount_arg_preserves_absolute_paths_verbatim() -> None:
    """Host and container paths pass through unchanged — no
    normalisation, no trailing-slash stripping.  Callers already
    join paths to their canonical form; this function shouldn't
    re-touch them because any mutation could change the mount
    target vs. what the app's env var points at."""
    arg = _bind_mount_arg("/a/b/c/", "/data/app_data/myapp/")
    assert arg == "/a/b/c/:/data/app_data/myapp/:idmap"


# ---------------------------------------------------------------------------
# container_image_storage_bytes
# ---------------------------------------------------------------------------


def test_container_image_storage_bytes_parses_images_row(monkeypatch: pytest.MonkeyPatch) -> None:
    df_json = json.dumps(
        [
            {"Type": "Images", "Total": 3, "Active": 1, "RawSize": 4567, "RawReclaimable": 100},
            {"Type": "Containers", "Total": 1, "Active": 1, "RawSize": 99, "RawReclaimable": 0},
        ]
    )
    _patch_subprocess_run(monkeypatch, lambda *a, **k: _FakeCompleted(stdout=df_json))
    assert containers.container_image_storage_bytes() == (4567, 100)


def test_container_image_storage_bytes_returns_none_values_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_subprocess_run(monkeypatch, lambda *a, **k: _FakeCompleted(returncode=1))
    assert containers.container_image_storage_bytes() == (None, None)


def test_container_image_storage_bytes_handles_missing_values_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    df_json = json.dumps([{"Type": "Images", "RawReclaimable": 100}])
    _patch_subprocess_run(monkeypatch, lambda *a, **k: _FakeCompleted(stdout=df_json))
    assert containers.container_image_storage_bytes() == (None, 100)

    df_json = json.dumps([{"Type": "Images", "RawSize": 4567}])
    _patch_subprocess_run(monkeypatch, lambda *a, **k: _FakeCompleted(stdout=df_json))
    assert containers.container_image_storage_bytes() == (4567, None)


def test_container_image_storage_bytes_returns_none_values_when_podman_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*a, **k):
        raise FileNotFoundError("podman")

    _patch_subprocess_run(monkeypatch, _raise)
    assert containers.container_image_storage_bytes() == (None, None)
