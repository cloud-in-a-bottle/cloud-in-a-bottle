# Using your instance

Everything day to day happens in the dashboard at `https://<your-domain>/`. This page is the tour: what is there, what came preinstalled, and how to put your own things on it.

## The dashboard

| Page | What it is |
|---|---|
| **Dashboard** | Every app, its status, and a link into each one |
| **Catalog** and **Deploy app** | The two ways to install something |
| **Settings** | Your account, API tokens, domains, archive storage, updates |
| **System info** | Memory, disk and per-app resource use, plus a full diagnostics dump |
| **Terminal** | A shell on the machine, in the browser |
| **Docs** | This manual, served from the version you are running |

An app's "detail" page has its logs, its resource use, the paths it declared, and the buttons to reload, stop or remove it.

## Default Apps

A new instance installs a handful of apps at setup:

| App | What it is for |
|---|---|
| **Catalog** | Browse apps and install them in a click |
| **Backup** | Encrypted backups to external storage. See [Backups and restore](./backups.md) |
| **Files** | A file browser over your instance's data |
| **Secrets** | Key-value secret storage other apps can request through the [secrets service](../creating_an_app/cross_app_services.md) |
| **OAuth provider** | Holds your Google and GitHub tokens so apps can act on your behalf. See the [oauth service spec](https://github.com/cloud-in-a-bottle/cloud-in-a-bottle/blob/main/services/oauth/README.md) |
| **Community chat** | A Matrix server and client, pre-configured to join the Cloud in a Bottle chat server |

Each one is an ordinary app on its own subdomain, and you can remove any of them.

## Installing apps

Two routes, both ending in the same place.

**From the catalog.** Open the catalog app, find something, click Install. It hands you to the Deploy app page with the repository and name already filled in. The catalog is a curated feed of known-good apps; open a PR in the [app-manifest](https://github.com/cloud-in-a-bottle/app-manifest) repo to request adding a new app.

**From a git repository.** Deploy app → paste the URL of any repo with a `cloudinabottle.toml` at its root. Private GitHub repos prompt for authorization the first time. The router clones the repo, reads the manifest, builds the container image, and starts the app at `https://<app>.<your-domain>/`. See [Creating an App](../creating_an_app/overview.md) for more on creating your own apps.

### Before you click install

The install page shows what the app asked for in its manifest: how much memory, which data it wants, whether it wants other apps' data, extra Linux capabilities, host devices, host networking, and which cross-app services it consumes. You should review this carefully. By default, apps have minimal permissions to do unsafe things in your space. But if given elevated access, the potential harm can become much greater. [Security](../how_it_works/security.md) explains what each request actually grants.

Updates get the same treatment: when an app's manifest changes, the update page shows the changed settings and any new service permissions, and nothing is granted until you approve.

### Updating apps

Apps do not currently auto-update themselves. There's a button in the app details page to manually fetch + install any updates.

## Who can use your apps

By default, only you. Every route of every app requires your owner session, and the router enforces that before the request reaches the app.

An app can open specific routes to everyone by listing them in `public_paths` in its manifest, which is how a blog, a webhook receiver, or a public share link works. That is all-or-nothing: a public path is public to the entire internet, and anything behind it is the app's job to protect.

There is no way yet to give a named person their own login to your instance or to specific apps; this is on the roadmap.

## Settings worth knowing about

- **Domains.** Add or remove the names the instance answers on, and choose the primary domain used for canonical links and app configuration. A public domain can acquire a wildcard certificate automatically when DNS-01 is configured, and must have an active certificate before it can become primary. See [Routing](../how_it_works/routing.md).
- **Archive storage.** Point the bulk-content tier at an S3 bucket instead of local disk. See [Data](../how_it_works/data.md#the-archive-tier).
- **API tokens.** Create and revoke the tokens the [CLI](./cli.md) and any scripts use. They are owner-equivalent, so give them expiries.
- **Owner account.** Change the username apps see, and your password.
- **Updates.** Check for and apply new Cloud in a Bottle versions. It pulls the code, runs any host migrations, and restarts, streaming progress while it goes.

## Backups

See [backups](./backups.md).
