import yaml
from litestar.datastructures import Accept
from litestar.datastructures.headers import MediaTypeHeader
from yaml.nodes import ScalarNode


class _ExportDumper(yaml.SafeDumper):
    pass


def _represent_string(dumper: _ExportDumper, value: str) -> ScalarNode:
    style = None
    # Escape line separators to prevent YAML's line-break normalization changing values.
    if any(char in value for char in "\r\x85\u2028\u2029"):
        style = '"'
    elif "\n" in value:
        # The emitter selects chomping/indentation and falls back to quotes when needed.
        style = "|"
    elif value and value[0] in "+-0123456789.":
        # Quote number-like prefixes conservatively: js-yaml also resolves 1e3, 0o12 and ._5.
        style = '"'
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


# Register only on this subclass, never on the process-wide safe dumper.
_ExportDumper.add_representer(str, _represent_string)


def dump_export_yaml(document: dict[str, object]) -> str:
    ordered = {key: document[key] for key in ("schema_version", "mode")} | document
    return yaml.dump(ordered, Dumper=_ExportDumper, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _accept_priority(media_type: MediaTypeHeader) -> tuple[int, float]:
    specificity = media_type.priority[1]
    # HTTP permits three decimal places; Litestar's priority truncates quality to two.
    try:
        return specificity, float(media_type.params.get("q", "1"))
    except ValueError:
        return specificity, 1.0


def export_media_type(accept: Accept) -> str:
    accepted = [MediaTypeHeader(value) for value in accept]
    qualities: dict[str, float] = {}
    for media_type in ("application/json", "application/yaml"):
        provided = MediaTypeHeader(media_type)
        matches = [_accept_priority(item) for item in accepted if item.match(provided)]
        # The most specific range sets quality, even when a wildcard has higher quality.
        qualities[media_type] = max(matches)[1] if matches else 0
    best_quality = max(qualities.values())
    # Litestar best_match alone also accepts q=0. Filter exclusions before resolving ties.
    candidates = [media_type for media_type, quality in qualities.items() if quality == best_quality and quality > 0]
    return accept.best_match(candidates, default="application/json")
