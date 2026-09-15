# Managed spaces

> **DRAFT (CB-295).** This page is written but not yet listed in `SUMMARY.md`, so it is not published in the manual. The `TODO` callouts below are the facts only the Imbue side can supply. Once they are filled in, add the page to the Setup section of `docs/src/SUMMARY.md` and delete this banner.

The other pages in this section are about running Cloud in a Bottle yourself. This one is about the other option: [Imbue runs the machine for you](https://cloudinabottle.imbue.com/) and hands you a space that is already up.

It is the same software either way. A managed space runs the same Cloud in a Bottle release from the same [public repository](https://github.com/cloud-in-a-bottle/cloud-in-a-bottle), with the same dashboard, the same app catalog, the same manifests, and the same shell on the machine. Nothing in this manual stops applying because your space is managed, and nothing in a managed space is proprietary. What Imbue takes off your hands is the parts of [Deploying on a cloud instance](./cloud_instance.md) that have nothing to do with actually using your space: buying a domain, delegating DNS, provisioning a server, and running the installer.

## Which one you want

Pick a managed space if you want a working space today and would rather not own a server or a domain.

Self-host instead if you want the machine to be physically yours, if you already have hardware sitting idle, if you need particular hardware (lots of disk, a GPU), or if you would rather no third party be able to touch the host at all. Start at [Deploying on a cloud instance](./cloud_instance.md) or [Deploying on a dedicated home server](./dedicated_homeserver.md).

You are not locked into the choice. See [Moving between managed and self-hosted](#moving-between-managed-and-self-hosted).

> **TODO:** the honest one-paragraph pitch, and the honest reasons not to. Worth naming the real tradeoff up front: Imbue can reach the host.

## What you get

> **TODO:** tiers, price, and what each includes (memory, vCPU, disk, bandwidth). Where the machines run. Whether there is a trial or a free tier (the [roadmap](../roadmap.md) lists a free tier as planned, so say what exists today). What the support commitment is, and what the uptime story is.

## Getting one

1. Sign up at [cloudinabottle.imbue.com](https://cloudinabottle.imbue.com/).
2. Give Imbue your SSH public key and pick a name for your space.
3. Imbue provisions the machine, points a domain at it, and hands it over.
4. Open your space's URL, create your owner account, and you are on the [dashboard](../operation/overview.md).

> **TODO:** the real steps and the real screens. How long provisioning takes. What the space's URL looks like. Whether you claim it with a claim token the way a self-hosted instance does, or whether the account is already created when you get it. What happens to the SSH key, and whether you can change or add one later.

## What is different from a self-hosted instance

Three things are set up differently. Everything else in the manual applies unchanged.

**The domain.** You do not buy a domain or delegate a zone. Your space gets a subdomain of a zone Imbue operates, and each of your apps gets a subdomain of that, exactly as [Routing](../how_it_works/routing.md) describes. There is no DNS step for you at any point.

> **TODO:** the actual domain shape, and whether you can bring your own domain to a managed space (the dashboard's **Settings → Domains** supports adding one, and a managed space has no CoreDNS zone of its own for it, so say what is actually supported).

**TLS certificates.** A self-hosted instance talks to Let's Encrypt with its own ACME account. A managed space gets certificates through the Imbue certificate broker instead. The instance generates its own keypair and certificate signing request locally and sends only the request, so the private key never leaves your machine. See [TLS certificates](../how_it_works/routing.md#tls-certificates).

**The Imbue credential.** A managed space is provisioned with a per-instance credential that authenticates it to Imbue services (the certificate broker today, more later). A self-hosted instance can get the same credential at any time by clicking **Connect to Imbue** in Settings, which is how a self-hosted instance opts into the same services.

## What stays yours

- The data is on your instance, in the storage tiers described in [Data](../how_it_works/data.md). Imbue does not run your apps' databases.
- The software is AGPL. You can read every line of what is running, and the dashboard's update button pulls from the same public repo.
- You have a shell on the machine, through the dashboard's terminal and over SSH, and the [bottle CLI](../operation/cli.md) works the same way.
- Backups are yours to configure, to storage you choose. See [Backups and restore](../operation/backups.md).

> **TODO:** the part this section is really for. Who at Imbue can access the host, under what circumstances, and what the policy is. Whether backups are taken for you or are entirely your job. What happens to the machine and its data if you stop paying or close the account. This is the section a careful reader will actually read, so it should be specific rather than reassuring.

## Operating a managed space

Day to day it is the manual: [Using your instance](../operation/overview.md), then [Backups and restore](../operation/backups.md), and [Debugging](../operation/debugging.md) when something misbehaves.

> **TODO:** what Imbue operates and what you operate. Specifically: who applies Cloud in a Bottle updates (self-hosted is a button on the settings page), who patches the host OS, who is responsible when an app you installed eats the disk, and how you reach support.

## Moving between managed and self-hosted

> **TODO:** the migration story in both directions, which is the thing that makes "not locked in" a real claim rather than a slogan. Presumably it is a backup on one side and a restore on the other ([Backups and restore](../operation/backups.md)), plus a domain change; confirm and write it down, including what does not survive the move.
