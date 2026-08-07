# Bundled Service Specs

This page documents the API routes of the bundled service apps in Openhost. They hold the same permissions as other apps, but just are default. A consumer app reaches these through the router, not directly:

```
GET|POST|WS [OPENHOST_ROUTER_URL]/api/services/v2/call/<name>/<rest>
```

`<name>` is the name of the app, which is mutable. `<rest>` is the remainder of the path. See [cross_app_services](./cross_app_services.md) for conventions on providers and permissions. 

The `description` sections document requirements for each request. The router enforces the grants to and from the service.

## Machine-readable specs

`GET /docs/services` lists the service names exposed by all the apps. The default apps serve plain docs at `GET /docs/services/<name>/openapi.yaml`, which are also in the Openhost repo at `services/<name>/openapi.yaml`.

## Reference

<div id="service-specs" class="redoc-embed"></div>
<script src="/static/vendor/redoc.js"></script>
<script>
  (async () => {
    const host = document.getElementById("service-specs");
    if (typeof Redoc === "undefined") {
      host.textContent = "Could not load the spec browser. The raw documents are at /docs/services/<name>/openapi.yaml.";
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
