#!/usr/bin/env python3
"""End-to-end test for the cb CLI - runs against a real compute space backend.

Assumes `cb login` has already been done (legacy or multi-instance config).
Deploys a test app, exercises all commands, then cleans up.

Usage: python e2e.py
"""

import os
import re
import shutil
import subprocess
import sys
import time

import httpx

from compute_space_cli.config import Instance
from compute_space_cli.config import get_multi_config

REPO_URL = "https://github.com/imbue-ai/openhost-backup"


def _default_instance() -> Instance:
    """Load the default instance for test setup (direct API calls, etc.)."""
    return get_multi_config().resolve()


def cb(*args: str, env: dict[str, str] | None = None) -> str:
    """Run a cb command and return stdout. Raises on non-zero exit."""
    run_env = {**os.environ, **(env or {})}
    result = subprocess.run(["cb", *args], capture_output=True, text=True, timeout=300, env=run_env)
    if result.returncode != 0:
        raise AssertionError(f"cb {' '.join(args)} failed (exit {result.returncode}):\n{result.stderr}")
    return result.stdout


def test_legacy_compat() -> None:
    """Existing commands work with legacy single-instance config."""
    print("=== legacy compatibility ===")

    print("--- cb status ---")
    cb("status")
    print("  ok: compute space reachable")

    print("--- cb instance list ---")
    output = cb("instance", "list")
    assert "default" in output, f"legacy config should show as 'default': {output}"
    print("  ok: legacy config listed as 'default' instance")


def test_instance_management() -> None:
    """Test instance add/list/set-default/remove commands."""
    print("\n=== instance management ===")
    cfg = _default_instance()

    print("--- cb instance add ---")
    cb("instance", "add", "test-inst", cfg.url, cfg.token)
    output = cb("instance", "list")
    assert "test-inst" in output, f"test-inst not in list: {output}"
    print("  ok: instance added")

    print("--- cb instance set-default ---")
    cb("instance", "set-default", "test-inst")
    output = cb("instance", "list")
    for line in output.splitlines():
        if "test-inst" in line:
            assert "default" in line, f"test-inst not marked default: {line}"
            break
    else:
        raise AssertionError("test-inst not found in list")
    print("  ok: default instance changed")

    print("--- cb --instance test-inst status ---")
    cb("--instance", "test-inst", "status")
    print("  ok: --instance flag works")

    print("--- CB_INSTANCE=test-inst cb status ---")
    cb("status", env={"CB_INSTANCE": "test-inst"})
    print("  ok: CB_INSTANCE env var works")

    print("--- cb instance remove ---")
    cb("instance", "set-default", "default")
    cb("instance", "remove", "test-inst")
    output = cb("instance", "list")
    assert "test-inst" not in output, f"test-inst still present: {output}"
    print("  ok: instance removed")


def test_app_lifecycle() -> None:
    """Full app deploy/status/logs/stop/reload/rename/remove cycle."""
    print("\n=== app lifecycle ===")
    cfg = _default_instance()
    app_name = f"test-{int(time.time())}"
    renamed_app = f"{app_name}-renamed"

    print(f"app name: {app_name}")

    # ── status ──
    print("--- cb status ---")
    cb("status")
    print("  ok: compute space reachable")

    # ── app deploy (--wait) ──
    print("--- cb app deploy ---")
    cb("app", "deploy", REPO_URL, "--name", app_name, "--wait")
    print("  ok: app deployed and running")

    try:
        # ── app list ──
        print("--- cb app list ---")
        output = cb("app", "list")
        assert app_name in output, f"app not in list: {output}"
        print("  ok: app appears in list")

        # ── app status ──
        print("--- cb app status ---")
        output = cb("app", "status", app_name)
        assert "running" in output, f"app not running: {output}"
        print("  ok: app status is running")

        # ── app proxy ──
        print("--- curl app / ---")
        app_url = re.sub(r"://", f"://{app_name}.", cfg.url)
        resp = httpx.get(
            f"{app_url}/",
            headers={"Authorization": f"Bearer {cfg.token}"},
            timeout=10,
            follow_redirects=True,
        )
        assert resp.status_code == 200, f"app / returned {resp.status_code}"
        print("  ok: app / returned 200")

        # ── app logs ──
        print("--- cb app logs ---")
        output = cb("app", "logs", app_name)
        lines = output.rstrip().split("\n")
        if len(lines) > 10:
            print(f"  ... ({len(lines) - 10} lines truncated)")
        print("\n".join(lines[-10:]))
        print("  ok: app logs returned")

        # ── app stop ──
        print("--- cb app stop ---")
        cb("app", "stop", app_name)
        output = cb("app", "status", app_name)
        assert "stopped" in output, f"app not stopped: {output}"
        print("  ok: app stopped")

        # ── app reload (--wait) ──
        print("--- cb app reload ---")
        cb("app", "reload", app_name, "--wait")
        output = cb("app", "status", app_name)
        assert "running" in output, f"app not running after reload: {output}"
        print("  ok: app reloaded and running")

        # ── app rename ──
        print("--- cb app rename ---")
        cb("app", "rename", app_name, renamed_app)
        output = cb("app", "list")
        assert renamed_app in output, f"renamed app not in list: {output}"
        print(f"  ok: app renamed to {renamed_app}")
        # from here on, cleanup uses renamed_app
        app_name = renamed_app

        # ── tokens create ──
        print("--- cb tokens create ---")
        output = cb("tokens", "create", "--name", "test-token", "--expiry-hours", "1")
        assert "Token:" in output, f"token not created: {output}"
        print("  ok: token created")

        # ── tokens list ──
        print("--- cb tokens list ---")
        output = cb("tokens", "list")
        assert "test-token" in output, f"token not in list: {output}"
        m = re.search(r"\[(\d+)\] test-token", output)
        assert m, f"could not extract token id from: {output}"
        token_id = m.group(1)
        print(f"  ok: token appears in list (id={token_id})")

        print("--- cb tokens delete ---")
        cb("tokens", "delete", token_id)
        print("  ok: token deleted")

    finally:
        # ── app remove (always clean up) ──
        print("--- cb app remove ---")
        try:
            cb("app", "remove", app_name)
            output = cb("app", "list")
            assert app_name not in output, "app still in list after remove"
            print("  ok: app removed")
        except Exception as e:
            print(f"  cleanup failed: {e}", file=sys.stderr)


def main() -> None:
    if not shutil.which("cb"):
        print(
            "Error: 'cb' command not found.\nInstall it with: cd compute_space_cli && uv tool install --editable .",
            file=sys.stderr,
        )
        sys.exit(1)

    print("=== cb CLI e2e test ===\n")

    test_legacy_compat()
    test_instance_management()
    test_app_lifecycle()

    print("\n=== all tests passed ===")


if __name__ == "__main__":
    main()
