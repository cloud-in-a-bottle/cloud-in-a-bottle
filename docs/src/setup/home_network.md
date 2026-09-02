# Exposing a home server

The point of a Bottle instance is that it is your own piece of the cloud: your apps sit on the public internet at ordinary URLs, and anyone (you, your friends/family, anyone you share something with) can access them from an ordinary browser on any device (with appropriate authentication, of course). Therefore, the instance needs to be publicly exposed on the internet and have its own domain name (mycoolspace.com).

This is easy if you have a static IP address, but trickier at home where you typically don't.

## HTTP(s) tunnel services

Using a HTTP(s) tunnel like the (free) [Cloudflare Tunnels](https://developers.cloudflare.com/tunnel/) is the easiest way to get your instance online, currently.

HTTP tunnels are cheap to provide, because instead of having a dedicated IPv4 per client, they share a single IP among many clients. They do this via receiving HTTP(s) traffic on a shared IP, routing it by the hostname specified in the HTTP packet, and reverse-tunneling it into a potentially-firewalled machine. This only works for HTTP-based traffic, because it needs to know how to differentiate between traffic for different users on the shared IP, which is impossible to do in general for arbitrary non-HTTP protocols that don't carry hostnames. 

This isn't ideal, because there are some non-HTTP protocols that we would like Cloud in a Bottle should be able to receive inbound traffic on, eg SMTP (email). We're working on a better alternative (see below). That said, most apps only use HTTP and will work fine. Just remember that any apps specifying non-standard `[[ports]]` in the manifest won't work properly.

TODO: include instructions on how to actually set this up.

## Tailscale: private HTTP or HTTPS

For a fresh tailnet-only installation, the simplest option is HTTP over [Tailscale](https://tailscale.com/). Use DNS you control to point a base domain and its wildcard (`*.<base-domain>`) at the server's Tailscale IP, then provision with `--domain <base-domain>:8080 --local-http-only --bind-host <tailscale-ip>`. [MagicDNS cannot create arbitrary records](https://tailscale.com/docs/reference/dns-in-tailscale), so use public DNS or a private resolver configured as Tailscale split DNS. Tailscale encrypts traffic between tailnet devices, but browsers still treat the resulting `http://` URLs as an insecure context and may disable HTTPS-only features.

[Tailscale Serve](https://tailscale.com/kb/1242/tailscale-serve) provides an HTTPS machine hostname within the tailnet, while [Tailscale Funnel](https://tailscale.com/kb/1223/tailscale-funnel) makes that hostname public. Neither is currently a drop-in Cloud in a Bottle front end: they expose one `machine.tailnet.ts.net` hostname, while Cloud in a Bottle expects wildcard `<app>.<domain>` hostnames and generates redirects from the configured domain scheme. HTTPS for every app therefore requires a separate wildcard DNS and TLS proxy arrangement that preserves each request's original host.

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
