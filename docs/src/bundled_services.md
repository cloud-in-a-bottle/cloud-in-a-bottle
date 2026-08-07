# Bundled Service Specs

This page documents the API routes of the bundled service apps in Openhost. They hold the same permissions as other apps, but just are default. A consumer app reaches these through the router, not directly:

```
GET|POST|WS [OPENHOST_ROUTER_URL]/api/services/v2/call/<name>/<rest>
```

`<name>` is the name of the app, which is mutable. `<rest>` is the remainder of the path. See [cross_app_services](./cross_app_services.md) for conventions on providers and permissions. 

The `description` sections document requirements for each request. The router enforces the grants to and from the service.

## Reference

`GET /docs/services` lists the service names exposed by all the apps. The default apps serve plain docs at `GET /docs/services/<name>/openapi.yaml`, which are also in the Openhost repo at `services/<name>/openapi.yaml`.

[Bundled Service Reference](/docs/reference/services){target=_blank rel=noopener}
