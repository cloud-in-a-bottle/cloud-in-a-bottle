

## DNS

our imbue.com domain is managed at godaddy.
for each server we set up 2 DNS records:
- NS record pointing `host`(.imbue.com) to `ns.host`(.imbue.com). this says "the DNS server handling `host.` requests is at this location".
- "glue" A record pointing ns.host to the IP of the server. the NS record can't take an IP, so this resolves ns.host.imbue.com to the specific IP of the server.

on the server, the router owns CoreDNS through a single `InternalDnsProvider`
(`core/dns/coredns_provider/`): it holds the CoreDNS process, the set of zones the instance is
authoritative for, and the records those zones serve. everything that wants to publish a record
goes through it, so there is one way to write DNS.

it serves authoritative DNS for each zone:
- A record for `host.imbue.com` -> server IP
- A record for `ns.host.imbue.com` -> server IP (glue for the NS the SOA points at)
- wildcard A record for `*.host.imbue.com` -> server IP (so app subdomains resolve)
- TXT records for `_acme-challenge.host.imbue.com` (written during ACME DNS-01 cert acquisition)

**records live in memory, not on disk.** the provider holds them and re-renders the zone files
whenever they change; nothing is persisted, because every record is re-published on each boot
(`publish_router_addresses` for the three above, cert acquisition for the challenge tokens), so a
copy on disk could only go stale.

records carry no zone. every zone the space answers on is an alias for the same set of apps, so a
record lands in all of them — which is also why a zone added later needs no backfill.

zone files are outputs, never inputs: nothing reads them back, so any change is a whole-file
rewrite (via a temp file and rename, since CoreDNS re-reads on mtime change and a partial write
would leave the zone unparseable). every render bumps the SOA serial, which is what makes CoreDNS
reload. a *new* zone needs a new Corefile server block, so it needs a CoreDNS restart rather than
just a reload.

## TLS certs

we use wildcard certs: one cert covers both `host.imbue.com` and `*.host.imbue.com`. this means app subdomains get HTTPS without per-app cert issuance.

certs are acquired via ACME DNS-01 challenge:
1. router creates an ACME order for `[domain, *.domain]`
2. ACME server asks us to prove we control the domain by setting a TXT record
3. router publishes the TXT record through the DNS provider, which renders it into every zone file
4. ACME server queries our CoreDNS, sees the TXT record, issues the cert
5. router clears the TXT record

the cert and key are stored on disk and reused across restarts. the router only acquires a new cert if none exists.

### "Google Trust Service" certs

this is like let's encrypt but with higher rate limits tied to your GCP account (but still free).

https://docs.cloud.google.com/certificate-manager/docs/public-ca-tutorial
https://docs.cloud.google.com/certificate-manager/docs/quotas

prod server is at: https://dv.acme-v02.api.pki.goog/directory

brew install gcloud-cli

gcloud-init to genint project

`gcloud projects create openhost-tls-certs-1`
`gcloud config set project openhost-tls-certs-1`

`gcloud publicca external-account-keys create`

brew install certbot

if you have an existing account, `sudo rm /etc/letsencrypt/accounts/dv.acme-v02.api.pki.goog` to clear.

sudo certbot register \
    --email "me@example.com" \
    --no-eff-email \
    --server "https://dv.acme-v02.api.pki.goog/directory" \
    --eab-kid "(from previous step)" \
    --eab-hmac-key "(from previous step)"

it does not seem that the email becomes public. sudo is just needed because certbot writes its config to /etc/letsencrypt. this is the GCP prod keyserver.

grab the key from /etc/letsencrypt/accounts/dv.acme-v02.api.pki.goog/directory/[key id?]/private_key.json

put that in certbot_private_key.json in ansible secrets (this is now kept in 1password).

to revoke the keys, you have to delete the whole project. to reset rate limits, you can make a new project.

## reverse proxy

Caddy runs on the server alongside the router. it is started by the router process on boot.

when TLS is enabled:
- **:443** — terminates TLS using the ACME-acquired wildcard cert, reverse proxies all requests to the router on `:8080`
- **:80** — permanent redirect to HTTPS

when TLS is not enabled (e.g. Cloudflare Tunnel setups):
- **:80** — reverse proxies to the router on `:8080`

in a local setup (`openhost up` without `--domain`), Caddy does not run at all — the router serves HTTP directly on `:8080`. (TLS + Caddy are opted into by passing `--domain`; there is no `--dev` flag.)

the Caddyfile is generated dynamically by `compute_space/src/compute_space/core/caddy.py`. no static Caddyfile is checked in.

## app routing

the router (Hypercorn on :8080) handles all app routing via **subdomain routing**: `my-app.host.imbue.com` — the router extracts `my-app` from the Host header and proxies to the app's container port.

both HTTP and WebSocket requests are proxied. auth (an opaque, DB-backed `session_token` cookie — not a JWT) is checked before proxying to non-public paths.

## latency

centralized, global web services do some things to get latency down:
- multiple servers around the world, with routing to get users to the closest one
- if they don't do that, they'll do something like have cloudflare terminate TLS at the edge and reverse proxy back to the origin server. this cuts down on roundtrips to negotiate TLS. but this lets cloudflare see all the traffic.


for a single server setup, there's some optimizations you can do:
- OCSP stapling: some clients will add a check that the cert isn't revoked before accepting it. OCSP stapling lets the server check the OCSP status itself and "staple" it to the TLS handshake, so the client doesn't have to do a separate request to the CA's OCSP server.
- TLS session resumption: after the first TLS handshake, the client and server can cache the session parameters. then on subsequent connections, they can do a shorter handshake that just references the cached session, which saves roundtrips. this is tricky because it is only properly secure on GET requests.
- TLS 1.3 has less roundtrips
- HTTP/3 has less roundtrips
- use fast ECDSA P-256 keys (we do this — see `compute_space/src/compute_space/core/tls/util.py`)
