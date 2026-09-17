# App definition loading

The [export service specification](./openapi.yaml) describes the version 1 Sharing and Private documents. The builtin V2 service is read-only. Loading uses the router's separate owner APIs below, authenticated by an owner session or owner API token. App tokens, including those with Private export grants, cannot use them. Owner-session requests follow the normal same-origin policy.

Both endpoints accept `Content-Type: application/json` and always return JSON with `Cache-Control: no-store`, including errors. YAML content is limited to 1 MiB of UTF-8. Validation rejects unknown fields, malformed nested types, unsupported schema versions, duplicate app names or port labels, duplicate YAML mapping keys, aliases and merge keys. Quoted `"<<"` remains an ordinary string key. The parser also limits nesting to 32 levels and document nodes to 20,000. Errors contain fixed messages, never uploaded source lines or values.

## Parse and review

`POST /api/app-definitions/parse` accepts `{"content":"<exported YAML text>"}`. It validates the entire document before returning this plan:

```json
{
  "schema_version": 1,
  "mode": "private",
  "apps": [{
    "name": "example",
    "source_label": "https://github.com/example/app@feature/setup",
    "status": "ready",
    "secret_keys": ["API_KEY"],
    "install": {
      "repo_url": "https://github.com/example/app@feature/setup",
      "app_name": "example",
      "port_overrides": {"web": 9876},
      "permissions_v2_grants": [{
        "service_url": "github.com/imbue-openhost/openhost/services/secrets",
        "grant": {"key": "API_KEY"}
      }]
    }
  }],
  "secret_keys": ["API_KEY"],
  "missing_secret_keys": []
}
```

The top-level `secret_keys` contains sorted names from Private `secret_values`, while each app's `secret_keys` lists the explicit Secrets grants to review, including `*` for a wildcard grant. Neither values nor raw YAML appear in the plan. Sharing has empty top-level `secret_keys` and `missing_secret_keys` lists.

Apps whose names already exist have `status: "existing"`, an `app_id` and no `install`. Missing local/unknown sources or absent bundled apps have `status: "unavailable"` and no `install`. Ready apps have an `install` and no `app_id`. The UI skips existing and unavailable apps and submits each ready `install` unchanged to `POST /api/add_app` after successful secret import. There is no batch job or transaction spanning installations.

Remote sources require canonical credential-free HTTP, HTTPS or git URLs. Userinfo, parameters, queries, fragments, path smuggling and ambiguous `@ref` forms are rejected rather than stripped. A missing or null `ref` selects the repository's default branch; a provided safe ref is preserved exactly. Builtin identifiers are single directory names contained under the configured bundled apps directory. Parsing only checks for a manifest's presence there. It never clones or probes remote sources. The normal add-app operation reads the manifest and performs installation checks later, so a ready plan is not a deployment guarantee.

Published port overrides map each label to its host port. Container ports must be integers from 1 to 65535; host ports must be 0 (auto-assign) or 25 to 65535, following the existing unprivileged-port policy. Booleans are not ports. The install grants only the file's explicit Secrets permissions, never all manifest service permissions.

## Import managed secret values

`POST /api/app-definitions/import-secrets` accepts `{"content":"<same YAML text>","replace_existing":true}`. It revalidates the entire document. If Private `secret_values` is nonempty, `replace_existing: true` is required before any provider request. This explicitly authorizes UPSERT of the named values, including replacing existing values with empty strings. Sharing and empty Private value maps perform no provider I/O and do not require confirmation.

The router captures the selected compatible, running Secrets provider once and uses its existing owner JSON API: `GET /api/secrets` for metadata, then sequential `POST /api/secrets` requests with `{key,value,description}`. Existing descriptions are preserved from the metadata listing. New keys get empty descriptions. The destination and API path are router-controlled, redirects and environment proxies are disabled, and the router asserts owner identity without forwarding owner credentials. A selected read-only/custom provider without the owner API fails clearly; no provider is installed automatically.

Success is `{"ok":true,"saved_secret_count":2}`. Provider failure returns HTTP 502 with `{"error":"<safe message>","saved_secret_count":1}`. The count reports only acknowledged writes. A failed or lost response may follow a successful write, so values can already have been saved even when the count is zero. The UI must stop without starting apps on failure. Check Secrets before retrying. These HTTP writes are not atomic.

`missing_secret_keys` is informational and never deletes or clears anything. Loading is additive: existing app sources, permissions and ports are left in place, and apps absent from the file are not removed. Definitions do not restore databases, persistent data, local source trees, OAuth credentials or platform settings. Use backups to restore data.
