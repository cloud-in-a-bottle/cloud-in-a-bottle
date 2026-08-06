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
from litestar.exceptions import NotAuthorizedException
from litestar.params import FromPath
from litestar.response import Response
from litestar.response.base import ASGIResponse
from packaging.specifiers import InvalidSpecifier
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from compute_space.config import Config
from compute_space.core.apps import find_app_by_name
from compute_space.core.apps import get_app_from_hostname
from compute_space.core.auth.permissions_v2 import get_granted_permissions_v2
from compute_space.core.containers import get_docker_logs
from compute_space.core.installer import GRANT_KEY_CAPABILITY
from compute_space.core.installer import GRANT_KEY_REPO_URL_PREFIX
from compute_space.core.installer import INSTALLER_SERVICE_URL
from compute_space.core.installer import INSTALLER_SERVICE_VERSION
from compute_space.core.installer import INSTALL_CAPABILITY
from compute_space.core.installer import InstallError
from compute_space.core.installer import check_install_allowed
from compute_space.core.installer import install_from_repo_url
from compute_space.core.manifest import parse_manifest_from_string
from compute_space.core.platform_service import PLATFORM_SERVICE_URL
from compute_space.core.services_v2 import ServiceNotAvailable
from compute_space.core.services_v2 import ShortnameNotDeclared
from compute_space.core.services_v2 import build_grant_approval_url
from compute_space.core.services_v2 import lookup_shortname
from compute_space.core.services_v2 import resolve_provider
from compute_space.web.auth.auth import require_app_auth
from compute_space.web.auth.auth import verify_app_auth
from compute_space.web.helpers.proxy import proxy_http_request
from compute_space.web.helpers.proxy import proxy_websocket_request
from compute_space.web.routes.platform_dispatch import handle_platform_request

_CALL_PATH = "/api/services/v2/call/{shortname:str}/{rest:path}"
_HTTP_METHODS = [
    HttpMethod.GET,
    HttpMethod.POST,
    HttpMethod.PUT,
    HttpMethod.DELETE,
    HttpMethod.PATCH,
    HttpMethod.HEAD,
]


def _json_error(error: str, message: str, status: int) -> Response[dict[str, Any]]:
    return Response(content={"error": error, "message": message}, status_code=status, media_type=MediaType.JSON)


def _json_ok(body: dict[str, Any]) -> Response[dict[str, Any]]:
    return Response(content=body, status_code=200, media_type=MediaType.JSON)


def _asgi_json_error(error: str, message: str, status: int) -> ASGIResponse:
    """JSON error as an already-encoded ASGIResponse — for handlers that return ASGIResponse."""
    return ASGIResponse(
        body=json.dumps({"error": error, "message": message}).encode(),
        status_code=status,
        media_type=MediaType.JSON,
    )


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
    # Thin wrapper kept for the existing call sites; the shared builder lives in
    # core.services_v2 so the platform service can reuse it too.
    return build_grant_approval_url(consumer_app_id, service_url, grant, db)


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


@route(_CALL_PATH, http_method=[HttpMethod.OPTIONS])
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
        return Response(content="Forbidden", status_code=403, media_type=MediaType.TEXT)
    return Response(content="", status_code=204, headers=_cors_headers(origin))


@attr.s(auto_attribs=True, frozen=True)
class InstallerServiceRequest:
    service_url: str
    version_spec: str


@attr.s(auto_attribs=True, frozen=True)
class PlatformServiceRequest:
    """A call to the router-internal platform service (dispatched in-process,
    like the installer)."""

    service_url: str
    version_spec: str


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
) -> ServiceRequest | InstallerServiceRequest | PlatformServiceRequest:
    service_url, version_spec = lookup_shortname(consumer_app_id, shortname, db)

    if service_url == INSTALLER_SERVICE_URL:
        return InstallerServiceRequest(
            service_url=service_url,
            version_spec=version_spec,
        )
    elif service_url == PLATFORM_SERVICE_URL:
        return PlatformServiceRequest(
            service_url=service_url,
            version_spec=version_spec,
        )
    else:
        provider_app_id, provider_port, _, provider_endpoint = resolve_provider(
            service_url, version_spec, db, provider_app_id=provider_app_id
        )
        # `rest` is captured as "/sub/path" (leading slash); fold into the
        # provider's endpoint.
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


@route(_CALL_PATH, http_method=_HTTP_METHODS, guards=[require_app_auth])
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

    try:
        resolved = _service_call_common(consumer_app_id, shortname, rest, db, provider_app_id=provider_override)
    except ShortnameNotDeclared as e:
        return _asgi_json_error("shortname_not_declared", e.message, 404)
    except ServiceNotAvailable as e:
        return _asgi_json_error("service_not_available", e.message, 503)

    if isinstance(resolved, InstallerServiceRequest):
        installer_response = await _handle_installer_request(
            consumer_app_id, resolved.version_spec, rest, request, db, config
        )
        return installer_response.to_asgi_response(None, request=request)

    if isinstance(resolved, PlatformServiceRequest):
        platform_response = await handle_platform_request(
            consumer_app_id, resolved.version_spec, rest, request, db, config
        )
        return platform_response.to_asgi_response(app=request.app, request=request)

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
    except ShortnameNotDeclared as e:
        await socket.accept()
        await socket.close(code=4404, reason=e.message)
        return
    except ServiceNotAvailable as e:
        await socket.accept()
        await socket.close(code=4503, reason=e.message)
        return

    if isinstance(resolved, (InstallerServiceRequest, PlatformServiceRequest)):
        await socket.accept()
        await socket.close(code=1011, reason="This router-internal service is not available over WebSocket")
        return

    await proxy_websocket_request(
        socket,
        target_port=resolved.provider_port,
        override_path=resolved.target_path,
        extra_headers=resolved.extra_headers,
    )


@get("/api/services/v2/oauth_callback")
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
        return _asgi_json_error("bad_request", "Missing state parameter", 400)

    try:
        state = json.loads(state_raw)
    except json.JSONDecodeError:
        return _asgi_json_error("bad_request", "Invalid state parameter", 400)

    app_name = state.get("app")
    if not app_name or not isinstance(app_name, str):
        return _asgi_json_error("bad_request", "Missing app in state", 400)

    app_row = find_app_by_name(app_name)
    if not app_row:
        return _asgi_json_error("service_not_available", f"App '{app_name}' not found", 503)
    if app_row.status != "running":
        return _asgi_json_error("service_not_available", f"App '{app_name}' is not running", 503)

    return await proxy_http_request(request, target_port=app_row.local_port, override_path="/callback")


# ─── Installer (router-internal v2 service) ─────────────────────────────────
#
# The installer has no provider app — its handlers run in-process so they can
# share the router's DB and apps.* state.  Apps that consume it declare:
#
#     [[services.v2.consumes]]
#     service   = "github.com/imbue-openhost/openhost/services/installer"
#     shortname = "installer"
#     version   = ">=0.1.0"
#     grants    = [{capability = "install", repo_url_prefix = "https://..."}]
#
# and call /api/services/v2/call/installer/{install,status/<name>,logs/<name>}.


async def _handle_installer_request(
    consumer_app_id: str,
    version_spec: str,
    rest: str,
    request: Request[Any, Any, Any],
    db: sqlite3.Connection,
    config: Config,
) -> Response[Any]:
    """Dispatch installer v2 service requests in-process.

    Routes:
        POST /install                — body: {repo_url, app_name?}
        GET  /status/<app_name>      — only for apps this consumer installed
        GET  /logs/<app_name>        — only for apps this consumer installed
    """
    try:
        spec = SpecifierSet(version_spec)
    except InvalidSpecifier:
        return _json_error("bad_request", f"Invalid version specifier: {version_spec}", 400)
    if Version(INSTALLER_SERVICE_VERSION) not in spec:
        return _json_error(
            "service_not_available",
            f"installer version {INSTALLER_SERVICE_VERSION} does not match {version_spec}",
            503,
        )

    method = str(request.method)
    parts = rest.strip("/").split("/")

    if method == "POST" and parts == ["install"]:
        try:
            body = await request.json()
        except Exception:
            return _json_error("bad_request", "request body must be JSON object", 400)
        if not isinstance(body, dict):
            return _json_error("bad_request", "request body must be JSON object", 400)
        repo_url = (body.get("repo_url") or "").strip()
        if not repo_url:
            return _json_error("bad_request", "repo_url is required", 400)
        app_name = (body.get("app_name") or "").strip() or None

        grants = [g.grant for g in get_granted_permissions_v2(consumer_app_id, INSTALLER_SERVICE_URL)]
        if (reason := check_install_allowed(repo_url, grants)) is not None:
            return _installer_permission_denied(consumer_app_id, repo_url, reason, db, config)

        try:
            result = await install_from_repo_url(repo_url, config, db, app_name=app_name, installed_by=consumer_app_id)
        except InstallError as exc:
            return _json_error("install_failed", exc.message, exc.status_code)
        return _json_ok({"ok": True, "app_name": result.app_name, "status": result.status})

    if method == "GET" and len(parts) == 2 and parts[0] in ("status", "logs"):
        sub, app_name = parts
        row, denied = _lookup_consumer_install(consumer_app_id, app_name, db)
        if denied is not None:
            return denied
        assert row is not None
        if sub == "status":
            return _json_ok({"status": row["status"], "error": row["error_message"]})
        logs = get_docker_logs(app_name, config.temporary_data_dir, row["container_id"])
        return Response(content=logs, status_code=200, media_type="text/plain; charset=utf-8")

    return _json_error("bad_request", f"Unknown installer endpoint: {method} /{rest.lstrip('/')}", 404)


def _lookup_consumer_install(
    consumer_app_id: str, app_name: str, db: sqlite3.Connection
) -> tuple[sqlite3.Row | None, Response[dict[str, Any]] | None]:
    row = db.execute(
        "SELECT status, error_message, container_id, installed_by FROM apps WHERE name = ?",
        (app_name,),
    ).fetchone()
    if not row:
        return None, _json_error("not_found", f"app {app_name!r} not found", 404)
    if row["installed_by"] != consumer_app_id:
        return None, _json_error("forbidden", f"{consumer_app_id} did not install {app_name!r}", 403)
    return row, None


def _proposed_install_grant_from_manifest(
    consumer_app_id: str, repo_url: str, db: sqlite3.Connection
) -> dict[str, str]:
    """Pick the install grant payload to offer the owner on a 403.

    Prefers a grant the consumer **already declared** in its
    ``[[services.v2.consumes]]`` block for the installer service whose
    ``repo_url_prefix`` matches ``repo_url`` — so a manifest-declared broad
    grant (e.g. ``"https://github.com/"``) gets approved once and covers every
    subsequent install instead of producing one approval prompt per repo.

    Falls back to a per-URL grant only if the consumer's manifest declares no
    installer grants at all, or none whose prefix covers the requested URL.
    """
    fallback = {GRANT_KEY_CAPABILITY: INSTALL_CAPABILITY, GRANT_KEY_REPO_URL_PREFIX: repo_url}
    row = db.execute("SELECT manifest_raw FROM apps WHERE app_id = ?", (consumer_app_id,)).fetchone()
    if not row or not row["manifest_raw"]:
        return fallback
    try:
        manifest = parse_manifest_from_string(row["manifest_raw"])
    except Exception:
        return fallback

    for consume in manifest.consumes_services_v2:
        if consume.service != INSTALLER_SERVICE_URL:
            continue
        for g in consume.grants:
            if not isinstance(g, dict):
                continue
            if g.get(GRANT_KEY_CAPABILITY) != INSTALL_CAPABILITY:
                continue
            prefix = g.get(GRANT_KEY_REPO_URL_PREFIX, "")
            if not isinstance(prefix, str):
                continue
            if prefix in ("", "*") or repo_url.startswith(prefix):
                return {GRANT_KEY_CAPABILITY: INSTALL_CAPABILITY, GRANT_KEY_REPO_URL_PREFIX: prefix}
    return fallback


def _installer_permission_denied(
    consumer_app_id: str, repo_url: str, reason: str, db: sqlite3.Connection, config: Config
) -> Response[dict[str, Any]]:
    grant = _proposed_install_grant_from_manifest(consumer_app_id, repo_url, db)
    body = {
        "error": "permission_required",
        "message": reason,
        "required_grant": {
            "grant": grant,
            "scope": "global",
            "grant_url": _approve_grant_url(consumer_app_id, INSTALLER_SERVICE_URL, grant, db),
        },
    }
    return Response(content=body, status_code=403, media_type=MediaType.JSON)


# ─── Router ─────────────────────────────────────────────────────────────────


services_v2_routes = Router(
    path="/",
    # service_call_cors MUST come before service_call: Litestar v2.21.1 has a
    # bug where registering a non-OPTIONS handler first causes an auto-generated
    # OPTIONS handler to be appended, which then silently overwrites any explicit
    # OPTIONS handler via last-writer-wins in route_handler_method_map.
    route_handlers=[service_call_cors, service_call, service_call_ws, oauth_callback_proxy_v2],
)
