# Introduction

This is the Cloud in a Bottle Manual. It documents the platform from the perspective of an *owner* (someone running a Cloud in a Bottle instance) and of an *app author* (someone packaging an application to run on Cloud in a Bottle).

## For owners

Sections about running a zone, deploying apps, managing data, and debugging when things go wrong.

Most of this is in the dashboard at [https://your-zone-domain/](./). This manual fills in the *conceptual* model behind what you see in the UI.

## For app authors

Sections about how Cloud in a Bottle expects an app to be packaged: the manifest format, the runtime contract, what your container can expect from the environment, and how to integrate with the Cloud in a Bottle identity / permissions / inter-app services machinery.

If you're building an app from scratch, start at [Creating an App](./creating_an_app/overview.md).  If you have an existing app and want to know which knob in `cloudinabottle.toml` controls what, jump to the [App Manifest Spec](./creating_an_app/manifest_spec.md).


## Improving the docs

PRs against `docs/src/*.md` in the [cloud in a bottle repo](https://github.com/cloud-in-a-bottle/cloud-in-a-bottle) are welcome.
