import argparse
import base64
import datetime
import itertools
import json
import re
import string
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import attr
from catalog import COMPONENTS
from catalog import PAGE_ONLY_CLASSES
from extract import CssRule
from extract import MacroInfo
from extract import find_class_uses
from extract import find_macro_uses
from extract import jinja_literal
from extract import parse_css
from extract import parse_macros
from extract import rule_matches
from jinja2 import Environment
from jinja2 import FileSystemLoader
from model import ICONS
from model import Choice
from model import Component
from model import Control
from model import ControlValue
from model import Text
from model import Toggle

HERE = Path(__file__).parent
REPO_ROOT = HERE.parents[1]
WEB = Path("compute_space/src/compute_space/web")
GITHUB = "https://github.com/cloud-in-a-bottle/cloud-in-a-bottle/blob/main/"
GROUP_ORDER = ("Actions", "Navigation", "Content", "Status", "Forms")
MAX_AUTO_TOGGLES = 3


@attr.s(auto_attribs=True, frozen=True)
class Repo:
    root: Path
    env: Environment
    preamble: str
    macros: dict[str, MacroInfo]
    rules: tuple[CssRule, ...]
    templates: dict[str, str]
    scripts: dict[str, str]
    icons: tuple[str, ...]


def control_label(control: Control) -> str:
    return control.label or control.name.replace("_", " ").capitalize()


def to_value(raw: str) -> ControlValue:
    if raw == "none":
        return None
    return int(raw) if raw.isdigit() else raw


def sentinel(name: str) -> str:
    return f"@@{name}@@"


def resolve(controls: Sequence[Control], icons: tuple[str, ...]) -> tuple[Control, ...]:
    return tuple(
        attr.evolve(c, options=icons) if isinstance(c, Choice) and c.options == ICONS else c for c in controls
    )


def macro_call(
    macro: MacroInfo, controls: Sequence[Control], values: dict[str, ControlValue], as_template: bool
) -> str:
    known = {control.name: control for control in controls}
    parts = []
    for arg in macro.args:
        if arg.name not in known:
            if arg.required:
                raise ValueError(f"{macro.name}: required argument {arg.name} has no control")
            continue
        value = values[arg.name]
        if not arg.required and value == arg.default:
            continue
        rendered = f"v.{arg.name}" if as_template else jinja_literal(value)
        parts.append(rendered if arg.required else f"{arg.name}={rendered}")
    return "{{ " + f"{macro.name}({', '.join(parts)})" + " }}"


def preview_template(component: Component, macro: MacroInfo | None, controls: Sequence[Control]) -> str:
    if component.template is not None:
        return component.template
    if macro is None:
        raise ValueError(f"{component.id}: needs a macro or a template")
    every_arg = {control.name: object() for control in controls}
    return macro_call(macro, controls, every_arg, as_template=True)


def usage_for(
    component: Component,
    macro: MacroInfo | None,
    controls: Sequence[Control],
    values: dict[str, ControlValue],
    rendered: str,
) -> str:
    if component.usage is not None:
        return string.Template(component.usage).substitute({k: _plain(v) for k, v in values.items()})
    if macro is not None:
        return macro_call(macro, controls, values, as_template=False)
    return rendered


def _plain(value: ControlValue) -> str:
    return "" if value is None else str(value)


def render(repo: Repo, template: str, values: dict[str, ControlValue]) -> str:
    return repo.env.from_string(repo.preamble + template).render(v=values).strip()


def combinations(controls: Sequence[Control]) -> list[dict[str, ControlValue]]:
    axes: list[list[tuple[str, ControlValue]]] = []
    for control in controls:
        if isinstance(control, Text):
            axes.append([(control.name, sentinel(control.name))])
        elif isinstance(control, Toggle):
            axes.append([(control.name, False), (control.name, True)])
        else:
            axes.append([(control.name, to_value(option)) for option in control.options])
    return [dict(combo) for combo in itertools.product(*axes)]


def combo_key(controls: Sequence[Control], values: dict[str, ControlValue]) -> str:
    enumerated = [control for control in controls if not isinstance(control, Text)]
    return "|".join(_key_part(values[control.name]) for control in enumerated)


def _key_part(value: ControlValue) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "none" if value is None else str(value)


def default_values(controls: Sequence[Control]) -> dict[str, ControlValue]:
    values: dict[str, ControlValue] = {}
    for control in controls:
        if isinstance(control, Text):
            values[control.name] = sentinel(control.name)
        elif isinstance(control, Toggle):
            values[control.name] = control.default
        else:
            values[control.name] = to_value(control.options[0])
    return values


def gallery_cases(controls: Sequence[Control]) -> list[tuple[str, dict[str, ControlValue]]]:
    base = default_values(controls)
    choices = [control for control in controls if isinstance(control, Choice)]
    cases = []
    if choices:
        lead = choices[0]
        cases = [(option, {**base, lead.name: to_value(option)}) for option in lead.options]
    for control in controls:
        if isinstance(control, Toggle):
            case = {**base, control.name: not control.default}
            cases.append((f"{control_label(control)}: {'on' if not control.default else 'off'}", case))
    return cases


def describe_control(control: Control) -> dict[str, object]:
    if isinstance(control, Text):
        return {"name": control.name, "label": control_label(control), "kind": "text", "default": control.default}
    if isinstance(control, Toggle):
        return {"name": control.name, "label": control_label(control), "kind": "toggle", "default": control.default}
    return {
        "name": control.name,
        "label": control_label(control),
        "kind": "choice",
        "options": list(control.options),
        "default": control.options[0],
    }


def css_payload(rules: Sequence[CssRule]) -> list[dict[str, object]]:
    return [{"text": rule.text, "file": rule.file, "line": rule.line, "url": _css_url(rule)} for rule in rules]


def _css_url(rule: CssRule) -> str:
    return f"{GITHUB}{WEB}/static/css/{rule.file}#L{rule.line}"


def macro_payload(macros: Sequence[MacroInfo]) -> list[dict[str, object]]:
    return [
        {
            "name": macro.name,
            "file": f"_components/{macro.file}",
            "url": f"{GITHUB}{WEB}/templates/_components/{macro.file}#L{macro.line}",
            "doc": macro.doc,
            "source": macro.source,
            "takesContent": macro.uses_caller,
            "args": [{"name": a.name, "required": a.required, "default": a.default_text} for a in macro.args],
        }
        for macro in macros
    ]


def used_in(component: Component, macros: Sequence[MacroInfo], repo: Repo) -> list[dict[str, object]]:
    counts: dict[str, int] = {}
    for macro in macros:
        for file, count in find_macro_uses(macro.name, repo.templates).items():
            counts[file] = counts.get(file, 0) + count
    if not macros:
        counts = find_class_uses(component.classes, component.selectors, repo.templates, repo.scripts)
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [{"file": file, "count": count, "url": _source_url(file)} for file, count in ordered]


def _source_url(file: str) -> str:
    folder = "static/js" if file.endswith(".js") else "templates"
    return f"{GITHUB}{WEB}/{folder}/{file}"


def build_component(component: Component, repo: Repo) -> dict[str, object]:
    primary = repo.macros[component.macro] if component.macro else None
    macros = ([primary] if primary else []) + [repo.macros[name] for name in component.also]
    controls = resolve(component.controls, repo.icons)
    template = preview_template(component, primary, controls)

    combos = {}
    for values in combinations(controls):
        rendered = render(repo, template, values)
        combos[combo_key(controls, values)] = {
            "html": component.wrap.replace("{}", rendered),
            "usage": usage_for(component, primary, controls, values, rendered),
        }
    gallery = []
    for label, values in gallery_cases(controls):
        gallery.append({"label": label, "html": component.wrap.replace("{}", render(repo, template, values))})

    rules = [rule for rule in repo.rules if rule_matches(rule, component.classes, component.selectors)]
    return {
        "id": component.id,
        "name": component.name,
        "group": component.group,
        "summary": component.summary,
        "kind": "macro" if primary else "css",
        "phone": component.phone,
        "controls": [describe_control(control) for control in controls],
        "combos": combos,
        "gallery": gallery,
        "macros": macro_payload(macros),
        "css": css_payload(rules),
        "usedIn": used_in(component, macros, repo),
        "notes": list(component.notes),
    }


def auto_controls(macro: MacroInfo) -> tuple[Control, ...]:
    controls: list[Control] = []
    toggles = 0
    for arg in macro.args:
        if arg.required:
            controls.append(Text(arg.name, arg.name.replace("_", " ").capitalize()))
        elif isinstance(arg.default, bool) and toggles < MAX_AUTO_TOGGLES:
            controls.append(Toggle(arg.name, arg.default))
            toggles += 1
    return tuple(controls)


def auto_macro_component(macro: MacroInfo, repo: Repo) -> dict[str, object]:
    controls = auto_controls(macro)
    template = macro_call(macro, controls, {control.name: object() for control in controls}, as_template=True)
    if macro.uses_caller:
        template = template.replace("{{ ", "{% call ", 1).replace(" }}", " %}Example content{% endcall %}", 1)
    component = Component(
        id=f"auto-{macro.name}",
        name=macro.name.replace("_", " ").capitalize(),
        group="New",
        summary=macro.doc or f"Found in _components/{macro.file}.",
        macro=macro.name,
        template=template,
        controls=controls,
        classes=(macro.name.replace("_", "-"),),
        notes=("Found automatically. Add it to catalog.py to give it better sample content and more controls.",),
    )
    try:
        return build_component(component, repo)
    except Exception as error:
        print(f"warning: {macro.name} found but could not be previewed automatically: {error}", file=sys.stderr)
        return {
            **_source_only(component.id, component.name, "New", component.summary, repo, [macro]),
            "previewError": f"This macro couldn't be previewed with placeholder values: {error}. "
            "Add it to catalog.py with sample content.",
        }


def _source_only(
    id_: str, name: str, group: str, summary: str, repo: Repo, macros: Sequence[MacroInfo]
) -> dict[str, object]:
    return {
        "id": id_,
        "name": name,
        "group": group,
        "summary": summary,
        "kind": "macro" if macros else "css",
        "phone": False,
        "controls": [],
        "combos": {},
        "gallery": [],
        "macros": macro_payload(macros),
        "css": [],
        "usedIn": [],
        "notes": [],
    }


def undocumented_css(repo: Repo) -> list[dict[str, object]]:
    uncovered: dict[str, list[CssRule]] = {}
    for rule in repo.rules:
        if rule.file != "components.css" or not rule.section or "." not in rule.selector:
            continue
        if any(rule_matches(rule, c.classes, c.selectors) for c in COMPONENTS):
            continue
        if rule_matches(rule, PAGE_ONLY_CLASSES, ()):
            continue
        uncovered.setdefault(rule.section, []).append(rule)
    entries = []
    for section, rules in uncovered.items():
        slug = re.sub(r"[^a-z0-9]+", "-", section.lower()).strip("-")
        page = _source_only(
            f"css-{slug}", section, "No preview yet", "Styles in components.css with no preview yet.", repo, []
        )
        page["css"] = css_payload(rules)
        page["kind"] = "css"
        entries.append(page)
    return entries


def parse_tokens(css: str) -> list[dict[str, object]]:
    root = css[css.index(":root") : css.index("@media")]
    tokens = []
    group = ""
    for line in root.splitlines():
        heading = re.match(r"\s*/\*\s*([\w/ ]+?)(?:\.|\s*\*/)", line)
        if heading:
            group = heading.group(1).strip()
        token = re.match(r"\s*(--[\w-]+):\s*(.+);", line)
        if token:
            tokens.append({"name": token.group(1), "value": token.group(2), "group": group})
    return tokens


def data_uri(path: Path) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(path.read_bytes()).decode()


def repo_state(root: Path) -> dict[str, object]:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    return {
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "commit": git("rev-parse", "--short", "HEAD"),
        "uncommitted": bool(git("status", "--porcelain", "--", str(WEB))),
        "built": datetime.date.today().isoformat(),
    }


def load_repo(root: Path) -> Repo:
    web = root / WEB
    if not web.is_dir():
        raise SystemExit(f"{root} is not a Cloud in a Bottle checkout: {web} is missing")
    templates_dir = web / "templates"
    env = Environment(loader=FileSystemLoader(templates_dir), autoescape=True)
    env.globals["static_url"] = lambda path: f"/static/{path}"
    env.globals["app_url"] = lambda name: f"https://{name}.alice.example.com/"
    macros = parse_macros(env, templates_dir / "_components")
    preamble = "".join(
        f'{{% from "_components/{file}" import {", ".join(sorted(m.name for m in macros.values() if m.file == file))}'
        " with context %}"
        for file in sorted({macro.file for macro in macros.values()})
    )
    rules = tuple(
        rule
        for file in ("components.css", "base.css")
        for rule in parse_css((web / "static/css" / file).read_text(), file)
    )
    return Repo(
        root=root,
        env=env,
        preamble=preamble,
        macros=macros,
        rules=rules,
        templates={str(p.relative_to(templates_dir)): p.read_text() for p in sorted(templates_dir.rglob("*.html"))},
        scripts={p.name: p.read_text() for p in sorted((web / "static/js").glob("*.js"))},
        icons=tuple(sorted(p.stem for p in (web / "static/img/icons").glob("*.svg"))),
    )


def build_page(repo: Repo, fragment: bool) -> str:
    web = repo.root / WEB
    static = web / "static"
    catalogued = {name for c in COMPONENTS for name in ((c.macro,) if c.macro else ()) + c.also}
    missing = catalogued - set(repo.macros)
    if missing:
        raise SystemExit(f"catalog.py names macros that no longer exist: {', '.join(sorted(missing))}")

    pages = [build_component(component, repo) for component in COMPONENTS]
    pages.sort(key=lambda page: (GROUP_ORDER.index(page["group"]), page["name"]))
    new = [auto_macro_component(m, repo) for name, m in sorted(repo.macros.items()) if name not in catalogued]
    unpreviewed = undocumented_css(repo)

    assets = {
        f"/static/img/{p.relative_to(static / 'img').as_posix()}": data_uri(p) for p in (static / "img").rglob("*.svg")
    }
    product_css = "\n".join(
        (static / "css" / name).read_text() for name in ("tokens.css", "base.css", "components.css")
    )
    product_css = re.sub(
        r'url\("\.\./img/([^"]+)"\)', lambda m: f'url("{assets["/static/img/" + m.group(1)]}")', product_css
    )

    data = {
        "pages": new + pages + unpreviewed,
        "tokens": parse_tokens((static / "css/tokens.css").read_text()),
        "icons": list(repo.icons),
        "assets": assets,
        "productCss": product_css,
        "frameScripts": "\n".join(
            (static / "js" / name).read_text() for name in ("info-tip.js", "nav-menu.js", "field.js")
        ),
        "repo": repo_state(repo.root),
        "tokensUrl": f"{GITHUB}{WEB}/static/css/tokens.css",
    }
    payload = json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")
    shell = (HERE / "shell.html").read_text()
    page = (
        shell.replace("/*PRODUCT_CSS*/", product_css)
        .replace("/*SHELL_CSS*/", (HERE / "shell.css").read_text())
        .replace("/*SHELL_JS*/", (HERE / "shell.js").read_text())
        .replace("/*DATA*/null", payload)
    )
    if fragment:
        return page
    head = '<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
    return f'<!doctype html>\n<html lang="en">\n{head}\n{page}\n</html>\n'


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Cloud in a Bottle component library page.")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help="Checkout to build from. Defaults to this one")
    parser.add_argument("--out", type=Path, default=HERE / "cloud-in-a-bottle-components.html")
    parser.add_argument("--fragment", action="store_true", help="Leave out <!doctype> and <html>, for publishing")
    args = parser.parse_args()
    repo = load_repo(args.repo.expanduser().resolve())
    page = build_page(repo, args.fragment)
    args.out.write_text(page)
    print(f"Wrote {args.out} ({len(page) // 1024} KB)")


if __name__ == "__main__":
    main()
