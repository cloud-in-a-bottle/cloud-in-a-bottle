from __future__ import annotations

import attr

from compute_space.core.proxy_target import ProxyTarget


@attr.s(auto_attribs=True, frozen=True)
class ResolvedProvider:
    """Who serves a service right now, and how to reach them.

    Nothing about a call differs between a builtin and a provider app except ``target``, which is
    what keeps the two from drifting apart.
    """

    service_url: str
    app_id: str
    app_name: str
    service_version: str
    # Path prefix the provider serves this service under; "/" for a builtin.
    endpoint: str
    target: ProxyTarget


class ProviderUnavailable(RuntimeError):
    """No provider can serve this call."""


class NoProviderError(ProviderUnavailable):
    """Nothing provides the service, or the named provider isn't installed."""


class ProviderNotRunningError(ProviderUnavailable):
    """The provider app is installed but not running."""


class ProviderVersionError(ProviderUnavailable):
    """The provider's version doesn't satisfy what the consumer asked for."""
