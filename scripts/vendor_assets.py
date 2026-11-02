#!/usr/bin/env python3
"""Vendor the third-party browser assets instance pages use, from pinned npm packages.

Instances serve these from ``/static`` so no page asks a third party (eg Google Fonts, jsDelivr) for them. This
downloads each pinned package, checks it against the registry's integrity hash, and regenerates ``static/fonts/``,
``static/css/fonts.css`` and ``static/vendor/``. To bump a package, change its version and integrity below (from
``https://registry.npmjs.org/<name>/<version>``, field ``dist.integrity``) and rerun:

    pixi run -e dev python scripts/vendor_assets.py
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
import shutil
import tarfile
import urllib.request
from pathlib import Path

import attr

STATIC_DIR = Path(__file__).resolve().parent.parent / "compute_space" / "src" / "compute_space" / "web" / "static"
FONTS_DIR = STATIC_DIR / "fonts"
FONTS_CSS = STATIC_DIR / "css" / "fonts.css"
VENDOR_DIR = STATIC_DIR / "vendor"
# Keep in sync with --weight-regular / --weight-emphasized in tokens.css.
FONT_WEIGHTS = (400, 500)
_FONT_SRC = re.compile(
    r"src: url\(\./files/(?P<name>[\w.-]+\.woff2)\) format\('woff2'\)(?:, url\([^)]*\) format\('woff'\))?;"
)
# Dropped from vendored scripts: the maps aren't vendored, so the comment would only make devtools request a 404.
_SOURCE_MAP_COMMENT = re.compile(r"\n//# sourceMappingURL=\S+\s*$")


@attr.s(auto_attribs=True, frozen=True)
class NpmPackage:
    name: str
    version: str
    integrity: str

    @property
    def spec(self) -> str:
        return f"{self.name}@{self.version}"


@attr.s(auto_attribs=True, frozen=True)
class FontPackage:
    """A Fontsource package; its woff2 files and @font-face rules go to static/fonts/ and fonts.css."""

    package: NpmPackage
    family_dir: str

    @property
    def dir_name(self) -> str:
        # Versioned so a bump changes every font URL; fonts.css references them without static_url's cache-buster.
        return f"{self.family_dir}-{self.package.version}"


@attr.s(auto_attribs=True, frozen=True)
class ScriptPackage:
    """A package whose built files are copied to static/vendor/<out_dir>/, renamed per ``files`` (source -> dest)."""

    package: NpmPackage
    out_dir: str
    files: tuple[tuple[str, str], ...]


FONTS = (
    FontPackage(
        package=NpmPackage(
            name="@fontsource/inter",
            version="5.3.0",
            integrity="sha512-RofMylZmjlJEfELXeNHFWBRcSs75rGU/6bV2S2jfnvv/3rPXPGe0LgUJTklcHZ9lM4OZmAVFhcJPnACfb91A3g==",
        ),
        family_dir="inter",
    ),
    FontPackage(
        package=NpmPackage(
            name="@fontsource/ibm-plex-mono",
            version="5.3.0",
            integrity="sha512-eTgnZjZEGk1QtD3ZstF+Vclo2HLAni8YMy34/DxllwZvyz1lR/1RF/xTiAquOBO7MvqBx8D2Ig2WCPMVfdZu7Q==",
        ),
        family_dir="ibm-plex-mono",
    ),
)

SCRIPTS = (
    ScriptPackage(
        package=NpmPackage(
            name="@xterm/xterm",
            version="5.5.0",
            integrity="sha512-hqJHYaQb5OptNunnyAnkHyM8aCjZ1MEIDTQu1iIbbTD/xops91NB5yq1ZK/dC2JDbVWtF23zUtl9JE2NqwT87A==",
        ),
        out_dir="xterm",
        files=(("lib/xterm.js", "xterm.js"), ("css/xterm.css", "xterm.css")),
    ),
    ScriptPackage(
        package=NpmPackage(
            name="@xterm/addon-fit",
            version="0.10.0",
            integrity="sha512-UFYkDm4HUahf2lnEyHvio51TNGiLK66mqP2JoATy7hRZeXaGMRDr00JiSF7m63vR5WKATF605yEggJKsw0JpMQ==",
        ),
        out_dir="addon-fit",
        files=(("lib/addon-fit.js", "addon-fit.js"),),
    ),
)


def _download(package: NpmPackage) -> tarfile.TarFile:
    basename = package.name.rsplit("/", 1)[-1]
    url = f"https://registry.npmjs.org/{package.name}/-/{basename}-{package.version}.tgz"
    with urllib.request.urlopen(url) as response:
        data = response.read()
    algorithm, expected = package.integrity.split("-", 1)
    actual = base64.b64encode(hashlib.new(algorithm, data).digest()).decode("ascii")
    if actual != expected:
        raise SystemExit(f"{url}: integrity mismatch (expected {expected}, got {actual})")
    return tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")


def _read(archive: tarfile.TarFile, path: str) -> bytes:
    member = archive.extractfile(f"package/{path}")
    if member is None:
        raise SystemExit(f"package/{path} is not a regular file")
    return member.read()


def _vendor_font(font: FontPackage, archive: tarfile.TarFile) -> str:
    """Copy one font package's woff2 files and license into place and return its @font-face rules."""
    out_dir = FONTS_DIR / font.dir_name
    out_dir.mkdir(parents=True)
    rules = []
    (out_dir / "LICENSE").write_bytes(_read(archive, "LICENSE"))
    for weight in FONT_WEIGHTS:
        css = _read(archive, f"{weight}.css").decode("utf-8")
        for match in _FONT_SRC.finditer(css):
            (out_dir / match["name"]).write_bytes(_read(archive, f"files/{match['name']}"))
        # Absolute URLs, so the rules also work when setup_app inlines this file into a page.
        rules.append(
            _FONT_SRC.sub(
                lambda m: f'src: url("/static/fonts/{font.dir_name}/{m["name"]}") format("woff2");',
                css,
            ).strip()
        )
    if not any(out_dir.glob("*.woff2")):
        raise SystemExit(f"{font.package.spec}: no woff2 files matched; has the Fontsource CSS format changed?")
    license_note = f"SIL OFL 1.1, see ../fonts/{font.dir_name}/LICENSE"
    return f"/* {font.package.spec} ({license_note}) */\n" + "\n\n".join(rules)


def _vendor_script(script: ScriptPackage, archive: tarfile.TarFile) -> None:
    out_dir = VENDOR_DIR / script.out_dir
    out_dir.mkdir(parents=True)
    (out_dir / "LICENSE").write_bytes(_read(archive, "LICENSE"))
    for source, dest in script.files:
        text = _read(archive, source).decode("utf-8")
        if dest.endswith(".js"):
            text = _SOURCE_MAP_COMMENT.sub("\n", text)
        header = f"/* {script.package.spec} {source}, vendored by scripts/vendor_assets.py. See LICENSE. */\n"
        (out_dir / dest).write_text(header + text, encoding="utf-8")


def main() -> None:
    # Download and verify everything before touching the tree, so a bad pin leaves the current files in place.
    font_archives = [_download(font.package) for font in FONTS]
    script_archives = [_download(script.package) for script in SCRIPTS]
    shutil.rmtree(FONTS_DIR, ignore_errors=True)
    shutil.rmtree(VENDOR_DIR, ignore_errors=True)
    sections = [_vendor_font(font, archive) for font, archive in zip(FONTS, font_archives, strict=True)]
    header = "/* Generated by scripts/vendor_assets.py; edit that script, not this file. */\n\n"
    FONTS_CSS.write_text(header + "\n\n".join(sections) + "\n", encoding="utf-8")
    for script, archive in zip(SCRIPTS, script_archives, strict=True):
        _vendor_script(script, archive)


if __name__ == "__main__":
    main()
