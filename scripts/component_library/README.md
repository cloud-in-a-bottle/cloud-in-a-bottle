# Cloud in a Bottle component library

Builds one self-contained HTML page from this repo. Previews render the real Jinja macros with the real CSS, so the page can't drift from the code.

## Rebuild

From the repo root:

```bash
pixi run -e dev just components
```

This writes `scripts/component_library/cloud-in-a-bottle-components.html`, which git ignores. It builds from whatever is checked out, so it shows components on your branch before they're merged. Open the file in a browser.

## Adding a component

- **A new macro in `_components/`** appears on the next rebuild under **New**, with placeholder content and a control for each required argument and on/off option.
- **New CSS** with no matching page appears under **No preview yet**, showing its styles.
- **To finish a page**, add a `Component` to `catalog.py`. That gives it realistic sample content, its controls, notes and a place in the list.

The build stops with an error if `catalog.py` names a macro that no longer exists. A catalog entry that can't render also stops the build.
