"""V2 cross-app service proxy: git-URL-based service identity, versioned routing,
provider-side permission validation.

Routes:
    OPTIONS /api/services/v2/call/{shortname}/{rest:path} — CORS preflight
    *       /api/services/v2/call/{shortname}/{rest:path} — proxied call
    WS      /api/services/v2/call/{shortname}/{rest:path} — proxied WS call
    GET     /api/services/v2/oauth_callback              — OAuth callback fan-out

The HTTP call route proxies the response back to the client.  Small responses
(including 403s) are automatically buffered by ``proxy_http_request``, so we
can inspect and inject ``grant_url`` into ``permission_required`` payloads
before relaying them.

CORS:
- we need to handle CORS so that cross-origin service requests are allowed for client-side requests from the user's browser.
- this involves responding to the preflight OPTIONS request, and also adding CORS headers to the proxied response.
- the receiving app will not see or interact with this.
"""

import json
import sqlite3
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlencode

import attr
from litestar import HttpMethod
from litestar import MediaType
from litestar import Request
from litestar import Router
from litestar import WebSocket
from litestar import get
from litestar import route
from litestar import websocket
from litestar.datastructures import MutableScopeHeaders
from litestar.di import NamedDependency
from litestar.exceptions import ClientException
from litestar.exceptions import NotAuthorizedException
from litestar.exceptions import NotFoundException
from litestar.exceptions import PermissionDeniedException
from litestar.exceptions import ServiceUnavailableException
from litestar.openapi import ResponseSpec
from litestar.params import FromPath
from litestar.response import Response
from litestar.response.base import ASGIResponse

from compute_space.config import Config
from compute_space.core.apps import find_app_by_name
from compute_space.core.apps import get_app_from_hostname
from compute_space.core.auth.permissions_v2 import get_granted_permissions_v2
from compute_space.core.domains import primary_domain_or_none
from compute_space.core.service_interface.services_v2 import lookup_shortname
from compute_space.core.service_interface.services_v2 import resolve_provider
from compute_space.web.auth.auth import require_app_auth
from compute_space.web.auth.auth import verify_app_auth
from compute_space.web.helpers.proxy import proxy_http_request
from compute_space.web.helpers.proxy import proxy_websocket_request

_CALL_PATH = "/api/services/v2/call/{shortname:str}/{rest:path}"
_HTTP_METHODS = [
    HttpMethod.GET,
    HttpMethod.POST,
    HttpMethod.PUT,
    HttpMethod.DELETE,
    HttpMethod.PATCH,
    HttpMethod.HEAD,
]


def _build_permissions_header(consumer_app_id: str, service_url: str, provider_app_id: str) -> str:
    """JSON for ``X-OpenHost-Permissions`` — the consumer's grants applicable to this provider.

    Includes global-scoped grants and any app-scoped grants targeting this
    provider.  ``provider_app_id`` is stripped from each entry since the
    provider already knows it's the addressee.
    """
    grants = get_granted_permissions_v2(consumer_app_id, service_url)
    forwarded = [
        {"grant": g.grant, "scope": g.scope}
        for g in grants
        if g.scope == "global" or g.provider_app_id == provider_app_id
    ]
    return json.dumps(forwarded)


def _consumer_identity_headers(consumer_app_id: str, db: sqlite3.Connection) -> dict[str, str]:
    """X-OpenHost-Consumer-Name + X-OpenHost-Consumer-Id headers for a consumer.

    Providers get both: the human-readable name (good for logs/UI) and the
    stable app_id (good for keying stored data that should survive renames).
    """
    row = db.execute("SELECT name FROM apps WHERE app_id = ?", (consumer_app_id,)).fetchone()
    assert row is not None
    return {"X-OpenHost-Consumer-Name": row["name"], "X-OpenHost-Consumer-Id": consumer_app_id}


def _inject_grant_url_if_global(
    response: ASGIResponse,
    service_url: str,
    consumer_app_id: str,
    config: Config,
    db: sqlite3.Connection,
) -> ASGIResponse:
    """If the provider's 403 body is ``permission_required`` with a global-scoped
    grant request, decorate it with ``grant_url`` pointing at the owner-facing
    approval page.  The provider populates ``grant_url`` itself for app-scoped
    grants — this only handles the global case."""
    raw = response.body if isinstance(response.body, bytes) else response.body.encode("utf-8")
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return response

    required_grant = body.get("required_grant") if isinstance(body, dict) else None
    if not isinstance(required_grant, dict):
        return response
    if required_grant.get("scope", "global") != "global":
        return response
    grant = required_grant.get("grant")
    if not isinstance(grant, (str, dict)):
        return response

    required_grant["grant_url"] = _approve_grant_url(consumer_app_id, service_url, grant, db)

    return ASGIResponse(
        body=json.dumps(body).encode(),
        status_code=403,
        headers=list(_carry_response_headers(response.headers)),
        media_type=MediaType.JSON,
    )


def _carry_response_headers(headers: MutableScopeHeaders) -> Iterable[tuple[str, str]]:
    """Forward provider headers except framing ones that ASGIResponse owns itself."""
    for k, v in headers.items():
        if k.lower() in ("content-length", "content-type"):
            continue
        yield k, v


def _approve_grant_url(consumer_app_id: str, service_url: str, grant: Any, db: sqlite3.Connection) -> str:
    # urlencode each value: service_url contains "/" and ":", grant is JSON with "{", "}",
    # ",", '"' — all of which break query-string parsing if interpolated raw.
    query = urlencode({"app": consumer_app_id, "service": service_url, "grant": json.dumps(grant, sort_keys=True)})
    approve_path = f"/approve-permissions-v2?{query}"
    # Cross-app approval is server-side (no browsing request in hand), so this stays on
    # the canonical/primary domain; use its scheme rather than a hardcoded https so a
    # plain-http primary (e.g. a `.local` instance) builds a correct URL.
    primary = primary_domain_or_none(db)
    if primary is None:
        return approve_path
    return f"{primary.scheme}://{primary.name}{approve_path}"


def _cors_headers(origin: str) -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Credentials": "true",
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, PATCH, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
    }


def _add_cors_response_headers(response: ASGIResponse, request: Request[Any, Any, Any]) -> None:
    origin = request.headers.get("Origin", None)
    if origin:
        for k, v in _cors_headers(origin).items():
            response.headers.add(k, v)


@route(_CALL_PATH, http_method=[HttpMethod.OPTIONS], raises=[PermissionDeniedException])
async def service_call_cors(
    request: Request[Any, Any, Any],
    shortname: FromPath[str],
    rest: FromPath[str],
    db: NamedDependency[sqlite3.Connection],
) -> Response[str]:
    """Hande CORS preflight HTTP OPTIONS request, respond with appropriate CORS headers."""
    origin = request.headers.get("Origin", None)
    # block CORS preflight if Origin is not a known app - no auth headers yet but we can at least verify this,
    # to help avoid XSRF from external sites.
    if origin is None or get_app_from_hostname(origin, db) is None:
        raise PermissionDeniedException(detail="Forbidden")
    return Response(content="", status_code=204, headers=_cors_headers(origin))


@attr.s(auto_attribs=True, frozen=True)
class ServiceRequest:
    service_url: str
    version_spec: str
    provider_app_id: str
    provider_port: int
    target_path: str
    extra_headers: list[tuple[str, str]]


def _service_call_common(
    consumer_app_id: str,
    shortname: str,
    rest: str,
    db: sqlite3.Connection,
    provider_app_id: str | None = None,
) -> ServiceRequest:
    """Resolve a consumer's shortname to a proxyable request.

    Only the two resolution calls are translated, so an accidental lookup bug still surfaces as a 500.
    """
    try:
        service_url, version_spec = lookup_shortname(consumer_app_id, shortname, db)
    except LookupError as e:
        raise NotFoundException(detail=str(e), extra={"code": "shortname_not_declared"}) from e

    try:
        provider_app_id, provider_port, _, provider_endpoint = resolve_provider(
            service_url, version_spec, db, provider_app_id=provider_app_id
        )
    except RuntimeError as e:
        raise ServiceUnavailableException(detail=str(e), extra={"code": "service_not_available"}) from e
    # `rest` is captured as "/sub/path" (leading slash); fold into the provider's endpoint.
    target_path = provider_endpoint.rstrip("/") + "/" + rest.lstrip("/")
    extra_headers = [
        ("X-OpenHost-Permissions", _build_permissions_header(consumer_app_id, service_url, provider_app_id)),
        *_consumer_identity_headers(consumer_app_id, db).items(),
    ]
    return ServiceRequest(
        service_url=service_url,
        version_spec=version_spec,
        provider_app_id=provider_app_id,
        provider_port=provider_port,
        target_path=target_path,
        extra_headers=extra_headers,
    )


@route(
    _CALL_PATH,
    http_method=_HTTP_METHODS,
    guards=[require_app_auth],
    raises=[
        NotFoundException,
        ServiceUnavailableException,
        ClientException,
        NotAuthorizedException,
        PermissionDeniedException,
    ],
    responses={
        502: ResponseSpec(
            data_container=str, media_type=MediaType.TEXT, description="The provider app is unavailable."
        ),
        504: ResponseSpec(data_container=str, media_type=MediaType.TEXT, description="The provider app timed out."),
    },
)
async def service_call(
    shortname: FromPath[str],
    rest: FromPath[str],
    request: Request[Any, Any, Any],
    db: NamedDependency[sqlite3.Connection],
    config: NamedDependency[Config],
) -> ASGIResponse:
    """Proxy a request to the provider declared under <shortname> in the
    consumer's manifest.
    """
    consumer_app_id = verify_app_auth(request)
    provider_override = request.headers.get("X-OpenHost-Provider") or None

    resolved = _service_call_common(consumer_app_id, shortname, rest, db, provider_app_id=provider_override)

    response = await proxy_http_request(
        request,
        target_port=resolved.provider_port,
        override_path=resolved.target_path,
        extra_headers=resolved.extra_headers,
    )

    if response.status_code == 403:
        response = _inject_grant_url_if_global(response, resolved.service_url, consumer_app_id, config, db)

    _add_cors_response_headers(response, request)
    return response


@websocket(_CALL_PATH)
async def service_call_ws(
    socket: WebSocket[Any, Any, Any],
    shortname: FromPath[str],
    rest: FromPath[str],
    db: NamedDependency[sqlite3.Connection],
) -> None:
    """WebSocket variant of ``service_call``"""
    # not using guards bc they currently only return HTTP exceptions
    try:
        consumer_app_id = verify_app_auth(socket)
    except NotAuthorizedException:
        await socket.accept()
        await socket.close(code=4401, reason="Missing or invalid authorization")
        return

    provider_override = socket.headers.get("X-OpenHost-Provider") or None

    try:
        resolved = _service_call_common(consumer_app_id, shortname, rest, db, provider_app_id=provider_override)
    except NotFoundException as e:
        await socket.accept()
        await socket.close(code=4404, reason=e.detail)
        return
    except ServiceUnavailableException as e:
        await socket.accept()
        await socket.close(code=4503, reason=e.detail)
        return

    await proxy_websocket_request(
        socket,
        target_port=resolved.provider_port,
        override_path=resolved.target_path,
        extra_headers=resolved.extra_headers,
    )


@get(
    "/api/services/v2/oauth_callback",
    raises=[ClientException, ServiceUnavailableException],
    responses={
        502: ResponseSpec(
            data_container=str, media_type=MediaType.TEXT, description="The OAuth provider app is unavailable."
        ),
        504: ResponseSpec(
            data_container=str, media_type=MediaType.TEXT, description="The OAuth provider app timed out."
        ),
    },
)
async def oauth_callback_proxy_v2(request: Request[Any, Any, Any]) -> ASGIResponse:
    """Proxy OAuth provider callbacks to the correct oauth service app.

    OAuth providers (Google, GitHub, etc.) redirect to a fixed callback URL on
    MY_REDIRECT_DOMAIN after user authorization. This endpoint receives that
    redirect and forwards it to the oauth app that initiated the flow.

    The oauth app encodes its app name in the OAuth ``state`` parameter as
    JSON: ``{"app": "<app_name>", "nonce": "<random>"}``. This endpoint parses
    that to determine which app should receive the callback, then proxies the
    full request to that app's ``/callback`` handler.
    """
    state_raw = request.query_params.get("state", "")
    if not state_raw:
        raise ClientException(detail="Missing state parameter", extra={"code": "bad_request"})

    try:
        state = json.loads(state_raw)
    except json.JSONDecodeError as e:
        raise ClientException(detail="Invalid state parameter", extra={"code": "bad_request"}) from e

    app_name = state.get("app")
    if not app_name or not isinstance(app_name, str):
        raise ClientException(detail="Missing app in state", extra={"code": "bad_request"})

    app_row = find_app_by_name(app_name)
    if not app_row:
        raise ServiceUnavailableException(
            detail=f"App '{app_name}' not found", extra={"code": "service_not_available"}
        )
    if app_row.status != "running":
        raise ServiceUnavailableException(
            detail=f"App '{app_name}' is not running", extra={"code": "service_not_available"}
        )

    return await proxy_http_request(request, target_port=app_row.local_port, override_path="/callback")


# ─── Router ─────────────────────────────────────────────────────────────────


services_v2_routes = Router(
    path="/",
    # service_call_cors MUST come before service_call: Litestar v2.21.1 has a
    # bug where registering a non-OPTIONS handler first causes an auto-generated
    # OPTIONS handler to be appended, which then silently overwrites any explicit
    # OPTIONS handler via last-writer-wins in route_handler_method_map.
    route_handlers=[service_call_cors, service_call, service_call_ws, oauth_callback_proxy_v2],
)
