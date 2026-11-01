import attr

ControlValue = str | int | bool | None


@attr.s(auto_attribs=True, frozen=True)
class Text:
    name: str
    default: str
    label: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class Choice:
    """A pick-one control. Use options=ICONS for "every file in static/img/icons"."""

    name: str
    options: tuple[str, ...]
    label: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class Toggle:
    name: str
    default: bool = False
    label: str | None = None


Control = Text | Choice | Toggle

ICONS: tuple[str, ...] = ("__icons__",)


@attr.s(auto_attribs=True, frozen=True)
class Component:
    """One page of the library.

    macro: the macro this page demos. Left out for components that are only CSS classes.
    also: other macros that belong to this page, such as info_row on the info_table page.
    template: Jinja for the preview. Control values are v.<name>. Left out for macros, where the call is built from
        the controls.
    usage: what to paste into a template, with $name for control values. Left out, it is the macro call for a macro
        and the rendered HTML otherwise.
    wrap: extra HTML around the preview only, with {} for the component.
    classes: the CSS classes this page owns. Variants come along, so "btn" also covers btn--primary.
    selectors: element selectors this page owns, for rules with no class such as select.
    phone: open at phone width, for components that only appear there.
    """

    id: str
    name: str
    group: str
    summary: str
    macro: str | None = None
    also: tuple[str, ...] = ()
    template: str | None = None
    usage: str | None = None
    wrap: str = "{}"
    controls: tuple[Control, ...] = ()
    classes: tuple[str, ...] = ()
    selectors: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    phone: bool = False
