from __future__ import annotations

from urllib.parse import urlsplit

import httpx

# Served by the router on every host it answers, including the bare domain while an app is served there.
DISCOVERY_PATH = "/.well-known/cloud-in-a-bottle"


class DiscoveryError(Exception):
    pass


def discover_router_url(base_url: str, timeout: float = 10) -> str | None:
    """Ask the instance at ``base_url`` where its router (dashboard and API) lives.

    Returns None for an instance from before discovery existed (404), whose router is at ``base_url`` itself.
    Raises DiscoveryError for anything else that isn't a usable answer, rather than guessing: during an update,
    say, every path answers 503, and falling back then would point the CLI at whatever app is served at the
    bare domain.  No token is sent, since the bare domain may be serving an app.
    """
    try:
        resp = httpx.get(f"{base_url}{DISCOVERY_PATH}", timeout=timeout, follow_redirects=False)
    except httpx.HTTPError as e:
        raise DiscoveryError(f"could not reach {base_url}: {e}") from e
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise DiscoveryError(f"{base_url}{DISCOVERY_PATH} answered HTTP {resp.status_code}")
    try:
        router_url = resp.json()["router_url"]
    except (ValueError, KeyError, TypeError) as e:
        raise DiscoveryError(f"{base_url}{DISCOVERY_PATH} returned a malformed response") from e
    if not isinstance(router_url, str):
        raise DiscoveryError(f"{base_url}{DISCOVERY_PATH} returned a malformed response")
    router_url = router_url.rstrip("/")
    _check_router_url(base_url, router_url)
    return router_url


def _check_router_url(base_url: str, router_url: str) -> None:
    """Only follow an answer on the queried domain itself or one of its subdomains, over https: the API token is
    sent there next."""
    queried = (urlsplit(base_url).hostname or "").lower()
    parsed = urlsplit(router_url)
    host = (parsed.hostname or "").lower()
    on_domain = bool(queried) and (host == queried or host.endswith(f".{queried}"))
    if parsed.scheme != "https" or not on_domain or parsed.path or parsed.query or parsed.fragment:
        raise DiscoveryError(f"{base_url}{DISCOVERY_PATH} pointed at {router_url!r}, which is not on {queried}")
