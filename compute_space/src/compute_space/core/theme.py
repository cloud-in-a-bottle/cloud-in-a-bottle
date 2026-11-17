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
    COMPACT = "compact"
    TABLE = "table"
    CHIPS = "chips"


class AppColumns(StrEnum):
    """How many apps sit side by side in the layouts that put them in columns (grid, compact)."""

    AUTO = "auto"
    TWO = "2"
    THREE = "3"
    FOUR = "4"
    FIVE = "5"
    SIX = "6"


@attr.s(auto_attribs=True, frozen=True)
class Theme:
    """The dashboard's look: one named choice per option, each independent of the others.

    Each field is rendered as a ``data-*`` attribute on ``<html>`` by layout.html, and themes.css keys off those. Adding
    an option means a field here, its attribute in layout.html, a select in settings.html and CSS for its non-default
    values. The defaults are the original look."""

    style: Style = Style.CLOUDY
    app_list: AppListLayout = AppListLayout.LIST
    app_columns: AppColumns = AppColumns.AUTO


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
        app_columns=AppColumns(stored.get("app_columns", default.app_columns)),
    )


def write_theme(db: sqlite3.Connection, theme: Theme) -> None:
    set_setting(db, THEME_KEY, json.dumps(attr.asdict(theme)))
