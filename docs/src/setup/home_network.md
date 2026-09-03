# Exposing a home server

The point of a Bottle instance is that it is your own piece of the cloud: your apps sit on the public internet at ordinary URLs, and anyone (you, your friends/family, anyone you share something with) can access them from an ordinary browser on any device (with appropriate authentication, of course). Therefore, the instance needs to be publicly exposed on the internet and have its own domain name (mycoolspace.com).

This is easy if you have a static IP address, but trickier at home where you typically don't.

## HTTP(s) tunnel services

Using a HTTP(s) tunnel like the (free) [Cloudflare Tunnels](https://developers.cloudflare.com/tunnel/) is the easiest way to get your instance online, currently.

HTTP tunnels are cheap to provide, because instead of having a dedicated IPv4 per client, they share a single IP among many clients. They do this via receiving HTTP(s) traffic on a shared IP, routing it by the hostname specified in the HTTP packet, and reverse-tunneling it into a potentially-firewalled machine. This only works for HTTP-based traffic, because it needs to know how to differentiate between traffic for different users on the shared IP, which is impossible to do in general for arbitrary non-HTTP protocols that don't carry hostnames. 

This isn't ideal, because there are some non-HTTP protocols that we would like Cloud in a Bottle should be able to receive inbound traffic on, eg SMTP (email). We're working on a better alternative (see below). That said, most apps only use HTTP and will work fine. Just remember that any apps specifying non-standard `[[ports]]` in the manifest won't work properly.

### Cloudflare Tunnel setup

These steps assume you already have a running OpenHost instance — stand one up first via the [dedicated machine](./dedicated_machine.md) or [shared machine](./shared_machine.md) guide. They then work the same on the VM image, a `provision.sh` install, or bare metal: every instance ends up as a plain-HTTP router listening on `127.0.0.1:8080`, and `cloudflared` reverse-tunnels public traffic to it. Cloudflare terminates TLS at its edge, so OpenHost itself runs in HTTP-only mode — no Caddy, CoreDNS, ACME, or open inbound ports on your side.

> **Claim your instance before the tunnel goes live.** The VM image and any `--local-http-only` bring-up assume the instance is private behind NAT, so claiming may be open (no token). A tunnel makes `/setup` reachable by anyone, so claim it immediately — or bake in a claim token first (`--claim-token` when building the image) so nobody can race you to it.

A couple of limitations to know going in:

- **HTTP(s) only.** Apps using a custom `[[ports]]` entry or a non-HTTP protocol (e.g. SMTP) won't work over the tunnel.
- **Cloudflare free-plan limits.** Request bodies are capped at 100 MB and origin responses time out after ~100 s, so large uploads and long-lived streaming connections may be cut.

**Prerequisites**

- A domain whose DNS is managed by Cloudflare (nameservers delegated to Cloudflare — the free plan is enough).
- **Always Use HTTPS** enabled on the zone (Cloudflare dashboard: **SSL/TLS → Edge Certificates**). The local hop from `cloudflared` to the router is plain HTTP, so an app that builds an absolute URL from the forwarded scheme could occasionally emit an `http://` link; this setting upgrades those at the edge. (The dashboard itself already builds `https://` links correctly from the `X-Forwarded-Proto` header Cloudflare sends.)

**1. Point OpenHost at your domain, in HTTP-only mode.** Edit `/home/host/.openhost/local_compute_space/config.toml`:

```toml
zone_domain = "example.com"
host = "127.0.0.1"
tls_enabled = false
start_caddy = false
coredns_enabled = false
acquire_tls_cert_if_missing = false
```

Then `sudo systemctl restart openhost`. Note there's no `:8080` in the domain (unlike the Tailscale HTTP option) — the browser reaches Cloudflare on 443, and Cloudflare forwards to your local `:8080`. Binding to `127.0.0.1` (rather than the image's default `0.0.0.0`) is good hygiene — `cloudflared` reaches the router over loopback, so nothing needs to be exposed on your LAN.

Prefer a domain used at its apex (`example.com`, so apps land at `<app>.example.com`). Cloudflare's free Universal SSL certificate covers the apex and a *first-level* wildcard (`example.com` and `*.example.com`) — but nothing deeper. If you nest OpenHost under `bottle.example.com`, app hostnames like `<app>.bottle.example.com` are a second label deep and browsers will show certificate warnings unless you pay for [Advanced Certificate Manager](https://developers.cloudflare.com/ssl/edge-certificates/advanced-certificate-manager/). The examples here use `example.com` at the apex.

**2. Install `cloudflared`** (Ubuntu):

```bash
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install -y cloudflared
```

**3. Create the tunnel.** `login` prints a URL — open it in a browser (on any machine) and authorize the zone. `create` writes a credentials file to `~/.cloudflared/<UUID>.json` and prints the tunnel's UUID.

```bash
cloudflared tunnel login
cloudflared tunnel create openhost
```

**4. Route DNS to the tunnel** — one record for the dashboard, one wildcard that gives every app its own subdomain. Quote the wildcard so your shell doesn't glob-expand it (zsh otherwise fails with `no matches found: *.example.com`):

```bash
cloudflared tunnel route dns openhost example.com
cloudflared tunnel route dns openhost '*.example.com'
```

Each creates a **proxied** CNAME to `<UUID>.cfargotunnel.com`. (Proxied wildcard records used to be Enterprise-only, but work on all plans now.) You can also add them by hand in the Cloudflare dashboard under **DNS → Records** if you prefer.

**5. Write the tunnel config** with a wildcard ingress rule so all app subdomains flow to the one router. Put both the config and credentials under `/etc/cloudflared/` so the system service can read them:

```bash
sudo mkdir -p /etc/cloudflared
sudo cp ~/.cloudflared/<UUID>.json /etc/cloudflared/
```

`/etc/cloudflared/config.yml`:

```yaml
tunnel: <UUID>
credentials-file: /etc/cloudflared/<UUID>.json
ingress:
  - hostname: example.com
    service: http://127.0.0.1:8080
  - hostname: "*.example.com"
    service: http://127.0.0.1:8080
  - service: http_status:404
```

**6. Run it as a service:**

```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

## Tailscale: HTTP or HTTPS

These steps assume you have already provisioned an instance and can access its dashboard.

This approach does not make the instance publicly accessible. It makes the instance available only to devices on your tailnet, wherever those devices are connected to the internet.

### 1. Install and connect Tailscale

Run the commands in this guide from the terminal in the Cloud in a Bottle dashboard or over SSH to the instance.

First, install Tailscale on the instance:

```bash
sudo snap install tailscale --classic
```

Then log in to Tailscale:

```bash
sudo tailscale up
```

For a long-running server, you may also want to disable key expiry for this machine in the Tailscale admin console so it does not require periodic reauthentication.

### 2. Point a domain to the Tailscale IP

Choose a domain you control the DNS for. We'll use `bottle.example.com`. Run `tailscale ip -4` on the instance, then create these records at your DNS provider using the address it prints:

| Type | Name                   | Value                |
|------|------------------------|----------------------|
| `A`  | `bottle.example.com`   | `<tailscale-ip>`     |
| `A`  | `*.bottle.example.com` | `<tailscale-ip>`     |

The wildcard record gives every app its own working subdomain.

### 3. Add the domain

Using your existing connection to the dashboard, open **Settings → Domains** and enter `bottle.example.com`. Choose **HTTP** to continue serving plain HTTP, or **HTTPS** to serve the domain with a certificate.

#### HTTP

Choose **HTTP** and click **Add domain**.

#### HTTPS

Choose **HTTPS** and click **Add domain**. The initial automatic certificate attempt may fail because this setup keeps DNS at your provider rather than delegating it to the instance. Use [acme.sh](https://github.com/acmesh-official/acme.sh) with your DNS provider's [DNS API plugin](https://github.com/acmesh-official/acme.sh/wiki/dnsapi) to complete the DNS-01 challenge and issue a wildcard certificate:

```bash
DOMAIN=bottle.example.com
CERT_DIR=/home/host/.openhost/local_compute_space/persistent_data/openhost/certs
sudo install -d -o host -g host "$CERT_DIR"
curl -fsSL https://get.acme.sh | sh -s email=you@example.com
# Export the credentials required by your DNS provider's plugin first.
~/.acme.sh/acme.sh --issue --server letsencrypt --dns dns_yourprovider \
  -d "$DOMAIN" -d "*.$DOMAIN"
~/.acme.sh/acme.sh --install-cert -d "$DOMAIN" \
  --fullchain-file "$CERT_DIR/$DOMAIN.pem" \
  --key-file "$CERT_DIR/$DOMAIN.key" \
  --reloadcmd "sudo systemctl restart openhost"
sudo chown host:host "$CERT_DIR/$DOMAIN.pem" "$CERT_DIR/$DOMAIN.key"
sudo chmod 0644 "$CERT_DIR/$DOMAIN.pem"
sudo chmod 0600 "$CERT_DIR/$DOMAIN.key"
```

The dashboard then uses `https://bottle.example.com`, apps use `https://<app>.bottle.example.com`, and acme.sh renews and installs the certificate through the same DNS API.

## IPv4 tunnel service

IPv4 addresses aren't free, but also aren't that expensive (see spot lease prices eg [here](https://www.ipxo.com/lease-ips/)). It ought to be possible to operate a service that attaches an IP address to a server and forwards any traffic arriving at that IP to your firewalled Bottle instance over a reverse proxy connection, thus avoiding any need for an IP from your ISP and fiddling with router settings.

Unfortunately, we can't find any service that actually does this for an individual for reasonable price - so we're building this ourselves, to make it easier for users to get their self-hosted instances online. This feature should be available soon!

## Using the dynamic IP from your ISP

This isn't an officially supported path, for a few reasons:
- not all ISPs give you you own IP - CGNAT (putting many users on a single shared IP) is becoming more common
- residential IPs are typically not static, which means you need some way to update your external DNS records automatically
- often they will restrict the inbound ports you can receive traffic from. eg receiving port 25 (SMTP, for email) is typically blocked
- to allow traffic in, you have to tell your router to forward traffic from external ports to your home server

Overall, the benefits seem low vs using eg Cloudflare Tunnels, which is much easier to setup. But it could be done if you really wanted to, probably.
