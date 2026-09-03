# Exposing a home server

The point of a Bottle instance is that it is your own piece of the cloud: your apps sit on the public internet at ordinary URLs, and anyone (you, your friends/family, anyone you share something with) can access them from an ordinary browser on any device (with appropriate authentication, of course). Therefore, the instance needs to be publicly exposed on the internet and have its own domain name (mycoolspace.com).

This is easy if you have a static IP address, but trickier at home where you typically don't.

## HTTP(s) tunnel services

Using a HTTP(s) tunnel like the (free) [Cloudflare Tunnels](https://developers.cloudflare.com/tunnel/) is the easiest way to get your instance online, currently.

HTTP tunnels are cheap to provide, because instead of having a dedicated IPv4 per client, they share a single IP among many clients. They do this via receiving HTTP(s) traffic on a shared IP, routing it by the hostname specified in the HTTP packet, and reverse-tunneling it into a potentially-firewalled machine. This only works for HTTP-based traffic, because it needs to know how to differentiate between traffic for different users on the shared IP, which is impossible to do in general for arbitrary non-HTTP protocols that don't carry hostnames. 

This isn't ideal, because there are some non-HTTP protocols that we would like Cloud in a Bottle should be able to receive inbound traffic on, eg SMTP (email). We're working on a better alternative (see below). That said, most apps only use HTTP and will work fine. Just remember that any apps specifying non-standard `[[ports]]` in the manifest won't work properly.

TODO: include instructions on how to actually set this up.

## Tailscale: HTTP or HTTPS

These steps assume you have already provisioned an instance in HTTP-only mode by following one of the preceding home server setup guides.

First, install Tailscale on the instance:

```bash
sudo snap install tailscale --classic
```

Then log in to Tailscale:

```bash
sudo tailscale up
```

For a long-running server, you may also want to disable key expiry for this machine in the Tailscale admin console so it does not require periodic reauthentication.

Choose a domain you control the DNS for. We'll use `bottle.example.com` for this purpose. Point both that domain and `*.bottle.example.com` at the server's Tailscale IPv4 address (`tailscale ip -4`). The wildcard record gives every app its own working subdomain.

### Option 1: HTTP

Using your existing connection to the dashboard, open **Settings → Domains**, enter `bottle.example.com`, choose **Local (HTTP)**, and click **Add domain**.

Run `tailscale ip -4`, then edit `/home/host/.openhost/local_compute_space/config.toml` and set `host` under `[openhost]` to the address it prints:

```toml
host = "100.x.y.z"
```

Restart the instance:

```bash
sudo systemctl restart openhost
```

The dashboard uses `http://bottle.example.com:8080` and apps use `http://<app>.bottle.example.com:8080`.

### Option 2: HTTPS

Use the same DNS records, then use [acme.sh](https://github.com/acmesh-official/acme.sh) with your DNS provider's [DNS API plugin](https://github.com/acmesh-official/acme.sh/wiki/dnsapi) to complete the DNS-01 challenge and issue a wildcard certificate:

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

Using your existing connection to the dashboard, open **Settings → Domains**, enter `bottle.example.com`, leave the type as **Public (HTTPS)**, and click **Add domain**. In `/home/host/.openhost/local_compute_space/config.toml`, set `host = "127.0.0.1"`, `start_caddy = true`, `coredns_enabled = false`, and `acquire_tls_cert_if_missing = false`, then restart the service. The dashboard then uses `https://bottle.example.com`, apps use `https://<app>.bottle.example.com`, and acme.sh renews and installs the certificate through the same DNS API.

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
