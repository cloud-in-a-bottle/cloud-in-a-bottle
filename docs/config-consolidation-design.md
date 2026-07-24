# Consolidating domain + claim-token state into the DB — design & plan

> **Status:** living design doc. Planning only — no code written yet.
> Last updated: 2026-07-23.

## Goal

Stop splitting an instance's **domain set** across two stores (`config.toml`
`[[openhost.domains]]` + `runtime_domains.json`) and its **claim token** across a
file. Make the **router-owned DB** the single source of truth for both. Keep
`config.toml` for infra/bootstrap only. A small **first-boot seed file** provides
the initial primary domain + claim token on a fresh instance.

## Requirements (from the ask)

1. A **first-boot config** file sets `claim_token` and the (primary) domain name.
2. **All domains live in the DB**; all references resolve from the DB.
3. The **claim token** lives in the DB too (not a bare file).
4. Speed is not a concern yet — correctness/simplicity first.

---

## Why `config.toml` can't fully disappear

The router needs `data_root_dir` / `host` / `port` just to *locate and open* the
DB, plus infra it can't self-derive (`public_ip`, `coredns_enabled`, ACME/cert
credentials, `cert_provider`, ports). So `config.toml` stays — it just **sheds the
domain array and the claim-token fields**. This is a *consolidation of the two
domain stores + the claim token*, not the elimination of `config.toml`.

Split after this change:

| Concern | Before | After |
|---|---|---|
| infra/bootstrap (host, port, data_root, public_ip, acme, coredns) | `config.toml` | `config.toml` (unchanged) |
| domain set | `config.toml` `[[openhost.domains]]` **∪** `runtime_domains.json` | **`domains` table (DB)** |
| primary domain identity (`zone_domain`) | `config.toml` scalar | **`domains` table, `is_primary` row** |
| claim token (value) | `claim_token` file | **`settings` table (DB)** |
| initial domain + claim token (fresh box) | provisioning writes config.toml + token file | **`first_boot.toml` seed file** |

---

## Target architecture

### Keep the `Config.all_domains` API; change only its source

The multi-domain refactor already funnels every read (routing, scheme, cookies,
URL-building, Caddy/CoreDNS generation, renewal, `/api/domains`) through
`Config.all_domains` / `primary_domain` / `match_domain`. We **keep that API** and
change where the data comes from:

- At startup and after every mutation, **load the domain set from the DB** and swap
  it into the active `Config` — exactly the `rebuild_active_domains` pattern already
  in the tree, but reading a `domains` table instead of `runtime_domains.json` +
  the config-file base. The in-memory `Config.domains` becomes a **cache of the DB**,
  refreshed on change; the DB is the source of truth.
- `Config.zone_domain` (and `zone_domain_no_port`, used by cert paths, env vars,
  zonefile naming) is **derived from the `is_primary` row**, populated at load time.
  It stops being a `config.toml` field.

Net: the ~90 read sites are **untouched**. Only the loader (`domain_store` → DB) and
the `Config` construction/refresh change.

### DB schema

```sql
CREATE TABLE domains (
    name          TEXT PRIMARY KEY,     -- lowercased, no port
    tls           INTEGER NOT NULL,     -- bool
    mdns          INTEGER NOT NULL,     -- bool
    is_primary    INTEGER NOT NULL DEFAULT 0,  -- exactly one row is 1
    cert_status   TEXT NOT NULL DEFAULT 'none',  -- none|acquiring|active|error
    error_message TEXT
);
-- partial unique index so exactly one primary can exist:
CREATE UNIQUE INDEX one_primary ON domains(is_primary) WHERE is_primary = 1;

CREATE TABLE settings (             -- small kv for singletons
    key   TEXT PRIMARY KEY,
    value TEXT
);
-- claim_token stored as settings('claim_token', <value>), deleted at /setup completion.
```

`domains` folds in today's `DomainRecord` (name/tls/mdns/cert_status/error_message)
**plus** `is_primary`. **Bonus:** SQLite gives real atomic updates, so the
read-modify-write race flagged in the runtime_domains.json review **disappears** —
no more load-all/save-all lost updates.

### First-boot seed file

Provisioning writes `~/.openhost/local_compute_space/first_boot.toml` (same
directory as `config.toml`, a **separate** file):

```toml
domain      = "host.example.com"
claim_token = "…"
# optionally: tls = true, mdns = false (defaults: public TLS domain)
```

Lifecycle:
- On boot, if the DB has **no primary domain** (fresh instance), the router reads
  `first_boot.toml` and seeds the `domains` table (primary row) + `settings`
  claim_token.
- On every later boot the DB already has a primary → the file is **ignored**. It
  **stays on disk** (not deleted) — inert after first boot.

### claim token in the DB

- Value moves to `settings('claim_token', …)`, seeded from `first_boot.toml` (fresh)
  or the legacy file (migration). `/setup` reads it from the DB and **deletes the
  row** on completion (same lifecycle as today's `os.remove(claim_token_path)`).
- `claim_token_required` (the *policy* flag — whether to enforce the gate) **stays in
  `config.toml`**: it's a per-deployment infra decision provisioning already sets,
  not runtime state. (Open question below if we'd rather move it too.)

---

## Migration (instances already running)

On the first boot after upgrade the `domains` table is empty, and there's **no**
`first_boot.toml` (existing boxes won't have one). Seed from the legacy sources
instead, once:

- `domains` ← `config.toml` `zone_domain` (as `is_primary`) + `[[openhost.domains]]`
  + `runtime_domains.json`, deduped (primary first).
- `settings('claim_token')` ← the existing `claim_token` file if present.

After seeding, those legacy sources are **inert**: `runtime_domains.json` and the
`[[openhost.domains]]` array are no longer read. Zero operator action; no reprovision.
(Optionally log a one-time warning if `config.toml` still carries `[[openhost.domains]]`
that diverges from the DB, so operators know it's now ignored.)

---

## Implementation plan (phased)

Dependency-ordered. Note the ask's "step 1 (first-boot) then step 2 (domains in DB)"
is inverted here only because the **table must exist before anything can seed into it**;
the end state is identical.

### Phase 0 — DB schema
`domains` + `settings` tables via the existing versioned-migration framework
(`compute_space/db/versioned/migrations`). No behavior change yet.

### Phase 1 — Domains: DB is source of truth  *(the ask's step 2)*
- Rewrite `core/domain_store.py` CRUD against the `domains` table (drop the JSON
  file); keep the same function names so `/api/domains` is untouched.
- Loader: `rebuild_active_domains` reads the table → swaps into active `Config`.
- One-time migration seeds the table from `config.toml` domains + `runtime_domains.json`.
- Everything downstream (routing, Caddy/CoreDNS reload, renewal loop, cookies,
  URLs) is unchanged — it already reads `config.all_domains`.
- **Retires the read-modify-write race** (atomic DB writes).

### Phase 2 — First-boot seed + claim token in DB + `zone_domain` from DB  *(the ask's step 1)*
- Make `Config.zone_domain` derive from the `is_primary` row rather than a
  `config.toml` field (config-file field becomes optional/removed).
- Add `first_boot.toml` reader + seed-when-DB-empty logic.
- Move `claim_token` value to `settings`; `/setup` reads/clears it there.
- Legacy claim-token file used only for migration seeding.

### Phase 3 — Cleanup
- Drop `runtime_domains.json` code paths, `[[openhost.domains]]` from
  `ansible/templates/config.toml.j2` + `routerd_cli/.../config_gen.py`, and
  `zone_domain`/`claim_token` from the config-file schema.
- Provisioning writes `first_boot.toml` instead of `claim_token` file + config.toml
  `zone_domain`.
- Docs: routing + provisioning.

---

## Risks / open questions

- **`zone_domain`-empty window.** Between a fresh boot and seeding, there's no
  primary domain. On a provisioned box `first_boot.toml` always supplies it, so the
  window is momentary. But define behavior if `first_boot.toml` is absent *and*
  there's nothing to migrate (truly bare instance): serve `/setup` over plain http
  with no domain until one is set, and don't start CoreDNS/Caddy-TLS until a primary
  exists. (This edges toward the "/setup captures the domain" flow as a fallback.)
- **Changing the primary domain** post-setup: with `is_primary` in the DB this becomes
  possible via `/api/domains` (promote another domain). Decide whether to allow it
  (cert paths + `OPENHOST_ZONE_DOMAIN` env for apps would shift — probably gate behind
  an explicit "set primary" action, not a casual toggle).
- **`claim_token_required`**: keep in `config.toml` (proposed) or move to `settings`?
  Leaning keep — it's a provisioning policy flag, not runtime state.
- **DB availability for `/setup`.** `/setup` now depends on the DB being migrated
  before it can read the claim token — already true (init_db runs at startup), but
  worth confirming the ordering holds for the setup-only app.

## Progress

- [x] **Phase 0 — DB schema** _(landed 2026-07-23)_ — `domains` (with a partial unique index
  enforcing one primary) + `settings` tables, in `schema.sql` and migration v13; snapshots regen'd.
- [x] **Phase 1 — Domains: DB is source of truth** _(landed 2026-07-23)_ — `domain_store` rewritten
  against the table (single-statement atomic writes retire the read-modify-write race);
  `seed_domains_from_legacy` folds config-file domains + `runtime_domains.json` in on first boot;
  `set_base_domains`/the base-vs-runtime split removed.
- [x] **Phase 2 — First-boot seed + claim token in DB + `zone_domain` from primary** _(landed 2026-07-23)_
  - `core/settings_store.py` (kv over the `settings` table); `core/first_boot.py` reads
    `first_boot.toml` beside the router config and seeds the primary domain + claim token once.
  - `rebuild_active_domains` now also derives `zone_domain`/`tls_enabled` from the DB primary, so a
    first-boot domain takes effect in cert paths, the DNS zone, and `OPENHOST_ZONE_DOMAIN` — not
    just routing.
  - `/setup` verifies the claim token against `settings` (seeded from `first_boot.toml` or the legacy
    file) and clears it there on completion (still best-effort removing the legacy file, so
    file-based provisioning + the claim-token integration test keep working).
  - **Deferred to Phase 3:** `zone_domain` is still a *required* `config.toml` field (a seed /
    fallback); making `config.toml` omit it needs the Config-optional refactor and the provisioning
    switch to `first_boot.toml`.
- [x] **Phase 3 — `zone_domain` never read at runtime + scrubbed from config.toml** _(landed 2026-07-23)_
  - Removed the `all_domains` synthesis fallback: `all_domains` is now strictly the DB-sourced set,
    and `primary_domain` raises if the set is unseeded rather than reading `zone_domain`.
  - Re-sourced **every** runtime reader (cert paths, CoreDNS zone, `provision`/renewal, the
    `OPENHOST_ZONE_DOMAIN` app env, archive volume name, diagnostics, template globals, canonical
    OAuth/approval URLs) from `zone_domain` → `config.primary_domain`.  `zone_domain` is now read in
    exactly **one** place: the first-boot seed (the migration capture).
  - Made `zone_domain` optional in `DefaultConfig` and had the first-boot seed **scrub the
    `zone_domain` line from `config.toml`** after capturing it — surgical (one line), atomic
    (temp+rename), best-effort (never fatal).  A scrubbed config still loads (`zone_domain=""`).
    _(This deliberately overrides the earlier "router never rewrites config.toml" invariant, per the
    explicit request; the write is a one-time, one-line scrub.)_
  - `_make_test_config` now seeds `domains` from `zone_domain` so tests mirror a seeded instance.
- [ ] **Remaining cleanup (provisioning)** — drop `runtime_domains.json` code paths and switch
  provisioning to write `first_boot.toml` instead of `zone_domain`/`[[openhost.domains]]`/the
  claim-token file.  The runtime no longer depends on any of these — they're read once at
  migration and then inert/scrubbed — so this is a pure provisioning-side follow-up.

## Decision log

- **2026-07-24** — Dropped the `runtime_domains.json` migration entirely (the intermediate-branch
  domain store): the two states that will actually be tested are an **old production instance**
  (config.toml `zone_domain` [+ `[[openhost.domains]]`]) and a **fresh deployment**. `read_legacy_runtime_json`
  and `Config.runtime_domains_path` removed; the seed captures only `zone_domain` + `[[openhost.domains]]`.
- **2026-07-24** — The `config.toml` `zone_domain` **scrub goes through the system agent**
  (`sudo -n openhost_system_agent config scrub-zone-domain`, new `openhost_system_agent/config_edit.py`),
  not a direct router write — so all config-file mutation goes through the single privileged writer,
  which preserves `host:host` ownership. The router captures `zone_domain` into the DB first (at
  startup, `seeded=True`), then delegates the scrub. Best-effort: no agent (dev/CI) → logged no-op,
  and `zone_domain` is ignored at runtime regardless. `_run_system_agent` gained `-n` so a
  misconfigured host fails fast instead of hanging (the router now calls the agent at startup).
- **2026-07-23** — First-boot seed is a **separate file next to `config.toml`**
  (`first_boot.toml`), read only when the DB has no primary; it **remains on disk**
  after first boot but is never read again. (Not a consumed-and-deleted file, not
  reusing `config.toml` seed fields, not a router-generated token.)
- **2026-07-23** — Keep the `Config.all_domains`/`match_domain` API; back it with the
  DB instead of rewriting the ~90 read sites. `zone_domain` derives from the primary row.
