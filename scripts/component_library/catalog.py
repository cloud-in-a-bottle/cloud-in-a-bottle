from model import ICONS
from model import Choice
from model import Component
from model import Part
from model import Text
from model import Toggle

# One-off page styles that aren't components, kept off the "No preview yet" list.
PAGE_ONLY_CLASSES = ("app-definition-modes",)

LINK = Choice("href", ("none", "/dashboard"), label="Link to")

COMPONENTS = (
    # ── Actions ──
    Component(
        id="button",
        name="Button",
        group="Actions",
        summary="The default action control. Renders an <a> when it has an href, a <button> otherwise.",
        macro="button",
        classes=("btn",),
        controls=(
            Choice("variant", ("default", "primary", "accent", "danger")),
            Text("label", "Deploy app"),
            LINK,
            Toggle("disabled"),
            Toggle("block", label="Full width"),
        ),
        notes=(
            "Grows from 28px to 36px tall on touch screens.",
            'disabled only applies to a <button>. A link needs aria-disabled="true", passed some other way.',
        ),
    ),
    Component(
        id="icon-button",
        name="Icon button",
        group="Actions",
        summary="A bare icon with no border. The label becomes its accessible name and tooltip.",
        macro="icon_button",
        classes=("icon-btn",),
        controls=(
            Choice("icon", ICONS),
            Text("label", "Edit"),
            Choice("icon_size", ("12", "14", "16"), label="Icon size"),
            LINK,
            Toggle("disabled"),
        ),
        notes=(
            "Grows from 28px to 36px on touch screens.",
            "Page scripts build the same markup with dom.iconButton() in static/js/dom.js. Change both together.",
        ),
    ),
    Component(
        id="action-bar",
        name="Action bar",
        group="Actions",
        summary="A wrapping row of buttons, above content (.action-bar) or closing a form (.actions).",
        template="""<div class="{{ v.kind }}">
  {{ button("Start", variant="primary") }}
  {{ button("Restart") }}
  {{ button("Remove", variant="danger") }}
</div>""",
        classes=("action-bar", "actions", "control-row"),
        controls=(Choice("kind", ("action-bar", "actions", "control-row")),),
    ),
    # ── Navigation ──
    Component(
        id="banner",
        name="Banner",
        group="Navigation",
        summary="The space's name between two dashed rules. It sits at the top of every page.",
        template='<h1 class="banner"><span class="banner__text">{{ v.label }}</span></h1>',
        classes=("banner",),
        controls=(Text("label", "rose's personal compute space"),),
        notes=("text-transform: capitalize turns the lowercase owner name into title case.",),
    ),
    Component(
        id="icon-nav",
        name="Icon nav",
        group="Navigation",
        summary="The compute space's whole navigation, rendered on every page by layout.html.",
        macro="icon_nav",
        classes=("icon-nav",),
        controls=(Toggle("on_dashboard", label="On the dashboard"),),
        notes=("Switch the preview to Phone width to see the icons collapse into the hamburger menu.",),
    ),
    Component(
        id="nav-menu",
        name="Nav menu",
        group="Navigation",
        summary="The dashboard's phone nav. It stands in for the icon row and the tiles below 640px.",
        macro="nav_menu",
        classes=("nav-menu",),
        controls=(Toggle("catalog_installed", label="Catalog installed"),),
        wrap='<div style="display:flex;align-items:flex-start;justify-content:flex-end;min-height:280px">{}</div>',
        phone=True,
        notes=("This is hidden above 640px, so the preview opens at Phone width.", "Needs js/nav-menu.js."),
    ),
    Component(
        id="nav-back",
        name="Back link",
        group="Navigation",
        summary="The way home, in the page's top-left corner, outside the content column.",
        macro="nav_back",
        classes=("nav-back",),
        wrap='<div style="position:relative;height:56px;--layout-gutter:0px">{}</div>',
    ),
    # ── Content ──
    Component(
        id="section-head",
        name="Section header",
        group="Content",
        summary="A small uppercase heading, with an optional cluster of actions on the right.",
        macro="section_head",
        template="""{% if v.actions -%}
  {% call section_head(v.label, level=v.level) %}{{ button("Add app") }}{% endcall %}
{%- else -%}
  {{ section_head(v.label, level=v.level) }}
{%- endif %}""",
        usage='{% call section_head("$label", level=$level) %}\n  {{ button("Add app") }}\n{% endcall %}',
        classes=("section", "section-head", "section-label", "section-actions", "section-title"),
        controls=(Text("label", "Your apps"), Choice("level", ("2", "3")), Toggle("actions", True, label="Actions")),
    ),
    Component(
        id="card",
        name="Card",
        group="Content",
        summary="A dashboard tile with a title, a sentence and one full-width button.",
        macro="card",
        classes=("card", "card-grid"),
        wrap='<div class="card-grid" style="max-width:240px">{}</div>',
        controls=(
            Choice("cta_variant", ("primary", "accent", "default", "danger"), label="Button variant"),
            Text("title", "Catalog"),
            Text("desc", "Browse apps you can install."),
            Text("cta_label", "Open catalog", label="Button label"),
        ),
    ),
    Component(
        id="panel",
        name="Panel",
        group="Content",
        summary="The surface for single-purpose pages such as login, setup and approvals.",
        template="""<div class="panel">
  <h2>{{ v.title }}</h2>
  <p class="hint">Use the password you chose during setup.</p>
  <div class="field"><label for="panel-password">Password</label><input id="panel-password" type="password"></div>
  {{ button("Sign in", variant="primary") }}
</div>""",
        classes=("panel",),
        wrap='<div style="max-width:420px">{}</div>',
        controls=(Text("title", "Sign in"),),
    ),
    Component(
        id="app-row",
        name="App row",
        group="Content",
        summary="One row of the dashboard's app list. The status drives the coloured dot.",
        macro="app_row",
        template="""{{ app_row(dict(app_id=1, name=v.name, status=v.status,
                       manifest_name="file_browser" if v.alias else v.name)) }}""",
        usage="{{ app_row(app) }}",
        classes=("app-row", "app-list"),
        wrap='<div class="app-list">{}</div>',
        controls=(
            Choice("status", ("running", "stopped", "starting", "building", "removing", "error")),
            Text("name", "files"),
            Toggle("alias", label="Show alias"),
        ),
        notes=("Details stays hidden until you hover the row or tab to it. Try it in the preview.",),
    ),
    Component(
        id="info-table",
        name="Info table",
        group="Content",
        summary="The one key/value table, used by system info, app detail and settings.",
        macro="info_table",
        also=("info_row",),
        template="""{% call info_table(v.label_head, v.value_head) %}
  {{ info_row("Hostname", "alice.example.com") }}
  {{ info_row("Version", "0.3.1") }}
  {{ info_row("Disk used", "18.2 GB of 80 GB") }}
{% endcall %}""",
        usage="""{% call info_table("$label_head", "$value_head") %}
  {{ info_row("Hostname", "alice.example.com") }}
{% endcall %}""",
        classes=("info-table",),
        controls=(
            Text("label_head", "Property", label="Label heading"),
            Text("value_head", "Value", label="Value heading"),
        ),
        notes=("Rows filled in by a page script use dom.infoRow in static/js/dom.js, which builds the same markup.",),
    ),
    Component(
        id="spec-list",
        name="Spec list",
        group="Content",
        summary="A key/value list that collapses to stacked rows in a narrow container.",
        macro="spec_list",
        also=("spec_row",),
        template="""{% call spec_list() %}
  {{ spec_row("Image", "ghcr.io/alice/files:1.4") }}
  {{ spec_row("Memory", "512 MB") }}
  {{ spec_row("Ports", "8080") }}
{% endcall %}""",
        usage="""{% call spec_list() %}
  {{ spec_row("Memory", "512 MB") }}
{% endcall %}""",
        classes=("spec", "spec-list"),
        notes=("Switch to Phone width to see the rows stack.",),
    ),
    Component(
        id="list-table",
        name="List table",
        group="Content",
        summary="A plain multi-column table for ports, domains and tokens, with row actions on the right.",
        template="""<div class="table-scroll"><table>
  <thead><tr><th>Domain</th><th>Status</th><th class="col-actions"></th></tr></thead>
  <tbody>
    <tr><td>files.alice.example.com</td><td><span class="status-text status-text--ok">Active</span></td>
      <td class="col-actions">{{ icon_button("trash", "Remove files.alice.example.com") }}</td></tr>
    <tr><td>notes.alice.example.com</td><td><span class="status-text status-text--warn">Pending</span></td>
      <td class="col-actions">{{ icon_button("trash", "Remove notes.alice.example.com") }}</td></tr>
  </tbody>
</table></div>""",
        classes=("table-scroll", "col-actions", "is-expired"),
        selectors=("table", "th", "td"),
    ),
    Component(
        id="log-list",
        name="Log list",
        group="Content",
        summary="Timestamped lines on a dark surface, used for update progress.",
        template="""<ol class="log-list">
  <li><span class="ts">14:02:11</span><span>Pulling image</span></li>
  <li><span class="ts">14:02:39</span><span>Applying migrations</span></li>
  <li><span class="ts">14:02:41</span><span>Restarting services</span></li>
</ol>""",
        classes=("log-list", "log-output"),
    ),
    Component(
        id="rule",
        name="Divider",
        group="Content",
        summary="A plain horizontal rule between groups of content.",
        template='<hr class="rule">',
        classes=("rule",),
    ),
    # ── Status ──
    Component(
        id="badge",
        name="Badge",
        group="Status",
        summary="A square status chip, for short labels that sit beside other content.",
        template='<span class="badge{% if v.tone != "neutral" %} badge--{{ v.tone }}{% endif %}">{{ v.label }}</span>',
        classes=("badge",),
        controls=(Choice("tone", ("neutral", "ok", "info", "warn", "error")), Text("label", "Update available")),
        notes=("Page scripts build the same markup with dom.badge() in static/js/dom.js.",),
    ),
    Component(
        id="status",
        name="Status words",
        group="Status",
        summary="Status as coloured text. Read, not clicked, so a filled chip beside a real button can't pass for one.",
        template='<span class="status-text status-text--{{ v.tone }}">{{ v.label }}</span>',
        classes=(
            "status-text",
            "status-dot",
            "status-value",
            "status-running",
            "status-error",
            "status-stopped",
            "status-building",
            "status-starting",
            "status-removing",
        ),
        controls=(Choice("tone", ("ok", "warn", "error", "muted")), Text("label", "Running")),
    ),
    Component(
        id="notice",
        name="Notice",
        group="Status",
        summary="A tinted message at panel scale, with a 3px bar on the left carrying the status.",
        template='<div class="notice{% if v.tone != "info" %} notice--{{ v.tone }}{% endif %}">{{ v.text }}</div>',
        classes=("notice",),
        controls=(
            Choice("tone", ("info", "ok", "warn", "error")),
            Text("text", "Backups run every night at 03:00."),
        ),
    ),
    Component(
        id="msg",
        name="Inline message",
        group="Status",
        summary="One line of feedback beside a control.",
        template='<span class="msg msg--{{ v.tone }}">{{ v.text }}</span>',
        classes=("msg", "action-msg"),
        controls=(Choice("tone", ("ok", "warn", "error")), Text("text", "Saved")),
    ),
    Component(
        id="meter",
        name="Meter",
        group="Status",
        summary="A blocky progress bar. Its segments echo the pixel-art clouds and grass.",
        template="""<div class="meter"><div class="meter__fill{% if v.tone != "primary" %} meter__fill--{{ v.tone }}{% endif %}"
  style="width:{{ v.percent }}%"></div></div>""",
        classes=("meter",),
        controls=(Choice("tone", ("primary", "ok", "warn", "error")), Choice("percent", ("25", "50", "75", "100"))),
    ),
    Component(
        id="empty",
        name="Empty state",
        group="Status",
        summary="A box that says there is nothing here yet. Inside a panel it drops to a plain line.",
        template='<div class="empty">{{ v.text }}</div>',
        classes=("empty",),
        controls=(Text("text", "No apps yet. Deploy one to get started."),),
    ),
    Component(
        id="toast",
        name="Toast",
        group="Status",
        summary="A floating message in the bottom-right corner, with optional actions.",
        template="""<div class="toast" role="status">
  <p>{{ v.text }}</p>
  <div class="toast-actions">{{ button("Review", variant="primary") }}{{ button("Later") }}</div>
</div>""",
        classes=("toast",),
        wrap='<div style="min-height:150px">{}</div>',
        controls=(Text("text", "An update is ready to install."),),
    ),
    Component(
        id="info-tip",
        name="Info tip",
        group="Status",
        summary="An explanation parked behind an icon, so it costs nothing until someone wants it.",
        macro="info_tip",
        template="""<div style="display:flex;justify-content:{{ "flex-end" if v.align == "end" else "flex-start" }};min-height:90px">
  <div class="field__head"><label>Username</label>
  {% call info_tip("library-tip", "About the username", align=v.align) %}<p>{{ v.text }}</p>{% endcall %}</div>
</div>""",
        usage="""{% call info_tip("about-username", "About the username", align="$align") %}
  <p>$text</p>
{% endcall %}""",
        classes=("info-tip",),
        controls=(
            Choice("align", ("start", "end")),
            Text("text", "This appears in your space's address."),
        ),
        notes=("Hover it, or click to pin it open.", "Needs js/info-tip.js, which both layouts load."),
    ),
    # ── Forms ──
    Component(
        id="field",
        name="Field",
        group="Forms",
        summary="A label, a control and an explainer, treated as one unit.",
        macro="field",
        template="""{% call field(v.label, "library-field", hint="Lowercase letters and digits." if v.hint else none,
                     error="That username is taken." if v.state == "error" else none) %}
  {% if v.control == "select" %}
  <select id="library-field"{% if v.state == "error" %} class="invalid"{% endif %}{% if v.state == "disabled" %} disabled{% endif %}
          aria-describedby="library-field-hint library-field-error">
    <option>alice</option>
    <option>bob</option>
  </select>
  {% else %}
  <input id="library-field" type="text"{% if v.state == "error" %} class="invalid"{% endif %}{% if v.state == "disabled" %} disabled{% endif %} value="alice"
         aria-describedby="library-field-hint library-field-error">
  {% endif %}
{% endcall %}""",
        usage="""{% call field("$label", "username", hint="Lowercase letters and digits.") %}
  <input type="text" id="username" aria-describedby="username-hint username-error">
{% endcall %}""",
        classes=("field", "field__head", "field-error", "invalid", "field-stack"),
        controls=(
            Text("label", "Username"),
            Choice("state", ("default", "error", "disabled"), label="State"),
            Choice("control", ("input", "select"), label="Control"),
            Toggle("hint", True, label="Hint"),
        ),
        parts=(
            Part(".field__head", "Label row"),
            Part("label", "Label"),
            Part('input[type="text"]', "Input"),
            Part(".hint", "Hint text"),
            Part(".field-error", "Error text"),
        ),
        notes=(
            "An error replaces the hint, and editing the input or select clears it.",
            "On touch screens the input is 36px with 16px text, and the label, hint and error are 14px. Phone width here does not switch this on.",
            "A field is either disabled or in error. If it is both, disabled wins and the error stays hidden.",
            "Hover the input for an fg/secondary border, and click into it for fg/primary. There is no focus ring.",
            "The 8 rows in Settings with a title and buttons still use .field by hand.",
        ),
    ),
    Component(
        id="hint",
        name="Hint",
        group="Forms",
        summary="A short note tied to one control. Text about a whole page or section is a Description.",
        template='<p class="hint">{{ v.text }}</p>',
        classes=("hint",),
        controls=(Text("text", "Lowercase letters and digits."),),
        notes=("12px, and 14px on touch screens. Inside a panel table it stays 12px.",),
    ),
    Component(
        id="desc",
        name="Description",
        group="Forms",
        summary="Text that describes a page or a section rather than one control.",
        template='<p class="desc">{{ v.text }}</p>',
        classes=("desc",),
        controls=(Text("text", "Hostnames this instance answers on."),),
    ),
    Component(
        id="field-row",
        name="Field row",
        group="Forms",
        summary='The inline "add a thing" bar. The text field takes the slack and the button sits at the right.',
        template="""<div class="field-row field-row--add{% if v.disabled %} field-row--disabled{% endif %}">
  <div class="field"><label for="library-domain">Domain</label>
    <input id="library-domain" type="text" placeholder="files.example.com"{% if v.disabled %} disabled{% endif %}></div>
  {{ button("Add domain", variant="primary", disabled=v.disabled) }}
</div>""",
        classes=("field-row",),
        controls=(Toggle("disabled"),),
    ),
    Component(
        id="check",
        name="Checkbox and radio",
        group="Forms",
        summary="Square, flat replacements for the native controls, which arrive rounded and tinted by the OS.",
        template="""<label class="field--check">
  <input type="{{ v.kind }}"{% if v.checked %} checked{% endif %}>
  <span>{{ v.label }}</span>
</label>""",
        classes=("field--check",),
        selectors=('input[type="checkbox"]', 'input[type="radio"]'),
        controls=(Choice("kind", ("checkbox", "radio")), Text("label", "Back up nightly"), Toggle("checked", True)),
    ),
    Component(
        id="select",
        name="Select",
        group="Forms",
        summary="A native select with a pixel-drawn arrow in place of the OS chevron.",
        template="""<div class="field">
  <label for="library-select">{{ v.label }}</label>
  <select id="library-select"><option>Every night</option><option>Every week</option><option>Never</option></select>
</div>""",
        selectors=("select",),
        controls=(Text("label", "Backup schedule"),),
    ),
    Component(
        id="search",
        name="Search",
        group="Forms",
        summary="An icon that opens into a text field when you focus it or type.",
        template="""<div class="search">
  {{ icon("search", 14, class_="search__icon") }}
  <input class="search__input" type="search" placeholder="{{ v.placeholder }}" aria-label="{{ v.placeholder }}">
</div>""",
        classes=("search",),
        controls=(Text("placeholder", "Filter apps"),),
        notes=("Click the icon in the preview to see it expand.",),
    ),
    Component(
        id="copy-field",
        name="Copyable value",
        group="Forms",
        summary="A one-time secret, such as an API token, shown for copying.",
        template="""<div class="copy-field">
  <code>{{ v.value }}</code>
  {{ button("Copy") }}
</div>""",
        classes=("copy-field",),
        controls=(Text("value", "example-not-a-real-token"),),
    ),
    Component(
        id="icon",
        name="Icon",
        group="Actions",
        summary="An image from static/img/icons. Pass height too for artwork that isn't square.",
        macro="icon",
        classes=("icon",),
        controls=(Choice("name", ICONS), Choice("size", ("12", "14", "16", "24"))),
    ),
)
