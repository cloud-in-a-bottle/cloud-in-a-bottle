# App definition loading

The [export service specification](./openapi.yaml) describes schema version 2 Sharing and Private documents. Older files must be re-exported. The builtin V2 service is read-only. Loading uses the router's separate owner APIs below, authenticated by an owner session or owner API token. App tokens, including those with Private export grants, cannot use them. Owner-session requests follow the normal same-origin policy.

Both endpoints accept `Content-Type: application/json` and always return JSON with `Cache-Control: no-store`, including errors. YAML content is limited to 1 MiB of UTF-8. Validation rejects unknown fields, malformed nested types, unsupported schema versions, duplicate app names, port labels or token hashes, duplicate YAML mapping keys, aliases and merge keys. Quoted `"<<"` remains an ordinary string. The parser also limits nesting to 32 levels, document nodes to 20,000 and integer scalars to 64 characters before numeric conversion. Errors contain fixed messages, never uploaded source lines or values.

## Parse and review

`POST /api/app-definitions/parse` accepts `{"content":"<exported YAML text>"}`. It validates the entire document before returning this plan:

```json
{
  "schema_version": 2,
  "mode": "private",
  "apps": [{
    "name": "example",
    "source_label": "https://github.com/example/app@feature/setup",
    "status": "ready",
    "install": {
      "repo_url": "https://github.com/example/app@feature/setup",
      "app_name": "example",
      "port_overrides": {"web": 9876}
    }
  }],
  "platform_api_token_names": ["CLI key"]
}
```

`platform_api_token_names` contains the names from Private token records in file order. Duplicate and empty labels are preserved. Token hashes and raw YAML do not appear in the plan. Sharing has an empty names list. Each app definition has exactly `name`, `source` and `port_mappings`; all apps use the same model.

Apps whose names already exist have `status: "existing"`, an `app_id` and no `install`. Missing local/unknown sources or absent bundled apps have `status: "unavailable"` and no `install`. Ready apps have an `install` and no `app_id`. The UI skips existing and unavailable apps and submits each ready `install` unchanged to `POST /api/add_app` after importing any Private API-token records. There is no batch job or transaction spanning installations.

Remote sources require canonical credential-free HTTP, HTTPS or git URLs. Userinfo, parameters, queries, fragments, path smuggling and ambiguous `@ref` forms are rejected rather than stripped. The required `ref` field is null for the repository's default branch; a safe string ref is preserved exactly. Builtin identifiers are single directory names contained under the configured bundled apps directory. Parsing only checks for a manifest's presence there. It never clones or probes remote sources. The normal add-app operation reads the manifest and performs installation checks later, so a ready plan is not a deployment guarantee.

Published port overrides map each label to its host port. Container ports must be integers from 1 to 65535; host ports must be 0 (auto-assign) or 25 to 65535, following the existing unprivileged-port policy. Booleans are not ports. The install payload contains only `repo_url`, `app_name` and `port_overrides`. The normal install operation retains its own permission-approval semantics.

## Import Private API-token records

`POST /api/app-definitions/import-private` accepts only `{"content":"<same YAML text>"}`. The UI calls it before installing apps when a Private file contains API-token records. It revalidates the entire document and builtin containment before any write. Private exports always include `platform_api_tokens`, including `[]`, and Load always adds the records without a separate opt-in field.

Each record has exactly `name`, `token_hash` and `expires_at`. Names are UTF-8 strings, including empty labels. Hashes must be 64 lowercase hexadecimal characters: the stored SHA256 verifier, not a raw API key. Expiry is null or a valid timezone-aware ISO timestamp. Null is stored as an empty expiry string; non-null expiry is preserved exactly. Expired keys stay invalid. For newly added records, the original source key is usable on the destination until that absolute expiry; the hash itself is not an authentication key.

The router inserts the batch in one SQLite savepoint with `ON CONFLICT(token_hash) DO NOTHING`. Existing records keep their names, expiries, IDs and creation times. Different hashes may have the same name. Repeated imports do not extend expiry, rename keys or remove destination keys absent from the file. A failed batch rolls back all its inserts. Sharing and empty Private arrays perform no token-record queries or writes. No HTTP or app-store calls are involved.

Success is `{"ok":true,"added_api_token_count":2,"existing_api_token_count":1}`. Sharing and empty Private arrays return both counts as zero. Validation failures return HTTP 400; database failures return sanitized HTTP 500 JSON. The UI stops before installing apps on import failure. If a response is lost after commit, retrying safely skips the already-imported hashes.

Loading only adds: existing app sources, permissions and ports stay in place, and apps absent from the file are not removed. The Secrets app is an ordinary app definition, and its stored values are ordinary app data. Definitions do not restore app databases, persistent data, local source trees, app tokens, sessions, passwords, OAuth credentials or platform settings. Use backups to restore data.
