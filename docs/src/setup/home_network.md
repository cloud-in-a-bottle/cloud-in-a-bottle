# Exposing a home server

An instance is reached by delegating a DNS zone to it. Your registrar points the zone's nameserver at the machine's public IPv4, and the instance's own CoreDNS answers for everything in the zone, including the ACME challenge that gets its certificates. On a VPS with a static IP that is the whole story.

At home there are three more problems: your IP changes, your router has to be told to accept the traffic, and if the instance is in a VM the traffic has to be passed through the host as well.

None of this depends on how you installed. Do it alongside [dedicated machine](./dedicated_machine.md) or [shared machine](./shared_machine.md) setup.

## Why you need a public IPv4

The point of an instance is that it is your own piece of the cloud: your apps sit on the public internet at ordinary URLs, and anyone (you, your friends/family, anyone you share something with) can access them from an ordinary browser on any device (with appropriate authentication, of course).

Only IPv4 delivers that today. Around half of clients on the internet still have no IPv6 path, and to them an IPv6-only zone does not resolve at all. So the instance publishes `A` records, and it needs a public IPv4 address of its own to point them at.

## IPv4 tunnel service

As you can see below, using the IP address from your ISP is a rather tricky process and doesn't always work. IPv4 addresses aren't free, but also aren't that expensive (see spot lease prices eg [here](https://www.ipxo.com/lease-ips/)). It ought to be possible to operate a service that attaches an IP address to a server and forwards any traffic arriving at that IP to your firewalled Bottle instance over a reverse proxy connection, thus avoiding any need for an IP from your ISP and fiddling with router settings.

Unfortunately, we can't find any service that actually does this for an individual for reasonable price - so we're building this ourselves, to make it easier for users to get their self-hosted instances online. The closest available option is a HTTP tunnel (see next section), but these don't support non-HTTP protocols. This feature should be available soon!


## HTTP(s) tunnel services

Using a HTTP(s) tunnel like [Cloudflare Tunnels](https://developers.cloudflare.com/tunnel/) partially allows you to bypass the requirement of a dedicated IPv4 address, via receiving HTTP(s) traffic on a shared IP, routing it by the hostname specified in the HTTP packet, and reverse-tunneling it into a potentially-firewalled machine. This only works for HTTP-based traffic, because it needs to know how to differentiate between traffic for different users on the shared IP, which is impossible to do in general for arbitrary non-HTTP protocols that don't carry hostnames. 

Unfortunately, there are some important non-HTTP protocols that we think Cloud in a Bottle should support, with SMTP (email) being a prominent one - we plan to setup instances with email out of the box, to enable email-based sharing and email-verified authentication. Therefore, we think it's best to standardize on instances having real, non-shared IPv4 addresses. However, our email features aren't available yet, and nor is our IPv4 proxy service, so Cloudflare Tunnels is a practical option to getting your instance online. Just remember that any apps specifying non-standard `[[ports]]` in the manifest won't work properly (but most apps don't need this and will work properly).

## Using the IP from your ISP

If your ISP gives you a real public IPv4, you can point the zone straight at your own connection. Nothing sits in the path, every protocol works, and you depend on nobody. The cost is that this is the fiddliest of the three options, and a few things about a home connection can rule it out entirely.

Three ports have to be reachable from the public internet at your address:

| Port | Protocol     | Used for                                                               |
|------|--------------|------------------------------------------------------------------------|
| 53   | TCP and UDP  | the instance's CoreDNS answering for your zone, including ACME DNS-01   |
| 443  | TCP          | the dashboard and every app                                             |
| 80   | TCP          | redirecting plain HTTP to HTTPS, optional                               |

Some residential ISPs block inbound 80 and 443. Losing 80 costs you only the redirect. If 443 is blocked, this route will not work on that connection.

### 1. Check that you have a public IPv4

Behind CGNAT (carrier-grade NAT) your ISP shares one public address across many customers, so inbound connections cannot reach you and there is nothing to delegate to. Compare two addresses:

- **What the internet sees:** `curl -4 https://ifconfig.me`, run from your home network.
- **What your router holds:** the WAN or internet address on your router's admin page.

If they match, you have a public IPv4. If they differ, the router's address says why:

- `100.64.0.0` to `100.127.255.255` is CGNAT. Ask your ISP for a public address, or use a tunnel.
- `10.x`, `172.16.x` to `172.31.x`, or `192.168.x` means an upstream router or modem is doing its own NAT. Bridge it and check again.

### 2. Delegate the zone

Two records at your DNS provider, using `host.example.com` as the zone:

| Type | Name                   | Value                  |
|------|------------------------|------------------------|
| `A`  | `ns1.host.example.com` | your public IPv4       |
| `NS` | `host.example.com`     | `ns1.host.example.com` |

The `A` record is the glue for the delegation, and it has to follow your IP, so set its TTL as low as your provider allows. 60 seconds is typical.

### 3. Handle when your IP address changes

A residential IP address is typically not static, at least for most ISPs. In practice though it often changes infrequently, when your router reboots or something similar. This can be worked around with "dynamic DNS" - a small program that runs inside your network, watching for when your public IP changes, and then updating your DNS record to point to the new address.

Two things point at your address, and both have to be updated.

**The glue record at your DNS provider.** Run a dynamic DNS client to keep `ns1.host.example.com` current. `ddclient` and `inadyn` are both packaged for Ubuntu; run one on the instance itself. Any DNS provider with an update API works, which is worth checking before you pick one.

**The instance's own zone file.** The instance serves `host.example.com` and `*.host.example.com` out of the `public_ip` value in its config, read once at startup, so a DDNS client on its own leaves it handing out a stale address. Update it with something like:

```bash
#!/usr/bin/env bash
set -euo pipefail
CONFIG=/home/host/.openhost/local_compute_space/config.toml
IP=$(curl -4 -fsS https://ifconfig.me)
grep -q "public_ip = \"$IP\"" "$CONFIG" && exit 0
sed -i "s|^public_ip = .*|public_ip = \"$IP\"|" "$CONFIG"
systemctl restart openhost
```

Run it as root from cron every few minutes, next to your DDNS update. The early `exit 0` means it only restarts when the address actually moved. Records the instance serves carry a 300 second TTL, so clients pick up the change within about five minutes of the restart.

### 4. Forward the ports on your router

Give the machine a static DHCP lease so its LAN address stops moving, then forward 53/TCP, 53/UDP, 443/TCP, and 80/TCP to it.

Three things commonly get in the way:

- Your router runs its own DNS server on port 53 and will not give the port up. Disable it or move it.
- The machine's own firewall. If `ufw` is enabled, `sudo ufw allow 53`, `sudo ufw allow 80`, and `sudo ufw allow 443`.
- NAT hairpinning. From inside your LAN, `host.example.com` resolves to your public IP, and plenty of routers will not loop that back inside. Test from a phone on cellular before concluding anything is broken.

### 5. If the instance is in a VM, pass the traffic through the host

Traffic now reaches the host machine and still has to reach the VM. Two ways to do that.

**Bridged networking** is the easier one. The VM gets its own address on your LAN and your router forwards straight to it, as though it were a physical machine. Nothing extra to configure on the host, client IPs stay intact, and the port list above applies unchanged. Give the VM a static DHCP lease. Most hypervisors offer this as a networking mode; under QEMU it means setting up a bridge and tap device instead of the default user-mode networking.

**NAT with per-port forwards** also works, at the cost of forwarding twice: router to host, then host to VM. Under QEMU's user-mode networking that is one `hostfwd` per port:

```
-netdev user,id=n0,hostfwd=tcp::53-:53,hostfwd=udp::53-:53,hostfwd=tcp::80-:80,hostfwd=tcp::443-:443,hostfwd=tcp::2222-:22
```

Binding 53, 80, and 443 on the host needs privileges, so QEMU has to run as root or with `cap_net_bind_service`. The guest also sees every connection as coming from the VM gateway, so app logs show a single source address for all traffic. Bridged networking avoids both problems.

### 6. Verify from outside your network

Run these from a machine on a different network:

```bash
dig +trace host.example.com                 # delegation resolves down to your instance
dig @<your-public-ip> host.example.com      # your CoreDNS answers directly
curl -I https://host.example.com/
```

If `dig +trace` stops at the delegation, the `NS` or glue record is wrong or has not propagated yet. If the direct `dig @` times out, port 53 is not getting through. If DNS resolves but `curl` hangs, 443 is not getting through.
