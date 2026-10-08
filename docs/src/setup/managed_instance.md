# Managed instances

As an alternative to hosting your own instance, you can rent a fully-managed instance from us [here](https://cloudinabottle.imbue.com).

The software is exactly the same either way; there are no features that are managed-only (other than the `bottle.cloud` subdomains that managed instances get). Managed instances provide a low-friction way to get started (you can have an instance up in <5 min) and don't require any maintenance to keep running.

You can always [migrate](../operation/backups.md#moving-to-another-machine) a managed instance to self-hosted, and vice versa.

## What's included in a managed instance

- your own VPS with CPU, memory, and local disk ([see plans](https://cloudinabottle.imbue.com/))
  - currently these are hosted in US-west - we will add more regions soon!
- a subdomain of your choice at `bottle.cloud`, eg `johndoe.bottle.cloud`, and a TLS certificate covering this subdomain
- coming soon: archive backend and backups auto-configured


TODO: bandwidth

## Notes

- If you want to use your own domain name, you would sign up for an instance with a `bottle.cloud` subdomain, and then you can change it to your own domain in the instance's settings once it's setup.
- Managed instances come auto-setup with a credential to our web backend that ties it to your account, which gives it access to our managed services:
  - the certificate provider that allows it to get TLS certificates for your `bottle.cloud` subdomain
  - soon: a S3 provider for archive/backup storage
  - soon: an email deliverability provider
  - (self-hosted instances can be linked to a `cloudinabottle.imbue.com` account to get access to these services also)

## Security/Privacy

- Once your instance is provisioned, we don't keep a SSH key for it. This means we can't readily access your instance, nor can we recover your data if you lose access.
  - (we do technically have low-level (eg serial terminal, or offline disk) access to the VPS your instance runs on. it is unfortunately hard to run a VPS without being able to access its data; we hope to enable this via SEV-VMs in the future)
- Data in the S3 archive/backup store is encrypted via your own key
