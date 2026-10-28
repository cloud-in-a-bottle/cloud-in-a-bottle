# Managed instances

As an alternative to hosting your own instance, you can rent a fully-managed instance from us [here](https://cloudinabottle.imbue.com).

The software is exactly the same either way; there are no features that are managed-only (other than the `bottle.cloud` subdomains that managed instances get). Managed instances provide a low-friction way to get started (you can have an instance up in <5 min) and don't require any maintenance to keep running.

You can always [migrate](../operation/backups.md#moving-to-another-machine) a managed instance to self-hosted, and vice versa.

## What's included in a managed instance

- your own VPS with CPU, memory, local disk, a static IPv4, and bandwidth quota ([see plans](https://cloudinabottle.imbue.com/))
  - currently these are hosted in US-west - we will add more regions soon!
- a subdomain of your choice at `bottle.cloud`, eg `johndoe.bottle.cloud`, and a TLS certificate covering this subdomain
- an auto-configured S3 archive backend (Cloudflare R2), billed at cost after a free monthly amount. It belongs to your account: if you delete your instance and create another, the new one starts with your archive (losing at most the last 15 minutes of archive writes). Your instance's local data is not kept.
- coming soon: backups

## Notes

- If you want to use your own domain name, you would sign up for an instance with a `bottle.cloud` subdomain, and then you can change it to your own domain in the instance's settings once it's setup.
- Managed instances come auto-setup with a credential to our web backend that ties it to your account, which gives it access to our managed services:
  - the certificate provider that allows it to get TLS certificates for your `bottle.cloud` subdomain
  - the S3 provider for the archive storage (soon: backups)
  - soon: an email deliverability provider
  - (coming soon: self-hosted instances can be linked to a `cloudinabottle.imbue.com` account to get access to these services also)

## Security/Privacy

- Once your instance is provisioned, we don't keep a SSH key for it. This means we can't readily access your instance, nor can we recover your data if you lose access.
  - (we do technically have low-level (eg serial terminal, or offline disk) access to the VPS your instance runs on)
- Data in the S3 archive store is encrypted in transit and at rest by the storage provider; it is not end-to-end encrypted with your own key
