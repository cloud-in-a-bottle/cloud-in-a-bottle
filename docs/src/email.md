# Email (Design)

How OpenHost instances send and receive mail for their zone, and why the design looks the way it does.

Email has no on/off switch. It turns on automatically once the instance config has what it needs: the email API URL, the per-instance Keycloak client-credentials, and the instance's public IP. Provisioning fills these in when email infrastructure is configured, so a fresh instance simply has working mail; when they're absent the instance runs fine without it.

The design starts from one fact: **OpenHost instances are run by untrusted users.** No single instance may harm another's deliverability, drain a shared quota, or send as a domain it doesn't own. Everything below follows from that.

## Why email needs platform support

Two things make email unlike a normal app:

1. **Outbound port 25 is blocked.** Hetzner, GCP, and most hosts block outbound TCP/25 to fight spam, so an instance can't deliver straight to a recipient's MX. An outbound relay is mandatory. (Inbound 25 is separate — the instances OpenHost provisions do accept it, which is why inbound goes directly to the instance.)

2. **Deliverability is a shared reputation.** Whether mail reaches the inbox depends on the sending IP's reputation and on SPF/DKIM/DMARC alignment. If every instance sent from its own IP, one abusive tenant could get an address block-listed and poison everyone sharing that space.

Both point to the same answer: outbound flows through a **central, OpenHost-operated relay** that owns the sending reputation and enforces per-instance limits, instead of each instance talking to the world directly.

Each instance runs a real mailbox server (**Stalwart**) and webmail client (**Bulwark**) as default apps, giving it a genuine inbox and outbox. Outbound is relayed through the central relay as an **SMTP smarthost**; the relay authenticates the instance, enforces that it only sends as its own zone, applies rate limits, and hands off to the upstream provider (SES). The relay credential is delivered to the mailbox app at runtime through a scoped router endpoint (`GET /api/email/relay-config`), gated to the app names in `email_mailbox_app_names`, so it never lands in any other app's environment.

## The platform / app divide

The split follows one rule: anything **trust-critical or multi-tenant-unsafe** is central or platform-level; anything **user-facing** is an app.

| Piece | Where | Why |
|-------|-------|-----|
| Outbound relay + spam/abuse + provider identity | Central | Shared reputation must be centrally governed; provider creds stay off the instance |
| Per-instance credential (Keycloak) | Central-issued, instance-held | Authenticates the instance and anchors From-enforcement |
| DNS records (DKIM/SPF/DMARC/MX) | Platform (CoreDNS) | Only the platform writes the authoritative zone |
| Provisioning (inject credential + mail config) | Platform | Part of instance finalize |
| Mailbox server (SMTP/JMAP + storage) | App (default) | User-facing, swappable |
| Webmail client | App (default) | Pure UX |

## Architecture overview

```
   instance ──Keycloak token──▶ email API ──▶ upstream provider (SES) ──▶ recipient MX   (outbound)

   CoreDNS on the instance writes DKIM / SPF / DMARC / MX
   (authoritative for the selfhost subzone; and for a BYO domain via optional NS delegation)

   On the instance: mailbox server (receives inbound on :25) + webmail client

   sender's MX ──▶ mail.<zone> (A → instance IP) ──▶ Stalwart :25   (inbound, direct)
```

Outbound flows through the email API to the provider. Inbound does **not**: the zone's MX always points at the instance, and its mail server accepts SMTP on port 25 directly.

## Request authentication

Each instance is provisioned with its own Keycloak confidential client in the `openhost-customers` realm. It fetches a client-credentials token and presents it to the email API's `/api/email/*` endpoints. This mirrors the cert-api pattern exactly. The sending zone is derived from the instance's attested identity — not from anything in the message — which is what anchors From-domain enforcement. Revoking one instance is just disabling its Keycloak client; there's no shared secret.

## Abuse controls

Because outbound has a single central path, that's where all multi-tenant safety lives:

- **From-domain enforcement.** A message whose envelope-from or header-from is outside the instance's attested zone is rejected. An instance can only send as `*@<its-own-zone>`. This can't be enforced on the instance itself — only a party the tenant can't bypass can enforce it.
- **Per-instance rate and volume caps**, so one tenant can't consume the shared quota at others' expense.
- **Reputation isolation** per tenant, so a bad actor's bounce/complaint damage is contained.
- **Suppression and bounce/complaint handling**, maintained centrally.
- **Automatic per-instance suspension** when a bounce/complaint rate crosses a threshold, without affecting anyone else.

## DNS records (CoreDNS)

Each instance is authoritative for its own zone via CoreDNS (see [Routing](./routing.md)). The deliverability records are written automatically — one mechanism, no per-provider connectors:

- **SPF** authorizing the relay to send for the zone.
- **DKIM** public keys so signed mail aligns.
- **DMARC** policy for the zone.
- **MX** for inbound, always pointing at the instance's own mail host (`mail.<zone>`, whose A record CoreDNS also publishes → the instance's public IP), so mail is delivered straight to Stalwart on port 25.

These are persistent zone records, so they're written as part of the zone's base config (surviving router restarts), and the ACME-challenge cleanup is scoped so it never removes them.

### Bring-your-own domain (one NS record)

A `<name>.selfhost.imbue.com` address works out of the box, since the parent zone already delegates each subzone to the instance's CoreDNS.

For a **custom domain** (e.g. `me@mydomain.com`), the owner sets `email_custom_domain` and adds a **single NS record** at their registrar delegating that (sub)zone to the instance's CoreDNS — the same delegation model selfhost uses. The exact record comes from `Config.custom_domain_delegation_record()`:

```
mail.mydomain.com.   NS   ns.<zone>.
```

`ns.<zone>` already resolves to the instance's public IP, so that one record is all that's needed. The instance then serves the custom domain as a second authoritative zone and publishes the same SPF/DKIM/DMARC/MX records into it. Sending is authorized end-to-end because the NS delegation proves the owner controls the domain and the provider verifies the identity via the published DKIM records. The instance's authorized custom domain is supplied centrally, never asserted by the instance, so the From-domain boundary holds.

Recommendation: delegate a **subdomain** (e.g. `mail.mydomain.com`) rather than the apex, so OpenHost is only authoritative for the mail subzone and the owner's existing website/DNS is untouched.

## Receiving mail

Inbound is **always** delivered directly to the instance's own mail server; it never traverses OpenHost infrastructure, so the platform can't read a tenant's mail. The zone's MX points at the instance (`mail.<zone>` → its public IP), so a sender's MX connects straight to Stalwart on port 25. Only **outbound** goes through the central relay.

There is deliberately **no** managed/relayed inbound mode: routing incoming mail through OpenHost-operated infrastructure would let the platform read it, which the design forbids.

## The mailbox and webmail apps

The mailbox server (SMTP + JMAP with local storage) and the webmail client ship as **default apps**. Keeping them as apps means mail data lives on the operator's own zone, and they can iterate without a platform release.

- The mailbox server relays outbound through the smarthost and receives inbound directly on port 25.
- It exposes its JMAP interface as a [cross-app service](./cross_app_services.md); the webmail app consumes that service.

## Access control — who can read the mail

Three layers, kept distinct:

1. **Between instances (structural).** Each instance is a separate VM with its own mailbox and storage, so one instance physically can't read another's mail. Inbound goes directly to the destination instance, never through a shared component. This is the same isolation that already separates every OpenHost zone.

2. **Within an instance, single owner (the common case).** The webmail app and mailbox are gated by OpenHost owner authentication: the router only stamps `X-OpenHost-Is-Owner: true` for the authenticated owner; everyone else is bounced to login. A proxy in front of the mailbox server strips any client-supplied credentials and injects the owner's mailbox credentials before forwarding, so the webmail app never sees a mail password.

3. **Within an instance, multiple users (out of scope for now).** Owner-auth is binary today, so everyone who authenticates as the owner sees the same mailbox. Per-user mailboxes would require mapping each authenticated OpenHost user to a specific mailbox, which depends on OpenHost's federated-identity work.

## Provisioning

Per-instance email config is injected at **finalize time**, alongside the cert-broker config: the instance's Keycloak credential, the email API base URL, and the zone's mail settings. At provision, the provider domain identity is created and the DKIM tokens are published into CoreDNS with SPF/DMARC/MX, then verified once they resolve. For a `selfhost` subdomain this is fully automatic; for a custom domain the one manual step is the owner's NS delegation.

### Upgrading an existing instance (turning email on)

An instance provisioned before email (or with email off) can be upgraded with minimal action, because it reuses what cert-api already gave it:

- **Keycloak credentials inherit from cert-api.** `email_keycloak_*` resolve from `cert_api_keycloak_*` when not set, so no new credential is minted. The only server-side action is attaching the `openhost-email` audience scope to that existing client — done idempotently by vm-manager's `POST /api/instance/<name>/enable-email-scope` (no re-mint, no SSH).
- **public_ip is already present** on any CoreDNS instance.
- **The one value to add is `email_proxy_base_url`.** Once it's in the config, `email_enabled` derives true and, on the next router boot, the instance auto-publishes its DNS, auto-deploys the mailbox + webmail apps, and fetches its relay credential at runtime.

So the upgrade is: (1) attach the email scope to the instance's client, (2) add `email_proxy_base_url` to its config, (3) reboot the router. Everything downstream is automatic and reboot-safe.

## Trust and failure model

- **An instance can only send as its own zone**, enforced centrally from the Keycloak-attested identity.
- **An instance can't damage others' deliverability** — reputation is isolated per tenant; abuse triggers per-instance suspension only.
- **An instance holds no provider credentials** — it authenticates with a per-instance, individually-revocable Keycloak credential.
- **Relay unavailability is fail-safe** — outbound queues on the mailbox server and retries; inbound is unaffected because it's delivered directly (a sending MX retries per normal SMTP).
- **Mail data stays on the instance** — the relay enforces policy but is not the mail store.

## What is implemented today

- **Platform (this repo):** `email_*` config (no flag — on when its prerequisites are present), CoreDNS publishing of SPF/DKIM/DMARC/MX (`core/dns.py:apply_email_records`), an ACME-cleanup fix that no longer wipes email TXT records on cert renewal, the email API client + startup provisioning (`core/email/`), the scoped `/api/email/relay-config` endpoint, and finalize-time config injection (`ansible/templates/config.toml.j2`), including the always-direct inbound MX/A rendering.
- **Mailbox + webmail apps:** `openhost-stalwart-email-server` (relays outbound through the smarthost using the fetched relay-config; receives inbound on :25) and `openhost-bulwark-email-client` (consumes Stalwart's JMAP service).

Verified end-to-end on a fresh instance: it auto-published its DNS records, the domain auto-verified, and the instance sent DKIM-signed mail to a real external inbox. From-domain enforcement, audience/issuer checks, and rate limiting are covered by tests.

## Production readiness

Deliberately out of scope for the initial implementation (operational/infrastructure decisions or dependencies on other teams):

1. **Provider production access.** Move the upstream provider out of its sandbox and request production sending access before any real sending.
2. **A dedicated production provider account** with an IAM role/policy scoped to exactly the actions the relay needs, credentials delivered as secrets (never committed).
3. **The `openhost-email` Keycloak client scope.** vm-manager mints the per-instance client and attaches this scope, but the scope itself (a `subdomain` mapper + an audience mapper for `aud: openhost-email`) must be created by hand in the `openhost-customers` realm, like the existing cert-api scope. vm-manager fails the provision with a clear error if it's missing.
4. **Turn the feature on end-to-end.** Set the deployment-wide email API URL as `email_proxy_base_url` in vm-manager Settings; that alone turns email on for new instances. Until it's set, provisioned instances come up without email.
5. **Per-instance email authorization.** The API authenticates the instance but does not yet gate *which* instances may use email; a fail-closed per-instance flag is a pending, separate change.
6. **Custom-domain inbound in the mailbox app.** Stalwart marks only its default zone domain as local at first boot, so inbound for a BYO custom domain is rejected as non-local. Outbound from the custom domain and default-subdomain inbound both work; custom-domain inbound needs the mailbox app to be told its custom domain.
7. **Relay scaling / shared stores.** The relay runs a single machine because its rate-limiter and grant store are in-process/file-backed. Multi-machine HA needs a shared store. Same constraint as cert-api.
8. **Per-tenant reputation isolation** (configuration sets, dedicated IPs where warranted) plus bounce/complaint handling and a suppression list, before scale.
9. **DMARC policy + reporting.** Default policy is `p=quarantine`; decide the production policy and a `rua` aggregate-report address per zone (`email_dmarc_rua`).
10. **Canonical API URL + config defaults.** Point `email_proxy_base_url` at the production URL; consider a default once it has a stable DNS name.
