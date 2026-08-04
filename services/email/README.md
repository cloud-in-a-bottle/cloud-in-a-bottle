# OpenHost Email Service

Outbound email sending, provided by OpenHost itself (not a provider app).

**Service URL:** `github.com/imbue-openhost/openhost/services/email`

## What it does

Any app that wants to send mail requests this service's `send` permission. The
router runs a local SMTP submission listener; the app relays its message there,
and the router attaches the instance's relay credential and forwards the message
to the Imbue email proxy smarthost. The app never sees the relay credential.

This is spoken over SMTP (not the HTTP V2 service-call proxy) because the natural
clients are mail servers (e.g. Stalwart), which relay through an SMTP smarthost.
Permissioning is the same as every other service: a manifest-declared grant that
the owner approves at install.

## Consuming it

Declare the consume in your `openhost.toml`:

```toml
[[services.v2.consumes]]
service = "github.com/imbue-openhost/openhost/services/email"
shortname = "email"
version = ">=0.1.0"
grants = ["send"]
```

## Permission grant format

- `"send"` — permission to send outbound mail through the instance's relay.

## Sending

The router injects two env vars into every app:

- `OPENHOST_SMTP_HOST` — the router SMTP submission host (reachable from the app)
- `OPENHOST_SMTP_PORT` — the router SMTP submission port

Connect to `OPENHOST_SMTP_HOST:OPENHOST_SMTP_PORT` and authenticate with SMTP
AUTH:

- username = your app name (`OPENHOST_APP_NAME`)
- password = your app token (`OPENHOST_APP_TOKEN`)

The router validates the token, checks the `send` grant, then relays the message
to the Imbue smarthost with the per-instance credential attached. From/To
envelope addresses are preserved; the message body is forwarded verbatim (SES
signs DKIM downstream).

The listener is bound only to loopback and the container gateway (never a public
interface). Inbound mail is unaffected: it is delivered directly to the mailbox
server, not through this service.
