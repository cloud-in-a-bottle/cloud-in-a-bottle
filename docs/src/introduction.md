# Introduction

This is the OpenHost Manual.  It documents the platform from the
perspective of an *operator* (someone running a personal OpenHost
zone) and from the perspective of an *app author* (someone packaging
an application to run on OpenHost).

Both audiences need different things, so the manual is split into
two halves.

## For operators

Sections about running a zone, deploying apps, managing data, and
debugging when things go wrong.

Most of this is in the dashboard at
[https://your-zone-domain/](./).  This manual fills in the
*conceptual* model behind what you see in the UI.

## For app authors

Sections about how OpenHost expects an app to be packaged — the
manifest format, the runtime contract, what your container can
expect from the environment, and how to integrate with the
OpenHost identity / permissions / inter-app services machinery.

If you're building an app from scratch, start at [Creating an
App](./creating_an_app.md).  If you have an existing app and want
to know which knob in `openhost.toml` controls what, jump to the
[App Manifest Spec](./manifest_spec.md).

## How this manual is built and shipped

The Markdown source for this manual lives in `docs/src/` in the
[imbue-openhost/openhost](https://github.com/imbue-openhost/openhost)
repository.  The pages are rendered server-side on every request
(with an mtime-keyed cache), so a `git pull` on the zone is enough
to ship doc changes — no build step required.  When you're reading
the manual on your own zone at
`https://your-zone.example.com/docs/`, you're reading the docs
that match the OpenHost version you have running — never out of
sync.

## Feeding the manual to an AI agent

Any page is served as its own Markdown source by adding `.md` to its
URL, and the whole manual — every page, concatenated — is served at
[`/docs/all.md`](/docs/all.md):

```bash
curl https://your-zone.example.com/docs/routing.md
curl https://your-zone.example.com/docs/all.md
```

The copy icons put the same text on your clipboard: the one beside
each page's heading copies that page, and the one beside **OpenHost
Manual** in the sidebar copies the whole thing.

## Improving the docs

PRs against `docs/src/*.md` in the
[openhost repo](https://github.com/imbue-openhost/openhost)
are welcome.
