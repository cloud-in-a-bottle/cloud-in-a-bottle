from __future__ import annotations

import logging
from contextvars import ContextVar

import attr
import httpx

from compute_space.core.proxy_target import client_for
from compute_space.core.service_interface.headers import router_consumer_headers
from compute_space.core.service_interface.provider import ResolvedProvider

SECRETS_SERVICE_URL = "github.com/imbue-openhost/openhost/services/secrets"
SECRETS_VERSION = ">=0.1.0,<0.2.0"

_reading_secrets: ContextVar[bool] = ContextVar("reading_export_secrets", default=False)


class _SecretTransportFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _reading_secrets.get()


# HTTPX logs upstream reason phrases at INFO; HTTPcore logs headers and exceptions at DEBUG.
# These are the loggers used by client_for's direct HTTP/1 transport (no proxies or HTTP/2).
# A context-local filter leaves unrelated concurrent requests' logging alone.
for _logger_name in ("httpx", "httpcore.connection", "httpcore.http11"):
    logging.getLogger(_logger_name).addFilter(_SecretTransportFilter())


class ExportError(RuntimeError):
    """Only fixed, non-sensitive messages may cross the export's provider boundary."""


@attr.s(auto_attribs=True, frozen=True)
class SecretReadResult:
    values: dict[str, str]
    missing: tuple[str, ...]


async def _request(http: httpx.AsyncClient, url: str, keys: list[str] | None = None) -> dict[str, object]:
    # The export adapter has authorized this operation before we assert router grants.
    permissions = [{"grant": {"key": key}, "scope": "global"} for key in keys or []]
    headers = dict(router_consumer_headers(permissions))
    try:
        if keys is None:
            response = await http.get(url, headers=headers, follow_redirects=False)
        else:
            response = await http.post(url, json={"keys": keys}, headers=headers, follow_redirects=False)
        if response.status_code != 200:
            raise ExportError("Secrets provider did not complete the export request.")
        body = response.json()
        if not isinstance(body, dict):
            raise ExportError("Secrets provider returned an invalid response.")
        return body
    except Exception:
        # Do not propagate HTTP errors, JSON parse errors, upstream bodies or exception chains.
        # Providers (including in-process ones) may put secret values in any of those.
        raise ExportError("Secrets provider did not return a valid export response.") from None


async def read_secret_values(provider: ResolvedProvider, referenced_keys: set[str]) -> SecretReadResult:
    """Read only approved keys, using one pinned target for wildcard enumeration and retrieval."""
    token = _reading_secrets.set(True)
    try:
        return await _read_secret_values(provider, referenced_keys)
    finally:
        _reading_secrets.reset(token)


async def _read_secret_values(provider: ResolvedProvider, referenced_keys: set[str]) -> SecretReadResult:
    http, base_url = client_for(provider.target, timeout=30.0, trust_env=False)
    endpoint = f"{base_url}/{provider.endpoint.strip('/')}".rstrip("/")
    keys = referenced_keys - {"*"}
    async with http:
        if "*" in referenced_keys:
            listing = (await _request(http, f"{endpoint}/list")).get("keys")
            if not isinstance(listing, list):
                raise ExportError("Secrets provider returned an invalid key list.")
            for entry in listing:
                if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
                    raise ExportError("Secrets provider returned an invalid key list.")
                key = entry["key"]
                if not key or key == "*":
                    raise ExportError("Secrets provider returned an invalid key list.")
                keys.add(key)
        if not keys:
            return SecretReadResult(values={}, missing=())
        body = await _request(http, f"{endpoint}/get", sorted(keys))

    values = body.get("secrets")
    missing = body.get("missing", [])
    if (
        not isinstance(values, dict)
        or not isinstance(missing, list)
        or any(not isinstance(key, str) for key in missing)
    ):
        raise ExportError("Secrets provider returned an invalid key response.")
    missing_keys = set(missing)
    # Explicit absence is valid, but every requested key must be accounted for exactly once.
    # Reject extra missing names rather than exporting metadata about unrequested keys.
    if len(missing_keys) != len(missing) or not missing_keys <= keys or missing_keys.intersection(values):
        raise ExportError("Secrets provider returned inconsistent key results.")
    present_keys = keys - missing_keys
    if any(key not in values or not isinstance(values[key], str) for key in present_keys):
        raise ExportError("Secrets provider did not account for every approved key.")
    # Empty strings are valid. Extra, unrequested provider keys never enter the document.
    return SecretReadResult(
        values={key: values[key] for key in sorted(present_keys)}, missing=tuple(sorted(missing_keys))
    )
