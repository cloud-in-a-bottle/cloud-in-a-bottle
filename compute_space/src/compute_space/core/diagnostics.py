"""Collect diagnostic information for debugging OpenHost instances and apps.

Two public entry points:

  - :func:`collect_platform_diagnostics` — a snapshot of the whole instance:
    OpenHost git checkout (branch/SHA/dirty), host OS/kernel, Python and
    installed dependency versions, container runtime (podman) info, disk usage,
    and a summary of every installed app.  Owner-only; intended to be copied or
    downloaded and pasted into a bug report.

  - :func:`collect_app_diagnostics` — a per-app snapshot: the app's declared
    version + manifest git checkout, container status, plus a slim slice of the
    same host/system info so an app report is self-contained.

Error handling matches fault severity rather than degrading every failure the
same way. A foundational fault — an unreadable control-plane DB (the ``apps``
query, the primary-domain read) — propagates, so the endpoint returns 500 rather
than a hollow bundle that would masquerade as a healthy empty instance. A
field-level fault — one bad app row, an unreachable host, a podman probe that
fails — degrades that field to ``None``/an error string so the rest of the
bundle still renders, which is precisely when a diagnostics bundle is most
valuable.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import platform
import shutil
import sqlite3
import subprocess
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

import attr
import httpx

from compute_space import OPENHOST_PROJECT_DIR
from compute_space.config import Config
from compute_space.core.domains import primary_domain_or_none
from compute_space.core.git_ops import get_branch_name
from compute_space.core.git_ops import get_head_sha
from compute_space.core.git_ops import get_remote_url
from compute_space.core.git_ops import is_dirty
from compute_space.core.logging import logger
from compute_space.core.manifest import parse_manifest_from_string
from compute_space.core.storage import storage_status

# The schema version of the diagnostics payload.  Bump when the shape changes
# so consumers (support tooling, the CLI, the dashboard) can detect an
# incompatible bundle.  Present on both the platform and per-app bundles.
#
# v2: added resource_pressure, reachability (platform) and health + resources
#     (per-app).
DIAGNOSTICS_SCHEMA_VERSION = 2

# Distribution names whose versions are worth surfacing in a bug report.  These
# are the packages most likely to explain a runtime bug; the full environment is
# large and noisy, so we curate rather than dump everything.
_KEY_DEPENDENCIES = (
    "litestar",
    "hypercorn",
    "GitPython",
    "attrs",
    "cattrs",
    "typed-settings",
    "httpx",
    "bcrypt",
    "jinja2",
    "tomli-w",
    "cappa",
)

_SUBPROCESS_TIMEOUT_S = 10

# Per-target timeout for outbound reachability probes and per-app health checks.
# Kept short so a diagnostics request can't hang for long on a dead network.
_HEALTH_TIMEOUT_S = 5.0
_REACHABILITY_TIMEOUT_S = 5.0

# External hosts the platform depends on.  We probe these so a diagnostics
# bundle shows whether the instance can reach the services it needs (app clones,
# TLS cert issuance/brokering).  Each entry is (label, url); the URL only needs
# to resolve + connect, so a HEAD/GET that returns any HTTP status counts as
# "reachable".
_STATIC_REACHABILITY_TARGETS: tuple[tuple[str, str], ...] = (
    ("github", "https://github.com"),
    ("github_api", "https://api.github.com"),
    ("acme_gts", "https://dv.acme-v02.api.pki.goog/directory"),
)


# ─── attrs models ────────────────────────────────────────────────────────────


@attr.s(auto_attribs=True, frozen=True)
class GitInfo:
    """Git checkout state for a repository on disk.

    ``sha`` is empty and ``branch`` is None when the path isn't a git checkout
    (e.g. builtin apps or tarball deploys). ``branch`` is None when HEAD is
    detached even if ``sha`` is populated.
    """

    branch: str | None
    sha: str
    short_sha: str
    dirty: bool
    remote_url: str | None


@attr.s(auto_attribs=True, frozen=True)
class SystemInfo:
    """Host OS / Python / process facts."""

    hostname: str
    platform: str
    system: str
    release: str
    machine: str
    processor: str
    python_version: str
    python_implementation: str
    cpu_count: int | None
    boot_time: str | None


@attr.s(auto_attribs=True, frozen=True)
class ContainerRuntimeInfo:
    """Facts about the container runtime (podman)."""

    available: bool
    version: str | None
    rootless: bool | None
    error: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class HostResourcePressure:
    """Host-level memory + load, for spotting pressure / OOM conditions."""

    memory_total_bytes: int | None
    memory_available_bytes: int | None
    memory_used_percent: float | None
    load_avg_1m: float | None
    load_avg_5m: float | None
    load_avg_15m: float | None
    cpu_count: int | None
    error: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class AppResourceUsage:
    """Live container resource usage for one app vs its manifest limits.

    All ``*_actual`` fields are None when the app has no running container or
    podman stats can't be read; ``*_limit`` reflects the manifest.
    """

    running: bool
    cpu_percent: float | None
    memory_usage_bytes: int | None
    memory_limit_bytes: int | None
    memory_percent: float | None
    cpu_cores_limit: float | None
    memory_mb_limit: int | None
    error: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class AppHealth:
    """Result of probing an app's health endpoint over the loopback proxy port.

    ``checked_path`` is the app's declared ``health_check`` path, or ``/`` when
    none is declared.  ``healthy`` is True when the endpoint responds with an
    HTTP status < 500 (matching the router's readiness contract).
    """

    checked: bool
    healthy: bool | None
    status_code: int | None
    checked_path: str
    error: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class ReachabilityResult:
    """Outcome of an outbound reachability probe to one external host."""

    label: str
    url: str
    reachable: bool
    status_code: int | None
    latency_ms: float | None
    error: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class AppDiagnosticsSummary:
    """Per-app entry in the platform diagnostics bundle."""

    app_id: str
    name: str
    status: str
    version: str | None
    runtime_type: str | None
    error_message: str | None
    git: GitInfo | None
    health: AppHealth | None = None
    resources: AppResourceUsage | None = None


@attr.s(auto_attribs=True, frozen=True)
class PlatformDiagnostics:
    """Full instance diagnostics bundle (owner-only)."""

    schema_version: int
    generated_at: str
    zone_domain: str
    openhost: GitInfo
    system: SystemInfo
    container_runtime: ContainerRuntimeInfo
    dependencies: dict[str, str]
    storage: dict[str, object]
    resource_pressure: HostResourcePressure
    reachability: list[ReachabilityResult]
    apps: list[AppDiagnosticsSummary]


@attr.s(auto_attribs=True, frozen=True)
class AppDiagnostics:
    """Per-app diagnostics bundle (owner-only).

    Includes a slice of host/system info so an app report is self-contained and
    useful on its own, without the caller also having to grab the platform
    bundle.
    """

    schema_version: int
    generated_at: str
    zone_domain: str
    app_id: str
    name: str
    status: str
    version: str | None
    runtime_type: str | None
    error_message: str | None
    container_id: str | None
    git: GitInfo | None
    health: AppHealth | None
    resources: AppResourceUsage | None
    system: SystemInfo
    container_runtime: ContainerRuntimeInfo
    resource_pressure: HostResourcePressure
    openhost: GitInfo


# ─── low-level collectors ──────────────────────────────────────────────────


async def _collect_git_info(repo_path: Path | None) -> GitInfo | None:
    """Return :class:`GitInfo` for ``repo_path`` or None when it has no .git.

    Never raises: any error while reading git state degrades to None so a
    single bad repo doesn't sink the whole bundle.
    """
    if repo_path is None:
        return None
    if not (repo_path / ".git").exists():
        return None
    try:
        branch = await get_branch_name(repo_path)
        sha = await get_head_sha(repo_path)
        dirty = await is_dirty(repo_path)
    except Exception:
        logger.opt(exception=True).warning("Failed to read git info for %s", repo_path)
        return None
    remote_url: str | None
    try:
        remote_url = await get_remote_url(repo_path)
    except Exception:
        # Missing/unreadable remote is common (no 'origin'); not worth a warning.
        remote_url = None
    return GitInfo(branch=branch, sha=sha, short_sha=sha[:8], dirty=dirty, remote_url=remote_url)


def _collect_system_info() -> SystemInfo:
    """Gather host OS / Python facts. Best-effort; missing fields become None."""
    uname = platform.uname()
    try:
        cpu_count = os.cpu_count()
    except Exception:
        cpu_count = None
    boot_time = _read_boot_time()
    return SystemInfo(
        hostname=uname.node,
        platform=platform.platform(),
        system=uname.system,
        release=uname.release,
        machine=uname.machine,
        processor=uname.processor,
        python_version=platform.python_version(),
        python_implementation=platform.python_implementation(),
        cpu_count=cpu_count,
        boot_time=boot_time,
    )


def _read_boot_time() -> str | None:
    """Return system boot time as an ISO-8601 UTC string, or None if unavailable.

    Reads /proc/stat's ``btime`` (Linux only); avoids adding a psutil dependency.
    """
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime "):
                    epoch = int(line.split()[1])
                    return datetime.fromtimestamp(epoch, UTC).isoformat()
    except Exception:
        return None
    return None


def _collect_dependencies() -> dict[str, str]:
    """Return {distribution_name: version} for the curated key dependencies.

    A dependency that isn't installed maps to ``"(not installed)"`` rather than
    being omitted, so a missing package is visible in the report.
    """
    versions: dict[str, str] = {}
    for name in _KEY_DEPENDENCIES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "(not installed)"
        except Exception:
            versions[name] = "(error)"
    return versions


def _collect_container_runtime() -> ContainerRuntimeInfo:
    """Probe podman for version + rootless status.

    Mirrors the ``openhost doctor`` probe (parse ``podman info --format json``,
    assert the rootless flag) so the two agree on what "healthy" looks like.
    """
    if shutil.which("podman") is None:
        return ContainerRuntimeInfo(available=False, version=None, rootless=None, error="podman not found on PATH")
    try:
        info_proc = subprocess.run(
            ["podman", "info", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return ContainerRuntimeInfo(available=False, version=None, rootless=None, error="podman info timed out")
    except Exception as e:
        return ContainerRuntimeInfo(available=False, version=None, rootless=None, error=str(e))

    if info_proc.returncode != 0:
        return ContainerRuntimeInfo(available=False, version=None, rootless=None, error="podman info failed")

    version: str | None = None
    rootless: bool | None = None
    try:
        info = json.loads(info_proc.stdout)
        host = info.get("host", {})
        # podman reports its own version under the top-level ``version`` table
        # (``version.Version``); ``host.serverVersion`` is not populated in
        # current releases, so read the former and fall back to the latter.
        version = info.get("version", {}).get("Version") or host.get("serverVersion")
        rootless_val = host.get("security", {}).get("rootless")
        rootless = rootless_val if isinstance(rootless_val, bool) else None
    except (json.JSONDecodeError, AttributeError):
        return ContainerRuntimeInfo(
            available=True, version=None, rootless=None, error="podman info returned non-JSON output"
        )
    return ContainerRuntimeInfo(available=True, version=version, rootless=rootless, error=None)


def _manifest_fields(manifest_raw: str | None) -> tuple[str | None, str | None]:
    """Parse (version, runtime_type) from a stored manifest, or (None, None).

    Re-parsing ``manifest_raw`` is more accurate than the ``apps.version``
    column, which is only written at install time and not on reload.
    """
    if not manifest_raw:
        return None, None
    try:
        manifest = parse_manifest_from_string(manifest_raw)
    except Exception:
        return None, None
    return manifest.version, manifest.runtime_type


# ─── resource pressure ───────────────────────────────────────────────────────


def _read_meminfo() -> tuple[int | None, int | None]:
    """Return (MemTotal, MemAvailable) in bytes from /proc/meminfo, or (None, None).

    /proc/meminfo reports values in kibibytes; we convert to bytes. Best-effort,
    never raises (Linux-only path).
    """
    total: int | None = None
    available: int | None = None
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    available = int(line.split()[1]) * 1024
                if total is not None and available is not None:
                    break
    except Exception:
        return None, None
    return total, available


def _collect_resource_pressure() -> HostResourcePressure:
    """Gather host memory + load average. Defensive: fields degrade to None."""
    total, available = _read_meminfo()
    used_percent: float | None = None
    if total and available is not None and total > 0:
        used_percent = round((total - available) / total * 100, 1)

    load_1m: float | None = None
    load_5m: float | None = None
    load_15m: float | None = None
    try:
        load_1m, load_5m, load_15m = os.getloadavg()
    except (OSError, AttributeError):
        pass

    try:
        cpu_count = os.cpu_count()
    except Exception:
        cpu_count = None

    return HostResourcePressure(
        memory_total_bytes=total,
        memory_available_bytes=available,
        memory_used_percent=used_percent,
        load_avg_1m=load_1m,
        load_avg_5m=load_5m,
        load_avg_15m=load_15m,
        cpu_count=cpu_count,
    )


def _parse_stats_bytes(value: object) -> int | None:
    """Parse a podman-stats size token like '12.3MB' / '1.2GiB' / '512kB' to bytes.

    podman renders memory usage as a human string; we normalise to bytes so the
    bundle carries machine-comparable numbers. Returns None on anything unparseable.
    """
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s or s.lower() in ("--", "n/a"):
        return None
    units = {
        "b": 1,
        "kb": 1000,
        "mb": 1000**2,
        "gb": 1000**3,
        "tb": 1000**4,
        "kib": 1024,
        "mib": 1024**2,
        "gib": 1024**3,
        "tib": 1024**4,
    }
    # Split trailing unit letters from the leading number.
    num = s
    unit = ""
    for i, ch in enumerate(s):
        if ch.isalpha() or ch == "%":
            num, unit = s[:i], s[i:]
            break
    try:
        magnitude = float(num)
    except ValueError:
        return None
    factor = units.get(unit.strip().lower(), 1)
    return int(magnitude * factor)


def _parse_stats_percent(value: object) -> float | None:
    """Parse a podman-stats percent token like '3.14%' to a float, or None."""
    if not isinstance(value, str):
        return None
    s = value.strip().rstrip("%").strip()
    if not s or s in ("--", "N/A"):
        return None
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def _run_podman_json(args: list[str]) -> list[dict[str, Any]]:
    """Run a podman ``--format json`` subcommand and return its list of objects.

    This is the single place podman's untyped output is validated: it raises on
    any failure — a timeout, a non-zero exit, or output that isn't a JSON array
    of objects — rather than masking it. An empty list means podman genuinely
    reported nothing, never "we couldn't read it"; the caller decides how to
    surface a real failure.
    """
    proc = subprocess.run(["podman", *args], capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S)
    if proc.returncode != 0:
        raise RuntimeError(f"podman {' '.join(args)} exited {proc.returncode}: {proc.stderr.strip()}")
    data = json.loads(proc.stdout)
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ValueError(f"podman {' '.join(args)} did not return a JSON array of objects")
    return data


def _short_id(entry: dict[str, Any], *keys: str) -> str | None:
    """The 12-char short container id from the first present string key, or None
    (podman spells it ``Id`` in ``ps`` and ``id`` in ``stats``)."""
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value[:12]
    return None


def _split_mem_usage(raw: object) -> tuple[int | None, int | None]:
    """Split a podman ``mem_usage`` token like '12.3MB / 128MB' into
    (usage, limit) bytes; (None, None) when absent or malformed."""
    if not isinstance(raw, str) or "/" not in raw:
        return None, None
    usage, _, limit = raw.partition("/")
    return _parse_stats_bytes(usage), _parse_stats_bytes(limit)


@attr.s(auto_attribs=True, frozen=True)
class _PodmanStats:
    """Live resource usage for one container, already parsed out of podman's
    ``stats`` JSON — no raw tokens or podman-shape quirks past this point."""

    cpu_percent: float | None
    memory_usage_bytes: int | None
    memory_limit_bytes: int | None
    memory_percent: float | None


@attr.s(auto_attribs=True, frozen=True)
class _ContainerStatsBatch:
    """Fleet-wide container state fetched with a fixed number of podman calls.

    Both collections are keyed by 12-char short id because ``podman stats``
    reports short ids while the DB stores full ids; callers truncate before
    lookup. ``error`` is set only when podman is *present but its probe failed*
    (a real, unexpected fault) — so every app surfaces that reason instead of
    silently reading as stopped. Podman merely being absent is not an error
    here: it's reported once in ``container_runtime`` (see
    :func:`_collect_container_runtime`), and every app then reads as having no
    live stats, exactly like a stopped container.
    """

    running_short_ids: frozenset[str]
    stats_by_short_id: dict[str, _PodmanStats]
    error: str | None = None


def _running_short_ids() -> frozenset[str]:
    """Short ids of the running containers, from one ``podman ps``."""
    return frozenset(
        sid
        for entry in _run_podman_json(["ps", "--format", "json"])
        if entry.get("State") == "running" and (sid := _short_id(entry, "Id"))
    )


def _stats_by_short_id() -> dict[str, _PodmanStats]:
    """Parsed live usage per container, keyed by short id, from one
    ``podman stats``. All podman-shape parsing happens here, so callers work
    only with typed :class:`_PodmanStats`."""
    parsed: dict[str, _PodmanStats] = {}
    for entry in _run_podman_json(["stats", "--no-stream", "--format", "json"]):
        sid = _short_id(entry, "id", "ID", "ContainerID")
        if sid is None:
            continue
        usage_bytes, limit_bytes = _split_mem_usage(entry.get("mem_usage"))
        parsed[sid] = _PodmanStats(
            cpu_percent=_parse_stats_percent(entry.get("cpu_percent")),
            memory_usage_bytes=usage_bytes,
            memory_limit_bytes=limit_bytes,
            memory_percent=_parse_stats_percent(entry.get("mem_percent")),
        )
    return parsed


def _collect_container_stats_batch() -> _ContainerStatsBatch:
    """Running-state + live usage for ALL containers in two podman calls (one
    ``podman ps`` + one ``podman stats``) — instead of an inspect + stats per
    app.

    Podman being absent is not surfaced here: ``container_runtime`` already
    reports it authoritatively (see :func:`_collect_container_runtime`), so we
    return an empty batch and let every app read as "no live stats" rather than
    stamping the same message onto all of them. A podman that *is* present but
    whose probe fails is an unexpected fault, and that we do surface in
    ``error`` so apps don't masquerade as stopped.
    """
    if shutil.which("podman") is None:
        return _ContainerStatsBatch(frozenset(), {})
    try:
        return _ContainerStatsBatch(_running_short_ids(), _stats_by_short_id())
    except Exception as e:
        logger.opt(exception=True).warning("Failed to collect container stats batch")
        return _ContainerStatsBatch(frozenset(), {}, error=f"podman stats unavailable: {e}")


def _app_resources_from_batch(
    batch: _ContainerStatsBatch,
    container_id: str | None,
    cpu_cores_limit: float | None,
    memory_mb_limit: int | None,
) -> AppResourceUsage:
    """Build one app's :class:`AppResourceUsage` from a pre-fetched fleet batch.

    Running state is authoritative (a container appears in ``podman ps`` only
    while running), and the manifest limits are always echoed even when the
    container is down.
    """
    base = AppResourceUsage(
        running=False,
        cpu_percent=None,
        memory_usage_bytes=None,
        memory_limit_bytes=None,
        memory_percent=None,
        cpu_cores_limit=cpu_cores_limit,
        memory_mb_limit=memory_mb_limit,
    )
    if not container_id:
        return base
    if batch.error is not None:
        return attr.evolve(base, error=batch.error)
    short_id = container_id[:12]
    if short_id not in batch.running_short_ids:
        return base
    stats = batch.stats_by_short_id.get(short_id)
    if stats is None:
        # Running per ``podman ps`` but no stats row (a stats gap / race): report
        # running with unknown usage rather than dropping the app.
        return attr.evolve(base, running=True)
    return AppResourceUsage(
        running=True,
        cpu_percent=stats.cpu_percent,
        memory_usage_bytes=stats.memory_usage_bytes,
        memory_limit_bytes=stats.memory_limit_bytes,
        memory_percent=stats.memory_percent,
        cpu_cores_limit=cpu_cores_limit,
        memory_mb_limit=memory_mb_limit,
    )


# ─── health checks ───────────────────────────────────────────────────────────


async def _collect_app_health(local_port: int | None, health_check: str | None) -> AppHealth:
    """Probe an app's health endpoint over its loopback proxy port.

    Mirrors the router's readiness contract (any HTTP status < 500 = healthy)
    and honours the app's declared ``health_check`` path, defaulting to ``/``.
    Never raises: connection/timeout errors degrade to healthy=False + an error.
    """
    path = health_check or "/"
    if not path.startswith("/"):
        path = "/" + path
    checked_path = path
    if not local_port:
        return AppHealth(
            checked=False, healthy=None, status_code=None, checked_path=checked_path, error="no local port"
        )
    url = f"http://127.0.0.1:{local_port}{path}"
    try:
        async with httpx.AsyncClient(timeout=_HEALTH_TIMEOUT_S) as client:
            resp = await client.get(url)
    except httpx.TimeoutException:
        return AppHealth(checked=True, healthy=False, status_code=None, checked_path=checked_path, error="timeout")
    except httpx.HTTPError as e:
        return AppHealth(checked=True, healthy=False, status_code=None, checked_path=checked_path, error=str(e))
    except Exception as e:
        return AppHealth(checked=True, healthy=False, status_code=None, checked_path=checked_path, error=str(e))
    return AppHealth(
        checked=True,
        healthy=resp.status_code < 500,
        status_code=resp.status_code,
        checked_path=checked_path,
    )


# ─── outbound reachability ───────────────────────────────────────────────────


def _reachability_targets(config: Config) -> list[tuple[str, str]]:
    """Assemble the list of (label, url) reachability targets from static hosts
    plus any config-driven URLs (cert-api Keycloak issuer, ACME directory,
    redirect domain). The cert-api base URL is deliberately not probed."""
    targets: list[tuple[str, str]] = list(_STATIC_REACHABILITY_TARGETS)
    if config.cert_api_keycloak_issuer_url:
        targets.append(("cert_api_keycloak", config.cert_api_keycloak_issuer_url))
    if config.acme_directory_url:
        targets.append(("acme_directory", config.acme_directory_url))
    if config.my_openhost_redirect_domain:
        targets.append(("openhost_redirect", f"https://{config.my_openhost_redirect_domain}"))
    # De-duplicate by URL while preserving order (static ACME may equal config ACME).
    seen: set[str] = set()
    deduped: list[tuple[str, str]] = []
    for label, url in targets:
        if url in seen:
            continue
        seen.add(url)
        deduped.append((label, url))
    return deduped


async def _probe_reachability(client: httpx.AsyncClient, label: str, url: str) -> ReachabilityResult:
    """Probe a single external URL. Any HTTP response = reachable (we only care
    that DNS + TCP + TLS succeeded, not the status)."""
    start = asyncio.get_event_loop().time()
    try:
        resp = await client.get(url)
    except httpx.TimeoutException:
        return ReachabilityResult(
            label=label, url=url, reachable=False, status_code=None, latency_ms=None, error="timeout"
        )
    except httpx.HTTPError as e:
        return ReachabilityResult(
            label=label, url=url, reachable=False, status_code=None, latency_ms=None, error=str(e)
        )
    except Exception as e:
        return ReachabilityResult(
            label=label, url=url, reachable=False, status_code=None, latency_ms=None, error=str(e)
        )
    latency_ms = round((asyncio.get_event_loop().time() - start) * 1000, 1)
    return ReachabilityResult(
        label=label, url=url, reachable=True, status_code=resp.status_code, latency_ms=latency_ms
    )


async def _collect_reachability(config: Config) -> list[ReachabilityResult]:
    """Probe all external dependency hosts concurrently. Never raises."""
    targets = _reachability_targets(config)
    try:
        async with httpx.AsyncClient(timeout=_REACHABILITY_TIMEOUT_S, follow_redirects=False) as client:
            return list(await asyncio.gather(*(_probe_reachability(client, label, url) for label, url in targets)))
    except Exception:
        logger.opt(exception=True).warning("Failed to collect reachability diagnostics")
        return []


# ─── platform diagnostics ────────────────────────────────────────────────────


def _row_get(row: sqlite3.Row, key: str) -> Any:
    """Safe column access: returns None when the column is absent from the row."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


async def _collect_app_health_and_resources(
    row: sqlite3.Row, batch: _ContainerStatsBatch
) -> tuple[AppHealth, AppResourceUsage]:
    """Collect the (health, resource-usage) pair for one app row.

    Shared by the platform summary and the per-app bundle so both surface the
    same live data with the same defensive semantics. Resource usage is read
    from the pre-fetched fleet-wide ``batch`` rather than shelling out to podman
    per app.
    """
    local_port = _row_get(row, "local_port")
    health = await _collect_app_health(
        local_port if isinstance(local_port, int) else None,
        _row_get(row, "health_check"),
    )
    resources = _app_resources_from_batch(
        batch,
        _row_get(row, "container_id"),
        _row_get(row, "cpu_cores"),
        _row_get(row, "memory_mb"),
    )
    return health, resources


async def _collect_app_summary(row: sqlite3.Row, batch: _ContainerStatsBatch) -> AppDiagnosticsSummary:
    version, runtime_type = _manifest_fields(row["manifest_raw"])
    # Fall back to the stored column when the manifest can't be re-parsed.
    if version is None:
        version = _row_get(row, "version")
    repo_path = row["repo_path"]
    git = await _collect_git_info(Path(repo_path) if repo_path else None)
    health, resources = await _collect_app_health_and_resources(row, batch)
    return AppDiagnosticsSummary(
        app_id=row["app_id"],
        name=row["name"],
        status=row["status"],
        version=version,
        runtime_type=runtime_type,
        error_message=row["error_message"],
        git=git,
        health=health,
        resources=resources,
    )


def _zone_domain(db: sqlite3.Connection) -> str:
    """The primary domain name for the bundle, or "" when none is configured.

    Not guarded: a read failure here means the control-plane DB is broken, which
    a diagnostics bundle can't meaningfully paper over, so it propagates (→ 500,
    see the module docstring). "" therefore unambiguously means "no primary
    domain configured", never "couldn't read it".
    """
    primary = primary_domain_or_none(db)
    return primary.name if primary else ""


async def _collect_openhost_git_info() -> GitInfo:
    """Git checkout state for the running OpenHost install.

    Falls back to an empty-but-stable :class:`GitInfo` when OPENHOST_PROJECT_DIR
    isn't a git checkout (e.g. a tarball deploy) so the field shape is stable for
    consumers.
    """
    openhost_git = await _collect_git_info(OPENHOST_PROJECT_DIR)
    if openhost_git is None:
        return GitInfo(branch=None, sha="", short_sha="", dirty=False, remote_url=None)
    return openhost_git


def _collect_host_facts() -> tuple[SystemInfo, ContainerRuntimeInfo, dict[str, str], HostResourcePressure]:
    """Gather the purely-synchronous host facts in one shot.

    Bundled so the whole group (which includes the blocking ``podman info``
    probe) can be offloaded to a single worker thread rather than blocking the
    event loop.
    """
    return (
        _collect_system_info(),
        _collect_container_runtime(),
        _collect_dependencies(),
        _collect_resource_pressure(),
    )


async def _collect_storage(config: Config) -> dict[str, object]:
    """Disk/storage slice. Runs off the event loop; degrades to ``{}`` on error
    rather than sinking the whole bundle."""
    try:
        return await asyncio.to_thread(storage_status, config)
    except Exception:
        logger.opt(exception=True).warning("Failed to collect storage status for diagnostics")
        return {}


async def _collect_apps(db: sqlite3.Connection) -> list[AppDiagnosticsSummary]:
    """Per-app summary slice: one entry per installed app.

    The ``apps`` query is deliberately *not* guarded: if the control-plane DB
    can't be read the whole instance is broken and there's nothing to report, so
    the error propagates and the endpoint returns 500 rather than a hollow
    "0 apps" bundle. A fault collecting a *single* app, by contrast, only drops
    that app — see ``_safe_summary`` below.
    """
    rows = db.execute(
        "SELECT app_id, name, status, version, runtime_type, error_message, repo_path, "
        "manifest_raw, local_port, health_check, container_id, cpu_cores, memory_mb "
        "FROM apps ORDER BY name"
    ).fetchall()

    # One podman ps + one podman stats for the whole fleet, off the event loop —
    # instead of an inspect + stats per app.
    batch = await asyncio.to_thread(_collect_container_stats_batch)

    async def _safe_summary(row: sqlite3.Row) -> AppDiagnosticsSummary | None:
        # Collect each app independently so one malformed row can't drop the
        # rest of the fleet from the bundle.
        try:
            return await _collect_app_summary(row, batch)
        except Exception:
            logger.opt(exception=True).warning("Failed to collect diagnostics for one app; skipping it")
            return None

    # Collect all apps concurrently (git + health); resource usage comes from the
    # shared batch. gather preserves input order, so apps stay sorted by name.
    results = await asyncio.gather(*(_safe_summary(row) for row in rows))
    return [summary for summary in results if summary is not None]


async def collect_platform_diagnostics(db: sqlite3.Connection, config: Config) -> PlatformDiagnostics:
    """Assemble the full instance diagnostics bundle.

    The independent parts are collected concurrently — the slowest (reachability)
    bounds the wall-clock rather than the sum — and every blocking probe runs off
    the event loop so serving diagnostics can't stall the router.
    """
    openhost_git, host_facts, storage, reachability, apps = await asyncio.gather(
        _collect_openhost_git_info(),
        asyncio.to_thread(_collect_host_facts),
        _collect_storage(config),
        _collect_reachability(config),
        _collect_apps(db),
    )
    system, container_runtime, dependencies, resource_pressure = host_facts
    return PlatformDiagnostics(
        schema_version=DIAGNOSTICS_SCHEMA_VERSION,
        generated_at=datetime.now(UTC).isoformat(),
        zone_domain=_zone_domain(db),
        openhost=openhost_git,
        system=system,
        container_runtime=container_runtime,
        dependencies=dependencies,
        storage=storage,
        resource_pressure=resource_pressure,
        reachability=reachability,
        apps=apps,
    )


async def collect_app_diagnostics(row: sqlite3.Row, config: Config, db: sqlite3.Connection) -> AppDiagnostics:
    """Assemble a per-app diagnostics bundle for the given ``apps`` row."""
    version, runtime_type = _manifest_fields(row["manifest_raw"])
    if version is None:
        try:
            version = row["version"]
        except (IndexError, KeyError):
            version = None

    repo_path = row["repo_path"]

    # One fleet-wide podman ps + stats (shared with the platform bundle's path),
    # off the event loop; ``_collect_host_facts`` likewise wraps blocking probes.
    async def _health_and_resources() -> tuple[AppHealth, AppResourceUsage]:
        batch = await asyncio.to_thread(_collect_container_stats_batch)
        return await _collect_app_health_and_resources(row, batch)

    # Mirror the platform path: gather the independent slices so the slowest
    # bounds the wall-clock instead of the sum.
    git, openhost_git, host_facts, (health, resources) = await asyncio.gather(
        _collect_git_info(Path(repo_path) if repo_path else None),
        _collect_openhost_git_info(),
        asyncio.to_thread(_collect_host_facts),
        _health_and_resources(),
    )
    system, container_runtime, _dependencies, resource_pressure = host_facts

    return AppDiagnostics(
        schema_version=DIAGNOSTICS_SCHEMA_VERSION,
        generated_at=datetime.now(UTC).isoformat(),
        zone_domain=_zone_domain(db),
        app_id=row["app_id"],
        name=row["name"],
        status=row["status"],
        version=version,
        runtime_type=runtime_type,
        error_message=row["error_message"],
        container_id=row["container_id"],
        git=git,
        health=health,
        resources=resources,
        system=system,
        container_runtime=container_runtime,
        resource_pressure=resource_pressure,
        openhost=openhost_git,
    )
