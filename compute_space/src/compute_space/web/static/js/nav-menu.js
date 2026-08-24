// Driver for the phone-sized nav. Open state lives on the panel's `hidden`
// attribute, so the menu stays shut for a visitor without JS rather than
// hanging open over the page.

(function () {
  function panelOf(toggle) {
    return document.getElementById(toggle.getAttribute('aria-controls'));
  }

  function setOpen(toggle, isOpen) {
    var panel = panelOf(toggle);
    toggle.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
    if (panel) panel.hidden = !isOpen;
  }

  function openToggle() {
    return document.querySelector('.nav-menu__toggle[aria-expanded="true"]');
  }

  document.addEventListener('click', function (e) {
    var current = openToggle();
    var toggle = e.target.closest('.nav-menu__toggle');
    if (toggle) {
      setOpen(toggle, toggle !== current);
      return;
    }
    // Anywhere else puts the menu away -- including a link inside the panel,
    // which is about to navigate anyway.
    if (current) setOpen(current, false);
  });

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    var current = openToggle();
    if (!current) return;
    setOpen(current, false);
    current.focus();
  });
})();
