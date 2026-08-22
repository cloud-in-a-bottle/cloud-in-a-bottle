// Donut charts for the System Info page, plus the small HTML-escaping helpers
// they use. Loaded before system.js, which builds the per-chart segments and
// calls donutHtml/wireDonut.

function escHtml(s) {
  var d = document.createElement('div');
  d.textContent = (s == null) ? '' : String(s);
  return d.innerHTML;
}

function escAttr(s) {
  return escHtml(s).replace(/"/g, '&quot;');
}

// Golden-angle hue rotation so adjacent app slices stay distinguishable at any
// app count, pitched at the lightness/saturation of the brand pastels
// (#a2d9ff, #d4ffbc) so the charts read as part of the same palette. Shared by
// every app-colored donut so the same app gets a consistent hue slot across
// charts. Starts on the green pastel, not the blue one: the non-app slices
// below are both blue, and a blue first slice sits directly against "Unused"
// at 12 o'clock.
function appColor(i) {
  return 'hsl(' + ((97 + i * 137.5) % 360).toFixed(1) + ', 72%, 78%)';
}

function cssToken(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// The non-app slices of a usage donut, from the shared tokens.
var COLOR_UNUSED = cssToken('--color-chart-unused');  // free headroom on the box
var COLOR_OTHER = cssToken('--color-chart-system');   // used by the host but not attributed to an app

// Generic donut chart. `segments` is [{name, value, valueText, color}] drawn in
// order from 12 o'clock; hovering a slice shows its name + valueText in the
// center, which otherwise shows defaultName / defaultText. Give each donut a
// unique `id` so multiple can coexist on one page.
function donutHtml(id, segments, defaultName, defaultText) {
  var total = 0;
  segments.forEach(function(s) { total += s.value; });
  if (total <= 0) return '<span class="muted">No data yet.</span>';

  var cx = 100, cy = 100, rOuter = 92, rInner = 58;
  var TAU = 2 * Math.PI;
  var angle = -Math.PI / 2;

  function pt(r, a) {
    return (cx + r * Math.cos(a)).toFixed(2) + ' ' + (cy + r * Math.sin(a)).toFixed(2);
  }

  var slices = segments.map(function(s) {
    var frac = s.value / total;
    // Clamp just under a full turn so a single-slice "circle" still renders as one arc.
    var sweep = Math.min(frac * TAU, TAU - 0.0004);
    var a0 = angle;
    var a1 = a0 + sweep;
    angle += frac * TAU;
    var large = sweep > Math.PI ? 1 : 0;
    var d = 'M ' + pt(rOuter, a0) + ' A ' + rOuter + ' ' + rOuter + ' 0 ' + large + ' 1 ' + pt(rOuter, a1)
      + ' L ' + pt(rInner, a1) + ' A ' + rInner + ' ' + rInner + ' 0 ' + large + ' 0 ' + pt(rInner, a0) + ' Z';
    return '<path class="pie-slice" d="' + d + '" fill="' + s.color + '"'
      + ' data-name="' + escAttr(s.name) + '" data-size="' + escAttr(s.valueText) + '"></path>';
  }).join('');

  return '<svg class="usage-pie" id="' + id + '" viewBox="0 0 200 200" width="200" height="200" role="img"'
    + ' data-default-name="' + escAttr(defaultName) + '" data-default-size="' + escAttr(defaultText) + '">'
    + slices
    + '<text class="pie-center-name" data-role="name" x="100" y="96">' + escHtml(defaultName) + '</text>'
    + '<text class="pie-center-size" data-role="size" x="100" y="114">' + escHtml(defaultText) + '</text>'
    + '</svg>';
}

function wireDonut(id) {
  var svg = document.getElementById(id);
  if (!svg) return;
  var nameEl = svg.querySelector('[data-role="name"]');
  var sizeEl = svg.querySelector('[data-role="size"]');
  function setLabel(name, size) {
    nameEl.textContent = name.length > 18 ? name.slice(0, 17) + '…' : name;
    sizeEl.textContent = size;
  }
  function reset() {
    setLabel(svg.getAttribute('data-default-name'), svg.getAttribute('data-default-size'));
  }
  svg.addEventListener('mouseover', function(e) {
    var t = e.target;
    if (t && t.classList && t.classList.contains('pie-slice')) {
      setLabel(t.getAttribute('data-name'), t.getAttribute('data-size'));
    } else if (t === svg) {
      reset();
    }
  });
  svg.addEventListener('mouseleave', reset);
}
