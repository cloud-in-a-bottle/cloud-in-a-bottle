# Bundled Service Specs

These are the services that ship with OpenHost. Each one is a normal app that
declares itself a provider — nothing about them is privileged — so their specs
double as worked examples of what a provider looks like.

A consumer app reaches these through the router, not directly:

```
GET|POST|WS [OPENHOST_ROUTER_URL]/api/services/v2/call/<shortname>/<rest>
```

`<shortname>` is the name you gave the service in your own manifest's
`[[services.v2.consumes]]` block, and `<rest>` is the path below — so a call to
`/get` on a service you consume as `secrets` becomes
`/api/services/v2/call/secrets/get`. See
[Cross-App Services](./cross_app_services.md) for how providers are resolved and
how permission grants are declared and approved.

Each spec's `description` documents the permission grant format that service
expects. The router enforces the grants; the service validates them again on its
own side before returning anything.

<div id="service-specs" class="redoc-embed"></div>
<script src="https://cdn.jsdelivr.net/npm/redoc@2.5.0/bundles/redoc.standalone.js"></script>
<script>
  (async () => {
    const host = document.getElementById("service-specs");
    if (typeof Redoc === "undefined") {
      host.textContent = "Could not load the spec browser (no CDN access). The raw documents are at /docs/services/<name>/openapi.yaml.";
      return;
    }
    let names;
    try {
      names = await (await fetch("/docs/services")).json();
    } catch (e) {
      host.textContent = "Could not list the bundled services.";
      return;
    }
    if (!names.length) {
      host.textContent = "No bundled service specs found in this checkout.";
      return;
    }
    for (const name of names) {
      const title = document.createElement("h2");
      title.className = "service-spec-title";
      title.textContent = name;
      host.appendChild(title);
      const pane = document.createElement("div");
      host.appendChild(pane);
      Redoc.init(`/docs/services/${name}/openapi.yaml`, {hideDownloadButton: true, nativeScrollbars: true}, pane);
    }
  })();
</script>
