# Building your own VM image

The [release images](../src/setup/shared_homeserver.md#part-1-download-and-run-the-vm-image) are produced by `../../image/build.sh` in the [Cloud in a Bottle repo](https://github.com/cloud-in-a-bottle/cloud-in-a-bottle). Build your own to bake in a custom branch, an SSH key, or a claim token, or, with `--public`, [a TLS image for your own domain](#building-a-public-tls-image). The output is the same qcow2 + OVA pair the releases ship, which you then boot and claim exactly as in [Deploying on a shared home machine](../src/setup/shared_homeserver.md#part-1-download-and-run-the-vm-image).

## What you need

**To build the image**, a Linux host or (for arm64) an Apple silicon Mac with:

- `qemu-img` (the `qemu-utils` package), plus the emulator for the image's arch: `qemu-system-x86_64` (`qemu-system-x86`) for amd64, or `qemu-system-aarch64` and its UEFI firmware (`qemu-system-arm` and `qemu-efi-aarch64`) for arm64.
- A seed-ISO builder: `cloud-localds` (from `cloud-image-utils`), or `xorriso`, `genisoimage`, or `mkisofs`.
- `curl` and `tar`.
- (recommended) KVM (`/dev/kvm`), or HVF on macOS, on a host of the same arch as the image. Without it the build boot falls back to slow TCG emulation; that is how CI builds arm64, since GitHub's arm64 runners have no KVM.

On an Apple silicon Mac, `brew install qemu cdrtools coreutils` covers all of this (`cdrtools` provides `mkisofs`, `coreutils` provides `timeout`), and the arm64 build runs under HVF in about five minutes.

**To run the image it produces** (this applies to the release images too):

- About 8 GB of RAM and 4 cores.
- Free disk - we recommend at least 20G.

## Build it

Run from a checkout of the Cloud in a Bottle repo, on the build host:

```bash
image/build.sh
```

With no options this builds `main` for the host's arch into `image/out/openhost-<version>-<arch>.qcow2` (plus a matching `.ova` for amd64), in HTTP-only mode on `lvh.me`, with no claim token and a default console password. Pass `--arch amd64` or `--arch arm64` to pick the arch explicitly. The build boots a VM and runs the full provisioning, so it takes a while; Logs go into `image/out/build-console.log`.

### Common options

These customize the image in any mode:

| Option | Purpose |
| --- | --- |
| `--arch <arch>` | `amd64` or `arm64` (default: the host's) |
| `--domain <domain>` | subdomain-routing domain baked in (default `lvh.me`) |
| `--claim-token <tok>` | bake in a specific `/setup` token instead of the default |
| `--ssh-pubkey <path>` | authorize an SSH key for `host` (SSH is key-only; otherwise console-only) |

Run `image/build.sh --help` for the full set: repo and branch, disk and resource sizing, output paths, and more. For example, a custom HTTP-only image from a branch, with your SSH key:

```bash
image/build.sh \
  --branch my-feature \
  --ssh-pubkey ~/.ssh/id_ed25519.pub
```

### Building a public (TLS) image

By default the image is HTTP-only and not suitable for exposing publicly. Pass `--public` to bake a TLS image instead: one provisioned with CoreDNS, Caddy, and Let's Encrypt for `--domain`, ready to serve at `https://<domain>` once it's on the network.

You don't have to build a public image to go public: you can take an HTTP-only instance public in place by adding your domain from the dashboard, per [Exposing a server with a static IP](../src/setup/static_ip.md). The reason to build with `--public` is that it provisions for your domain from the start, so that domain is the instance's **primary**, whereas converting a running HTTP-only instance leaves the install-time domain as the primary.

| Option | Purpose |
| --- | --- |
| `--public` | build a TLS image (claiming is token-gated) |
| `--public-ip <ip>` | your public IPv4, baked in for the DNS records CoreDNS serves (**required** with `--public`) |
| `--acme-key <path>` | a pre-registered ACME account key to bake in (optional) |
| `--acme-email <email>` | email for the account the build registers, when you don't pass `--acme-key` |

Without `--acme-key`, the build generates and registers a fresh Let's Encrypt account key. Open claiming is refused on a reachable instance, so a public image is always token-gated. Pass `--claim-token` for a known value, or let the build print a random one.

```bash
image/build.sh \
  --public \
  --domain mycooldomain.com \
  --public-ip 203.0.113.4 \
  --ssh-pubkey ~/.ssh/id_ed25519.pub
```

The build does not get a TLS certificate issued - That can only happen once it's running and has DNS pointing at it. Once it boots with the right public ip `--public-ip`, delegate DNS and open ports 53 / 80 / 443 to it (see [Exposing a server with a static IP](../src/setup/static_ip.md) or [Exposing a home server](../src/setup/home_network.md)), and the instance will acquire its wildcard certificate and start serving at `https://mycooldomain.com/`.

## Smoke test it

`image/smoke_test.sh` boots a built HTTP-only image, claims it, and waits for every default app to build and reach `running`, which proves the image works on its arch rather than only that provisioning finished. CI runs it on every release image.

```bash
image/smoke_test.sh --arch arm64 image/out/openhost-<version>-arm64.qcow2
```

## Run it

Boot the resulting qcow2 (QEMU / KVM / libvirt) or `.ova` (VirtualBox). An HTTP-only image is reached and claimed exactly like a release image; see [Deploying on a shared home machine](../src/setup/shared_homeserver.md#part-1-download-and-run-the-vm-image). A `--public` image instead needs its networking in place first (delegate DNS, open the ports, as above), then you claim at `https://<domain>`. Either way, the build prints the dashboard URL, the claim mode, and the console login when it finishes.
