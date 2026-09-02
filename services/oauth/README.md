# OAuth Token Service

**Service URL:** `github.com/imbue-openhost/openhost/services/oauth` · **Version:** 0.1.0

Apps that need to act on the owner's behalf at Google or GitHub do not implement OAuth themselves. They ask the OAuth provider app for a token through the [cross-app service system](https://cloudinabottle.org/docs/creating_an_app/cross_app_services.html), and it owns the flow, the refresh, and the storage.

Request a token by provider and scopes; you get one back, or a URL to send the user to. Multiple accounts per provider are supported, and the flow differs by provider (authorization code for Google, device flow for GitHub) without the consumer having to care.

(Note: this is distinct from the `secrets` key-value service. The OAuth provider app *consumes* the secrets service to fetch its own Google client credentials, but the OAuth functionality described here is provided by the `oauth-provider` app, not a "secrets app".)

## Deployment

The OAuth provider is one of the apps [installed with every instance](https://cloudinabottle.org/docs/operation/overview.html#what-is-already-there), so consumer apps can request tokens as soon as the instance is set up.

- **GitHub** works out of the box. The provider ships with the client credentials of a Cloud in a Bottle GitHub OAuth app, and the device flow does not depend on those credentials being secret, so nothing is asked of the instance owner. The flip side is that they are not swappable: bringing your own GitHub OAuth app means editing `PROVIDERS` in the provider app, since only Google's credentials are read from the `secrets` service.
- **Google** needs the instance owner to bring their own OAuth client. It works, but it is a real errand, and worth knowing before you promise it to a user:

  1. Create a Google Cloud project, enable the APIs the scopes belong to (Gmail, Calendar, ...), and create an OAuth client of type **Web application**.
  2. Register the redirect URI. It is not on your own domain: the provider always sends Google to the shared redirect domain, `https://my.selfhost.imbue.com/api/services/v2/oauth_callback` unless `BOTTLE_MY_REDIRECT_DOMAIN` says otherwise. That page is static and bounces the browser on to your instance. Google's console may want the domain listed as an authorized domain, which is awkward for a domain you do not control.
  3. Store `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET` in the `secrets` app. The provider fetches them lazily, so its container starts cleanly before they exist and fails only when a Google token is actually requested.
  4. Gmail and the other sensitive scopes need Google to verify your app before anyone outside your test-user list can consent. Until then, expect the unverified-app warning and add yourself as a test user on the consent screen.

## How it works

### Requesting a token

1. **App requests a token:** `POST /api/services/v2/call/<shortname>/token` with `{"provider": "google", "scopes": ["https://www.googleapis.com/auth/gmail.readonly"], "return_to": "//app.zone.domain/callback", "account": "user@gmail.com"}` (where `<shortname>` is the alias declared in the app's `[[services.v2.consumes]]` block; the router proxies `token` onto the provider's `/oauth_service/` endpoint)

2. **Token cached?** Returns it immediately. Tokens that don't expire (GitHub) are returned as-is. Expiring tokens (Google) are checked with a 60s buffer.

3. **Token expired?** If a refresh token exists, refreshes automatically and returns the new access token.

4. **No token?** Returns `401` with `{"status": "authorization_required", "authorize_url": "..."}`. The app should redirect/popup the user to this URL. The `authorize_url` handles everything: permissions approval (if needed) and OAuth consent.

### Multiple accounts

Tokens are stored with an `account` label, keyed by `(provider, scopes, account)`. The account name is resolved automatically after OAuth: for Google it's the email address, for GitHub it's the username.

- **Connecting a new account:** Request a token with `"account": "NEW"`. After OAuth, the OAuth provider resolves the identity (e.g. `user@gmail.com`) and stores the token under that name.
- **Using a specific account:** Request with `"account": "user@gmail.com"` to get that account's token.
- **Default fallback:** Request with `"account": "default"` (or omit it, since the request body defaults to `"default"`). If there's exactly one token for that provider+scopes, it's returned regardless of account name.
- **Listing accounts:** `POST /api/services/v2/call/<shortname>/accounts` with `{"provider": "google", "scopes": [...]}` returns `{"accounts": ["user1@gmail.com", "user2@gmail.com"]}`.

### Authorization code flow (Google)

The `authorize_url` points directly to Google's consent page. After the user authorizes:

1. Google redirects to the shared redirect domain (`BOTTLE_MY_REDIRECT_DOMAIN`, `my.selfhost.imbue.com` by default): `https://my.selfhost.imbue.com/api/services/v2/oauth_callback?code=...&state=...`
2. That page is static and browser-local: it knows which instance this browser belongs to and forwards to `<zone_domain>/api/services/v2/oauth_callback?code=...&state=...`
3. The router has an explicit route for `/api/services/v2/oauth_callback` that proxies to the OAuth provider app
4. The OAuth provider exchanges the code for tokens, fetches the user's identity (email), stores the token under that account name, and redirects to `return_to`

Google tokens always request `email` and `openid` scopes in addition to the requested scopes, so the identity can be resolved. The callback also verifies that all requested scopes were granted (Google's granular consent lets users uncheck scopes).

### Device flow (GitHub)

The `authorize_url` points to `oauth-provider.<zone_domain>/device?...`, a page on the OAuth provider app that:

1. Starts the device flow with GitHub, getting a user code and verification URL
2. Shows the user the code + a link to GitHub's verification page + a copy button
3. Polls GitHub in the background until the user authorizes
4. Fetches the user's identity (GitHub username), stores the token, and redirects to `return_to`

### Permission flow (v2)

Before an app can request OAuth tokens, it needs v2 service permissions. Apps declare the services they consume in their `cloudinabottle.toml`:

```toml
[[services.v2.consumes]]
service   = "github.com/imbue-openhost/openhost/services/oauth"
shortname = "oauth"
version   = ">=0.1.0"
grants    = [{provider = "google", scopes = ["email"]}]
```

When an app calls the OAuth service without the needed permissions:

1. The OAuth provider returns `403` with `{"error": "permission_required", "required_grant": {"grant": ..., "scope": "app", "grant_url": "..."}}`
2. For app-scoped grants, the `grant_url` points to the OAuth provider's own consent page where the user approves the OAuth flow
3. After approval, the provider calls `POST /api/permissions/v2/grant_app_scoped` to record the grant
4. For global-scoped grants, the router injects a `grant_url` pointing to `/approve-permissions-v2` where the owner can approve
5. Pending requests are persisted so the owner can review them from the dashboard without needing to re-trigger the flow

Permissions approved by the owner on the deploy page are granted at install time and skip this flow entirely.

### Browser auth (CORS)

The `/api/services/v2/call/` endpoints accept two forms of authentication:

- **Server-to-server:** `Authorization: Bearer <app_token>` header
- **Browser:** owner session cookie + `Origin` header. The router derives the app identity from the Origin's subdomain (e.g. `oauth-demo.zone.domain` → app `oauth-demo`).

For browser requests, the router adds CORS headers allowing the app's subdomain origin with credentials. This lets client-side JavaScript call service endpoints on the zone domain directly.

### Token storage

Tokens are stored in SQLite, keyed by `(provider, scopes, account)` where scopes is a sorted comma-separated string. Different scope sets or accounts = different tokens.

### Token revocation

On deletion via the OAuth provider's dashboard:
- **Google:** revokes the refresh token via `https://oauth2.googleapis.com/revoke` (invalidates both access and refresh tokens)
- **GitHub:** revokes via `DELETE https://api.github.com/applications/{client_id}/token` with basic auth

## App integration patterns

### Server-side (redirects)

The app server requests the token. If auth is needed, it redirects the user's browser to the `authorize_url`. After OAuth, the user is redirected back to the original page (via `return_to`), which retries and gets the token.

```python
from oauth import get_oauth_token, AuthRedirectRequired

try:
    token = await get_oauth_token("google", [GMAIL_SCOPE], account,
                                   return_to="//app.zone.domain/unread")
except AuthRedirectRequired as e:
    return redirect(e.url)

# use token to call Google API server-side
```

### Client-side (popups)

JavaScript calls the service endpoint (`/api/services/v2/call/<shortname>/...`) directly from the browser. If auth is needed, it opens the `authorize_url` in a popup. The popup ends at the app's `/client/oauth-complete` page, which sends a `postMessage` back to the opener.

```javascript
var oauth = new OAuthClient({
    scopes: { google: ['https://...gmail.readonly'], github: ['repo'] },
    appName: 'my-app',
    zoneDomain: 'user.host.imbue.com',
});

// Get token (opens popup if needed, then retries)
var token = await oauth.getToken('google', 'user@gmail.com');

// Connect new account (opens popup, returns when done)
await oauth.connect('google');

// List connected accounts
var accounts = await oauth.getAccounts('google');
```

The `oauth.js` library handles the popup lifecycle, postMessage communication, and token retry. The `/client/oauth-complete` page sends `postMessage({ type: 'auth_complete' })` to `window.opener` and closes itself.

## Providers

### Google (auth code flow)

- OAuth client type: **Web application**
- Created at https://console.cloud.google.com/apis/credentials
- Redirect URI: the shared redirect domain, `https://my.selfhost.imbue.com/api/services/v2/oauth_callback` by default
- Enable needed APIs at https://console.cloud.google.com/apis/library
- No authorized JavaScript origins needed (server-side flow)
- Tokens expire (~1 hour) but have refresh tokens for automatic renewal
- Note: Google's device flow only supports a very limited set of scopes (no Gmail, Calendar, etc.), which is why we use the auth code flow

### GitHub (device flow)

Credentials for this one are bundled with the provider app; the notes below are what registering your own would involve.

- OAuth app created at https://github.com/settings/developers (or org settings)
- Enable "Device Flow" in the app settings
- No callback URL needed for the device flow
- Tokens don't expire and have no refresh token
- Device flow supports all scopes

## Adding a new provider

Add an entry to the providers configuration in the OAuth provider app with either `"flow": "auth_code"` or `"flow": "device"`:

```python
# Auth code flow provider
"newprovider": {
    "flow": "auth_code",
    "client_id": "...",
    "client_secret": "...",
    "auth_url": "https://provider.com/oauth/authorize",
    "token_url": "https://provider.com/oauth/token",
    "revoke_url": "https://provider.com/oauth/revoke",  # or None
    "extra_auth_params": {},  # provider-specific params for the auth URL
},

# Device flow provider
"anotherprovider": {
    "flow": "device",
    "client_id": "...",
    "client_secret": "...",
    "device_code_url": "https://provider.com/login/device/code",
    "token_url": "https://provider.com/login/oauth/access_token",
    "revoke_url": None,
},
```

For auth code providers, register the redirect URI: `https://my.<base_domain>/api/services/v2/oauth_callback`

For device flow providers, no redirect URI is needed.

To support identity resolution (account names), add a case in `fetch_account_identity()` that calls the provider's userinfo endpoint.

If the provider has a non-standard revocation API (like GitHub), add a case in the `revoke_token()` function.

## Source

The machine-readable spec for this service is [`openapi.yaml`](./openapi.yaml), alongside this file. The provider app lives in [`apps/oauth_provider/`](../../apps/oauth_provider/), and [`apps/oauth_demo/`](../../apps/oauth_demo/) is a working consumer with both a server-side and a browser-side demo. Copy from the demo rather than from this page if you want something that runs.
