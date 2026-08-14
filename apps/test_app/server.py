"""Minimal HTTP + WebSocket server for testing app deployment.

Serves both HTTP routes and a WebSocket echo endpoint on the same port,
which is required because the Cloud in a Bottle router proxies both HTTP and WS
traffic to the app's single port.

Routes:
    GET  /health         -> {"status": "ok"}
    GET  /               -> {"app": "test-app", "app_name": "..."}
    GET  /echo-headers   -> {"headers": {...}}
    POST /call-service   -> proxies a v2 service call through the router
    POST <any>           -> {"method": "POST", "body": "...", "path": "..."}
    WS   /ws             -> echo: sends back whatever it receives
    *    <other>         -> 404 {"error": "not found"}
"""

import json
import os
import re

import aiohttp.web

# matches the router's manifest shortname rule
SHORTNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SERVICE_PATH_RE = re.compile(r"^[A-Za-z0-9_/-]*$")


async def handle_health(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return _json_response({"status": "ok"})


async def handle_root(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return _json_response(
        {
            "app": "test-app",
            "app_name": os.environ.get("OPENHOST_APP_NAME", ""),
        }
    )


async def handle_echo_headers(request: aiohttp.web.Request) -> aiohttp.web.Response:
    headers = {k: v for k, v in request.headers.items()}
    return _json_response({"headers": headers})


async def handle_post(request: aiohttp.web.Request) -> aiohttp.web.Response:
    body = await request.text()
    return _json_response({"method": "POST", "body": body, "path": request.path})


async def handle_call_service(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Call another app's v2 service through the router, acting as this app.

    Body: {"shortname": "secrets", "method": "POST", "path": "get", "payload": {...}}
    Returns {"service_status": ..., "service_body": ...} so tests can assert on
    exactly what the service proxy returned to this app.
    """
    router_url = os.environ["OPENHOST_ROUTER_URL"]
    app_token = os.environ["OPENHOST_APP_TOKEN"]
    body = await request.json()
    shortname = body["shortname"]
    method = body.get("method", "POST")
    path = body.get("path", "")
    payload = body.get("payload")
    if not SHORTNAME_RE.match(shortname):
        return _json_response({"error": "invalid shortname"}, status=400)
    if not SERVICE_PATH_RE.match(path):
        return _json_response({"error": "invalid path"}, status=400)
    url = f"{router_url}/api/services/v2/call/{shortname}/{path}"
    async with aiohttp.ClientSession() as session:
        async with session.request(
            method,
            url,
            json=payload,
            headers={"Authorization": f"Bearer {app_token}"},
        ) as resp:
            try:
                service_body = await resp.json()
            except (aiohttp.ContentTypeError, ValueError):
                service_body = await resp.text()
            return _json_response({"service_status": resp.status, "service_body": service_body})


async def handle_ws(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
    """WebSocket echo endpoint: sends back whatever it receives."""
    ws = aiohttp.web.WebSocketResponse()
    await ws.prepare(request)
    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            await ws.send_str(msg.data)
        elif msg.type == aiohttp.WSMsgType.BINARY:
            await ws.send_bytes(msg.data)
        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
            break
    return ws


async def handle_404(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return _json_response({"error": "not found"}, status=404)


def _json_response(data: dict, status: int = 200) -> aiohttp.web.Response:
    return aiohttp.web.Response(
        text=json.dumps(data),
        content_type="application/json",
        status=status,
    )


def create_app() -> aiohttp.web.Application:
    app = aiohttp.web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/", handle_root)
    app.router.add_get("/echo-headers", handle_echo_headers)
    app.router.add_get("/ws", handle_ws)
    app.router.add_post("/call-service", handle_call_service)
    app.router.add_post("/{path:.*}", handle_post)
    # Catch-all for unknown GET paths -> 404
    app.router.add_get("/{path:.*}", handle_404)
    return app


if __name__ == "__main__":
    app = create_app()
    print("Test server listening on :5000", flush=True)
    aiohttp.web.run_app(app, host="0.0.0.0", port=5000, print=None)
