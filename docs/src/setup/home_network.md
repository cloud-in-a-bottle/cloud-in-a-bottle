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

### Check if you have an IP at all

Behind CGNAT (carrier-grade NAT) you share one address with other customers and cannot accept inbound connections on it, so there is nothing to delegate to. Check by comparing what the internet sees against the WAN address on your router's status page:

```bash
curl -4 https://ifconfig.me
```

If they differ, you are behind CGNAT. Most ISPs will hand out a real public address on request, sometimes free and sometimes as a paid static IP.

## What has to be reachable

| Port | Protocol     | Used for                                                                    |
|------|--------------|-----------------------------------------------------------------------------|
| 53   | TCP and UDP  | the instance's CoreDNS answering for your zone, including ACME DNS-01        |
| 443  | TCP          | the dashboard and every app                                                   |
| 80   | TCP          | redirecting plain HTTP to HTTPS, optional                                     |

Some residential ISPs block inbound 80 and 443. Losing 80 costs you only the redirect. If 443 is blocked, this approach will not work on that connection.

### 1. Delegate the zone, and keep the record current

Two records at your DNS provider, for a zone of `host.example.com`:

| Type | Name                   | Value             |
|------|------------------------|-------------------|
| `A`  | `ns1.host.example.com` | your public IPv4  |
| `NS` | `host.example.com`     | `ns1.host.example.com` |

Set the `A` record's TTL as low as your provider allows. 60 seconds is typical. That record is the glue for the delegation, and it has to follow your IP.

So run a dynamic DNS client that updates it. `ddclient` and `inadyn` are both packaged for Ubuntu and both do the job; run one on the instance itself. Any DNS provider with an update API works, which is worth checking before you pick one.

### 2. Update the instance's own idea of its IP

The instance serves `host.example.com` and `*.host.example.com` from a zone file built out of the `public_ip` value in its config. That is read at startup and does not track a changing address, so a DDNS client on its own is not enough. Update both together, with something like:

```bash
#!/usr/bin/env bash
set -euo pipefail
CONFIG=/home/host/.openhost/local_compute_space/config.toml
IP=$(curl -4 -fsS https://ifconfig.me)
grep -q "public_ip = \"$IP\"" "$CONFIG" && exit 0
sed -i "s|^public_ip = .*|public_ip = \"$IP\"|" "$CONFIG"
systemctl restart openhost
```

Run it as root from cron every few minutes, next to your DDNS update. The early `exit 0` keeps it from restarting the service on every tick. Records the instance serves carry a 300 second TTL, so clients pick up a new address within about five minutes of the restart.

### 3. Forward the ports on your router

Give the machine a static DHCP lease so its LAN address stops moving, then forward 53/TCP, 53/UDP, 443/TCP, and 80/TCP to it.

Three things commonly get in the way:

- Your router runs its own DNS server on port 53 and will not give the port up. Disable it or move it.
- The machine's own firewall. If `ufw` is enabled, `sudo ufw allow 53`, `sudo ufw allow 80`, and `sudo ufw allow 443`.
- NAT hairpinning. From inside your LAN, `host.example.com` resolves to your public IP, and plenty of routers will not loop that back inside. Test from a phone on cellular before concluding anything is broken.

### 4. If the instance is in a VM, pass the traffic through the host

Traffic now reaches the host machine and still has to reach the VM. Two ways to do that.

**Bridged networking** is the easier one. The VM gets its own address on your LAN and your router forwards straight to it, as though it were a physical machine. Nothing extra to configure on the host, client IPs stay intact, and the port list above applies unchanged. Give the VM a static DHCP lease. Most hypervisors offer this as a networking mode; under QEMU it means setting up a bridge and tap device instead of the default user-mode networking.

**NAT with per-port forwards** also works, at the cost of forwarding twice: router to host, then host to VM. Under QEMU's user-mode networking that is one `hostfwd` per port:

```
-netdev user,id=n0,hostfwd=tcp::53-:53,hostfwd=udp::53-:53,hostfwd=tcp::80-:80,hostfwd=tcp::443-:443,hostfwd=tcp::2222-:22
```

Binding 53, 80, and 443 on the host needs privileges, so QEMU has to run as root or with `cap_net_bind_service`. The guest also sees every connection as coming from the VM gateway, so app logs show a single source address for all traffic. Bridged networking avoids both problems.

### 5. Verify from outside your network

Run these from a machine on a different network:

```bash
dig +trace host.example.com                 # delegation resolves down to your instance
dig @<your-public-ip> host.example.com      # your CoreDNS answers directly
curl -I https://host.example.com/
```

If `dig +trace` stops at the delegation, the `NS` or glue record is wrong or has not propagated yet. If the direct `dig @` times out, port 53 is not getting through. If DNS resolves but `curl` hangs, 443 is not getting through.
