from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import date
from typing import Literal
from urllib.parse import urlsplit

import attr
import cattrs
import httpx

from compute_space.core.archive_backend import BackendState
from compute_space.core.settings_store import get_setting
from compute_space.core.tls.keycloak import KeycloakClientCredentials
from compute_space.core.tls.keycloak import KeycloakTokenProvider

SETTING_KEY = "managed_storage_binding"
STATUS_TIMEOUT_SECONDS = 15
Access = Literal["read_write", "read_only", "suspended"]
Phase = Literal["reserved", "bucket_ready", "token_pending", "activating", "ready"]


class ManagedStorageError(Exception):
    pass


def _https_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("expected an HTTPS URL without credentials, query or fragment")
    return value.rstrip("/")


@attr.s(auto_attribs=True, frozen=True)
class ManagedStorageBinding:
    allocation_id: str
    service_url: str
    s3_bucket: str
    s3_endpoint: str

    def __attrs_post_init__(self) -> None:
        if not re.fullmatch(r"[a-f0-9]{32}", self.allocation_id) or not self.s3_bucket:
            raise ValueError("invalid managed storage binding")
        _https_url(self.service_url)
        _https_url(self.s3_endpoint)


@attr.s(auto_attribs=True, frozen=True)
class ManagedUsage:
    used_bytes: int | None
    operation_microcents: int
    storage_microcents: int | None
    read_only_at_microcents: int
    suspend_at_microcents: int
    sample_at: int | None
    period_start: str
    resets_at: str
    operations_observed_at: int | None = None

    def __attrs_post_init__(self) -> None:
        if (self.used_bytes is None) != (self.sample_at is None):
            raise ValueError("storage size and sample time must be known together")
        if not 0 < self.read_only_at_microcents < self.suspend_at_microcents:
            raise ValueError("invalid activity thresholds")
        if not date.fromisoformat(self.period_start) < date.fromisoformat(self.resets_at):
            raise ValueError("invalid usage period")


@attr.s(auto_attribs=True, frozen=True)
class ManagedStatus:
    version: int
    allocation_id: str
    phase: Phase
    capacity_bytes: int
    desired_access: Access
    applied_access: Access
    reason: str
    enforcement_enabled: bool
    stale: bool
    observed_at: int | None
    applied_at: int | None
    reported_at: int
    usage: ManagedUsage | None

    def __attrs_post_init__(self) -> None:
        if self.version != 1 or self.capacity_bytes <= 0:
            raise ValueError("unsupported storage status")


def _integer(value: object, _: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("expected a nonnegative integer")
    return value


def _boolean(value: object, _: object) -> bool:
    if type(value) is not bool:
        raise ValueError("expected a boolean")
    return value


def _string(value: object, _: object) -> str:
    if not isinstance(value, str):
        raise ValueError("expected a string")
    return value


_converter = cattrs.Converter()
_converter.register_structure_hook(int, _integer)
_converter.register_structure_hook(bool, _boolean)
_converter.register_structure_hook(str, _string)


def active_binding(db: sqlite3.Connection, state: BackendState) -> ManagedStorageBinding | None:
    if state.backend != "s3":
        return None
    raw = get_setting(db, SETTING_KEY)
    if raw is None:
        return None
    try:
        binding = _converter.structure(json.loads(raw), ManagedStorageBinding)
    except (ValueError, TypeError, cattrs.BaseValidationError):
        raise ManagedStorageError("Managed storage connection needs attention.") from None
    if binding.s3_bucket != state.s3_bucket or binding.s3_endpoint.rstrip("/") != (state.s3_endpoint or "").rstrip(
        "/"
    ):
        return None
    return binding


async def fetch_status(binding: ManagedStorageBinding, credentials: KeycloakClientCredentials) -> ManagedStatus:
    try:
        async with asyncio.timeout(STATUS_TIMEOUT_SECONDS):
            async with KeycloakTokenProvider.create(credentials, timeout=5) as provider:
                token = await provider.get_token()
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                async with client.stream(
                    "GET",
                    f"{binding.service_url.rstrip('/')}/api/storage/allocations/{binding.allocation_id}/usage",
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                ) as response:
                    if response.status_code != 200:
                        raise ManagedStorageError("Cloud storage usage is temporarily unavailable. Try refreshing.")
                    payload = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        if len(payload) + len(chunk) > 65536:
                            raise ValueError("oversized storage response")
                        payload.extend(chunk)
            status = _converter.structure(json.loads(payload), ManagedStatus)
        if status.allocation_id != binding.allocation_id:
            raise ValueError("allocation mismatch")
        return status
    except ManagedStorageError:
        raise
    except Exception:
        # Identity/provider errors can embed credentials or private upstream text.
        raise ManagedStorageError("Could not load cloud storage usage. Try refreshing.") from None
