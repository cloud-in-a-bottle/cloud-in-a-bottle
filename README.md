# Cloud in a Bottle

Your corner of the cloud.

Deploy, use, and share web apps on a server you control. Built on the idea that modern software lives in the cloud, and if you want to be in control of your digital life, you need your own place in the cloud too.

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

## Why Cloud in a Bottle

Most people have no access to the cloud that isn't mediated by a company with different incentives than theirs. Open source web software exists but running it somewhere means fighting infrastructure that most people don't want to touch.

Cloud in a Bottle is the project our team needed and couldn't find: a corner of the cloud that's genuinely yours. Where apps install as easily as on your phone, and the data lives on hardware you control.

## What people deploy

- Personal tools — AI-generated apps, scripts, and utilities with nowhere useful to host them
- Open source software — Matrix, Minecraft servers, notes apps, project management tools
- Dev and creative tools — coding agents, image-making software, anything you built and want to share with a real URL
- Containerized web apps — add a `cloudinabottle.toml` manifest to a repo with a Dockerfile and it's deployable

## Get Cloud in a Bottle

### Run it yourself

Cloud in a Bottle runs on your own hardware, a local virtual machine, or a cloud server. Follow the
[deployment guide](https://cloudinabottle.org/docs/deploying.html) to install it.

### Managed hosting

If you'd rather not run your own server, [Imbue can provision one for you](https://cloudinabottle.imbue.com/): your
SSH key, your data, your instance. We set up what you need to get going, and then it's yours.

## How it works

The Python router provides the dashboard and app management APIs. It reads a `cloudinabottle.toml` manifest from each
app repository, builds and runs the app in a rootless Podman container, and routes requests to it by hostname. App and
instance data remain under your control.

## Documentation

Read the [Cloud in a Bottle manual](https://cloudinabottle.org/docs/) for platform concepts, app development, and
operating guides. Useful starting points include:

- [Deploying Cloud in a Bottle](https://cloudinabottle.org/docs/deploying.html)
- [Creating an app](https://cloudinabottle.org/docs/creating_an_app.html)
- [`cloudinabottle.toml` manifest specification](https://cloudinabottle.org/docs/manifest_spec.html)

The manual's source lives in `docs/src/` and is served directly by each Cloud in a Bottle instance, with no separate
build step. Add pages to `docs/src/SUMMARY.md` to include them in the manual.

## Agent skill

An agent skill that gives an AI coding agent context for deploying and debugging apps on Cloud in a Bottle via the `bottle` CLI. Install with:

```bash
npx skills add cloud-in-a-bottle/cloud-in-a-bottle --skill openhost-context
```

The skill works best with the `bottle` CLI installed and logged in:

```bash
uv tool install "cloud-in-a-bottle-cli @ git+https://github.com/cloud-in-a-bottle/cloud-in-a-bottle.git#subdirectory=compute_space_cli"
bottle instance login
```

Once set up, ask your coding agent to package any existing project for Cloud in a Bottle and deploy it directly — no manual manifest editing required.

---

## License

Cloud in a Bottle is provided under the [AGPL-3.0 license](LICENSE).

We may move to a different license in the future — something like a [fair source license](https://fair.io/licenses/) — with the intent that personal use will always be unrestricted, while commercial use may be scoped to support a sustainable project.

---

## About Imbue

We build honest software. Often open source. Tools that help people think, create, and build. Tools that are loyal to you. 

- [Explore Imbue's code on GitHub](https://github.com/imbue-ai)
- [Check us out at imbue.com](https://imbue.com/)
- [Follow @Imbue_AI on X](https://x.com/imbue_ai)
