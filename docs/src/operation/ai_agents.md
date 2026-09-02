# AI agents

A coding agent can do the whole loop on an instance: package an existing project as a Bottle app, deploy it, read the build log, fix what broke, and reload. It does that through the [`bottle` CLI](./cli.md), which injects auth so the agent never handles a token.

## The agent skill

Install the skill to give your agent the context it needs:

```bash
npx skills add cloud-in-a-bottle/cloud-in-a-bottle --skill cloud-in-a-bottle-context
```

It covers the CLI, the deploy and reload loop, the `cloudinabottle.toml` manifest, and the safety rules that matter here — chiefly that `public_paths` exposes a route to the entire internet, and that API tokens stay out of the repo.

The skill is one markdown file, [`skills/cloud-in-a-bottle-context/SKILL.md`](https://github.com/cloud-in-a-bottle/cloud-in-a-bottle/blob/main/skills/cloud-in-a-bottle-context/SKILL.md) in the repo. If you would rather not use the installer, copy it wherever your agent reads skills from, or just point the agent at it.

The skill assumes `bottle` is installed and logged in. Installing is one command, but logging in is interactive, so do it yourself before handing over:

```bash
uv tool install "cloud-in-a-bottle-cli @ git+https://github.com/cloud-in-a-bottle/cloud-in-a-bottle.git#subdirectory=compute_space_cli"
bottle instance login
```

If you have more than one instance, tell the agent which: every command takes `--instance <name>`.

## Feeding it the manual

Any page of this manual is served as its own Markdown source by adding `.md` to the URL, and the whole thing is at `/docs/all.md` — see [Introduction](../introduction.md#feeding-the-manual-to-an-ai-agent). An agent reading the manual off your own instance gets the version you are actually running.

## The workflow

[Creating an App](../creating_an_app/overview.md#ai-agent-development) has the commit → push → reload → check-logs loop the skill is built around, with the commands spelled out.
