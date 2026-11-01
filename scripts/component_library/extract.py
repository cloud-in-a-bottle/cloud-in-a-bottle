import re
from pathlib import Path

import attr
from jinja2 import Environment
from jinja2 import nodes
from jinja2.nodes import Impossible

SECTION_HEADER = re.compile(r"──\s*(.+?)\s*─")
SCRIPT_CLASS = re.compile(r"""(?:class:\s*|className\s*=\s*|classList\.\w+\()['"]([^'"\n]*)['"]""")
MACRO_DOC = re.compile(r"\{#-?(.*?)-?#\}", re.S)


@attr.s(auto_attribs=True, frozen=True)
class MacroArg:
    name: str
    required: bool
    default: object = None
    default_text: str = ""


@attr.s(auto_attribs=True, frozen=True)
class MacroInfo:
    name: str
    file: str
    line: int
    args: tuple[MacroArg, ...]
    doc: str
    source: str
    uses_caller: bool


@attr.s(auto_attribs=True, frozen=True)
class CssRule:
    selector: str
    text: str
    file: str
    line: int
    section: str


def jinja_literal(value: object) -> str:
    if value is None:
        return "none"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return '"' + value.replace('"', '\\"') + '"'
    return str(value)


def parse_macros(env: Environment, components_dir: Path) -> dict[str, MacroInfo]:
    found = {}
    for path in sorted(components_dir.glob("*.html")):
        source = path.read_text()
        doc_match = MACRO_DOC.search(source)
        doc = _clean_doc(doc_match.group(1)) if doc_match else ""
        for macro in env.parse(source).find_all(nodes.Macro):
            names = [arg.name for arg in macro.args]
            first_default = len(names) - len(macro.defaults)
            args = []
            for index, name in enumerate(names):
                if index < first_default:
                    args.append(MacroArg(name=name, required=True))
                    continue
                default = _const(macro.defaults[index - first_default])
                args.append(MacroArg(name=name, required=False, default=default, default_text=jinja_literal(default)))
            found[macro.name] = MacroInfo(
                name=macro.name,
                file=path.name,
                line=macro.lineno,
                args=tuple(args),
                doc=doc,
                source=source,
                uses_caller=_uses_caller(macro),
            )
    return found


def _const(node: nodes.Expr) -> object:
    try:
        return node.as_const()
    except Impossible:
        return None


def _uses_caller(macro: nodes.Macro) -> bool:
    return any(name.name == "caller" for name in macro.find_all(nodes.Name))


def _clean_doc(raw: str) -> str:
    return " ".join(line.strip() for line in raw.strip().splitlines())


def parse_css(text: str, file: str) -> list[CssRule]:
    rules: list[CssRule] = []
    _parse_block(text, 0, len(text), file, "", rules)
    return rules


def _parse_block(text: str, start: int, end: int, file: str, wrapper: str, rules: list[CssRule]) -> None:
    section = ""
    position = start
    pending_comment_start: int | None = None
    while position < end:
        if text[position].isspace():
            position += 1
            continue
        if text.startswith("/*", position):
            close = text.index("*/", position) + 2
            header = SECTION_HEADER.search(text[position:close])
            if header:
                section = header.group(1)
                pending_comment_start = None
            elif pending_comment_start is None:
                pending_comment_start = position
            position = close
            continue
        brace = text.index("{", position)
        selector = " ".join(text[position:brace].split())
        close = _matching_brace(text, brace)
        if selector.startswith(("@media", "@container")):
            inner: list[CssRule] = []
            _parse_block(text, brace + 1, close, file, selector, inner)
            rules.extend(attr.evolve(rule, section=section) for rule in inner)
        else:
            first = pending_comment_start if pending_comment_start is not None else position
            body = text[first : close + 1]
            if wrapper:
                body = f"{wrapper} {{\n  " + body.replace("\n", "\n  ") + "\n}"
            rules.append(CssRule(selector, body, file, text.count("\n", 0, position) + 1, section))
        pending_comment_start = None
        position = close + 1


def _matching_brace(text: str, opening: int) -> int:
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError(f"Unbalanced braces in CSS at offset {opening}")


def class_pattern(name: str) -> str:
    return re.escape(name) + r"(?:(?:--|__)[\w-]+)?(?![\w-])"


def rule_matches(rule: CssRule, classes: tuple[str, ...], selectors: tuple[str, ...]) -> bool:
    if any(re.search(r"\." + class_pattern(name), rule.selector) for name in classes):
        return True
    return any(_element_match(rule.selector, selector) for selector in selectors)


def _element_match(rule_selector: str, wanted: str) -> bool:
    return any(
        part.strip() == wanted or part.strip().startswith(wanted + ":") or part.strip().startswith(wanted + "[")
        for part in rule_selector.split(",")
    )


def find_macro_uses(name: str, template_files: dict[str, str]) -> dict[str, int]:
    pattern = re.compile(r"(?<![\w.])" + re.escape(name) + r"\(")
    uses = {}
    for file, text in template_files.items():
        count = len(pattern.findall(text)) - len(re.findall(r"macro\s+" + re.escape(name) + r"\(", text))
        if count > 0:
            uses[file] = count
    return uses


def find_class_uses(
    classes: tuple[str, ...], selectors: tuple[str, ...], template_files: dict[str, str], script_files: dict[str, str]
) -> dict[str, int]:
    uses: dict[str, int] = {}
    for selector in selectors:
        element = re.compile("<" + re.escape(selector.split("[")[0]) + r"[\s>]")
        for file, text in template_files.items():
            count = len(element.findall(text))
            if count:
                uses[file] = uses.get(file, 0) + count
    for name in classes:
        token = re.compile(r"(?<![\w-])" + class_pattern(name))
        for file, text in template_files.items():
            count = sum(len(token.findall(value)) for value in re.findall(r'class="([^"]*)"', text))
            if count:
                uses[file] = uses.get(file, 0) + count
        for file, text in script_files.items():
            count = sum(len(token.findall(value)) for value in SCRIPT_CLASS.findall(text))
            count += len(re.findall(r"\bdom\." + re.escape(name) + r"\(", text))
            if count:
                uses[file] = uses.get(file, 0) + count
    return uses
