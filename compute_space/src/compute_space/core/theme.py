from __future__ import annotations

import json
import sqlite3
from enum import StrEnum

import attr

from compute_space.core.settings_store import get_setting
from compute_space.core.settings_store import set_setting

THEME_KEY = "theme"


class Style(StrEnum):
    CLOUDY = "cloudy"
    SIMPLE = "simple"
    DARK = "dark"
    SIMPLE_DARK = "simple-dark"


class AppListLayout(StrEnum):
    LIST = "list"
    GRID = "grid"


@attr.s(auto_attribs=True, frozen=True)
class Theme:
    """Appearance choices rendered as ``data-*`` attributes on ``<html>``. Defaults preserve the original look."""

    style: Style = Style.CLOUDY
    app_list: AppListLayout = AppListLayout.LIST


def read_theme(db: sqlite3.Connection) -> Theme:
    raw = get_setting(db, THEME_KEY)
    if raw is None:
        return Theme()
    stored = json.loads(raw)
    default = Theme()
    # An option added after the theme was saved keeps its default; an unknown value raises.
    return Theme(
        style=Style(stored.get("style", default.style)),
        app_list=AppListLayout(stored.get("app_list", default.app_list)),
    )


def write_theme(db: sqlite3.Connection, theme: Theme) -> None:
    set_setting(db, THEME_KEY, json.dumps(attr.asdict(theme)))
