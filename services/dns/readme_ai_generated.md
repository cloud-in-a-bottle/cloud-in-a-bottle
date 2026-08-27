# `dns` service

`github.com/imbue-openhost/openhost/services/dns` — read and write DNS records for the space's
domains, without ever seeing the owner's registrar credentials.

Full spec: [`openapi.yaml`](./openapi.yaml).

## Two providers, one interface

| provider | where records live |
|---|---|
| the router (default) | CoreDNS zone files on the instance; the space is its own nameserver |
| a connector app, e.g. `external-dns-connector` | an external registrar via libdns |

An app cannot tell them apart, and does not need to. Which one applies is the owner's service
default: installing a connector app registers it and switches the space over, including the
router's own cert-acquisition writes. The router is the *implicit* provider — it has no row in
`apps`, so it is what you get when no app has claimed the service.

## Using it

Declare the dependency and the grants you need; the owner approves them at install time.

```toml
[[services.v2.consumes]]
service   = "github.com/imbue-openhost/openhost/services/dns"
shortname = "dns"
version   = ">=0.1.0"
grants    = [{name = "_acme-challenge**", type = "TXT", access = "rw"}]
```

```bash
curl -X POST "$OPENHOST_ROUTER_URL/api/services/v2/call/dns/records/set" \
  -H "Authorization: Bearer $OPENHOST_APP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"zone": "example.com",
       "records": [{"name": "_acme-challenge", "type": "TXT", "ttl": 60, "data": "token"}]}'
```

Endpoints: `POST /zones`, `/records/get`, `/records/set`, `/records/append`, `/records/delete`.

## Records

One flat shape covers every type, mirroring a zone-file line:

```json
{"name": "www", "type": "A", "ttl": 300, "data": "192.0.2.1"}
```

`name` is **relative to the zone** — `www`, not `www.example.com`; a name that already includes the
zone is rejected rather than silently doubled. `@` is the apex. `data` is unescaped RDATA, so `MX`
is `"10 mail.example.com."`.

Writable types: `A`, `AAAA`, `CAA`, `CNAME`, `MX`, `NS`, `SRV`, `TXT`. Reads pass through whatever
is in the zone.

On `delete`, **omit `data`** to clear the whole `(name, type)` RRset — which is how you clean up a
name without knowing what is currently there. `set` replaces an RRset; `append` adds to it.

## Zones

Reads default to every zone. **Writes must name one**, either exactly or `"*"` for all of them;
omitting it is a `zone_required` error rather than a silent fan-out. Responses report per zone:
`200` if every zone succeeded, `207` if some did, `502` if none did.

## Grants

A grant is a name pattern, a type, and `r` or `rw`. Matching is against the **zone-relative** name;
grants say nothing about zones, so one grant applies to every zone the owner has configured.

`**` matches any run of characters. A single `*` is **literal**, because it is a real DNS wildcard
label — `*.app` grants exactly that record.

> **Sharp edge.** `**` matches characters, not labels, so the `.` in `_acme-challenge.**` is
> required literally: that pattern matches `_acme-challenge.host` but **not** the bare
> `_acme-challenge`, which is the name a cert for the zone apex uses. Write `_acme-challenge**` to
> cover both. Both providers behave identically here, deliberately.

An app sees only what its grants match: a read omits everything else rather than refusing, so a
narrowly scoped app sees a zone containing just its own records. A write is authorized as a whole
batch before anything is applied, so a partly-permitted request changes nothing.

## Reserved records

`SOA` and `NS` at the apex, and `A`/`AAAA` at `@`, `ns`, and `*`, are maintained by OpenHost. It
rewrites them whenever a domain is added or the instance's public IP moves, so a write would be
silently undone — and a broad grant could otherwise delete the wildcard and take every app in the
space offline. Writing one returns `403 reserved_record` regardless of grants.

Everything else at those names is writable; an apex `MX` or `TXT` is exactly what a mail setup
needs, and nothing in OpenHost touches them.

## Router-side implementation

In `compute_space/src/compute_space/core/dns/`:

```
service_api.py       the contract both sides share: service identity, and the grant type
records.py           what a record is: shape, validation, the reserved-record rule
client.py            calling the service, from anywhere in the router
dynamic_dns.py       watch the public IP and re-point the A records when it moves
public_ip.py         detect and store where the instance actually is
coredns_provider/    the router's implementation of the service
  service.py           request handling; the only thing that touches zone-file records
  coredns.py           the CoreDNS process and zone-file lifecycle
  zonefile.py          read/modify/write a zone file, locked per zone and written atomically
```

Anything outside `coredns_provider/` is provider-agnostic and would be unchanged if the router
stopped serving DNS itself. `client.py` reaches into the subpackage for one thing only —
dispatching in-process when the router is the provider, rather than making it talk to itself over
loopback.

There is no abstraction over "local vs external DNS": the service *is* the interface, and the
router's own cert and dynamic-DNS writes go through it like any app's would. The router identifies
as a consumer (`_openhost_router`) with a narrow self-asserted grant covering only the challenge
TXT and the records it maintains, so a bug in those paths cannot rewrite an app's records — and
its calls are the one exemption from the reserved-record rule, since it is what maintains those
records.
