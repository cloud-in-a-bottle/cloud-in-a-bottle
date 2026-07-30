# Per-App Egress

By default an app's outbound traffic leaves your instance via the server's own
(datacenter) IP. Per-app egress lets you route **all** of a specific app's
outbound traffic through a WireGuard tunnel to an exit you control instead —
your home connection, a WireGuard-compatible VPN provider, or any other
WireGuard endpoint.

An app opts in by name; the actual tunnel is configured by the instance
operator. The app repo never contains any tunnel keys.

## How an app requests egress

Add an `egress` value to the app's `[runtime.container]` manifest section,
naming a profile the operator has registered:

```toml
[runtime.container]
image = "Dockerfile"
port = 8080
egress = "home"
```

That's the entire app-side change. `egress = ""` (or omitting it) keeps normal
datacenter egress.

`egress` cannot be combined with `network_host` (host networking shares the
host namespace and can't be confined to a tunnel).

## Registering a profile (operator)

A profile is an ordinary WireGuard client config placed on the instance at:

```
<data_root>/persistent_data/egress_profiles/<name>.conf
```

For example, `.../egress_profiles/home.conf`:

```ini
[Interface]
PrivateKey = <this instance's wg private key>
Address = 10.77.0.2/24
DNS = 1.1.1.1

[Peer]
PublicKey = <exit endpoint's wg public key>
Endpoint = your-home-endpoint.example.com:51820
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
```

- `Address` is the tunnel address WireGuard assigns this instance.
- `DNS` should be a resolver reachable **through** the tunnel, so name
  resolution can't leak around it. If omitted, `1.1.1.1` is used.
- `AllowedIPs = 0.0.0.0/0` routes everything through the exit.

The directory is created with `0700` permissions by the installer because
profiles contain private keys. Any WireGuard-compatible provider (Mullvad,
Proton, etc.) works — drop in the `.conf` they give you and reference it by
name.

The exit side (your home router / VPN provider) must accept this peer and
masquerade its traffic to the internet, exactly like any other WireGuard
client.

## What happens under the hood

When an egress app starts, OpenHost:

1. Starts a tiny **infra container** with no network at all
   (`--network none`) — a private network namespace with no route out.
2. A privileged helper injects a WireGuard interface into that namespace,
   configured from the profile, and makes it the **sole** default route.
3. The app container joins that namespace, so every packet it sends can only
   leave through the tunnel.
4. A host↔namespace veth is added so the router can still reach the app's HTTP
   port; that path is directly connected and does not cross (and is not killed
   by) the tunnel.

Because the namespace starts with no route, a dropped tunnel means the app has
**no** egress — it fails closed rather than silently leaking out the
datacenter IP. DNS is delivered through the tunnel via a generated
`resolv.conf`, so lookups can't leak either.

## Requirements & limits

- The host must be provisioned for egress (WireGuard tools + the privileged
  helper), which the standard Ansible setup installs. Deploying an egress app
  on a host without egress support fails the deploy with a clear error rather
  than running unconfined.
- Egress apps cannot publish extra `[[ports]]` (they have no host loopback);
  their HTTP port is reached through the router as usual.
- Inbound traffic (your app's public URL) is unaffected — only **outbound**
  traffic is rerouted.
