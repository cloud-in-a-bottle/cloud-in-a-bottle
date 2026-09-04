# Exposing a server with a static IP

The point of a Bottle instance is that it is your own piece of the cloud: your apps sit on the public internet at ordinary URLs, and anyone (you, your friends/family, anyone you share something with) can access them from an ordinary browser on any device (with appropriate authentication, of course). Therefore, the instance needs to be publicly exposed on the internet and have its own domain name (mycoolspace.com).

Using a static IP is the simpler path, and it is the case on essentially any VPS or cloud server. You need:

- a static public IPv4 for the machine (the examples use `203.0.113.10`),
- inbound `80/tcp`, `443/tcp`, and `53/tcp+udp` reachable from the internet. Port 53 is required, because the instance runs its own DNS server.

If the machine is at home you probably have neither; see [Exposing a home server](./home_network.md) instead.

This page converts an instance that is *already running* in HTTP-only mode. If you are starting fresh, [Deploying on a cloud instance](./cloud_instance.md) is the more direct path.

## Delegate DNS to the machine

Cloud in a Bottle runs an authoritative DNS server for your zone. It serves the wildcard `*.mycooldomain.com`, so every app gets a subdomain without you touching DNS again, and it answers the ACME DNS-01 challenge used to issue the wildcard TLS certificate.

So you delegate the whole zone to the machine rather than pointing an `A` record at it. For zone `mycooldomain.com` on a server at `203.0.113.10`, create two records at your DNS provider:

| Type | Name                   | Value                  |
|------|------------------------|------------------------|
| `A`  | `ns1.mycooldomain.com` | `203.0.113.10`         |
| `NS` | `mycooldomain.com`     | `ns1.mycooldomain.com` |

The `A` record is glue. An `NS` record can only name a host, not an IP, so something has to resolve `ns1` first.

Check the delegation before continuing. It can take a while to propagate.

```bash
dig +short NS mycooldomain.com     # -> ns1.mycooldomain.com.
```

Your DNS provider now delegates everything at or below `mycooldomain.com` to the instance. Once the domain is added below, the instance answers for the apex and wildcard, so you never create per-app records.

## Switch to TLS

Once the DNS records above are live, enable the TLS services, add the public domain, and then make it primary. Enabling the services first keeps every intermediate state safe to restart.

### 1. Turn on TLS services

The HTTP-only install skipped the ACME account key, so generate one:

```bash
KEY=/home/host/openhost/ansible/secrets/certbot_private_key.json
sudo -u host bash -c "cd /home/host/openhost && /home/host/.pixi/bin/pixi run python3 scripts/generate_acme_key.py $KEY"
sudo chmod 600 "$KEY"
```

Then edit `/home/host/.openhost/local_compute_space/config.toml`. Under `[openhost]`, flip three settings on, add the three ACME settings, and make sure `public_ip` is your server's real public IPv4, since CoreDNS answers every app subdomain with it:

```toml
acquire_tls_cert_if_missing = true
coredns_enabled = true
start_caddy = true

public_ip = "203.0.113.10"

acme_account_key_path = "/home/host/openhost/ansible/secrets/certbot_private_key.json"
acme_email = "openhost@mycooldomain.com"
acme_directory_url = "https://acme-v02.api.letsencrypt.org/directory"
```

`public_ip` is already in the file. A normal install detected it correctly at install time, but a downloaded VM image ships with a placeholder from the build machine, so set it to this server's address.

Restart:

```bash
sudo systemctl restart openhost
```

At this point the instance still uses its original HTTP primary, but it is ready to add and serve an HTTPS domain. A TLS domain in the database with `start_caddy = false` is a configuration the router rejects outright, which is why Caddy is enabled first.

### 2. Add the domain

In the dashboard, open **Settings → Domains**, enter `mycooldomain.com`, leave the type as **Public (HTTPS)**, and click **Add domain**.

The running instance makes CoreDNS authoritative for `mycooldomain.com`, acquires its wildcard certificate over DNS-01, and reloads Caddy to serve it. Watch it happen:

```bash
sudo journalctl -u openhost -f
```

The domain flips to active in the settings table once the certificate lands. It is now reachable at `https://mycooldomain.com/`, with apps at `https://<app>.mycooldomain.com/`, but the original HTTP domain remains canonical until the final step.

```bash
curl https://mycooldomain.com/health        # -> status "ok" and a process generation
```

### 3. Make the public domain primary

Return to **Settings → Domains** and choose **Make primary** beside `mycooldomain.com`. The instance and its running apps restart so app configuration uses the public domain. Stopped apps remain stopped. After restart, the browser opens the new primary and may ask you to sign in again because login cookies are domain-scoped.

Once you have confirmed the dashboard and apps work at their HTTPS URLs, you can remove the old local domain from Settings.
