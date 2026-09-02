# AI agents

A coding agent can do the whole loop on an instance: package a project as a Bottle app, deploy it, read the build log, fix what broke, and reload. It does that through the [`bottle` CLI](./cli.md), which injects auth so the agent never handles a token.

## The agent skill

Install the skill to give your agent the context it needs:

```bash
npx skills add cloud-in-a-bottle/cloud-in-a-bottle --skill cloud-in-a-bottle-context
```

The skill is just the markdown file [`skills/cloud-in-a-bottle-context/SKILL.md`](https://github.com/cloud-in-a-bottle/cloud-in-a-bottle/blob/main/skills/cloud-in-a-bottle-context/SKILL.md) in the repo, if you'd prefer to use it directly.

The skill assumes `bottle` is [installed and logged in](./cli.md#install-and-log-in). Logging in is interactive, so do it yourself before handing over.

If you have more than one instance, tell the agent which: every command takes `--instance <name>`.

## Feeding it the manual

Any page is served as its own Markdown source by adding `.md` to its URL, and the whole manual (every page, concatenated) is served at [`/docs/all.md`](/docs/all.md):

```bash
curl https://your-zone.example.com/docs/how_it_works/routing.md
curl https://your-zone.example.com/docs/all.md
```

The copy icons put the same text on your clipboard: the one beside each page's heading copies that page, and the one beside **Cloud in a Bottle Manual** in the sidebar copies the whole thing.

 An agent reading the manual off your own instance gets the version you are actually running.
