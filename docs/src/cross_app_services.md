## Cross-App Services

Apps can expose services that other apps consume. The router (compute_space) mediates all cross-app communication — apps never talk directly to each other.

A **service** is identified by a URL (typically a git URL pointing at a spec) plus a SemVer version. Multiple apps can implement the same service; the router resolves which provider to use per call.

### Service identity + spec

Services are identified by URL, e.g. `github.com/imbue-openhost/openhost/services/secrets`. The URL is a git path (and optional subdirectory).

Currently the URL is only used comparatively, to match providers and consumers. But probably in the future we'll fetch the git URL to lookup details about the service, eg from a manifest file.
You should put documentation on the service specification at the service URL - ideally a formal openAPI spec, but informal documentation is allowable also. This should document the API endpoints, and also the structure of the permission grants consumer apps must acquire to use the service (these are up to the service to define).

Versions follow SemVer. Providers declare a specific version; consumers declare a SemVer specifier (e.g. `>=0.1.0`). Major version indicate breaking changes; minor versions indicate backward-compatible changes. The git repo should have tags for each version (eg v1.1.1, or sub/dir:v1.1.1 if at a subdir).

Some example specs live in this repo's `services/` folder:
- **secrets** (`github.com/imbue-openhost/openhost/services/secrets`) — key-value secret storage. Grant payload: `{"key": "<NAME>"}` or `{"key": "*"}` for full access. Provider returns only the values for keys in the granted set.
- **oauth** (`github.com/imbue-openhost/openhost/services/oauth`) — OAuth token acquisition/refresh for third-party APIs. Grant payload: `{"provider": "<name>", "scopes": [...]}`.

### Provider apps

Provider apps declare what services they offer in their manifest:

```toml
[[services.v2.provides]]
service = "github.com/imbue-openhost/openhost/services/secrets"
version = "0.1.0"
endpoint = "/api/"
```

Service requests land rooted at `endpoint` in the provider app, ie `app_name.openhost_space.com/<endpoint>/<whatever_api_route>`.

### Consumer apps

Consumer apps declare what they consume, with a `shortname` they'll use to call it and the permission grants they're requesting:

```toml
[[services.v2.consumes]]
service = "github.com/imbue-openhost/openhost/services/oauth"
shortname = "oauth"
version = ">=0.1.0"
grants = [
    {provider = "google", scopes = ["https://www.googleapis.com/auth/gmail.readonly"]},
    {provider = "github", scopes = ["repo"]},
]
```

Each entry in `grants` is either an opaque string (e.g. `"read"`) or a TOML/JSON object (e.g. `{key = "DB_URL"}`). Strings work well for simple flag-style permissions; objects are for grants with structured fields. The shape is defined by the service, not the router — providers receive the raw grants verbatim and decide what they mean.

`shortname` must match `^[a-z][a-z0-9_-]{0,31}$` and be unique within the manifest.

### Calling a service

```
GET|POST|WS|... [OPENHOST_ROUTER_URL]/api/services/v2/call/<shortname>/<rest>
```
along with `Authorization: Bearer $OPENHOST_APP_TOKEN` header for server-side requests.
`OPENHOST_ROUTER_URL` is provided to apps as an env var.

This endpoint is app-specific - the router loads the consumer's manifest, finds the `[[services.v2.consumes]]` entry matching `<shortname>`, resolves the correct provider of the requested service, and proxies to `provider_app.OPENHOST_ROUTER_URL/<provider_endpoint>/<rest>`.

The router identifies and authenticates the calling app two ways:
- **Server-side calls:** must include `Authorization: Bearer $OPENHOST_APP_TOKEN`. Each app gets a unique `OPENHOST_APP_TOKEN` injected as an env var at deploy time.
- **Browser calls:** the request's `Origin` is matched against the app's subdomain, with the owner session cookie authenticating the user. No bearer token is needed for these — the browser provides the cookie automatically.

Service calls should be API-only - the user's browser should never be redirected to a service endpoint, with the exception of permission grant pages.

If permission is needed to access the service, a 403 is returned - see the Permissions section below.

### Provider selection

Each service URL has a configured default provider - by default it's first app installed providing that service, but can be configured in Cloud in a Bottle's settings.

If the resolved default's version doesn't satisfy the consumer's version specifier, the router returns 503 `service_not_available`.

#### Calling a specific provider

To call a non-default provider, include the `X-OpenHost-Provider` header with the target app's `app_id`:

```
GET [OPENHOST_ROUTER_URL]/api/services/v2/call/<shortname>/<rest>
Authorization: Bearer $OPENHOST_APP_TOKEN
X-OpenHost-Provider: <provider_app_id>
```

If the specified provider doesn't exist for that service, or its version doesn't match the consumer's version specifier, the router returns 503 `service_not_available`. Omitting the header uses the default provider as before.

#### Discovering providers

Apps can list all providers for a service using the discovery endpoint (see Management API below). This is useful when aggregating data across multiple providers of the same service.

### Permissions

Permissions are **opaque grant payloads** (strings or JSON objects), scoped per `(consumer_app, service_url)`. The router stores grants and forwards the granted set (those that apply to the calling app and service URL) to the provider on every call — but **the provider is what enforces access**, not the router. This lets services define whatever permission shape they need.

**Grant scope** is one of:
- `global`: applies to **all providers** of the given service. This is the scope for manifest-declared permission grants.
- `app`: applies only to a **specific provider** app. These are often data-dependent permissions, eg "access email for me@example.com", where that data only lives in a specific provider app, so a global scoped permission wouldn't make sense.

**On every proxied call, the router injects:**
- `X-OpenHost-Consumer-Name: <consumer_app_name>`
- `X-OpenHost-Consumer-Id: <consumer_app_id>`
- `X-OpenHost-Permissions: <json array of granted payloads>`

Each entry in the permissions array is `{"grant": <payload>, "scope": "global"|"app"}`. The router pre-filters the array: every `scope: "app"` entry the provider sees is one the router resolved as addressed to *this* provider. The internal `provider_app_id` field is stripped before forwarding, since the provider is the addressee by construction. Strict providers may still want to reject `scope: "global"` entries entirely if every legitimate grant for them flows through their own consent UI (the oauth service does this).

#### Global-scoped grants

When a consumer calls without sufficient grants, the provider returns:

```json
HTTP 403
{
  "error": "permission_required",
  "required_grant": {
    "grant": { ... },
    "scope": "global"
  }
}
```

For `scope: "global"`, the router rewrites the response to add a `grant_url` pointing at the owner-facing approval page. For `scope: "app"`, the provider must include its own `grant_url` (see below).

The consumer redirects the owner to `grant_url`; after approval, the call can be retried.

**Granting at deploy time.** Global-scoped permissions specified in the consumer app manifest (`grants`) can be granted as part of the app install, either in the Cloud in a Bottle web UI or via the compute_space CLI's `--grant-permissions-v2` flag.

#### Provider-app-scoped permissions

Because app-scoped grants are often data-dependent — "this consumer may access photos in *this* folder", "*this* email inbox", "*this* set of files" — the provider is responsible for the whole approval UX. The router only stores the resulting grant. This also allows a provider to ensure that its data can't be accessed by a permission granted to a different provider of this service, if that's desired. It gives this provider full control over access to its own data.

There's currently no way to grant these at install time of a consumer app (since consumers can potentially interact with multiple providers).

When a consumer calls without sufficient grants, the provider returns:

```json
HTTP 403
{
  "error": "permission_required",
  "required_grant": {
    "grant": { ... },
    "scope": "app",
    "grant_url": GRANT_URL
  }
}
```

Note for `scope: "app"`, the provider must include its own `grant_url`.

1. **Consumer hands the user off.** The consumer redirects the user's browser to the `grant_url` returned in the 403. The consumer should arrange for a `return_to` URL on its own subdomain to be propagated to that page (typical convention: include `return_to=https://<consumer>.<zone>/...` on the request to `grant_url`)

2. **Provider renders a consent page.** This is a normal page in the provider app — the user is on `<provider_app>.<zone>` with the owner cookie. The page should:
   - State plainly *which consumer app* is asking and *exactly what data* it's asking for. The narrower and more concrete, the better ("Grant *photos-app* read access to the folder `Vacation/Italy`?" beats "Grant *photos-app* read access?").
   - Let the user shape the grant where it makes sense (pick which folder, which inbox, which subset of items, etc.).
   - Run any side flows the grant needs — e.g. an OAuth dance with a third party, picking a row from the provider's own DB, prompting for a passphrase.

3. **Provider creates the grant.** Once the user confirms (and any side flow has completed), the provider's backend calls:

   ```
   POST [OPENHOST_ROUTER_URL]/api/permissions/v2/grant_app_scoped
   Authorization: Bearer $OPENHOST_APP_TOKEN
   Content-Type: application/json

   {"consumer_app_name": "<consumer_app_name>", "service_url": "<url>", "grant": <grant>}
   ```

   Identify the consumer by **name** — the value from the `X-OpenHost-Consumer-Name` header, which is also what your consent page should have shown the user. Keying the grant to the name is what makes that page trustworthy: the field the user read is the field the access is granted to, so a page that names the wrong app grants to the wrong app instead of to itself. An unknown name is a 404.

   The router takes the provider's own app ID from the bearer token, so the provider can only grant permissions *for itself* — it can't create grants attributed to another provider. The grant body is the same shape the provider will later see in `X-OpenHost-Permissions`. str or json can be used.

4. **Provider sends the user back.** After the grant call succeeds, the provider should redirect the user's browser to the consumer-supplied `return_to`. The consumer can then retry the original service call, which will now succeed. If the user declines, the provider should also redirect to `return_to` (without creating a grant) so the consumer can show its own "permission denied" UI rather than leaving the user stranded on the provider's page.


### Management API

These endpoints back the owner-facing UI and are authenticated by the owner login cookie unless otherwise noted. Bodies and responses are JSON.

**Permissions**

- `GET /api/permissions/v2[?app_id=<consumer_app_id>]` — list grants, optionally filtered to one consumer app. Returns an array of `{consumer_app_id, service_url, grant, scope, provider_app_id}`.
- `POST /api/permissions/v2/grant_global_scoped` — grant a global-scoped permission. Body: `{app_id, service_url, grant}`.
- `POST /api/permissions/v2/grant_app_scoped` — grant an app-scoped permission. **Authenticated with the calling provider's app token** (not the owner cookie); the provider's app ID is taken from the token. Body: `{consumer_app_name, service_url, grant}` (404 if no app has that name). Used by provider apps after running their own user-facing approval flow (e.g. an OAuth dance).
- `POST /api/permissions/v2/revoke` — revoke a permission. Body: `{app_id, service_url, grant, scope?, provider_app_id?}`. `scope` defaults to `"global"`. 404 if no matching row.

The `grant` field on these endpoints is whatever shape the service defines — passed through the router verbatim.

**Default provider**

Each service URL has at most one default provider (set automatically to the first app to register; the owner can change it). Calls without an explicit provider use this default.

- `GET /api/services/v2/defaults` — list all defaults; each entry includes `service_url`, `app_id`, and `app_name`.
- `GET /api/services/v2/providers?service=<url>` — list every registered provider for a service, with `is_default`, `service_version`, `endpoint`, and app `status`. Accepts both owner auth and app bearer tokens, so consumer apps can discover providers at runtime.
- `POST /api/services/v2/defaults` — set the default. Body: `{service_url, app_id}`. 404 if that app doesn't actually provide the service.
- `DELETE /api/services/v2/defaults` — clear the default. Body: `{service_url}`. After this, calls to the service return 503 until a new default is set (or another provider is installed).

### Retrofitting existing apps

Many existing apps provide or consume APIs in a way that is not native to Cloud in a Bottle, and can be adapted readily to consume or expose these through the service interface.

#### Consumer apps

Consumer apps need to include the `Authorization` header on server-side requests. This can be added by a little reverse proxy running in in the application container, like [`mitmproxy`](https://github.com/mitmproxy/mitmproxy).

```sh
# <shortname> matches the [[services.v2.consumes]] entry in your manifest.
mitmdump -p 9000 \
  --mode reverse:$OPENHOST_ROUTER_URL/api/services/v2/call/<shortname> \
  --set "modify_headers=/~q/Authorization/Bearer $OPENHOST_APP_TOKEN"
```

Then point the app at `http://localhost:9000` and a request to `http://localhost:9000/target_api_endpoint` will reach `$OPENHOST_ROUTER_URL/api/services/v2/call/<shortname>/target_api_endpoint` with the bearer token attached.

#### Provider apps

Provider apps should verify permissions attached to inbound requests. A simple permission structure might just requires a string grant `FULL_ACCESS`, and can be implemented with a Caddyfile rule that verifies that the request header (something like `X-OpenHost-Permissions=[{"grant": "FULL_ACCESS", "scope": "global", ...}]`) contains this permission before passing on to the app's existing API:

```
:8080 {
  @denied not header_regexp X-OpenHost-Permissions "\"grant\"\\s*:\\s*\"FULL_ACCESS\""
  handle @denied {
    header Content-Type application/json
    respond `{"error":"permission_required","required_grant":{"grant":"FULL_ACCESS","scope":"global"}}` 403
  }
  reverse_proxy localhost:3000
}```



