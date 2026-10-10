from __future__ import annotations

from compute_space.core.mdns.packets import TYPE_A
from compute_space.core.mdns.packets import TYPE_AAAA
from compute_space.core.mdns.packets import TYPE_ANY
from compute_space.core.mdns.packets import Query
from compute_space.core.mdns.packets import a_record
from compute_space.core.mdns.packets import build_response
from compute_space.core.mdns.packets import nsec_a_only

MDNS_PORT = 5353
# Short, so a box that moves to a new address (DHCP renewal, wifi -> ethernet) is re-learned quickly.
_TTL = 120
# RFC 6762 section 6.7: replies to legacy (non-5353 source port) queriers cap the TTL at 10s.
_LEGACY_TTL = 10


def _is_ours(name: str, domains: tuple[str, ...], hostname: str) -> bool:
    """``name`` is one of ``domains`` or any name under one.  The bare domain is left alone when it is
    this machine's own ``<hostname>.local``: avahi already publishes that, and answering it too (with
    possibly different addresses) would make avahi see a conflict and rename the host."""
    for domain in domains:
        if name.endswith("." + domain):
            return True
        if name == domain and domain != f"{hostname}.local":
            return True
    return False


def build_answer(query: Query, source_port: int, domains: tuple[str, ...], hostname: str, ip: str) -> bytes | None:
    """The reply to ``query`` for a box reachable at ``ip`` and publishing ``domains`` (and every name
    under them), or None if it asks about nothing we publish."""
    legacy = source_port != MDNS_PORT
    ttl = _LEGACY_TTL if legacy else _TTL
    # Legacy queriers are plain DNS resolvers that don't understand the cache-flush bit.
    cache_flush = not legacy
    answers: list[bytes] = []
    additionals: list[bytes] = []
    answered = []
    for q in query.questions:
        if not _is_ours(q.name, domains, hostname):
            continue
        if q.qtype in (TYPE_A, TYPE_ANY):
            answers.append(a_record(q.name, ip, ttl, cache_flush))
            additionals.append(nsec_a_only(q.name, ttl, cache_flush))
        elif q.qtype == TYPE_AAAA:
            answers.append(nsec_a_only(q.name, ttl, cache_flush))
        else:
            continue
        answered.append(q)
    if not answers:
        return None
    if legacy:
        return build_response(query.id, tuple(answered), answers, additionals)
    return build_response(0, (), answers, additionals)
