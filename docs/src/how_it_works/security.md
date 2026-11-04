# Security

An instance is a machine on the public internet, running app code that you may not fully trust. We do a lot to make this safer, but the user should be aware of how things work in order to keep it secure.

## App sandbox

Every app runs as a rootless Podman container under the unprivileged `host` user. Container-root maps to an unprivileged subuid on the host, not to real root, and bind mounts use idmapped mounts so files an app writes are owned by `host` on disk.

By default a container gets:

- **Its own data directories and nothing else.** `/data/app_data/<app>` and, if requested, its temp and archive directories. It cannot see other apps' data, the router's database, TLS keys, or anything else on the machine.
- **No capabilities beyond the Docker defaults.** The router drops all capabilities and re-adds the standard set, plus `no-new-privileges`.
- **No devices beyond the OCI baseline** (`/dev/null`, `/dev/zero`, `/dev/random`, and friends).
- **One port, on loopback.** The container's HTTP port is published to `127.0.0.1` on the host, so nothing reaches an app except through the router.
- **A resolver pointed at the instance's own DNS view**, which is what lets an app reach a sibling app at its public URL and get normal routing and auth applied on the way in.

Apps can request additional capabilities/permissions that significantly elevate the access they have to your instance. These are documented in [the manifest spec](../creating_an_app/manifest_spec.md); it is your choice if you are comfortable installing apps requesting elevated permissions.

Apps intentionally cannot communicate directly with other apps - any app-to-app communications need to be permissioned and go through the [Service Interface](../creating_an_app/cross_app_services.md).

## Who can reach an app

Every route requires the owner's session unless the app's manifest lists it in `public_paths`. The router checks this before proxying, so an unauthenticated request never reaches an app that did not ask for one.

Three credentials authenticate as you, all documented in [Overview](./overview.md#access): the browser session cookie, API tokens, and each app's own token for [cross-app calls](../creating_an_app/cross_app_services.md). App tokens identify the calling app to a provider; they are not owner credentials and cannot be used to reach the dashboard.

Owner sessions are refused on cross-origin requests, so JavaScript running in one app cannot make owner-authenticated calls to the router or to another app on your behalf.

The exception is full-page GET navigations (links, redirects; not frames, images, scripts or `fetch`). These carry your session into apps from any site, so OAuth providers can redirect back, and into the router from sites sharing your instance's parent domain (on a shared parent like `bottle.cloud`, that includes other instances). As with `SameSite=Lax` cookies, an app's GET routes must not change anything.

### Plain-HTTP domains

On HTTPS this check uses the browser's Fetch Metadata headers, which browsers don't send to plain-HTTP sites such as a LAN address or a `.local` name. There, the session is accepted only when the `Origin` or `Referer` header shows the request came from the same app or router page.

A request with neither header, such as a typed URL, a bookmark, or a link from another app, gets a **Continue** page instead. It contains no private data, can't be embedded in another page, and doesn't reach the router route or app until you click through. This stops another app from showing your pages in a hidden frame and tricking you into clicking their buttons. Scripts talking to a plain-HTTP instance should use an API token.

The router is the sole authority for the `X-OpenHost-*` headers an app receives. Anything a client sends under those names is stripped before the app sees it, so an app can trust `X-OpenHost-Is-Owner` and a provider can trust the consumer name it is handed.

## Credentials never reach an app

No openhost credential is forwarded to an app, since the app could replay it against the router. Apps identify the caller via `X-OpenHost-Is-Owner` and `X-OpenHost-Consumer-Id` instead.

Send your token as `X-OpenHost-Authorization: Bearer <token>`. Like every `X-OpenHost-*` header it is stripped by name, whatever its value, which leaves `Authorization` free for the app's own use. The session cookie is likewise stripped from `Cookie` by name.

Plain `Authorization: Bearer <token>` is deprecated but still accepted. It is only dropped when the router recognises the token, so a mistyped or expired token is forwarded to the app on `public_paths` routes.

## Catalog apps

We review apps before including them in our curated catalog manifest (https://github.com/cloud-in-a-bottle/app-manifest), but don't guarantee they are safe. Additionally, app authors can push changes to their apps after they are included in the catalog, and we don't re-review these changes. Currently we alleviate this by only including apps we have packaged, or from authors that we have a relationship with. We plan to revisit this, but ultimately it is the responsibility of the instance owner to determine if they trust an app with the capabilities/permissions the app is requesting.

## Reporting a vulnerability

See our [security policy](https://github.com/cloud-in-a-bottle/cloud-in-a-bottle/blob/main/SECURITY.md) for how to report a vulnerability privately.
