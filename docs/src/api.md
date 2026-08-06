# HTTP API

Everything the dashboard and the `oh` CLI do goes through this HTTP API, and
it is open to you directly — scripts, cron jobs, or another machine can drive
a zone without going near the UI.

The reference below is generated from the running code, so it always describes
the version this zone is actually serving.

## Authentication

Every endpoint except `/health` and the `/.well-known/` identity routes
requires an owner API token:

```
Authorization: Bearer <token>
```

Create a token from **Settings → API tokens** in the dashboard, or with
`oh tokens create` if you already have one. Tokens are owner-scoped: a token
can do anything you can do, so treat it like your password.

The base URL is your zone's own domain, e.g. `https://your-zone.example.com`.

```bash
curl -H "Authorization: Bearer $OPENHOST_TOKEN" \
  https://your-zone.example.com/api/apps
```

`oh curl` is a shortcut that injects the token for you and otherwise behaves
like plain `curl`:

```bash
oh curl /api/apps
```

## Two kinds of token

Most routes take the **owner token** described above. The cross-app service
proxy (`/api/services/v2/call/...`) instead takes an **app token** — the one
the router injects into each app as `OPENHOST_APP_TOKEN`. An owner token is
rejected there, and vice versa. The reference labels which of the two each
operation expects.

Those proxy routes pass the path, body and response straight through to the
provider app, so their real contract belongs to that service's own spec; the
reference links out to [Cross-App Services](./cross_app_services.md) for how
resolution and grants work.

Browser pages — the dashboard, login, the approval screens — aren't in the
reference at all: they serve HTML to a session cookie, not JSON to a token.

## When the token is rejected

Auth failure is handled the same way on every owner route, so the reference
doesn't repeat it per operation. Send `Accept: application/json` and a missing,
expired or revoked token gets `401` with an `{"error": ...}` body. Without that
header you get a `302` to `/login` instead — an HTML page, which is what a
plain `curl` sees. Always send the header from a script.

`401` means something different on `/api/clone_and_get_app_info` and
`/api/add_app`: there it carries an `authorize_url` for a repo that needs
GitHub authorization, and your owner token was fine. Check for the field
rather than assuming the token expired.

## Machine-readable spec

`GET /docs/openapi.yaml` serves the raw OpenAPI 3.1 document — point a client
generator or a tool like `schemathesis` at it. The same document is committed
to the OpenHost repo at `compute_space/openapi.yaml` and kept in sync by a
test, so you can generate a client without a zone to hand.

## Reference

The browser below renders the spec, and needs to reach a CDN to do it. On an
offline or egress-filtered zone it won't load — read
[/docs/openapi.yaml](/docs/openapi.yaml) directly instead.

<div id="redoc" class="redoc-embed"></div>
<script src="https://cdn.jsdelivr.net/npm/redoc@2.5.0/bundles/redoc.standalone.js"></script>
<script>
  (() => {
    const host = document.getElementById("redoc");
    if (typeof Redoc === "undefined") {
      host.textContent = "Could not load the spec browser (no CDN access). The raw document is at /docs/openapi.yaml.";
      return;
    }
    Redoc.init(
      "/docs/openapi.yaml",
      {hideDownloadButton: true, expandResponses: "200,201", nativeScrollbars: true},
      host
    );
  })();
</script>
