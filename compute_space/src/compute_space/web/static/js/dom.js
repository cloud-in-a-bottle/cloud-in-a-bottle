// Minimal DOM builders shared by the page scripts.
//
// These replace innerHTML string concatenation, which needed a hand-rolled HTML
// escaper (there were three near-identical copies) and made every interpolation
// a place to forget it. Text goes in as text here, so there is nothing to
// escape. No dependencies and no build step, so this survives whatever the
// frontend eventually settles on.
(function (global) {
  function append(node, children) {
    if (children == null || children === false) return;
    if (Array.isArray(children)) {
      children.forEach(function (c) { append(node, c); });
      return;
    }
    node.appendChild(children.nodeType ? children : document.createTextNode(String(children)));
  }

  // el('td', {class: 'x', text: name}, [childNode, 'literal text'])
  //
  // Recognised attrs: `text` sets textContent, `class` the class list,
  // `dataset` an object of data-* values, `on<event>` a listener, `true` a
  // bare boolean attribute. Anything else is setAttribute.
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        var value = attrs[key];
        if (value == null || value === false) return;
        if (key === 'text') node.textContent = value;
        else if (key === 'class') node.className = value;
        else if (key === 'dataset') {
          Object.keys(value).forEach(function (d) { node.dataset[d] = value[d]; });
        } else if (key.slice(0, 2) === 'on' && typeof value === 'function') {
          node.addEventListener(key.slice(2), value);
        } else if (value === true) node.setAttribute(key, '');
        else node.setAttribute(key, value);
      });
    }
    append(node, children);
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  // Swap a container's contents in one go — the common shape for "repaint this
  // table body from a fresh API response".
  function replace(node, children) {
    append(clear(node), children);
    return node;
  }

  // One row of an .info-table: a <th scope="row"> label and its value cell.
  // Mirrors the info_row() Jinja macro so the server- and client-rendered rows
  // of the same table can't drift apart.
  function infoRow(label, value, cls) {
    return el('tr', null, [
      el('th', {scope: 'row', text: label}),
      el('td', {class: cls || null}, value),
    ]);
  }

  function badge(label, variant, title) {
    return el('span', {
      class: 'badge' + (variant ? ' badge--' + variant : ''),
      text: label,
      title: title || null,
    });
  }

  global.dom = {el: el, clear: clear, replace: replace, badge: badge, infoRow: infoRow};
})(window);
