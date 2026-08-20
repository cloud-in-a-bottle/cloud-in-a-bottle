from __future__ import annotations

import sys
import time

import httpx


def resolve_app_id_by_name(url: str, token: str, app_name: str) -> str:
    """Look up an app's app_id by its current name. Exits if not found."""
    resp = make_api_request(url, token, "GET", "/api/apps")
    apps = resp.json()
    matches = [a for a in apps if a.get("name") == app_name]
    if not matches:
        print(f"No app named {app_name!r}.", file=sys.stderr)
        raise SystemExit(1)
    if len(matches) > 1:
        print(f"Multiple apps named {app_name!r} — refusing to guess.", file=sys.stderr)
        raise SystemExit(1)
    app_id = matches[0]["app_id"]
    assert isinstance(app_id, str)
    return app_id


def authorization_url(body: object) -> str | None:
    """Extract an OAuth URL from current or legacy API error envelopes."""
    if not isinstance(body, dict):
        return None
    extra = body.get("extra")
    candidate = extra.get("authorize_url") if isinstance(extra, dict) else None
    candidate = candidate or body.get("authorize_url")
    return candidate if isinstance(candidate, str) else None


def make_api_request(
    domain: str,
    token: str,
    method: str,
    path: str,
    *,
    data: dict[str, str] | None = None,
    timeout: float = 120,
    raw: bool = False,
) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    resp = httpx.request(
        method,
        f"{domain}{path}",
        headers=headers,
        json=data,
        timeout=timeout,
        follow_redirects=False,
    )
    if not raw and resp.status_code >= 300:
        print(f"Error ({resp.status_code}): {resp.text}", file=sys.stderr)
        raise SystemExit(1)
    return resp


def wait_for_app_running(url: str, token: str, app_id: str, app_name: str) -> None:
    while True:
        time.sleep(3)
        result = make_api_request(url, token, "GET", f"/api/app_status/{app_id}").json()
        s = result.get("status", "unknown")
        if s == "running":
            print(f"{app_name} is running.")
            return
        if s == "error":
            print(f"{app_name} failed: {result.get('error', 'unknown error')}")
            raise SystemExit(1)
        print(f"  status: {s}...")


def wait_for_app_removed(url: str, token: str, app_id: str, app_name: str, timeout: float = 600) -> None:
    """Poll ``/api/app_status/<app_id>`` until it returns 404.

    /remove_app returns 202 immediately and runs the teardown in a
    background thread; the CLI has to wait for the row to disappear
    before claiming success. 10-minute default timeout caps the wait
    so a stuck removal worker doesn't hang the CLI forever.
    """
    deadline = time.time() + timeout
    while True:
        if time.time() > deadline:
            print(
                f"Timed out waiting for {app_name} to finish removing after {timeout:.0f}s. "
                "The server may still be working — re-run 'cb app status' to check.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        time.sleep(2)
        try:
            resp = make_api_request(url, token, "GET", f"/api/app_status/{app_id}", raw=True)
        except httpx.HTTPError as e:
            # Transient network failure during a restart; keep polling.
            print(f"  (network error polling status: {type(e).__name__}; retrying)")
            continue
        if resp.status_code == 404:
            return
        if resp.status_code >= 300:
            print(f"Error polling status: HTTP {resp.status_code}", file=sys.stderr)
            raise SystemExit(1)
        try:
            result = resp.json()
        except ValueError:
            # 2xx non-JSON body (proxy HTML page during a restart).
            print("  (unparseable status response; retrying)")
            continue
        s = result.get("status", "unknown")
        if s == "error":
            print(f"{app_name} removal failed: {result.get('error', 'unknown error')}", file=sys.stderr)
            raise SystemExit(1)
        print(f"  status: {s}...")
