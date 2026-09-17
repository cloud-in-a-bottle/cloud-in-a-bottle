from __future__ import annotations

import logging
import sqlite3
from contextvars import ContextVar

import attr
import httpx

from compute_space.core.proxy_target import client_for
from compute_space.core.service_interface.headers import router_consumer_headers
from compute_space.core.service_interface.provider import ProviderUnavailable
from compute_space.core.service_interface.provider import ResolvedProvider
from compute_space.core.service_interface.resolve import resolve_provider

SECRETS_SERVICE_URL = "github.com/imbue-openhost/openhost/services/secrets"
SECRETS_VERSION = ">=0.1.0,<0.2.0"

_accessing_secrets: ContextVar[bool] = ContextVar("accessing_definition_secrets", default=False)


class _SecretTransportFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _accessing_secrets.get()


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
    token = _accessing_secrets.set(True)
    try:
        return await _read_secret_values(provider, referenced_keys)
    finally:
        _accessing_secrets.reset(token)


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


class SecretImportError(RuntimeError):
    def __init__(self, message: str, saved_secret_count: int = 0) -> None:
        super().__init__(message)
        self.saved_secret_count = saved_secret_count


def _secret_descriptions(body: object) -> dict[str, str | None]:
    if not isinstance(body, list):
        raise ValueError
    descriptions: dict[str, str | None] = {}
    for entry in body:
        if not isinstance(entry, dict):
            raise ValueError
        key = entry.get("key", entry.get("name"))
        description = entry.get("description", "")
        if (
            not isinstance(key, str)
            or not key
            or key in descriptions
            or (description is not None and not isinstance(description, str))
        ):
            raise ValueError
        descriptions[key] = description
    return descriptions


async def import_secret_values(db: sqlite3.Connection, values: dict[str, str]) -> int:
    """Owner-confirmed UPSERTs through one captured provider's existing owner JSON API."""
    if not values:
        return 0
    try:
        provider = resolve_provider(SECRETS_SERVICE_URL, SECRETS_VERSION, db)
    except ProviderUnavailable:
        raise SecretImportError("An available, compatible selected Secrets provider is required.") from None
    saved = 0
    token = _accessing_secrets.set(True)
    try:
        http, base_url = client_for(provider.target, timeout=30.0, trust_env=False)
        # This is an owner API at a fixed path, not the provider's read-only V2 service endpoint.
        endpoint = f"{base_url}/api/secrets"
        headers = dict(router_consumer_headers([])) | {"X-OpenHost-Is-Owner": "true"}
        async with http:
            response = await http.get(endpoint, headers=headers, follow_redirects=False)
            if response.status_code != 200:
                raise ValueError
            descriptions = _secret_descriptions(response.json())
            # Check every outgoing description before any writes, not while encoding each POST.
            for key in values:
                if (description := descriptions.get(key, "")) is not None:
                    description.encode("utf-8")
            for key, value in sorted(values.items()):
                response = await http.post(
                    endpoint,
                    headers=headers,
                    json={"key": key, "value": value, "description": descriptions.get(key, "")},
                    follow_redirects=False,
                )
                if response.status_code not in (200, 201):
                    raise ValueError
                body = response.json()
                if not isinstance(body, dict) or body.get("ok") is not True:
                    raise ValueError
                saved += 1
        return saved
    except Exception:
        # A failed response can follow a successful write. Report only confirmed saves and
        # never imply HTTP requests form an atomic transaction or expose upstream details.
        raise SecretImportError(
            "Selected Secrets provider could not complete its owner API request. "
            "Some values may already have been saved. Check Secrets before retrying.",
            saved,
        ) from None
    finally:
        _accessing_secrets.reset(token)
