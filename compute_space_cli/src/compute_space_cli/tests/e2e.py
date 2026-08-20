#!/usr/bin/env python3
"""End-to-end test for the bottle CLI - runs against a real compute space backend.

Assumes `bottle login` has already been done (legacy or multi-instance config).
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


def bottle(*args: str, env: dict[str, str] | None = None) -> str:
    """Run a bottle command and return stdout. Raises on non-zero exit."""
    run_env = {**os.environ, **(env or {})}
    result = subprocess.run(["bottle", *args], capture_output=True, text=True, timeout=300, env=run_env)
    if result.returncode != 0:
        raise AssertionError(f"bottle {' '.join(args)} failed (exit {result.returncode}):\n{result.stderr}")
    return result.stdout


def test_legacy_compat() -> None:
    """Existing commands work with legacy single-instance config."""
    print("=== legacy compatibility ===")

    print("--- bottle status ---")
    bottle("status")
    print("  ok: compute space reachable")

    print("--- bottle instance list ---")
    output = bottle("instance", "list")
    assert "default" in output, f"legacy config should show as 'default': {output}"
    print("  ok: legacy config listed as 'default' instance")


def test_instance_management() -> None:
    """Test instance add/list/set-default/remove commands."""
    print("\n=== instance management ===")
    cfg = _default_instance()

    print("--- bottle instance add ---")
    bottle("instance", "add", "test-inst", cfg.url, cfg.token)
    output = bottle("instance", "list")
    assert "test-inst" in output, f"test-inst not in list: {output}"
    print("  ok: instance added")

    print("--- bottle instance set-default ---")
    bottle("instance", "set-default", "test-inst")
    output = bottle("instance", "list")
    for line in output.splitlines():
        if "test-inst" in line:
            assert "default" in line, f"test-inst not marked default: {line}"
            break
    else:
        raise AssertionError("test-inst not found in list")
    print("  ok: default instance changed")

    print("--- bottle --instance test-inst status ---")
    bottle("--instance", "test-inst", "status")
    print("  ok: --instance flag works")

    print("--- BOTTLE_INSTANCE=test-inst bottle status ---")
    bottle("status", env={"BOTTLE_INSTANCE": "test-inst"})
    print("  ok: BOTTLE_INSTANCE env var works")

    print("--- bottle instance remove ---")
    bottle("instance", "set-default", "default")
    bottle("instance", "remove", "test-inst")
    output = bottle("instance", "list")
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
    print("--- bottle status ---")
    bottle("status")
    print("  ok: compute space reachable")

    # ── app deploy (--wait) ──
    print("--- bottle app deploy ---")
    bottle("app", "deploy", REPO_URL, "--name", app_name, "--wait")
    print("  ok: app deployed and running")

    try:
        # ── app list ──
        print("--- bottle app list ---")
        output = bottle("app", "list")
        assert app_name in output, f"app not in list: {output}"
        print("  ok: app appears in list")

        # ── app status ──
        print("--- bottle app status ---")
        output = bottle("app", "status", app_name)
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
        print("--- bottle app logs ---")
        output = bottle("app", "logs", app_name)
        lines = output.rstrip().split("\n")
        if len(lines) > 10:
            print(f"  ... ({len(lines) - 10} lines truncated)")
        print("\n".join(lines[-10:]))
        print("  ok: app logs returned")

        # ── app stop ──
        print("--- bottle app stop ---")
        bottle("app", "stop", app_name)
        output = bottle("app", "status", app_name)
        assert "stopped" in output, f"app not stopped: {output}"
        print("  ok: app stopped")

        # ── app reload (--wait) ──
        print("--- bottle app reload ---")
        bottle("app", "reload", app_name, "--wait")
        output = bottle("app", "status", app_name)
        assert "running" in output, f"app not running after reload: {output}"
        print("  ok: app reloaded and running")

        # ── app rename ──
        print("--- bottle app rename ---")
        bottle("app", "rename", app_name, renamed_app)
        output = bottle("app", "list")
        assert renamed_app in output, f"renamed app not in list: {output}"
        print(f"  ok: app renamed to {renamed_app}")
        # from here on, cleanup uses renamed_app
        app_name = renamed_app

        # ── tokens create ──
        print("--- bottle tokens create ---")
        output = bottle("tokens", "create", "--name", "test-token", "--expiry-hours", "1")
        assert "Token:" in output, f"token not created: {output}"
        print("  ok: token created")

        # ── tokens list ──
        print("--- bottle tokens list ---")
        output = bottle("tokens", "list")
        assert "test-token" in output, f"token not in list: {output}"
        m = re.search(r"\[(\d+)\] test-token", output)
        assert m, f"could not extract token id from: {output}"
        token_id = m.group(1)
        print(f"  ok: token appears in list (id={token_id})")

        print("--- bottle tokens delete ---")
        bottle("tokens", "delete", token_id)
        print("  ok: token deleted")

    finally:
        # ── app remove (always clean up) ──
        print("--- bottle app remove ---")
        try:
            bottle("app", "remove", app_name)
            output = bottle("app", "list")
            assert app_name not in output, "app still in list after remove"
            print("  ok: app removed")
        except Exception as e:
            print(f"  cleanup failed: {e}", file=sys.stderr)


def main() -> None:
    if not shutil.which("bottle"):
        print(
            "Error: 'bottle' command not found.\nInstall it with: cd compute_space_cli && uv tool install --editable .",
            file=sys.stderr,
        )
        sys.exit(1)

    print("=== bottle CLI e2e test ===\n")

    test_legacy_compat()
    test_instance_management()
    test_app_lifecycle()

    print("\n=== all tests passed ===")


if __name__ == "__main__":
    main()
