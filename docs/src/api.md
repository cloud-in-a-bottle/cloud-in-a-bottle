# Openhost API

Users take actions on the Openhost level using the dashboard or the `oh` CLI, which go through the HTTP API documented here in OpenAPI format. Custom tooling can also use and consume these API endpoints. The reference is generated on the latest commit using Litestar's OpenAPI framework.

All external calls to the instance are documented here. The [cross_app_services](./cross_app_services.md) page documents how apps can discover and interact with each other, and the [bundled_services](./bundled_services.md) page documents the API for Openhost-default service apps. 

Static parts of browser pages and redirects are not documented. 

## Authentication

All endpoint except `/health` and `/.well-known/*` require an owner API token

```
Authorization: Bearer <token>
```

Tokens are created in Settings/API tokens or `oh tokens create` which both `POST` at `/api/tokens`. All tokens allow for full owner access. The base URL is your zone's domain. 

```bash
curl -H "Authorization: Bearer $OPENHOST_TOKEN" https://your-zone.example.com/api/apps
```

`oh curl` is a shortcut that injects the token for you and otherwise behaves like plain `curl`:

```bash
oh curl /api/apps
```

Auth is standardized across requests and not repeated. An invalid request sent with an `Accept: application/json` header returns `401` with the following cases: 

| Case | Body |
|------|------|
| Missing or invalid token | `{"error": "User or API key authentication required"}` |
| Session cookie given cross-origin |  `{"error": "user authentication only valid for router-origin requests"}` |
| Cross-origin on guarded route (`/login`, `/logout`) | `{"error": "cross-origin request not allowed"}` |

Otherwise, you get `302` redirected to `/login`. 

Individual requests can also have other `401` errors, each documented below. 

## Reference

`GET /docs/openapi.yaml` returns the raw OpenAPI yaml in the Openhost repo at `compute_space/openapi.yaml`. 

[API Reference](/docs/reference/api){target=_blank rel=noopener}
